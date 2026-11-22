import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from tools.db_utils import SQLITE_WRITE_LOCK, open_sqlite_conn

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POOL_DB_PATH = str(PROJECT_ROOT / "data" / "orders.db")
HISTORY_JSON_PATH = str(PROJECT_ROOT / "data" / "discovered_history.json")

_NOISE_TOKENS = {
    "for", "with", "and", "the", "new", "pro", "hot", "sale", "set", "pcs", "in", "of", "to", "on",
    "üçün", "və", "ilə", "yeni", "smart", "agilli", "ağıllı", "portativ", "portable", "mini",
    "naqilsiz", "simsiz", "wireless", "cihaz", "cihazı", "aparat", "aparatı", "alət", "aləti",
}


@contextlib.contextmanager
def _get_conn() -> Iterator[sqlite3.Connection]:
    with SQLITE_WRITE_LOCK:
        conn = open_sqlite_conn(POOL_DB_PATH)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discovered_products (
                    fingerprint TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    english_name TEXT NOT NULL,
                    product_json TEXT NOT NULL,
                    discovered_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_product_history (
                    user_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    name TEXT NOT NULL,
                    english_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'seen',
                    seen_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, fingerprint)
                )
                """
            )
            with conn:
                yield conn
        finally:
            conn.close()


def _tokenize_title(text: str) -> List[str]:
    if not text:
        return []
    cleaned = text.lower()
    for src, dst in (("ə", "e"), ("ı", "i"), ("ö", "o"), ("ü", "u"), ("ğ", "g"), ("ş", "s"), ("ç", "c")):
        cleaned = cleaned.replace(src, dst)
    raw = re.findall(r"[a-z0-9]+", cleaned)
    core = [w for w in raw if len(w) > 2 and w not in _NOISE_TOKENS]
    return core if core else [w for w in raw if len(w) > 2]


def compute_product_fingerprint(name: str, english_name: str = "") -> str:
    """Compute canonical fingerprint based on sorted core English (or normalized AZ) tokens."""
    en_tokens = sorted(set(_tokenize_title(english_name)))
    az_tokens = sorted(set(_tokenize_title(name)))
    basis = " ".join(en_tokens if en_tokens else az_tokens) or (english_name or name).strip().lower()
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    return f"fp_{digest}"


def is_fuzzy_duplicate(
    cand_or_name: Any,
    cand_en_or_blocked: Any = "",
    blocked_items: Optional[List[Dict[str, Any]]] = None,
    threshold: float = 0.56,
) -> bool:
    """
    Detect if a candidate product is the same as or a close variation of any blocked product
    (catches 'Naqilsiz' vs 'Simsiz' and English word-order permutations).
    Supports both:
      - is_fuzzy_duplicate(candidate_dict, blocked_items)
      - is_fuzzy_duplicate(cand_name, cand_en, blocked_items)
    """
    if isinstance(cand_or_name, dict):
        cand_name = str(cand_or_name.get("name") or "").strip()
        cand_en = str(cand_or_name.get("english_name") or cand_name).strip()
        items = cand_en_or_blocked if isinstance(cand_en_or_blocked, list) else (blocked_items or [])
    else:
        cand_name = str(cand_or_name or "").strip()
        if isinstance(cand_en_or_blocked, list):
            cand_en = cand_name
            items = cand_en_or_blocked
        else:
            cand_en = str(cand_en_or_blocked or cand_name).strip()
            items = blocked_items or []

    if not items:
        return False

    c_fp = compute_product_fingerprint(cand_name, cand_en)
    c_en_tok = set(_tokenize_title(cand_en))
    c_az_tok = set(_tokenize_title(cand_name))

    for b in items:
        if isinstance(b, str):
            b_name, b_en = b.strip(), b.strip()
        elif isinstance(b, dict):
            b_name = str(b.get("name") or "").strip()
            b_en = str(b.get("english_name") or "").strip()
        else:
            continue
        if not b_name and not b_en:
            continue
        if compute_product_fingerprint(b_name, b_en) == c_fp:
            return True

        b_en_tok = set(_tokenize_title(b_en))
        if c_en_tok and b_en_tok:
            inter = len(c_en_tok & b_en_tok)
            smaller = min(len(c_en_tok), len(b_en_tok))
            if smaller > 0 and (inter / smaller) >= threshold:
                return True

        b_az_tok = set(_tokenize_title(b_name))
        if c_az_tok and b_az_tok:
            inter_az = len(c_az_tok & b_az_tok)
            smaller_az = min(len(c_az_tok), len(b_az_tok))
            if smaller_az > 0 and (inter_az / smaller_az) >= threshold:
                return True

    return False


def _sync_pool_to_json_file() -> None:
    """Mirror active shared product pool and user ledger summary to data/discovered_history.json."""
    try:
        with _get_conn() as conn:
            p_rows = conn.execute(
                "SELECT fingerprint, category, name, english_name, product_json, discovered_at FROM discovered_products ORDER BY discovered_at DESC LIMIT 200"
            ).fetchall()
            h_rows = conn.execute(
                "SELECT user_id, fingerprint, name, english_name, status, seen_at FROM user_product_history ORDER BY seen_at DESC LIMIT 500"
            ).fetchall()

        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "shared_products": [
                {
                    "fingerprint": r["fingerprint"],
                    "category": r["category"],
                    "name": r["name"],
                    "english_name": r["english_name"],
                    "discovered_at": r["discovered_at"],
                    "product": json.loads(r["product_json"]),
                }
                for r in p_rows
            ],
            "user_history": [dict(r) for r in h_rows],
        }
        os.makedirs(os.path.dirname(HISTORY_JSON_PATH), exist_ok=True)
        with open(HISTORY_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.debug(f"discovered_history.json sync note: {exc}")


def save_discovered_to_pool(products: List[Dict[str, Any]], category: str = "all") -> int:
    """Save newly discovered products into the shared multi-tenant pool."""
    if not products:
        return 0
    cat_norm = (category or "all").strip().lower()
    now_iso = datetime.now(timezone.utc).isoformat()
    saved = 0
    with _get_conn() as conn:
        for p in products:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            name = str(p.get("name") or "").strip()
            en_name = str(p.get("english_name") or name).strip()
            fp = compute_product_fingerprint(name, en_name)
            conn.execute(
                """
                INSERT INTO discovered_products (fingerprint, category, name, english_name, product_json, discovered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    product_json = excluded.product_json,
                    discovered_at = excluded.discovered_at
                """,
                (fp, cat_norm, name, en_name, json.dumps(p, ensure_ascii=False), now_iso),
            )
            saved += 1
    _sync_pool_to_json_file()
    return saved


def mark_products_for_user(user_id: str, products: List[Dict[str, Any]], status: str = "seen") -> None:
    """Record that user_id has seen, rejected, or approved specific products so they never repeat for this user."""
    uid = (user_id or "default_user").strip()
    if not products:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        for p in products:
            if isinstance(p, str):
                name = p.strip()
                en_name = name
            elif isinstance(p, dict):
                name = str(p.get("name") or "").strip()
                en_name = str(p.get("english_name") or name).strip()
            else:
                continue
            if not name:
                continue
            fp = compute_product_fingerprint(name, en_name)
            conn.execute(
                """
                INSERT INTO user_product_history (user_id, fingerprint, name, english_name, status, seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, fingerprint) DO UPDATE SET
                    status = CASE WHEN excluded.status IN ('rejected', 'approved') THEN excluded.status ELSE user_product_history.status END,
                    seen_at = excluded.seen_at
                """,
                (uid, fp, name, en_name, status, now_iso),
            )
    _sync_pool_to_json_file()


def get_user_excluded_items(user_id: str, extra_rejected: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """Return all products already seen or rejected by this user_id for prompt exclusion & fuzzy filtering."""
    uid = (user_id or "default_user").strip()
    items: List[Dict[str, str]] = []
    seen_fps = set()

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT fingerprint, name, english_name FROM user_product_history WHERE user_id = ? ORDER BY seen_at DESC LIMIT 120",
            (uid,),
        ).fetchall()
        for r in rows:
            seen_fps.add(r["fingerprint"])
            items.append({"name": r["name"], "english_name": r["english_name"]})

    for raw in extra_rejected or []:
        s = str(raw or "").strip()
        if not s:
            continue
        fp = compute_product_fingerprint(s, s)
        if fp not in seen_fps:
            seen_fps.add(fp)
            items.append({"name": s, "english_name": s})

    return items


def fetch_unseen_from_pool(
    user_id: str,
    category: str = "all",
    limit: int = 3,
    ttl_days: int = 14,
    extra_rejected: Optional[List[str]] = None,
    count: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch up to `limit` (or `count`) fresh (< ttl_days) products from the shared pool that were discovered for
    other users in the same category, which `user_id` has NEVER seen or rejected before.
    Reduces Gemini API calls while guaranteeing each user sees every product at most once.
    """
    if count is not None:
        limit = int(count)
    uid = (user_id or "default_user").strip()
    cat_norm = (category or "all").strip().lower()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    excluded = get_user_excluded_items(uid, extra_rejected=extra_rejected)

    with _get_conn() as conn:
        if cat_norm == "all":
            rows = conn.execute(
                """
                SELECT d.product_json, d.name, d.english_name
                FROM discovered_products d
                WHERE d.discovered_at >= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM user_product_history u
                      WHERE u.user_id = ? AND u.fingerprint = d.fingerprint
                  )
                ORDER BY d.discovered_at DESC
                LIMIT 25
                """,
                (cutoff, uid),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT d.product_json, d.name, d.english_name
                FROM discovered_products d
                WHERE d.category = ?
                  AND d.discovered_at >= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM user_product_history u
                      WHERE u.user_id = ? AND u.fingerprint = d.fingerprint
                  )
                ORDER BY d.discovered_at DESC
                LIMIT 25
                """,
                (cat_norm, cutoff, uid),
            ).fetchall()

    selected: List[Dict[str, Any]] = []
    local_blocked = list(excluded)
    for r in rows:
        try:
            prod = json.loads(r["product_json"])
        except Exception:
            continue
        p_name = str(prod.get("name") or r["name"] or "").strip()
        p_en = str(prod.get("english_name") or r["english_name"] or p_name).strip()
        if is_fuzzy_duplicate(p_name, p_en, local_blocked):
            continue
        selected.append(prod)
        local_blocked.append({"name": p_name, "english_name": p_en})
        if len(selected) >= limit:
            break

    return selected
