import contextlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from tools.aliexpress_ds_tool import check_aliexpress_ds_product_status
from tools.binding_store import detect_sourcing_channel, update_binding_after_failover
from tools.cargo_3pl import is_1688_url
from tools.cj_dropshipping_tool import check_cj_product_live_status
from tools.notification_tools import send_telegram_notification
from tools.order_automation import ORDERS_DB_PATH, list_orders, sync_cj_order_status, sync_ae_order_status
from tools.sourcing_tools import get_live_fx_rates, scrape_1688_link_direct

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _get_conn() -> Iterator[sqlite3.Connection]:
    from tools.db_utils import SQLITE_WRITE_LOCK, open_sqlite_conn

    with SQLITE_WRITE_LOCK:
        conn = open_sqlite_conn(ORDERS_DB_PATH)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watched_suppliers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    binding_sku TEXT,
                    product_name TEXT NOT NULL,
                    primary_json TEXT NOT NULL,
                    backups_json TEXT NOT NULL,
                    baseline_cost_usd REAL,
                    last_cost_usd REAL,
                    last_stock INTEGER,
                    status TEXT NOT NULL,
                    alert_message TEXT,
                    auto_failover INTEGER NOT NULL DEFAULT 1,
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cols = {r[1] for r in conn.execute("PRAGMA table_info(watched_suppliers)").fetchall()}
            if "auto_failover" not in cols:
                conn.execute("ALTER TABLE watched_suppliers ADD COLUMN auto_failover INTEGER NOT NULL DEFAULT 1")
            if "binding_sku" not in cols:
                conn.execute("ALTER TABLE watched_suppliers ADD COLUMN binding_sku TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS monitor_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_id INTEGER NOT NULL,
                    alert_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    recommended_backup_json TEXT,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            with conn:
                yield conn
        finally:
            conn.close()


def _supplier_cost(sup: Dict[str, Any]) -> Optional[float]:
    ue = sup.get("unit_economics") or {}
    val = ue.get("product_cost")
    if val is None:
        return None
    try:
        return round(float(val), 2)
    except (TypeError, ValueError):
        return None


def _backup_candidates(primary: Dict[str, Any], all_suppliers: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not all_suppliers:
        return []
    p_key = (primary.get("product_sku"), primary.get("supplier_name"), primary.get("cj_pid"), primary.get("ae_product_id"), primary.get("order_link"))
    backups = []
    for s in all_suppliers:
        s_key = (s.get("product_sku"), s.get("supplier_name"), s.get("cj_pid"), s.get("ae_product_id"), s.get("order_link"))
        if s_key != p_key:
            backups.append(s)
    return backups[:3]


def register_supplier_watch(
    selected_supplier: Dict[str, Any],
    all_suppliers: Optional[List[Dict[str, Any]]] = None,
    product_name: str = "",
    binding_sku: str = "",
    auto_failover: bool = True,
) -> Optional[int]:
    """Registers an approved supplier + backup candidates in SQLite for Phase 4 monitoring & failover."""
    if not selected_supplier:
        return None
    p_name = (product_name or selected_supplier.get("product_name") or selected_supplier.get("supplier_name") or "Product").strip()
    sku_val = (binding_sku or selected_supplier.get("binding_sku") or selected_supplier.get("product_sku") or "").strip()
    baseline = _supplier_cost(selected_supplier)
    backups = _backup_candidates(selected_supplier, all_suppliers)
    now_iso = datetime.now(timezone.utc).isoformat()

    with _get_conn() as conn:
        conn.execute(
            "UPDATE watched_suppliers SET status = 'REPLACED' WHERE product_name = ? AND status != 'REPLACED'",
            (p_name,),
        )
        cur = conn.execute(
            """
            INSERT INTO watched_suppliers (
                binding_sku, product_name, primary_json, backups_json, baseline_cost_usd,
                last_cost_usd, last_stock, status, alert_message, auto_failover, last_checked_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sku_val,
                p_name,
                json.dumps(selected_supplier, ensure_ascii=False),
                json.dumps(backups, ensure_ascii=False),
                baseline,
                baseline,
                None,
                "HEALTHY",
                "Normal işləyir",
                1 if auto_failover else 0,
                now_iso,
                now_iso,
            ),
        )
        conn.commit()
        return cur.lastrowid


def _notify_telegram_safe(text: str) -> str:
    try:
        raw = send_telegram_notification(text)
        res = json.loads(raw) if isinstance(raw, str) else {}
        return res.get("status", "unknown")
    except Exception as e:
        logger.debug(f"Telegram alert skip: {e}")
        return "error"


def _create_alert(
    conn: sqlite3.Connection,
    watch_id: int,
    alert_type: str,
    severity: str,
    message: str,
    backup: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO monitor_alerts (
            watch_id, alert_type, severity, message, recommended_backup_json, resolved, created_at
        ) VALUES (?, ?, ?, ?, ?, 0, ?)
        """,
        (watch_id, alert_type, severity, message, json.dumps(backup, ensure_ascii=False) if backup else None, now_iso),
    )
    return {
        "watch_id": watch_id,
        "alert_type": alert_type,
        "severity": severity,
        "message": message,
        "recommended_backup": backup,
        "created_at": now_iso,
    }


def _check_live_supplier_status(primary: Dict[str, Any]) -> Dict[str, Any]:
    """Query live stock & USD wholesale price across CJ, AliExpress DS, or 1688.com."""
    channel = detect_sourcing_channel(primary)
    if channel == "cj_dropshipping":
        cj_pid = str(primary.get("cj_pid") or "").strip()
        cj_vid = str(primary.get("cj_vid") or "").strip()
        if not (cj_pid or cj_vid):
            return {"checked": False, "available": True}
        res = check_cj_product_live_status(pid=cj_pid, vid=cj_vid)
        return {"checked": True, **res}

    if channel == "aliexpress_ds":
        ae_pid = str(primary.get("ae_product_id") or "").strip()
        ae_sid = str(primary.get("ae_sku_id") or "").strip()
        if not ae_pid:
            return {"checked": False, "available": True}
        return check_aliexpress_ds_product_status(ae_pid, ae_sid)

    # 1688_yunexpress channel
    link_1688 = str(primary.get("order_link_1688") or primary.get("order_link") or "").strip()
    if not is_1688_url(link_1688):
        return {"checked": False, "available": True}
    scraped = scrape_1688_link_direct(link_1688)
    if not scraped:
        return {"checked": False, "available": True}
    price_cny = scraped.get("price_cny")
    price_usd = None
    if price_cny:
        fx = get_live_fx_rates()
        price_usd = round(float(price_cny) / (fx.get("CNY") or 7.15), 2)
    return {
        "checked": True,
        "available": bool(scraped.get("in_stock", True)),
        "stock": 0 if not scraped.get("in_stock", True) else None,
        "price_usd": price_usd,
    }


def run_supply_monitor_cycle(price_spike_pct: float = 10.0) -> Dict[str, Any]:
    """
    Phase 4 Monitor Cycle:
    1) Checks watched CJ / AliExpress DS / 1688 suppliers for out-of-stock (0 units) OR >10% price spikes.
    2) Auto-promotes Backup #2 when auto_failover is active (updating binding_sku too).
    3) Syncs open CJ and AliExpress orders for tracking numbers.
    """
    checked_watches = 0
    new_alerts: List[Dict[str, Any]] = []
    order_updates: List[Dict[str, Any]] = []
    auto_failover_ids: List[int] = []
    auto_failovers: List[Dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM watched_suppliers WHERE status IN ('HEALTHY', 'ALERT', 'FAILOVER_ACTIVE') ORDER BY id DESC LIMIT 10"
        ).fetchall()

        for row in rows:
            checked_watches += 1
            primary = json.loads(row["primary_json"])
            backups = json.loads(row["backups_json"]) if row["backups_json"] else []
            backup_2 = backups[0] if backups else None

            live = _check_live_supplier_status(primary)
            if not live.get("checked"):
                conn.execute(
                    "UPDATE watched_suppliers SET last_checked_at = ? WHERE id = ?",
                    (now_iso, row["id"]),
                )
                continue

            live_cost = live.get("price_usd")
            live_stock = live.get("stock")
            baseline = row["baseline_cost_usd"]
            watch_status = "HEALTHY"
            status_msg = "Qiymət və stok stabildir"

            # Check Out-of-Stock / Unavailable
            if not live.get("available", True) or (live_stock is not None and live_stock <= 0):
                watch_status = "ALERT"
                backup_txt = (
                    f" Ehtiyat #2 təklif olunur: {backup_2.get('supplier_name')} (${_supplier_cost(backup_2)})"
                    if backup_2 else ""
                )
                status_msg = f"⚠️ STOK BİTDİ: '{row['product_name']}' üçün stok=0.{backup_txt}"
                new_alerts.append(_create_alert(conn, row["id"], "OUT_OF_STOCK", "HIGH", status_msg, backup_2))
                _notify_telegram_safe(status_msg)
                if row["auto_failover"] and backup_2:
                    auto_failover_ids.append(row["id"])

            # Check Price Spike (> 10%) -> Also triggers automatic failover to Backup #2
            elif live_cost is not None and baseline and baseline > 0:
                pct_change = ((live_cost - baseline) / baseline) * 100.0
                if pct_change > price_spike_pct:
                    watch_status = "ALERT"
                    backup_txt = (
                        f" Ehtiyat #2: {backup_2.get('supplier_name')} (${_supplier_cost(backup_2)})"
                        if backup_2 else ""
                    )
                    status_msg = (
                        f"📈 QİYMƏT ARTIMI (+{pct_change:.1f}%): '{row['product_name']}' "
                        f"${baseline:.2f} -> ${live_cost:.2f}.{backup_txt}"
                    )
                    new_alerts.append(_create_alert(conn, row["id"], "PRICE_SPIKE", "MEDIUM", status_msg, backup_2))
                    _notify_telegram_safe(status_msg)
                    if row["auto_failover"] and backup_2:
                        auto_failover_ids.append(row["id"])

            conn.execute(
                """
                UPDATE watched_suppliers
                SET last_cost_usd = COALESCE(?, last_cost_usd),
                    last_stock = ?,
                    status = ?,
                    alert_message = ?,
                    last_checked_at = ?
                WHERE id = ?
                """,
                (live_cost, live_stock, watch_status, status_msg, now_iso, row["id"]),
            )
        conn.commit()

    for wid in auto_failover_ids:
        sw = switch_to_backup_supplier(wid, 0)
        if sw.get("success"):
            ns = sw.get("new_supplier") or {}
            auto_failovers.append({"watch_id": wid, "new_supplier": ns.get("supplier_name"), "binding_sku": sw.get("binding_sku")})
            _notify_telegram_safe(f"🔄 AVTO-KEÇİD (#2): Tədarükçü dəyişdirildi -> {ns.get('supplier_name')}")

    # Sync open CJ and AliExpress orders for tracking number updates
    for o in list_orders(limit=15):
        ext_id = str(o.get("cj_order_id") or "")
        if not ext_id or str(o.get("status", "")).upper() in ("DELIVERED", "CANCELLED"):
            continue
        old_track = o.get("tracking_number") or ""
        old_status = o.get("status") or ""
        synced = sync_ae_order_status(o["order_number"]) if ext_id.startswith("AE:") else sync_cj_order_status(o["order_number"])
        if synced.get("success"):
            new_track = synced.get("tracking_number") or ""
            new_status = synced.get("status") or ""
            if (new_track and new_track != old_track) or (new_status and new_status != old_status):
                msg = f"🚚 Sifariş [{o['order_number']}] yeniləndi: {new_status}" + (f" | Tracking: {new_track}" if new_track else "")
                order_updates.append({"order_number": o["order_number"], "status": new_status, "tracking_number": new_track})
                _notify_telegram_safe(msg)

    return {
        "checked_watches": checked_watches,
        "new_alerts": new_alerts,
        "order_updates": order_updates,
        "auto_failovers": auto_failovers,
        "checked_at": now_iso,
    }


def toggle_auto_failover(watch_id: int, enabled: bool) -> Dict[str, Any]:
    """Enables or disables automatic failover to Backup #2 when primary goes out of stock or spikes."""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE watched_suppliers SET auto_failover = ? WHERE id = ?",
            (1 if enabled else 0, watch_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"success": False, "error": "Watch qeydi tapılmadı."}
    return {"success": True, "watch_id": watch_id, "auto_failover": bool(enabled)}


def get_active_watched_supplier(sku: str = "", title: str = "") -> Optional[Dict[str, Any]]:
    """Finds an active primary supplier from SQLite matching SKU, binding_sku, or product title."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT binding_sku, product_name, primary_json FROM watched_suppliers WHERE status != 'REPLACED' ORDER BY id DESC LIMIT 20"
        ).fetchall()

    sku_clean = (sku or "").strip().lower()
    title_clean = (title or "").strip().lower()
    best_fallback = None
    for row in rows:
        primary = json.loads(row["primary_json"])
        if best_fallback is None:
            best_fallback = primary
        row_bsku = str(row["binding_sku"] or "").strip().lower()
        p_sku = str(primary.get("product_sku") or "").strip().lower()
        p_name = str(row["product_name"] or primary.get("product_name") or "").strip().lower()
        if sku_clean and (sku_clean == row_bsku or sku_clean == p_sku):
            return primary
        if title_clean and p_name and (title_clean in p_name or p_name in title_clean):
            return primary
    return best_fallback


def switch_to_backup_supplier(watch_id: int, backup_index: int = 0) -> Dict[str, Any]:
    """Promotes Backup #2 (or specified index) supplier to Primary, syncs binding_sku, and resolves alerts."""
    binding_sku = ""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM watched_suppliers WHERE id = ?", (watch_id,)).fetchone()
        if not row:
            return {"success": False, "error": "Watch qeydi tapılmadı."}

        binding_sku = str(row["binding_sku"] or "").strip()
        primary = json.loads(row["primary_json"])
        backups = json.loads(row["backups_json"]) if row["backups_json"] else []
        if not backups or backup_index < 0 or backup_index >= len(backups):
            return {"success": False, "error": "Ehtiyat tədarükçü (#2) siyahıda mövcud deyil."}

        new_primary = backups.pop(backup_index)
        remaining_backups = backups + [primary]
        new_baseline = _supplier_cost(new_primary)
        now_iso = datetime.now(timezone.utc).isoformat()

        conn.execute(
            """
            UPDATE watched_suppliers
            SET primary_json = ?, backups_json = ?, baseline_cost_usd = ?,
                last_cost_usd = ?, status = 'FAILOVER_ACTIVE',
                alert_message = 'Ehtiyat tədarükçüyə (#2) avtomatik keçid edildi', last_checked_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(new_primary, ensure_ascii=False),
                json.dumps(remaining_backups, ensure_ascii=False),
                new_baseline,
                new_baseline,
                now_iso,
                watch_id,
            ),
        )
        conn.execute("UPDATE monitor_alerts SET resolved = 1 WHERE watch_id = ?", (watch_id,))
        conn.commit()

    if binding_sku:
        update_binding_after_failover(
            binding_sku,
            new_primary,
            new_backup=remaining_backups[0] if remaining_backups else None,
        )

    return {
        "success": True,
        "watch_id": watch_id,
        "binding_sku": binding_sku,
        "new_supplier": new_primary,
        "remaining_backups": remaining_backups,
    }


def list_watches_and_alerts(limit: int = 15) -> Dict[str, Any]:
    """Returns active supplier watches and recent alerts from SQLite."""
    with _get_conn() as conn:
        watch_rows = conn.execute(
            "SELECT * FROM watched_suppliers WHERE status != 'REPLACED' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        alert_rows = conn.execute(
            "SELECT * FROM monitor_alerts ORDER BY resolved ASC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    watches = []
    for w in watch_rows:
        watches.append({
            "id": w["id"],
            "product_name": w["product_name"],
            "primary": json.loads(w["primary_json"]),
            "backups": json.loads(w["backups_json"]) if w["backups_json"] else [],
            "baseline_cost_usd": w["baseline_cost_usd"],
            "last_cost_usd": w["last_cost_usd"],
            "last_stock": w["last_stock"],
            "status": w["status"],
            "alert_message": w["alert_message"],
            "auto_failover": bool(w["auto_failover"]),
            "last_checked_at": w["last_checked_at"],
        })

    alerts = []
    for a in alert_rows:
        alerts.append({
            "id": a["id"],
            "watch_id": a["watch_id"],
            "alert_type": a["alert_type"],
            "severity": a["severity"],
            "message": a["message"],
            "recommended_backup": json.loads(a["recommended_backup_json"]) if a["recommended_backup_json"] else None,
            "resolved": bool(a["resolved"]),
            "created_at": a["created_at"],
        })

    return {"watches": watches, "alerts": alerts}
