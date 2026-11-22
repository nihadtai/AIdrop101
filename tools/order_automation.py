import contextlib
import csv
import io
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from tools.aliexpress_ds_tool import create_aliexpress_ds_order, sync_aliexpress_ds_order
from tools.binding_store import detect_sourcing_channel
from tools.cargo_3pl import build_1688_yunexpress_3pl_dispatch
from tools.cj_dropshipping_tool import create_cj_order, get_cj_order_detail, get_cj_tracking_info

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORDERS_DB_PATH = os.path.join(_PROJECT_ROOT, "data", "orders.db")

_COUNTRY_MAP = {
    "US": "United States",
    "GB": "United Kingdom",
    "UK": "United Kingdom",
    "DE": "Germany",
    "FR": "France",
    "CA": "Canada",
    "AU": "Australia",
    "NL": "Netherlands",
    "ES": "Spain",
    "IT": "Italy",
    "AZ": "Azerbaijan",
}


@contextlib.contextmanager
def _get_conn() -> Iterator[sqlite3.Connection]:
    from tools.db_utils import SQLITE_WRITE_LOCK, open_sqlite_conn

    with SQLITE_WRITE_LOCK:
        conn = open_sqlite_conn(ORDERS_DB_PATH)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_number TEXT UNIQUE NOT NULL,
                    order_type TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    supplier_name TEXT,
                    product_name TEXT,
                    product_sku TEXT,
                    cj_pid TEXT,
                    cj_vid TEXT,
                    quantity INTEGER NOT NULL,
                    customer_name TEXT,
                    country_code TEXT,
                    shipping_address TEXT,
                    city TEXT,
                    province TEXT,
                    zip_code TEXT,
                    phone TEXT,
                    email TEXT,
                    logistic_line TEXT,
                    estimated_cost_usd REAL,
                    cj_order_id TEXT,
                    tracking_number TEXT,
                    status TEXT NOT NULL,
                    api_message TEXT,
                    agent_pack_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            with conn:
                yield conn
        finally:
            conn.close()


def build_agent_order_pack(
    order_number: str,
    supplier: Dict[str, Any],
    shipping: Dict[str, Any],
    quantity: int = 1,
    order_type: str = "sample",
) -> Dict[str, Any]:
    """
    Generates a standardized US/EU fulfillment pack for 1688 -> YunExpress (HyperSKU/SourcinBox) / AE / CJ.
    """
    product_name = supplier.get("product_name") or supplier.get("supplier_name") or "Product"
    sku = supplier.get("binding_sku") or supplier.get("product_sku") or "N/A"
    order_link = supplier.get("order_link") or supplier.get("order_link_1688") or ""
    cn_keywords = supplier.get("chinese_keywords") or ""
    logistic = supplier.get("logistic_line") or "YunExpress Tracked"
    country_code = (shipping.get("country_code") or "US").upper()
    country_name = _COUNTRY_MAP.get(country_code, shipping.get("country") or country_code)

    agent_message = (
        f"ORDER PACK [{order_number}] ({'SAMPLE' if order_type == 'sample' else 'CUSTOMER'})\n"
        f"Binding SKU: {sku}\n"
        f"Product: {product_name}\n"
        f"Quantity: {quantity} pcs\n"
        f"Supplier Link: {order_link}\n"
        + (f"1688 Search Keywords (CN): {cn_keywords}\n" if cn_keywords else "")
        + f"Preferred Line: {logistic} -> {country_code}\n"
        f"---\n"
        f"Recipient: {shipping.get('customer_name', '')}\n"
        f"Address: {shipping.get('address', '')}\n"
        f"City / State / ZIP: {shipping.get('city', '')}, {shipping.get('province', '')} {shipping.get('zip_code', '')}\n"
        f"Country: {country_name} ({country_code})\n"
        f"Phone: {shipping.get('phone', '')} | Email: {shipping.get('email', '')}"
    )

    return {
        "order_number": order_number,
        "order_type": order_type,
        "binding_sku": sku,
        "product_name": product_name,
        "sku": sku,
        "quantity": quantity,
        "supplier_platform": supplier.get("platform", "1688 -> YunExpress (3PL)"),
        "order_link": order_link,
        "order_link_1688": supplier.get("order_link_1688", order_link),
        "chinese_keywords": cn_keywords,
        "image_url": supplier.get("product_image_url", ""),
        "logistic_line": logistic,
        "shipping": {
            "customer_name": shipping.get("customer_name", ""),
            "address": shipping.get("address", ""),
            "city": shipping.get("city", ""),
            "province": shipping.get("province", ""),
            "zip_code": shipping.get("zip_code", ""),
            "country_code": country_code,
            "country": country_name,
            "phone": shipping.get("phone", ""),
            "email": shipping.get("email", ""),
        },
        "agent_links": {
            "hypersku": "https://app.hypersku.com/",
            "sourcinbox": "https://app.sourcinbox.com/",
            "cjdropshipping": "https://cjdropshipping.com/",
            "aliexpress": "https://www.aliexpress.com/",
        },
        "copy_message": agent_message,
    }


