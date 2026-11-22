import json
import logging
import os
import re
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Baseline YunExpress Tracked 3PL air-line rates per destination (USD)
YUNEXPRESS_CORRIDORS: Dict[str, Dict[str, Any]] = {
    "US": {"base_usd": 3.40, "per_kg_usd": 9.20, "days": "6-9 days", "line": "YunExpress Standard US"},
    "GB": {"base_usd": 3.10, "per_kg_usd": 8.40, "days": "5-8 days", "line": "YunExpress UK Direct"},
    "DE": {"base_usd": 3.20, "per_kg_usd": 8.60, "days": "6-9 days", "line": "YunExpress EU IOSS"},
    "FR": {"base_usd": 3.25, "per_kg_usd": 8.70, "days": "6-9 days", "line": "YunExpress EU IOSS"},
    "CA": {"base_usd": 3.60, "per_kg_usd": 9.60, "days": "7-11 days", "line": "YunExpress Canada Line"},
    "AU": {"base_usd": 3.30, "per_kg_usd": 8.90, "days": "6-10 days", "line": "YunExpress AU Direct"},
    "AZ": {"base_usd": 3.50, "per_kg_usd": 7.50, "days": "7-12 days", "line": "YunExpress / Air Cargo AZ"},
}


def extract_1688_offer_id(url: str) -> str:
    """Extract numeric 1688 offer ID from URL when available."""
    if not url:
        return ""
    m = re.search(r"/offer/(\d+)\.html", url)
    if m:
        return m.group(1)
    m2 = re.search(r"(\d{9,15})", url)
    return m2.group(1) if m2 else ""


def is_1688_url(url: str) -> bool:
    return "1688.com" in (url or "").lower()


def calculate_1688_yunexpress_landed_cost(
    price_cny: Optional[float],
    fx_cny_per_usd: float = 7.15,
    weight_kg: float = 0.35,
    target_country: str = "US",
    agent_fee_pct: float = 5.0,
    cn_domestic_cny: float = 6.0,
    handling_fee_usd: float = 0.80,
) -> Dict[str, Any]:
    """
    Calculates true 1688 -> HyperSKU/SourcinBox 3PL -> YunExpress -> Customer landed economics:
      1) 1688 Factory Price (CNY -> USD)
      2) China Domestic Freight to Yiwu/Shenzhen 3PL warehouse (~¥6)
      3) 3PL QC + Packaging + Agent Commission (5% + $0.80)
      4) YunExpress Tracked International Freight to US/EU/Customer
    """
    corridor = YUNEXPRESS_CORRIDORS.get(target_country.upper(), YUNEXPRESS_CORRIDORS["US"])
    safe_fx = fx_cny_per_usd if fx_cny_per_usd and fx_cny_per_usd > 0 else 7.15
    safe_weight = max(0.10, float(weight_kg or 0.35))
    yunexpress_usd = round(corridor["base_usd"] + (safe_weight * corridor["per_kg_usd"]), 2)

    if price_cny is None or float(price_cny) <= 0:
        return {
            "channel": "1688_yunexpress",
            "logistic_line": f"{corridor['line']} (HyperSKU / SourcinBox)",
            "delivery_days": corridor["days"],
            "product_cost": None,
            "shipping_cost": yunexpress_usd,
            "weight_kg": safe_weight,
            "target_country": target_country.upper(),
            "cost_breakdown": {
                "factory_cny": None,
                "factory_usd": None,
                "cn_domestic_cny": cn_domestic_cny,
                "cn_domestic_usd": round(cn_domestic_cny / safe_fx, 2),
                "yunexpress_usd": yunexpress_usd,
                "agent_3pl_fee_usd": None,
                "total_landed_cost_usd": None,
            },
        }

    factory_cny = round(float(price_cny), 2)
    factory_usd = round(factory_cny / safe_fx, 2)
    cn_domestic_usd = round(float(cn_domestic_cny) / safe_fx, 2)
    agent_fee_usd = round((factory_usd * (float(agent_fee_pct) / 100.0)) + float(handling_fee_usd), 2)
    product_landed_at_3pl = round(factory_usd + cn_domestic_usd + agent_fee_usd, 2)
    total_landed = round(product_landed_at_3pl + yunexpress_usd, 2)

    return {
        "channel": "1688_yunexpress",
        "logistic_line": f"{corridor['line']} (HyperSKU / SourcinBox)",
        "delivery_days": corridor["days"],
        "product_cost": product_landed_at_3pl,
        "shipping_cost": yunexpress_usd,
        "weight_kg": safe_weight,
        "target_country": target_country.upper(),
        "cost_breakdown": {
            "factory_cny": factory_cny,
            "factory_usd": factory_usd,
            "cn_domestic_cny": cn_domestic_cny,
            "cn_domestic_usd": cn_domestic_usd,
            "agent_3pl_fee_usd": agent_fee_usd,
            "yunexpress_usd": yunexpress_usd,
            "total_landed_cost_usd": total_landed,
        },
    }


