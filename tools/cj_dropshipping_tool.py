import os
import json
import time
import logging
import urllib.request
import urllib.parse
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

CJ_API_BASE = "https://developers.cjdropshipping.com/api2.0/v1"
_CJ_TOKEN_CACHE: Dict[str, Any] = {
    "token": None,
    "expires_at": 0,
}

def get_cj_access_token(api_key: Optional[str] = None) -> Optional[str]:
    """
    Obtains or returns the cached CJ Dropshipping Access Token.
    Tokens are valid for up to 180 days and cached locally to prevent API rate limiting.
    """
    global _CJ_TOKEN_CACHE
    
    # 1. Check in-memory cache
    now = time.time()
    if _CJ_TOKEN_CACHE["token"] and _CJ_TOKEN_CACHE["expires_at"] > now + 3600:
        return _CJ_TOKEN_CACHE["token"]

    key = api_key or os.getenv("CJ_API_KEY")
    if not key or "your_cj_api_key" in key:
        logger.warning("CJ_API_KEY is not configured in .env")
        return None

    try:
        logger.info("Requesting new CJ Dropshipping Access Token...")
        url = f"{CJ_API_BASE}/authentication/getAccessToken"
        payload = json.dumps({"apiKey": key.strip()}).encode("utf-8")
        
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ECommerce-Agent/1.0"
            }
        )
        
        with urllib.request.urlopen(req, timeout=12) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            
            if res_data.get("code") == 200 and res_data.get("data"):
                token = res_data["data"].get("accessToken")
                # Tokens are valid for 180 days; cache for 7 days locally for safety
                _CJ_TOKEN_CACHE["token"] = token
                _CJ_TOKEN_CACHE["expires_at"] = now + (7 * 24 * 3600)
                logger.info("Successfully acquired CJ Dropshipping Access Token.")
                return token
            else:
                logger.error(f"Failed to get CJ access token: {res_data.get('message')}")
                return None
    except Exception as e:
        logger.error(f"Error authenticating with CJ Dropshipping API: {e}")
        return None


def _parse_cj_price(raw_price: Any) -> float:
    raw_str = str(raw_price or "0")
    try:
        if "--" in raw_str:
            parts = raw_str.split("--")
            return round((float(parts[0].strip()) + float(parts[1].strip())) / 2, 2)
        return round(float(raw_str.strip()), 2)
    except Exception:
        return 0.0


def get_cj_product_detail(pid: str) -> Optional[Dict[str, Any]]:
    """Fetches detailed product specs, weight, variants, and shipping calculation from CJ API."""
    token = get_cj_access_token()
    if not token or not pid:
        return None
    try:
        params = urllib.parse.urlencode({"pid": pid})
        url = f"{CJ_API_BASE}/product/query?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "CJ-Access-Token": token,
                "User-Agent": "ECommerce-Agent/1.0",
                "Accept": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if res.get("code") == 200 and res.get("data"):
                return res["data"]
    except Exception as e:
        logger.debug(f"CJ product/query note for {pid}: {e}")
    return None


