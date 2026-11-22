import base64
import hashlib
import hmac
import json
import logging
import os
import urllib.request
from typing import Any, Dict, List, Optional

from tools.binding_store import resolve_binding_by_sku
from tools.notification_tools import send_telegram_notification
from tools.order_automation import place_automated_order
from tools.supplier_monitor import get_active_watched_supplier

logger = logging.getLogger(__name__)


def verify_shopify_webhook_hmac(raw_body: bytes, hmac_header: Optional[str]) -> Dict[str, Any]:
    """
    Verifies X-Shopify-Hmac-Sha256 when SHOPIFY_WEBHOOK_SECRET is configured in .env.
    If SHOPIFY_WEBHOOK_SECRET is unset, runs in explicit unsigned test mode (never claims HMAC verified).
    """
    secret = (os.getenv("SHOPIFY_WEBHOOK_SECRET") or "").strip()
    if not secret or secret.startswith("your_"):
        return {"ok": True, "verified": False, "mode": "test_unsigned"}

    if not hmac_header:
        return {"ok": False, "verified": False, "mode": "hmac_missing", "error": "X-Shopify-Hmac-Sha256 header əskikdir."}

    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    if hmac.compare_digest(computed, hmac_header.strip()):
        return {"ok": True, "verified": True, "mode": "hmac_verified"}

    return {"ok": False, "verified": False, "mode": "hmac_invalid", "error": "Shopify webhook imzası etibarsızdır."}


def process_shopify_paid_order(
    payload: Dict[str, Any],
    fallback_supplier: Optional[Dict[str, Any]] = None,
    pay_from_balance: bool = False,
) -> Dict[str, Any]:
    """
    Phase 5 Shopify orders/paid auto-fulfillment handler:
    Matches each Shopify line item to a watched/active supplier and creates CJ API 2.0
    orders or 1688/Agent fulfillment packs automatically.
    """
    shopify_order_id = str(payload.get("id") or "")
    shopify_order_num = str(payload.get("order_number") or payload.get("name") or shopify_order_id or "SHP-ORDER")
    ship_raw = payload.get("shipping_address") or {}
    customer = payload.get("customer") or {}

    full_name = (
        ship_raw.get("name")
        or f"{ship_raw.get('first_name', '')} {ship_raw.get('last_name', '')}".strip()
        or f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        or "Shopify Customer"
    )
    shipping = {
        "customer_name": full_name,
        "address": " ".join(p for p in [ship_raw.get("address1", ""), ship_raw.get("address2", "")] if p).strip(),
        "city": ship_raw.get("city", ""),
        "province": ship_raw.get("province_code") or ship_raw.get("province", ""),
        "zip_code": ship_raw.get("zip", ""),
        "country_code": (ship_raw.get("country_code") or "US").upper(),
        "country": ship_raw.get("country", "United States"),
        "phone": ship_raw.get("phone") or payload.get("phone") or "",
        "email": payload.get("email") or customer.get("email") or "",
    }

    line_items = payload.get("line_items") or []
    if not line_items:
        line_items = [{"title": "Default Store Product", "sku": "", "quantity": 1}]

    created_orders: List[Dict[str, Any]] = []
    for item in line_items:
        sku = str(item.get("sku") or "").strip()
        title = str(item.get("title") or item.get("name") or "").strip()
        qty = max(1, int(item.get("quantity") or 1))

        bound = resolve_binding_by_sku(sku) if sku else None
        sup = (bound.get("primary_supplier") if bound else None) or get_active_watched_supplier(sku=sku, title=title) or fallback_supplier
        if not sup:
            created_orders.append({
                "sku": sku,
                "title": title,
                "status": "NO_SUPPLIER_MATCH",
                "error": "SQLite izləmə/binding siyahısında uyğun tədarükçü tapılmadı.",
            })
            continue

        order_res = place_automated_order(
            supplier=sup,
            shipping=shipping,
            quantity=qty,
            order_type="customer",
            pay_from_balance=pay_from_balance,
        )
        order_res["shopify_order_id"] = shopify_order_id
        order_res["shopify_order_number"] = shopify_order_num
        if bound:
            order_res["binding_sku"] = bound["binding_sku"]
        created_orders.append(order_res)

    summary_msg = (
        f"🛒 *Shopify Sifariş Webhook [#{shopify_order_num}]*\n"
        f"Alıcı: {full_name} ({shipping['country_code']})\n"
        f"Yaradılan tədarük sifarişləri: {len(created_orders)}"
    )
    try:
        send_telegram_notification(summary_msg)
    except Exception:
        pass

    return {
        "success": True,
        "shopify_order_id": shopify_order_id,
        "shopify_order_number": shopify_order_num,
        "orders": created_orders,
    }