def build_1688_yunexpress_3pl_dispatch(
    order_number: str,
    supplier: Dict[str, Any],
    shipping: Dict[str, Any],
    quantity: int = 1,
    binding_sku: str = "",
) -> Dict[str, Any]:
    """
    Prepares a structured 1688 -> HyperSKU/SourcinBox 3PL -> YunExpress -> Customer
    fulfillment dispatch pack (bilingual EN + ZH) and dispatches via configured 3PL webhook if present.
    """
    offer_url = supplier.get("order_link_1688") or supplier.get("order_link") or ""
    offer_id = extract_1688_offer_id(offer_url)
    cn_kw = supplier.get("chinese_keywords") or ""
    product_name = supplier.get("product_name") or supplier.get("supplier_name") or "1688 Product"
    country_code = (shipping.get("country_code") or "US").upper()
    corridor = YUNEXPRESS_CORRIDORS.get(country_code, YUNEXPRESS_CORRIDORS["US"])

    bilingual_msg = (
        f"[1688 -> 3PL -> YUNEXPRESS DISPATCH | {order_number}]\n"
        f"Binding SKU: {binding_sku or supplier.get('product_sku', 'N/A')}\n"
        f"3PL Bridge: HyperSKU / SourcinBox (China Warehouse QC + Repack)\n"
        f"Shipping Line (物流渠道): {corridor['line']} -> {country_code}\n"
        f"Product (商品): {product_name}" + (f" ({cn_kw})" if cn_kw else "") + "\n"
        f"Quantity (数量): {quantity} pcs\n"
        f"1688 Source URL (采购链接): {offer_url}\n"
        + (f"1688 Offer ID: {offer_id}\n" if offer_id else "")
        + "--- RECIPIENT (收件人信息) ---\n"
        f"Name: {shipping.get('customer_name', '')}\n"
        f"Address: {shipping.get('address', '')}, {shipping.get('city', '')}, {shipping.get('province', '')} {shipping.get('zip_code', '')}\n"
        f"Country: {country_code} | Phone: {shipping.get('phone', '')} | Email: {shipping.get('email', '')}"
    )

    payload = {
        "order_number": order_number,
        "binding_sku": binding_sku,
        "channel": "1688_yunexpress",
        "bridge": "HyperSKU / SourcinBox 3PL",
        "logistic_line": corridor["line"],
        "estimated_days": corridor["days"],
        "source_1688_url": offer_url,
        "source_1688_offer_id": offer_id,
        "chinese_keywords": cn_kw,
        "quantity": quantity,
        "shipping": shipping,
        "copy_message": bilingual_msg,
    }

    webhook_url = (os.getenv("SOURCINBOX_WEBHOOK_URL") or "").strip()
    hypersku_key = (os.getenv("HYPERSKU_API_KEY") or "").strip()
    webhook_sent = False
    webhook_note = "3PL Webhook təyin edilməyib — HyperSKU / SourcinBox agent paketi hazırlandı."

    if webhook_url and webhook_url.startswith("http"):
        try:
            headers = {"Content-Type": "application/json", "User-Agent": "ApexCart-3PL-Bridge/1.0"}
            if hypersku_key:
                headers["Authorization"] = f"Bearer {hypersku_key}"
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    webhook_sent = True
                    webhook_note = f"3PL Webhook ({resp.status}) üzərindən HyperSKU / SourcinBox agentinə ötürüldü."
        except Exception as exc:
            webhook_note = f"3PL Webhook xətası (paket yaddaşda qaldı): {exc}"

    return {
        **payload,
        "webhook_sent": webhook_sent,
        "status": "DISPATCHED_TO_3PL_YUNEXPRESS" if webhook_sent else "YUNEXPRESS_3PL_PACK_READY",
        "message": webhook_note,
    }
