import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OAUTH_TOKEN_FILE = PROJECT_ROOT / "data" / "aliexpress_oauth.json"
AE_SYNC_URL = "https://api-sg.aliexpress.com/sync"
AE_REST_BASE = "https://api-sg.aliexpress.com/rest"
AE_OAUTH_AUTHORIZE_URL = "https://api-sg.aliexpress.com/oauth/authorize"


def _get_env_val(key: str) -> str:
    """Read credential dynamically from process env or .env file."""
    val = (os.getenv(key) or "").strip()
    if val and not val.startswith("your_"):
        return val
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        try:
            vals = dotenv_values(env_path)
            raw = str(vals.get(key) or "").strip()
            if raw and not raw.startswith("your_"):
                os.environ[key] = raw
                return raw
        except Exception:
            pass
    return ""


def _load_oauth_token_file() -> Dict[str, Any]:
    if not OAUTH_TOKEN_FILE.exists():
        return {}
    try:
        data = json.loads(OAUTH_TOKEN_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_oauth_token_file(data: Dict[str, Any]) -> None:
    OAUTH_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    OAUTH_TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_aliexpress_access_token() -> str:
    """Return active AliExpress OAuth access_token from env or saved OAuth file."""
    token = _get_env_val("ALIEXPRESS_ACCESS_TOKEN")
    if token:
        return token
    saved = _load_oauth_token_file()
    token = str(saved.get("access_token") or "").strip()
    if token:
        # Auto-refresh if close to expiry and refresh_token is available
        expires_at = float(saved.get("expires_at") or 0)
        if expires_at and time.time() > (expires_at - 300) and saved.get("refresh_token"):
            refreshed = refresh_aliexpress_token(saved["refresh_token"])
            if refreshed.get("access_token"):
                return refreshed["access_token"]
        return token
    return ""


def get_aliexpress_config_status() -> Dict[str, Any]:
    """Return non-secret configuration and OAuth connection status for AliExpress DS API."""
    app_key = _get_env_val("ALIEXPRESS_APP_KEY")
    app_secret = _get_env_val("ALIEXPRESS_APP_SECRET")
    access_token = get_aliexpress_access_token()
    saved = _load_oauth_token_file()
    return {
        "app_key_configured": bool(app_key),
        "app_secret_configured": bool(app_secret),
        "access_token_configured": bool(access_token),
        "ready": bool(app_key and app_secret and access_token),
        "oauth_ready": bool(app_key and app_secret),
        "expires_at": saved.get("expires_at"),
        "account": saved.get("account") or saved.get("user_nick") or "",
    }


def _sign_aliexpress_params(params: Dict[str, Any], app_secret: str, api_path: str = "") -> str:
    """
    Generate uppercase HMAC-SHA256 signature per AliExpress Open Platform (IOP/TOP) spec.
    - For /sync calls: api_path="" -> concat sorted key+value
    - For /rest system endpoints (/auth/token/...): api_path="/auth/token/create" + sorted key+value
    """
    sorted_items = sorted((str(k), str(v)) for k, v in params.items() if k != "sign" and v is not None)
    base_str = (api_path or "") + "".join(f"{k}{v}" for k, v in sorted_items)
    digest = hmac.new(app_secret.encode("utf-8"), base_str.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest.upper()


def _call_aliexpress_iop(
    method: Optional[str],
    biz_params: Dict[str, Any],
    api_path: str = "",
    require_token: bool = True,
) -> Dict[str, Any]:
    """Execute signed request against AliExpress Open Platform IOP /sync or /rest endpoint."""
    app_key = _get_env_val("ALIEXPRESS_APP_KEY")
    app_secret = _get_env_val("ALIEXPRESS_APP_SECRET")
    if not app_key or not app_secret:
        return {"ok": False, "configured": False, "error": "ALIEXPRESS_APP_KEY və ALIEXPRESS_APP_SECRET təyin edilməyib."}

    params: Dict[str, Any] = {
        "app_key": app_key,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
    }
    if method:
        params["method"] = method
    if require_token:
        token = get_aliexpress_access_token()
        if not token:
            return {"ok": False, "configured": False, "error": "AliExpress OAuth Access Token mövcud deyil."}
        params["session"] = token

    for k, v in biz_params.items():
        if v is None:
            continue
        params[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)

    params["sign"] = _sign_aliexpress_params(params, app_secret, api_path=api_path)
    target_url = f"{AE_REST_BASE}{api_path}" if api_path else AE_SYNC_URL
    encoded = urllib.parse.urlencode(params).encode("utf-8")

    try:
        req = urllib.request.Request(
            target_url,
            data=encoded,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
                "User-Agent": "ApexCart-AliExpress-DS/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(raw)
        if isinstance(data, dict) and data.get("error_response"):
            err = data["error_response"]
            return {
                "ok": False,
                "configured": True,
                "code": err.get("code"),
                "error": err.get("msg") or err.get("sub_msg") or "AliExpress API error",
                "raw": data,
            }
        if isinstance(data, dict) and data.get("code") not in (None, "0", 0, 200):
            return {
                "ok": False,
                "configured": True,
                "code": data.get("code"),
                "error": data.get("message") or data.get("msg") or "AliExpress IOP error",
                "raw": data,
            }
        return {"ok": True, "configured": True, "data": data}
    except urllib.error.HTTPError as he:
        body = he.read().decode("utf-8", errors="ignore")[:300]
        return {"ok": False, "configured": True, "error": f"HTTP {he.code}: {body}"}
    except Exception as exc:
        return {"ok": False, "configured": True, "error": str(exc)}


def get_aliexpress_oauth_url(redirect_uri: str, state: str = "ae_ds_oauth") -> Dict[str, Any]:
    """Build official AliExpress OAuth authorization URL for Dropshipping App."""
    app_key = _get_env_val("ALIEXPRESS_APP_KEY")
    if not app_key:
        return {"ok": False, "error": "Əvvəlcə ALIEXPRESS_APP_KEY daxil edilməlidir."}
    qs = urllib.parse.urlencode({
        "response_type": "code",
        "force_auth": "true",
        "redirect_uri": redirect_uri,
        "client_id": app_key,
        "state": state,
    })
    return {"ok": True, "auth_url": f"{AE_OAUTH_AUTHORIZE_URL}?{qs}"}


def _persist_oauth_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    token = str(data.get("access_token") or "").strip()
    if not token:
        return {"ok": False, "error": data.get("error_msg") or data.get("message") or "access_token alınmadı."}
    expires_in = int(data.get("expires_in") or 86400 * 30)
    payload = {
        "access_token": token,
        "refresh_token": str(data.get("refresh_token") or "").strip(),
        "expires_in": expires_in,
        "expires_at": int(time.time()) + expires_in,
        "user_nick": data.get("user_nick") or "",
        "account": data.get("account") or "",
        "updated_at": int(time.time()),
    }
    _save_oauth_token_file(payload)
    os.environ["ALIEXPRESS_ACCESS_TOKEN"] = token
    return {"ok": True, **payload}


def exchange_aliexpress_code(code: str, uuid_val: str = "") -> Dict[str, Any]:
    """Exchange OAuth authorization code for AliExpress access_token & refresh_token."""
    if not (code or "").strip():
        return {"ok": False, "error": "OAuth authorization code boşdur."}
    res = _call_aliexpress_iop(
        method=None,
        biz_params={"code": code.strip(), **({"uuid": uuid_val} if uuid_val else {})},
        api_path="/auth/token/create",
        require_token=False,
    )
    if not res.get("ok"):
        return res
    return _persist_oauth_payload(res.get("data") or {})


def refresh_aliexpress_token(refresh_token: Optional[str] = None) -> Dict[str, Any]:
    """Refresh expired AliExpress OAuth token using refresh_token."""
    rt = (refresh_token or _load_oauth_token_file().get("refresh_token") or "").strip()
    if not rt:
        return {"ok": False, "error": "refresh_token mövcud deyil."}
    res = _call_aliexpress_iop(
        method=None,
        biz_params={"refresh_token": rt},
        api_path="/auth/token/security/refresh",
        require_token=False,
    )
    if not res.get("ok"):
        return res
    return _persist_oauth_payload(res.get("data") or {})


def parse_aliexpress_url(url: str) -> Optional[str]:
    """Extract numeric AliExpress product_id from item URL."""
    if not url:
        return None
    m = re.search(r"/item/(\d+)\.html", url)
    if m:
        return m.group(1)
    m2 = re.search(r"[?&]productId=(\d+)", url)
    return m2.group(1) if m2 else None


def _parse_money(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        clean = re.sub(r"[^0-9.]", "", str(val))
        return round(float(clean), 2) if clean else None
    except (TypeError, ValueError):
        return None


def _unwrap_iop_list(raw: Any, nested_key: str) -> List[Any]:
    """Unwrap AliExpress IOP responses that return either a list or nested {nested_key: [...]}."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        inner = raw.get(nested_key)
        if isinstance(inner, list):
            return inner
        if inner is not None:
            return [inner]
    return []


def calculate_aliexpress_freight(
    product_id: str,
    sku_id: str = "",
    country_code: str = "US",
    quantity: int = 1,
) -> Optional[Dict[str, Any]]:
    """Query AliExpress DS freight endpoint for real shipping cost and delivery days."""
    if not product_id:
        return None
    query_req = {
        "quantity": quantity,
        "shipToCountry": country_code.upper(),
        "productId": int(product_id) if str(product_id).isdigit() else product_id,
        "locale": "en_US",
        "currency": "USD",
    }
    if sku_id:
        query_req["selectedSkuId"] = str(sku_id)

    res = _call_aliexpress_iop(
        method="aliexpress.ds.freight.query",
        biz_params={"queryDeliveryReq": query_req},
    )
    if not res.get("ok"):
        return None
    data = res.get("data") or {}
    resp_obj = data.get("aliexpress_ds_freight_query_response") or data
    result = resp_obj.get("result") or resp_obj
    options = _unwrap_iop_list(result.get("delivery_options"), "delivery_option_d_t_o")
    if not options:
        return None
    valid = sorted(options, key=lambda o: _parse_money(o.get("shipping_fee_cent") or o.get("freight")) or 999.0)
    best = valid[0]
    fee_usd = _parse_money(best.get("freight") or (best.get("shipping_fee_format") if isinstance(best.get("shipping_fee_format"), str) else None))
    if fee_usd is None and best.get("shipping_fee_cent") is not None:
        try:
            fee_usd = round(float(best["shipping_fee_cent"]) / 100.0, 2)
        except Exception:
            fee_usd = None
    return {
        "logistic_line": best.get("company") or best.get("code") or "AliExpress Selection Standard",
        "shipping_usd": fee_usd if fee_usd is not None else 0.0,
        "delivery_days": best.get("delivery_date_desc") or f"{best.get('min_delivery_days', 7)}-{best.get('max_delivery_days', 14)} days",
    }


def get_aliexpress_ds_product_detail(
    product_id: str,
    target_country: str = "US",
    ship_to_currency: str = "USD",
) -> Optional[Dict[str, Any]]:
    """Fetch full Dropshipping SKU, price, stock and shipping info for an AliExpress product_id."""
    if not product_id:
        return None
    res = _call_aliexpress_iop(
        method="aliexpress.ds.product.get",
        biz_params={
            "product_id": str(product_id),
            "ship_to_country": target_country.upper(),
            "target_currency": ship_to_currency,
            "target_language": "en",
        },
    )
    if not res.get("ok"):
        return None
    data = res.get("data") or {}
    root = data.get("aliexpress_ds_product_get_response") or data
    result = root.get("result") or root
    if not isinstance(result, dict):
        return None

    base_info = result.get("ae_item_base_info_dto") or {}
    sku_list = _unwrap_iop_list(result.get("ae_item_sku_info_dtos"), "ae_item_sku_info_d_t_o")

    best_sku = sku_list[0] if sku_list else {}
    sku_id = str(best_sku.get("sku_id") or best_sku.get("id") or "")
    price_usd = _parse_money(best_sku.get("offer_sale_price") or best_sku.get("sku_price"))
    stock = best_sku.get("sku_available_stock")
    try:
        stock_int = int(stock) if stock is not None else None
    except (TypeError, ValueError):
        stock_int = None

    multimedia = result.get("ae_multimedia_info_dto") or {}
    raw_imgs = multimedia.get("image_urls") or ""
    first_img = raw_imgs.split(";")[0].strip() if isinstance(raw_imgs, str) and raw_imgs else ""

    store_info = result.get("ae_store_info") or {}
    title = str(base_info.get("subject") or result.get("subject") or "").strip()
    orders_count = int(base_info.get("sales_count") or 0) if str(base_info.get("sales_count") or "").isdigit() else 0

    freight = calculate_aliexpress_freight(str(product_id), sku_id=sku_id, country_code=target_country)
    return {
        "id": str(product_id),
        "sku_id": sku_id,
        "sku": f"AE-{product_id}" + (f"-{sku_id}" if sku_id else ""),
        "name": title or f"AliExpress DS #{product_id}",
        "store_name": store_info.get("store_name") or "AliExpress DS Store",
        "product_cost_usd": price_usd,
        "stock": stock_int,
        "available": (stock_int is None or stock_int > 0) and base_info.get("product_status_type") != "offline",
        "estimated_shipping_usd": freight.get("shipping_usd") if freight else None,
        "logistic_line": freight.get("logistic_line") if freight else "AliExpress Selection Standard",
        "delivery_days": freight.get("delivery_days") if freight else "8-14 days",
        "image_url": first_img,
        "order_link": f"https://www.aliexpress.com/item/{product_id}.html",
        "listed_count": orders_count,
    }


def search_aliexpress_ds_products(
    keyword: str,
    target_country: str = "US",
    ship_to_currency: str = "USD",
    size: int = 5,
) -> Dict[str, Any]:
    """
    Search AliExpress Dropshipping API (aliexpress.ds.text.search) and enrich top matches
    with live SKU pricing & US/EU freight. Returns structured dict with configured status.
    """
    status = get_aliexpress_config_status()
    if not status["ready"]:
        return {
            "source": "AliExpress Dropshipping API",
            "configured": False,
            "status": "not_configured",
            "keyword": keyword,
            "products": [],
            "note": "ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET / OAuth Access Token hələ əlavə edilməyib.",
        }

    res = _call_aliexpress_iop(
        method="aliexpress.ds.text.search",
        biz_params={
            "keyWord": (keyword or "").strip(),
            "local": "en_US",
            "countryCode": target_country.upper(),
            "currency": ship_to_currency,
            "pageSize": min(max(1, size), 20),
            "pageIndex": 1,
            "sortBy": "orders,desc",
        },
    )
    if not res.get("ok"):
        return {
            "source": "AliExpress Dropshipping API",
            "configured": True,
            "status": "error",
            "keyword": keyword,
            "error": res.get("error"),
            "products": [],
        }

    data = res.get("data") or {}
    root = data.get("aliexpress_ds_text_search_response") or data
    payload = root.get("data") or root
    items = _unwrap_iop_list(payload.get("products"), "selection_search_product")

    mapped: List[Dict[str, Any]] = []
    for raw in items[:size]:
        pid = str(raw.get("itemId") or raw.get("product_id") or "").strip()
        if not pid:
            continue
        detail = get_aliexpress_ds_product_detail(pid, target_country=target_country, ship_to_currency=ship_to_currency)
        if detail and detail.get("product_cost_usd"):
            mapped.append(detail)
        else:
            cost = _parse_money(raw.get("targetSalePrice") or raw.get("targetOriginalPrice") or raw.get("sale_price"))
            if cost and cost > 0:
                mapped.append({
                    "id": pid,
                    "sku_id": str(raw.get("skuId") or ""),
                    "sku": f"AE-{pid}",
                    "name": str(raw.get("title") or keyword)[:100],
                    "store_name": "AliExpress Dropshipping",
                    "product_cost_usd": cost,
                    "estimated_shipping_usd": None,
                    "logistic_line": "AliExpress Selection Standard",
                    "delivery_days": "8-14 days",
                    "image_url": raw.get("itemMainPic") or raw.get("product_main_image_url") or "",
                    "order_link": raw.get("itemUrl") or f"https://www.aliexpress.com/item/{pid}.html",
                    "listed_count": int(raw.get("orders") or 0) if str(raw.get("orders") or "").isdigit() else 0,
                })

    return {
        "source": "AliExpress Dropshipping API",
        "configured": True,
        "status": "success" if mapped else "no_matches",
        "keyword": keyword,
        "target_country": target_country,
        "products": mapped,
    }


def check_aliexpress_ds_product_status(product_id: str, sku_id: str = "", target_country: str = "US") -> Dict[str, Any]:
    """Check live stock and wholesale USD price on AliExpress DS API for monitoring & failover."""
    if not get_aliexpress_config_status()["ready"]:
        return {"available": True, "checked": False, "reason": "AliExpress API konfiqurasiya olunmayıb"}
    detail = get_aliexpress_ds_product_detail(product_id, target_country=target_country)
    if not detail:
        return {"available": False, "checked": True, "stock": 0, "price_usd": None, "reason": "Məhsul tapılmadı və ya offline"}
    return {
        "available": bool(detail.get("available", True)),
        "checked": True,
        "stock": detail.get("stock"),
        "price_usd": detail.get("product_cost_usd"),
    }


def create_aliexpress_ds_order(
    product_id: str,
    sku_id: str,
    shipping: Dict[str, Any],
    quantity: int = 1,
    logistic_line: str = "CAINIAO_STANDARD",
) -> Dict[str, Any]:
    """Submit real order to AliExpress Open Platform via aliexpress.ds.order.create."""
    if not get_aliexpress_config_status()["ready"]:
        return {"success": False, "error": "AliExpress DS API açarları/OAuth Token təyin edilməyib."}

    if not product_id:
        return {"success": False, "error": "AliExpress product_id əskikdir."}

    phone = str(shipping.get("phone") or "0000000000").strip()
    phone_country = "+1" if (shipping.get("country_code") or "US").upper() == "US" else "+994"
    mobile_no = re.sub(r"[^0-9]", "", phone) or "501234567"

    logistics_address = {
        "address": shipping.get("address") or "123 Main St",
        "city": shipping.get("city") or "Los Angeles",
        "contact_person": shipping.get("customer_name") or "Customer",
        "country": (shipping.get("country_code") or "US").upper(),
        "full_name": shipping.get("customer_name") or "Customer",
        "mobile_no": mobile_no,
        "phone_country": phone_country,
        "province": shipping.get("province") or "CA",
        "zip": shipping.get("zip_code") or "90001",
    }
    product_items = [{
        "product_count": max(1, int(quantity or 1)),
        "product_id": int(product_id) if str(product_id).isdigit() else product_id,
        **({"sku_attr": str(sku_id)} if sku_id else {}),
        **({"logistics_service_name": logistic_line} if logistic_line else {}),
    }]

    res = _call_aliexpress_iop(
        method="aliexpress.ds.order.create",
        biz_params={
            "param_place_order_request4_open_api_d_t_o": {
                "logistics_address": logistics_address,
                "product_items": product_items,
            }
        },
    )
    if not res.get("ok"):
        return {"success": False, "error": res.get("error") or "AliExpress DS sifariş xətası"}

    data = res.get("data") or {}
    root = data.get("aliexpress_ds_order_create_response") or data
    result = root.get("result") or root
    ids = _unwrap_iop_list(result.get("order_list"), "number")
    ae_order_id = str(ids[0]) if ids else ""
    return {
        "success": bool(result.get("is_success", bool(ae_order_id))),
        "ae_order_id": ae_order_id,
        "message": "AliExpress DS sifarişi yaradıldı." if ae_order_id else str(result.get("error_msg") or "Sifariş cavabı boşdur."),
    }


def sync_aliexpress_ds_order(ae_order_id: str) -> Dict[str, Any]:
    """Fetch order & tracking status from AliExpress DS API."""
    if not ae_order_id or not get_aliexpress_config_status()["ready"]:
        return {"success": False, "error": "AliExpress order_id və ya API konfiqurasiyası əskikdir."}
    res = _call_aliexpress_iop(
        method="aliexpress.ds.order.get",
        biz_params={"single_order_query": {"order_id": int(ae_order_id) if str(ae_order_id).isdigit() else ae_order_id}},
    )
    if not res.get("ok"):
        return {"success": False, "error": res.get("error")}
    data = res.get("data") or {}
    root = data.get("aliexpress_ds_order_get_response") or data
    result = root.get("result") or root
    logistics = _unwrap_iop_list(result.get("logistics_info_list"), "ae_order_logistics_info")
    first_log = logistics[0] if logistics and isinstance(logistics[0], dict) else {}
    return {
        "success": True,
        "status": result.get("order_status") or "CREATED",
        "tracking_number": first_log.get("logistics_no") or "",
        "tracking_url": "",
        "logistic_name": first_log.get("logistics_service") or "AliExpress Standard",
    }