def push_tracking_to_shopify(
    shopify_order_id: str,
    tracking_number: str,
    tracking_company: str = "CJ Dropshipping",
) -> Dict[str, Any]:
    """
    Pushes tracking number back to Shopify Admin API (/admin/api/2024-10/orders/{id}/fulfillments.json)
    when SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN are configured.
    """
    store_url = (os.getenv("SHOPIFY_STORE_URL") or "").strip().rstrip("/")
    token = (os.getenv("SHOPIFY_ACCESS_TOKEN") or "").strip()
    if not store_url or not token or "your_" in token:
        return {
            "success": False,
            "status": "skipped_not_configured",
            "message": "SHOPIFY_STORE_URL və SHOPIFY_ACCESS_TOKEN .env-də təyin edilməyib.",
        }
    if not shopify_order_id or not tracking_number:
        return {"success": False, "error": "shopify_order_id və tracking_number tələb olunur."}

    if not store_url.startswith("http"):
        store_url = f"https://{store_url}"

    try:
        url = f"{store_url}/admin/api/2024-10/orders/{shopify_order_id}/fulfillments.json"
        payload = json.dumps({
            "fulfillment": {
                "notify_customer": True,
                "tracking_info": {
                    "number": tracking_number,
                    "company": tracking_company,
                },
            }
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"success": True, "status": "fulfilled_on_shopify", "data": data}
    except Exception as e:
        logger.error(f"Shopify fulfillment push error: {e}")
        return {"success": False, "error": str(e)}


def calculate_bulk_vs_dropship(
    unit_cost_usd: float,
    single_shipping_usd: float,
    selling_price_usd: float,
    batch_qty: int = 100,
    estimated_cpa_usd: float = 15.0,
    freight_per_unit_usd: float = 1.65,
    fulfillment_3pl_usd: float = 3.40,
) -> Dict[str, Any]:
    """
    Phase 5 Scaling Calculator: Compares 1-by-1 Dropshipping vs Bulk 1688 MOQ + US/EU 3PL Fulfillment.
    """
    qty = max(20, min(5000, int(batch_qty or 100)))
    gw_fee = round(selling_price_usd * 0.029 + 0.30, 2)

    # Scenario A: Single-piece Dropshipping
    ds_landed = round(unit_cost_usd + single_shipping_usd, 2)
    ds_profit = round(selling_price_usd - ds_landed - estimated_cpa_usd - gw_fee, 2)

    # Scenario B: Bulk 1688 MOQ factory tier discount (18% at 50+, 25% at 100+, 32% at 250+) + US/EU 3PL pick/pack
    discount_pct = 0.32 if qty >= 250 else (0.25 if qty >= 100 else 0.18)
    bulk_unit_cost = round(unit_cost_usd * (1.0 - discount_pct), 2)
    bulk_landed = round(bulk_unit_cost + freight_per_unit_usd + fulfillment_3pl_usd, 2)
    bulk_profit = round(selling_price_usd - bulk_landed - estimated_cpa_usd - gw_fee, 2)

    per_unit_savings = round(ds_landed - bulk_landed, 2)
    batch_capital_usd = round((bulk_unit_cost + freight_per_unit_usd) * qty, 2)
    batch_extra_profit_usd = round((bulk_profit - ds_profit) * qty, 2)

    return {
        "batch_qty": qty,
        "selling_price_usd": round(selling_price_usd, 2),
        "estimated_cpa_usd": round(estimated_cpa_usd, 2),
        "gateway_fee_usd": gw_fee,
        "dropship_single": {
            "unit_cost_usd": round(unit_cost_usd, 2),
            "shipping_usd": round(single_shipping_usd, 2),
            "landed_cost_usd": ds_landed,
            "net_profit_usd": ds_profit,
            "delivery_days": "7-12 days (CN -> US/EU)",
        },
        "bulk_3pl": {
            "factory_discount_pct": int(discount_pct * 100),
            "bulk_unit_cost_usd": bulk_unit_cost,
            "sea_air_freight_per_unit_usd": round(freight_per_unit_usd, 2),
            "local_3pl_pick_pack_usd": round(fulfillment_3pl_usd, 2),
            "landed_cost_usd": bulk_landed,
            "net_profit_usd": bulk_profit,
            "upfront_inventory_capital_usd": batch_capital_usd,
            "delivery_days": "2-4 days (US/EU Local 3PL)",
        },
        "per_unit_savings_usd": per_unit_savings,
        "batch_extra_profit_usd": batch_extra_profit_usd,
    }
