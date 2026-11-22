import contextlib
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BINDINGS_DB_PATH = str(PROJECT_ROOT / "data" / "orders.db")


@contextlib.contextmanager
def _get_conn() -> Iterator[sqlite3.Connection]:
    from tools.db_utils import SQLITE_WRITE_LOCK, open_sqlite_conn

    with SQLITE_WRITE_LOCK:
        conn = open_sqlite_conn(BINDINGS_DB_PATH)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS product_supplier_bindings (
                    binding_sku TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    fulfillment_method TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    product_json TEXT NOT NULL,
                    primary_supplier_json TEXT NOT NULL,
                    backup_supplier_json TEXT,
                    unit_economics_json TEXT,
                    failover_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            with conn:
                yield conn
        finally:
            conn.close()


def detect_sourcing_channel(supplier: Optional[Dict[str, Any]], manual_link: str = "") -> str:
    """Determine normalized sourcing channel: aliexpress_ds | cj_dropshipping | 1688_yunexpress."""
    sup = supplier or {}
    explicit = str(sup.get("channel") or "").strip().lower()
    if explicit in ("aliexpress_ds", "cj_dropshipping", "1688_yunexpress"):
        return explicit
    url = (manual_link or sup.get("order_link_1688") or sup.get("order_link") or "").lower()
    plat = str(sup.get("platform") or "").lower()
    if "1688.com" in url or "1688" in plat:
        return "1688_yunexpress"
    if "aliexpress" in url or "aliexpress" in plat or sup.get("ae_product_id"):
        return "aliexpress_ds"
    if "cjdropshipping" in url or "cj" in plat or sup.get("cj_pid") or sup.get("cj_vid"):
        return "cj_dropshipping"
    return "1688_yunexpress"


def describe_fulfillment_method(channel: str) -> str:
    return {
        "aliexpress_ds": "AliExpress Dropshipping API -> Customer (AliExpress Standard)",
        "cj_dropshipping": "CJ Dropshipping API 2.0 -> Customer (CJPacket)",
        "1688_yunexpress": "1688.com -> HyperSKU/SourcinBox 3PL -> YunExpress -> Customer",
    }.get(channel, "1688.com -> HyperSKU/SourcinBox 3PL -> YunExpress -> Customer")


def generate_binding_sku(channel: str, product_name: str, supplier_ref: str = "") -> str:
    """Create stable deterministic binding SKU linking Product + Supplier + Logistics method."""
    prefix = {
        "aliexpress_ds": "BND-AE",
        "cj_dropshipping": "BND-CJ",
        "1688_yunexpress": "BND-1688",
    }.get(channel, "BND-SUP")
    seed = f"{channel}|{product_name.strip().lower()}|{supplier_ref.strip().lower()}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:6].upper()
    return f"{prefix}-{digest}"


def create_or_update_binding(
    product: Dict[str, Any],
    primary_supplier: Dict[str, Any],
    backup_supplier: Optional[Dict[str, Any]] = None,
    unit_economics: Optional[Dict[str, Any]] = None,
    binding_sku: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Encode approved Product + Primary Supplier + Fulfillment Method + Optional Backup #2
    into a unified binding_sku record and persist it to SQLite.
    """
    prod = dict(product or {})
    sup = dict(primary_supplier or {})
    econ = dict(unit_economics or sup.get("unit_economics") or {})
    backup = dict(backup_supplier) if backup_supplier else None

    channel = detect_sourcing_channel(sup)
    fulfillment_method = describe_fulfillment_method(channel)
    prod_name = str(prod.get("name") or prod.get("english_name") or sup.get("product_name") or "Product").strip()
    ref = str(
        sup.get("ae_product_id")
        or sup.get("cj_pid")
        or sup.get("order_link_1688")
        or sup.get("order_link")
        or sup.get("product_sku")
        or ""
    )
    sku = (binding_sku or generate_binding_sku(channel, prod_name, ref)).strip()

    sup["channel"] = channel
    sup["binding_sku"] = sku
    sup["product_sku"] = sku
    if not sup.get("fulfillment_method"):
        sup["fulfillment_method"] = fulfillment_method

    prod["binding_sku"] = sku
    prod["sourcing_channel"] = channel

    now_iso = datetime.now(timezone.utc).isoformat()
    binding_obj = {
        "binding_sku": sku,
        "channel": channel,
        "fulfillment_method": fulfillment_method,
        "product_name": prod_name,
        "product": prod,
        "primary_supplier": sup,
        "backup_supplier": backup,
        "unit_economics": econ,
        "failover_count": 0,
        "status": "ACTIVE",
        "updated_at": now_iso,
    }

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO product_supplier_bindings (
                binding_sku, channel, fulfillment_method, product_name,
                product_json, primary_supplier_json, backup_supplier_json,
                unit_economics_json, failover_count, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'ACTIVE', ?, ?)
            ON CONFLICT(binding_sku) DO UPDATE SET
                channel = excluded.channel,
                fulfillment_method = excluded.fulfillment_method,
                product_name = excluded.product_name,
                product_json = excluded.product_json,
                primary_supplier_json = excluded.primary_supplier_json,
                backup_supplier_json = excluded.backup_supplier_json,
                unit_economics_json = excluded.unit_economics_json,
                status = 'ACTIVE',
                updated_at = excluded.updated_at
            """,
            (
                sku,
                channel,
                fulfillment_method,
                prod_name,
                json.dumps(prod, ensure_ascii=False),
                json.dumps(sup, ensure_ascii=False),
                json.dumps(backup, ensure_ascii=False) if backup else None,
                json.dumps(econ, ensure_ascii=False),
                now_iso,
                now_iso,
            ),
        )
        conn.commit()

    return binding_obj


def resolve_binding_by_sku(binding_sku: str) -> Optional[Dict[str, Any]]:
    """Lookup encoded product-supplier binding record by binding_sku."""
    sku = (binding_sku or "").strip()
    if not sku:
        return None
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM product_supplier_bindings WHERE binding_sku = ?", (sku,)).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def update_binding_after_failover(binding_sku: str, new_primary: Dict[str, Any], new_backup: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Promote backup #2 inside binding record while keeping binding_sku unchanged on the storefront."""
    existing = resolve_binding_by_sku(binding_sku)
    if not existing:
        return None
    sup = dict(new_primary or {})
    new_channel = detect_sourcing_channel(sup)
    new_method = describe_fulfillment_method(new_channel)
    sup["channel"] = new_channel
    sup["binding_sku"] = binding_sku
    sup["product_sku"] = binding_sku
    econ = dict(sup.get("unit_economics") or existing.get("unit_economics") or {})
    now_iso = datetime.now(timezone.utc).isoformat()
    fail_cnt = int(existing.get("failover_count") or 0) + 1

    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE product_supplier_bindings
            SET channel = ?, fulfillment_method = ?, primary_supplier_json = ?,
                backup_supplier_json = COALESCE(?, backup_supplier_json),
                unit_economics_json = ?, failover_count = ?, status = 'FAILOVER_ACTIVE', updated_at = ?
            WHERE binding_sku = ?
            """,
            (
                new_channel,
                new_method,
                json.dumps(sup, ensure_ascii=False),
                json.dumps(new_backup, ensure_ascii=False) if new_backup is not None else None,
                json.dumps(econ, ensure_ascii=False),
                fail_cnt,
                now_iso,
                binding_sku,
            ),
        )
        conn.commit()
    return resolve_binding_by_sku(binding_sku)


def list_bindings(limit: int = 20) -> List[Dict[str, Any]]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM product_supplier_bindings ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "binding_sku": row["binding_sku"],
        "channel": row["channel"],
        "fulfillment_method": row["fulfillment_method"],
        "product_name": row["product_name"],
        "product": json.loads(row["product_json"]),
        "primary_supplier": json.loads(row["primary_supplier_json"]),
        "backup_supplier": json.loads(row["backup_supplier_json"]) if row["backup_supplier_json"] else None,
        "unit_economics": json.loads(row["unit_economics_json"]) if row["unit_economics_json"] else {},
        "failover_count": row["failover_count"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
