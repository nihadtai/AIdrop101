import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CATALOG_FILE = PROJECT_ROOT / "data" / "winning_catalog.json"
_CATALOG_LOCK = threading.Lock()


def load_catalog() -> List[Dict[str, Any]]:
    """Load saved winning products catalog from disk."""
    with _CATALOG_LOCK:
        if not CATALOG_FILE.exists():
            return []
        try:
            data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as err:
            logger.warning(f"Failed to read winning_catalog.json: {err}")
            return []


def _write_catalog(items: List[Dict[str, Any]]) -> None:
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def save_catalog_item(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert a winning product + automated supply + storefront entry into winning_catalog.json."""
    item = dict(entry)
    if not item.get("id"):
        item["id"] = f"win_{uuid.uuid4().hex[:10]}"
    if not item.get("created_at"):
        item["created_at"] = datetime.now().isoformat(timespec="seconds")

    with _CATALOG_LOCK:
        items = []
        if CATALOG_FILE.exists():
            try:
                raw = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    items = raw
            except Exception:
                items = []

        # Deduplicate by id or normalized product name from same pipeline run
        prod_name = str((item.get("product") or {}).get("name") or "").strip().lower()
        existing_idx = next(
            (
                i
                for i, cur in enumerate(items)
                if cur.get("id") == item["id"]
                or (
                    prod_name
                    and str((cur.get("product") or {}).get("name") or "").strip().lower() == prod_name
                    and cur.get("thread_id") == item.get("thread_id")
                )
            ),
            None,
        )
        if existing_idx is not None:
            item["id"] = items[existing_idx].get("id", item["id"])
            items[existing_idx] = item
        else:
            items.insert(0, item)

        # Keep latest 50 catalog items with bounded memory
        _write_catalog(items[:50])

    return item


def get_catalog_item(item_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve a single catalog entry by ID."""
    for item in load_catalog():
        if item.get("id") == item_id:
            return item
    return None


def delete_catalog_item(item_id: str) -> bool:
    """Remove a catalog entry by ID."""
    with _CATALOG_LOCK:
        if not CATALOG_FILE.exists():
            return False
        try:
            raw = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return False
        except Exception:
            return False

        filtered = [cur for cur in raw if cur.get("id") != item_id]
        if len(filtered) == len(raw):
            return False
        _write_catalog(filtered)
        return True