def calculate_cj_freight(vid: str, country_code: str = "US", quantity: int = 1) -> Optional[Dict[str, Any]]:
    """Calculates real CJ packet / CJPacket Ordinary / YunExpress shipping cost & transit time to US/EU."""
    token = get_cj_access_token()
    if not token or not vid:
        return None
    try:
        url = f"{CJ_API_BASE}/logistic/freightCalculate"
        payload = json.dumps({
            "startCountryCode": "CN",
            "endCountryCode": country_code.upper(),
            "products": [{"quantity": quantity, "vid": vid}]
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "CJ-Access-Token": token,
                "Content-Type": "application/json",
                "User-Agent": "ECommerce-Agent/1.0",
            }
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if res.get("code") == 200 and isinstance(res.get("data"), list) and res["data"]:
                # Pick fastest cost-effective line (e.g. CJPacket / YunExpress)
                lines = sorted(res["data"], key=lambda x: float(x.get("logisticPrice", 999)))
                best = lines[0]
                return {
                    "logistic_name": best.get("logisticName", "CJPacket"),
                    "shipping_usd": round(float(best.get("logisticPrice", 5.50)), 2),
                    "delivery_days": best.get("logisticAging", "7-12 days"),
                    "country": country_code.upper(),
                }
    except Exception as e:
        logger.debug(f"CJ freight note: {e}")
    return None


def _build_cj_query_candidates(keyword: str, fallback_query: str = "") -> List[str]:
    """
    Builds ordered 2-3 word core-noun search queries for CJ Dropshipping product/listV2.
    Preserves product descriptors ('portable', 'ultrasonic', 'mini', 'smart', 'wireless', 'magnetic')
    and prioritizes the head nouns at the end of English product names.
    """
    _FLUFF_WORDS = {
        "with", "and", "the", "for", "pro", "max", "ultra", "new", "best",
        "premium", "original", "official", "in", "of", "a", "an", "set",
        "viral", "tiktok", "2024", "2025", "2026", "luxury", "edition",
        "display", "power", "upgraded", "automatic",
    }
    queries: List[str] = []

    for raw in [keyword, fallback_query]:
        if not raw or not raw.strip():
            continue
        words = [
            w for w in raw.replace("-", " ").replace("/", " ").split()
            if w.lower() not in _FLUFF_WORDS and len(w) > 1 and any(c.isalpha() for c in w)
        ]
        if not words:
            continue
        if len(words) <= 3:
            q_main = " ".join(words)
            if q_main.lower() not in {q.lower() for q in queries}:
                queries.append(q_main)
        else:
            q_last3 = " ".join(words[-3:])
            if q_last3.lower() not in {q.lower() for q in queries}:
                queries.append(q_last3)
        if len(words) >= 2:
            q_last2 = " ".join(words[-2:])
            if q_last2.lower() not in {q.lower() for q in queries}:
                queries.append(q_last2)

    if not queries and keyword.strip():
        queries.append(keyword.strip())
    return queries


def search_cj_products_live(
    keyword: str,
    page: int = 1,
    size: int = 15,
    target_country: str = "US",
    fallback_query: str = "",
) -> List[Dict[str, Any]]:
    """
    Searches CJ Dropshipping for real products matching the keyword using product/listV2.
    Uses core product noun queries (e.g. 'wireless sports earbuds', 'mini espresso maker')
    so leading adjectives never truncate the actual product noun.
    """
    token = get_cj_access_token()
    if not token:
        logger.warning("No CJ Access Token available, skipping live search.")
        return []

    query_candidates = _build_cj_query_candidates(keyword, fallback_query=fallback_query)
    seen_pids: set = set()
    collected_products: List[Dict[str, Any]] = []
    import re

    for clean_keyword in query_candidates[:2]:
        try:
            logger.info(f"Searching CJ Dropshipping catalog for keyword: '{clean_keyword}'...")
            params = urllib.parse.urlencode({
                "keyWord": clean_keyword,
                "page": page,
                "size": size,
            })
            url = f"{CJ_API_BASE}/product/listV2?{params}"

            req = urllib.request.Request(
                url,
                headers={
                    "CJ-Access-Token": token,
                    "User-Agent": "ECommerce-Agent/1.0",
                    "Accept": "application/json",
                },
            )

            with urllib.request.urlopen(req, timeout=15) as response:
                res_data = json.loads(response.read().decode("utf-8"))

                if res_data.get("code") == 200 and res_data.get("data"):
                    content = res_data["data"].get("content", [])
                    if isinstance(content, list) and len(content) > 0:
                        raw_list = content[0].get("productList", []) if isinstance(content[0], dict) else []

                        for item in raw_list[:size]:
                            pid = str(item.get("id") or "").strip()
                            if not pid or pid in seen_pids:
                                continue
                            price = _parse_cj_price(item.get("sellPrice", "0"))
                            if price <= 0:
                                continue

                            seen_pids.add(pid)
                            name_en = item.get("nameEn", clean_keyword)
                            slug = re.sub(r"[^a-zA-Z0-9]+", "-", name_en.lower()).strip("-")

                            collected_products.append({
                                "id": pid,
                                "vid": "",
                                "name": name_en,
                                "sku": item.get("sku"),
                                "product_cost_usd": price,
                                "estimated_shipping_usd": None,
                                "delivery_days": "7-12 days",
                                "logistic_line": "CJPacket / YunExpress",
                                "weight_grams": item.get("productWeight"),
                                "image_url": item.get("bigImage"),
                                "listed_count": item.get("listedNum", 0),
                                "platform": "CJ Dropshipping",
                                "fulfillment_type": "Direct Auto-Dropship (API 2.0)",
                                "order_link": f"https://cjdropshipping.com/product/{slug}-p-{pid}.html",
                            })
        except Exception as e:
            logger.error(f"Error querying CJ Dropshipping API for '{clean_keyword}': {e}")

    logger.info(f"Found {len(collected_products)} raw candidate products on CJ Dropshipping for queries {query_candidates[:2]}.")
    return collected_products


def enrich_cj_top_candidates(
    candidates: List[Dict[str, Any]],
    target_country: str = "US",
    top_n: int = 3,
) -> List[Dict[str, Any]]:
    """
    Enriches the Top-N relevance-verified CJ Dropshipping supplier candidates with
    real variant ID (vid), product weight, and live CJ freight quotes (calculate_cj_freight).
    Running this AFTER relevance scoring avoids wasting API calls on unrelated items.
    """
    enriched_count = 0
    for sup in candidates:
        if enriched_count >= top_n:
            break
        if sup.get("platform") != "CJ Dropshipping" or not sup.get("is_live_verified"):
            continue
        pid = str(sup.get("cj_pid") or "").strip()
        if not pid:
            continue
        enriched_count += 1
        try:
            detail = get_cj_product_detail(pid)
            if detail:
                weight_g = detail.get("productWeight") or sup.get("weight_grams")
                if weight_g:
                    sup["weight_grams"] = weight_g
                variants = detail.get("variants") or []
                if variants and variants[0].get("vid"):
                    vid = str(variants[0]["vid"]).strip()
                    sup["cj_vid"] = vid
                    freight = calculate_cj_freight(vid, country_code=target_country)
                    if freight and freight.get("shipping_usd") is not None:
                        sup["estimated_shipping_usd"] = freight["shipping_usd"]
                        sup["delivery_days"] = freight.get("delivery_days") or sup.get("delivery_days")
                        sup["logistic_line"] = freight.get("logistic_name") or sup.get("logistic_line")
        except Exception as e:
            logger.debug(f"Post-ranking CJ enrichment note for pid={pid}: {e}")
    return candidates


def _resolve_cj_variant_and_logistic(
    pid: str = "",
    vid: str = "",
    country_code: str = "US",
    quantity: int = 1,
    preferred_logistic: str = "",
) -> Dict[str, Any]:
    """Resolves valid CJ variant ID (vid) and shipping line before order creation."""
    resolved_vid = (vid or "").strip()
    if not resolved_vid and pid:
        detail = get_cj_product_detail(pid)
        if detail and isinstance(detail.get("variants"), list) and detail["variants"]:
            resolved_vid = str(detail["variants"][0].get("vid") or "").strip()

    resolved_logistic = (preferred_logistic or "").strip()
    shipping_usd = None
    if resolved_vid and (not resolved_logistic or "/" in resolved_logistic):
        freight = calculate_cj_freight(resolved_vid, country_code=country_code, quantity=quantity)
        if freight:
            resolved_logistic = freight.get("logistic_name", "CJPacket Ordinary")
            shipping_usd = freight.get("shipping_usd")

    return {
        "vid": resolved_vid,
        "logistic_name": resolved_logistic or "CJPacket Ordinary",
        "shipping_usd": shipping_usd,
    }


def create_cj_order(
    order_number: str,
    shipping_customer_name: str,
    shipping_address: str,
    shipping_city: str,
    shipping_province: str,
    shipping_zip: str,
    shipping_country_code: str = "US",
    shipping_country: str = "United States",
    shipping_phone: str = "",
    email: str = "",
    pid: str = "",
    vid: str = "",
    quantity: int = 1,
    logistic_name: str = "",
    from_country_code: str = "CN",
    pay_from_balance: bool = False,
    remark: str = "",
) -> Dict[str, Any]:
    """
    Creates an automated fulfillment or sample order via CJ Dropshipping API 2.0
    (/shopping/order/createOrderV2).
    Defaults to payType='3' (creates unpaid order draft safely without deducting balance).
    """
    token = get_cj_access_token()
    if not token:
        return {
            "success": False,
            "error": "CJ_API_KEY mövcud deyil və ya access token alına bilmədi.",
        }

    resolved = _resolve_cj_variant_and_logistic(
        pid=pid,
        vid=vid,
        country_code=shipping_country_code,
        quantity=quantity,
        preferred_logistic=logistic_name,
    )
    final_vid = resolved["vid"]
    if not final_vid:
        return {
            "success": False,
            "error": "Məhsulun CJ Variant ID (vid) dəyəri tapılmadı (cj_pid və ya cj_vid tələb olunur).",
        }

    pay_type = "2" if pay_from_balance else "3"
    payload_dict = {
        "orderNumber": order_number,
        "shippingZip": shipping_zip,
        "shippingCountryCode": shipping_country_code.upper(),
        "shippingCountry": shipping_country,
        "shippingProvince": shipping_province,
        "shippingCity": shipping_city,
        "shippingCounty": "",
        "shippingPhone": shipping_phone,
        "shippingCustomerName": shipping_customer_name,
        "shippingAddress": shipping_address,
        "shippingAddress2": "",
        "taxId": "",
        "remark": remark or f"Automated Order ({order_number})",
        "email": email,
        "consigneeID": "",
        "fromCountryCode": from_country_code.upper(),
        "logisticName": resolved["logistic_name"],
        "payType": pay_type,
        "shopAmount": "",
        "products": [
            {
                "vid": final_vid,
                "quantity": max(1, int(quantity)),
            }
        ],
    }

    try:
        url = f"{CJ_API_BASE}/shopping/order/createOrderV2"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload_dict).encode("utf-8"),
            headers={
                "CJ-Access-Token": token,
                "Content-Type": "application/json",
                "User-Agent": "ECommerce-Agent/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            res = json.loads(resp.read().decode("utf-8"))

        if res.get("code") == 200 and res.get("result") is True:
            data = res.get("data") or {}
            cj_order_id = data.get("orderId") or data.get("cjOrderId") or order_number
            return {
                "success": True,
                "order_id": str(cj_order_id),
                "order_number": order_number,
                "vid": final_vid,
                "logistic_name": resolved["logistic_name"],
                "pay_type": pay_type,
                "status": "PAID_AND_CONFIRMED" if pay_type == "2" else "CREATED_UNPAID_DRAFT",
                "total_amount": data.get("orderAmount") or data.get("actualPayment"),
                "shipping_usd": resolved["shipping_usd"],
                "message": res.get("message", "CJ sifarişi uğurla yaradıldı."),
                "raw": data,
            }

        return {
            "success": False,
            "order_number": order_number,
            "vid": final_vid,
            "logistic_name": resolved["logistic_name"],
            "error": res.get("message") or f"CJ API error (code={res.get('code')})",
            "raw": res,
        }
    except Exception as e:
        logger.error(f"CJ createOrderV2 error: {e}")
        return {
            "success": False,
            "order_number": order_number,
            "vid": final_vid,
            "error": str(e),
        }


def get_cj_order_detail(order_id: str) -> Dict[str, Any]:
    """Queries CJ Dropshipping API 2.0 (/shopping/order/getOrderDetail) for order status & tracking."""
    token = get_cj_access_token()
    if not token or not order_id:
        return {"success": False, "error": "Token və ya order_id əskikdir."}
    try:
        params = urllib.parse.urlencode({"orderId": order_id})
        url = f"{CJ_API_BASE}/shopping/order/getOrderDetail?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "CJ-Access-Token": token,
                "User-Agent": "ECommerce-Agent/1.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        if res.get("code") == 200 and res.get("data"):
            d = res["data"]
            return {
                "success": True,
                "order_id": d.get("orderId", order_id),
                "order_num": d.get("orderNum"),
                "status": d.get("orderStatus", "UNKNOWN"),
                "tracking_number": d.get("trackNumber") or "",
                "logistic_name": d.get("logisticName") or "",
                "order_amount": d.get("orderAmount"),
                "raw": d,
            }
        return {"success": False, "error": res.get("message", "CJ order detail alınmadı.")}
    except Exception as e:
        logger.error(f"CJ getOrderDetail error: {e}")
        return {"success": False, "error": str(e)}


def get_cj_tracking_info(track_number: str) -> Dict[str, Any]:
    """Queries CJ Dropshipping API 2.0 (/logistic/trackInfo) for shipment tracking events."""
    token = get_cj_access_token()
    if not token or not track_number:
        return {"success": False, "error": "Token və ya track_number əskikdir."}
    try:
        params = urllib.parse.urlencode({"trackNumber": track_number})
        url = f"{CJ_API_BASE}/logistic/trackInfo?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "CJ-Access-Token": token,
                "User-Agent": "ECommerce-Agent/1.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        if res.get("code") == 200:
            return {"success": True, "tracking_number": track_number, "events": res.get("data") or []}
        return {"success": False, "error": res.get("message", "Tracking tapılmadı.")}
    except Exception as e:
        logger.error(f"CJ trackInfo error: {e}")
        return {"success": False, "error": str(e)}


def get_cj_stock_by_vid(vid: str) -> Dict[str, Any]:
    """Queries CJ Dropshipping API 2.0 (/product/stock/queryByVid) for live warehouse inventory."""
    token = get_cj_access_token()
    if not token or not vid:
        return {"success": False, "error": "Token və ya vid əskikdir."}
    try:
        params = urllib.parse.urlencode({"vid": vid})
        url = f"{CJ_API_BASE}/product/stock/queryByVid?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "CJ-Access-Token": token,
                "User-Agent": "ECommerce-Agent/1.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            res = json.loads(resp.read().decode("utf-8"))
        if res.get("code") == 200:
            rows = res.get("data") if isinstance(res.get("data"), list) else []
            total_qty = sum(int(r.get("storageNum", 0) or 0) for r in rows if isinstance(r, dict))
            return {"success": True, "vid": vid, "total_stock": total_qty, "warehouses": rows}
        return {"success": False, "error": res.get("message", "CJ stock sorğusu uğursuz oldu.")}
    except Exception as e:
        logger.debug(f"CJ stock queryByVid note: {e}")
        return {"success": False, "error": str(e)}


def check_cj_product_live_status(pid: str = "", vid: str = "") -> Dict[str, Any]:
    """
    Checks live wholesale price and stock availability on CJ Dropshipping API 2.0
    for Phase 4 price-spike (>10%) and out-of-stock monitoring.
    """
    detail = get_cj_product_detail(pid) if pid else None
    if pid and not detail:
        return {
            "success": True,
            "available": False,
            "price_usd": None,
            "stock": 0,
            "reason": "Məhsul CJ kataloqunda tapılmadı (silinib və ya deaktivdir).",
        }

    resolved_vid = (vid or "").strip()
    live_price = None
    if detail:
        live_price = _parse_cj_price(detail.get("sellPrice"))
        variants = detail.get("variants") or []
        if variants:
            matched = next((v for v in variants if str(v.get("vid", "")) == resolved_vid), variants[0])
            if not resolved_vid:
                resolved_vid = str(matched.get("vid") or "").strip()
            v_price = _parse_cj_price(matched.get("variantSellPrice"))
            if v_price > 0:
                live_price = v_price

    stock_qty = None
    if resolved_vid:
        stock_res = get_cj_stock_by_vid(resolved_vid)
        if stock_res.get("success"):
            stock_qty = stock_res.get("total_stock", 0)

    available = True
    if stock_qty is not None and stock_qty <= 0:
        available = False

    return {
        "success": True,
        "available": available,
        "pid": pid,
        "vid": resolved_vid,
        "price_usd": live_price if live_price and live_price > 0 else None,
        "stock": stock_qty,
    }