def place_automated_order(
    supplier: Dict[str, Any],
    shipping: Dict[str, Any],
    quantity: int = 1,
    order_type: str = "sample",
    pay_from_balance: bool = False,
) -> Dict[str, Any]:
    """
    Unified Phase 3 Order Router:
    - aliexpress_ds: calls AliExpress Open Platform aliexpress.ds.order.create
    - cj_dropshipping: calls CJ API 2.0 createOrderV2
    - 1688_yunexpress: builds 1688 -> HyperSKU/SourcinBox 3PL -> YunExpress -> Customer dispatch
    Persists every order to SQLite (data/orders.db).
    """
    qty = max(1, int(quantity or 1))
    country_code = (shipping.get("country_code") or "US").upper()
    if country_code == "UK":
        country_code = "GB"
    country_name = _COUNTRY_MAP.get(country_code, shipping.get("country") or country_code)

    prefix = "SMP" if order_type == "sample" else "ORD"
    order_number = f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

    channel = detect_sourcing_channel(supplier)
    platform = supplier.get("platform") or {
        "aliexpress_ds": "AliExpress DS API",
        "cj_dropshipping": "CJ Dropshipping (API 2.0)",
        "1688_yunexpress": "1688.com -> YunExpress (3PL)",
    }.get(channel, "1688.com -> YunExpress (3PL)")
    binding_sku = str(supplier.get("binding_sku") or supplier.get("product_sku") or "N/A")
    cj_pid = str(supplier.get("cj_pid") or "").strip()
    cj_vid = str(supplier.get("cj_vid") or "").strip()
    ae_pid = str(supplier.get("ae_product_id") or "").strip()
    ae_sid = str(supplier.get("ae_sku_id") or "").strip()

    ue = supplier.get("unit_economics") or {}
    unit_cost = ue.get("product_cost")
    ship_cost = ue.get("shipping_cost")
    est_cost = None
    if unit_cost is not None:
        est_cost = round(float(unit_cost) * qty + float(ship_cost or 0), 2)

    pack = build_agent_order_pack(order_number, supplier, shipping, quantity=qty, order_type=order_type)
    pack["channel"] = channel

    ext_order_id = ""
    status = "AGENT_PACK_READY"
    api_message = "Tədarük sifariş paketi hazırdır."

    if channel == "aliexpress_ds" and ae_pid:
        ae_res = create_aliexpress_ds_order(
            product_id=ae_pid,
            sku_id=ae_sid,
            shipping={**shipping, "country_code": country_code},
            quantity=qty,
            logistic_line=supplier.get("logistic_line") or "CAINIAO_STANDARD",
        )
        if ae_res.get("success"):
            ext_order_id = f"AE:{ae_res.get('ae_order_id', '')}"
            status = "AE_ORDER_CREATED"
            api_message = ae_res.get("message") or "AliExpress DS sifarişi yaradıldı."
        else:
            status = "AE_API_ERROR"
            api_message = ae_res.get("error") or "AliExpress DS API sifariş xətası."
    elif channel == "cj_dropshipping" and (cj_pid or cj_vid):
        cj_res = create_cj_order(
            order_number=order_number,
            shipping_customer_name=shipping.get("customer_name", "").strip(),
            shipping_address=shipping.get("address", "").strip(),
            shipping_city=shipping.get("city", "").strip(),
            shipping_province=shipping.get("province", "").strip(),
            shipping_zip=shipping.get("zip_code", "").strip(),
            shipping_country_code=country_code,
            shipping_country=country_name,
            shipping_phone=shipping.get("phone", "").strip(),
            email=shipping.get("email", "").strip(),
            pid=cj_pid,
            vid=cj_vid,
            quantity=qty,
            logistic_name=supplier.get("logistic_line", ""),
            pay_from_balance=pay_from_balance,
            remark=f"{'Sample' if order_type == 'sample' else 'Customer'} order via E-Commerce Agent ({binding_sku})",
        )
        if cj_res.get("success"):
            ext_order_id = cj_res.get("order_id", "")
            cj_vid = cj_res.get("vid") or cj_vid
            status = cj_res.get("status", "CREATED_UNPAID_DRAFT")
            api_message = cj_res.get("message") or "CJ Dropshipping sifarişi yaradıldı."
            if cj_res.get("total_amount"):
                try:
                    est_cost = round(float(cj_res["total_amount"]), 2)
                except (TypeError, ValueError):
                    pass
        else:
            status = "CJ_API_ERROR"
            api_message = cj_res.get("error") or "CJ API sifariş xətası."
    else:
        dispatch = build_1688_yunexpress_3pl_dispatch(
            order_number=order_number,
            supplier=supplier,
            shipping={**shipping, "country_code": country_code},
            quantity=qty,
            binding_sku=binding_sku,
        )
        pack["cargo_dispatch"] = dispatch
        pack["copy_message"] = dispatch["copy_message"]
        status = dispatch["status"]
        api_message = dispatch["message"]

    created_at = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO orders (
                order_number, order_type, platform, supplier_name, product_name,
                product_sku, cj_pid, cj_vid, quantity, customer_name, country_code,
                shipping_address, city, province, zip_code, phone, email,
                logistic_line, estimated_cost_usd, cj_order_id, tracking_number,
                status, api_message, agent_pack_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_number,
                order_type,
                platform,
                supplier.get("supplier_name", ""),
                supplier.get("product_name") or supplier.get("supplier_name", ""),
                binding_sku,
                cj_pid or ae_pid,
                cj_vid or ae_sid,
                qty,
                shipping.get("customer_name", ""),
                country_code,
                shipping.get("address", ""),
                shipping.get("city", ""),
                shipping.get("province", ""),
                shipping.get("zip_code", ""),
                shipping.get("phone", ""),
                shipping.get("email", ""),
                supplier.get("logistic_line", "YunExpress Tracked"),
                est_cost,
                ext_order_id,
                "",
                status,
                api_message,
                json.dumps(pack, ensure_ascii=False),
                created_at,
            ),
        )
        conn.commit()

    return {
        "order_number": order_number,
        "order_type": order_type,
        "channel": channel,
        "binding_sku": binding_sku,
        "platform": platform,
        "cj_order_id": ext_order_id,
        "status": status,
        "api_message": api_message,
        "estimated_cost_usd": est_cost,
        "agent_pack": pack,
        "created_at": created_at,
    }


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    pack_raw = d.pop("agent_pack_json", None)
    if pack_raw:
        try:
            d["agent_pack"] = json.loads(pack_raw)
        except json.JSONDecodeError:
            d["agent_pack"] = None
    return d


