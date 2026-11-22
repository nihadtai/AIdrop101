"""
Trend Radar — shared helpers
============================

Common building blocks used by the background trend radar modules
(``shopify_tracker`` and ``amazon_tracker``):

* a polite HTTP session (single, honest browser User-Agent, timeouts,
  no proxy rotation / no anti-bot evasion),
* JSON snapshot persistence so each scan can be diffed against the previous one,
* a common ``RadarSignal`` structure and report writer,
* a lightweight title normaliser used for cross-store / cross-source matching.

These modules are intentionally independent from the LangGraph pipeline:
they consume **no LLM tokens** and are meant to be run by a scheduler.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RADAR_DATA_DIR = PROJECT_ROOT / "data" / "radar"
RADAR_CONFIG_PATH = PROJECT_ROOT / "config" / "radar_sources.json"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ──────────────────────────── CONFIG ─────────────────────────────────────────

def load_radar_config() -> Dict[str, Any]:
    """Load ``config/radar_sources.json`` (returns an empty dict if missing/invalid)."""
    try:
        return json.loads(RADAR_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(f"Radar config not found: {RADAR_CONFIG_PATH}")
    except Exception as e:
        logger.error(f"Radar config could not be parsed: {e}")
    return {}


# ──────────────────────────── HTTP ───────────────────────────────────────────

class PoliteSession:
    """
    Thin wrapper around ``requests.Session`` that enforces a minimum delay
    between requests to the same host and a global inter-request delay.
    It does NOT retry on 429/403/CAPTCHA — those responses are surfaced
    to the caller so the scan can back off.
    """

    def __init__(
        self,
        min_delay_s: float = 1.0,
        timeout_s: float = 15.0,
        global_delay_s: float = 0.35,
        use_browser_ua: bool = True,
    ):
        self.session = requests.Session()
        if use_browser_ua:
            self.session.headers.update({
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            })
        else:
            self.session.headers.update({
                "Accept": "application/json",
            })
        self.min_delay_s = min_delay_s
        self.global_delay_s = global_delay_s
        self.timeout_s = timeout_s
        self._last_hit: Dict[str, float] = {}
        self._last_global_hit: float = 0.0
        self._lock = threading.Lock()

    def get(self, url: str, **kwargs) -> requests.Response:
        host = requests.utils.urlparse(url).netloc
        with self._lock:
            now = time.monotonic()
            wait_host = self.min_delay_s - (now - self._last_hit.get(host, 0.0))
            wait_global = self.global_delay_s - (now - self._last_global_hit)
            wait = max(wait_host, wait_global, 0.0)
            self._last_hit[host] = now + wait
            self._last_global_hit = now + wait
        if wait > 0:
            time.sleep(wait)
        kwargs.setdefault("timeout", self.timeout_s)
        return self.session.get(url, **kwargs)


# ──────────────────────────── SNAPSHOTS ──────────────────────────────────────

class SnapshotStore:
    """Persist the last scan per key under ``data/radar/<namespace>/``."""

    def __init__(self, namespace: str):
        self.dir = RADAR_DATA_DIR / namespace
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", key)
        return self.dir / f"{safe}.json"

    def load(self, key: str) -> Optional[Dict[str, Any]]:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Corrupt snapshot {p.name}, ignoring: {e}")
            return None

    def save(self, key: str, data: Dict[str, Any]) -> None:
        p = self._path(key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)  # atomic on the same filesystem


# ──────────────────────────── SIGNALS ────────────────────────────────────────

@dataclass
class RadarSignal:
    source: str                      # "shopify" | "amazon"
    origin: str                      # store domain or "amazon:<category>:<list>"
    title: str
    url: str
    score: int
    reasons: List[str] = field(default_factory=list)
    image: str = ""
    price: Optional[float] = None
    currency: str = "USD"
    metrics: Dict[str, Any] = field(default_factory=dict)
    detected_at: str = field(default_factory=lambda: utc_now_iso())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def write_report(source: str, report: Dict[str, Any]) -> Path:
    """Write a scan report to ``data/radar/reports/`` and also update ``<source>_latest.json``."""
    reports_dir = RADAR_DATA_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"{source}_{stamp}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    path.write_text(payload, encoding="utf-8")
    (reports_dir / f"{source}_latest.json").write_text(payload, encoding="utf-8")
    return path


# ──────────────────────────── TEXT MATCHING ──────────────────────────────────

_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "of", "to", "in", "on", "by", "new",
    "pack", "pcs", "piece", "pieces", "set", "kit", "size", "color", "colour", "black",
    "white", "blue", "red", "pink", "green", "grey", "gray", "men", "women", "mens",
    "womens", "unisex", "edition", "premium", "original", "best", "inch", "oz", "ml",
}


def title_tokens(title: str) -> set:
    """Normalise a product title into a set of meaningful lowercase tokens."""
    words = re.findall(r"[a-z]{3,}", (title or "").lower())
    return {w.rstrip("s") for w in words if w not in _STOPWORDS}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def contains_any(text: str, terms: List[str]) -> Optional[str]:
    """Return the first blacklist term found in ``text`` (word-boundary, case-insensitive)."""
    low = (text or "").lower()
    for t in terms:
        if re.search(rf"(?<![a-z]){re.escape(t.lower())}(?![a-z])", low):
            return t
    return None