def list_orders(limit: int = 20) -> List[Dict[str, Any]]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?",
            (max(1, min(100, int(limit))),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_order(order_ref: str) -> Optional[Dict[str, Any]]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_number = ? OR cj_order_id = ?",
            (order_ref, order_ref),
        ).fetchone()
    return _row_to_dict(row) if row else None


def sync_cj_order_status(order_number: str) -> Dict[str, Any]:
    """Queries live CJ order status and tracking number, updating SQLite."""
    order = get_order(order_number)
    if not order:
        return {"success": False, "error": "Sifariş tapılmadı."}

    cj_id = order.get("cj_order_id") or order.get("order_number")
    detail = get_cj_order_detail(cj_id)
    if not detail.get("success"):
        return detail

    new_status = detail.get("status") or order["status"]
    track_num = detail.get("tracking_number") or order.get("tracking_number") or ""
    tracking_events = []
    if track_num:
        t_info = get_cj_tracking_info(track_num)
        if t_info.get("success"):
            tracking_events = t_info.get("events", [])

    with _get_conn() as conn:
        conn.execute(
            "UPDATE orders SET status = ?, tracking_number = ? WHERE order_number = ?",
            (new_status, track_num, order["order_number"]),
        )
        conn.commit()

    return {
        "success": True,
        "order_number": order["order_number"],
        "cj_order_id": cj_id,
        "status": new_status,
        "tracking_number": track_num,
        "tracking_events": tracking_events,
    }


def sync_ae_order_status(order_number: str) -> Dict[str, Any]:
    """Queries live AliExpress DS order status and tracking number, updating SQLite."""
    order = get_order(order_number)
    if not order:
        return {"success": False, "error": "Sifariş tapılmadı."}

    raw_ref = str(order.get("cj_order_id") or "")
    ae_id = raw_ref.split("AE:", 1)[1] if raw_ref.startswith("AE:") else raw_ref
    if not ae_id:
        return {"success": False, "error": "AliExpress order ID mövcud deyil."}

    detail = sync_aliexpress_ds_order(ae_id)
    if not detail.get("success"):
        return detail

    new_status = detail.get("status") or order["status"]
    track_num = detail.get("tracking_number") or order.get("tracking_number") or ""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE orders SET status = ?, tracking_number = ? WHERE order_number = ?",
            (new_status, track_num, order["order_number"]),
        )
        conn.commit()

    return {
        "success": True,
        "order_number": order["order_number"],
        "ae_order_id": ae_id,
        "status": new_status,
        "tracking_number": track_num,
    }


def export_order_csv(order_number: str) -> Optional[str]:
    """Returns a Shopify / HyperSKU / SourcinBox compatible CSV string for an order."""
    order = get_order(order_number)
    if not order:
        return None
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "OrderNumber",
        "OrderType",
        "ProductName",
        "SKU",
        "Quantity",
        "CustomerName",
        "ShippingAddress",
        "City",
        "Province",
        "ZipCode",
        "CountryCode",
        "Phone",
        "Email",
        "LogisticLine",
        "SupplierLink",
    ])
    pack = order.get("agent_pack") or {}
    writer.writerow([
        order["order_number"],
        order["order_type"],
        order["product_name"],
        order["product_sku"],
        order["quantity"],
        order["customer_name"],
        order["shipping_address"],
        order["city"],
        order["province"],
        order["zip_code"],
        order["country_code"],
        order["phone"],
        order["email"],
        order["logistic_line"],
        pack.get("order_link", ""),
    ])
    return buf.getvalue()
