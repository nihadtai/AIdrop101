import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from tools.cj_dropshipping_tool import search_cj_products_live

logger = logging.getLogger(__name__)

# Live currency cache (1 hour TTL)
_FX_CACHE: Dict[str, Any] = {"rates": None, "fetched_at": 0}


def get_live_fx_rates() -> Dict[str, float]:
    """
    Fetches live USD -> CNY and USD -> EUR exchange rates from open.er-api.com (free, no key).
    Falls back to baseline rates only if network is unreachable.
    """
    now = time.time()
    if _FX_CACHE["rates"] and (now - _FX_CACHE["fetched_at"] < 3600):
        return _FX_CACHE["rates"]

    try:
        req = urllib.request.Request(
            "https://open.er-api.com/v6/latest/USD",
            headers={"User-Agent": "ECommerce-Agent/1.0"}
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            rates = data.get("rates", {})
            cny = float(rates.get("CNY") or 7.15)
            eur = float(rates.get("EUR") or 0.92)
            gbp = float(rates.get("GBP") or 0.78)
            res = {"CNY": round(cny, 4), "EUR": round(eur, 4), "GBP": round(gbp, 4), "live": True}
            _FX_CACHE["rates"] = res
            _FX_CACHE["fetched_at"] = now
            return res
    except Exception as e:
        logger.debug(f"Live FX fetch fallback: {e}")

    return {"CNY": 7.15, "EUR": 0.92, "GBP": 0.78, "live": False}


def _find_local_chrome() -> Optional[str]:
    """Finds locally installed Chrome or Edge executable for 100% free Playwright execution."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def search_1688_free_playwright(keyword: str, max_items: int = 8) -> List[Dict[str, Any]]:
    """
    100% FREE local 1688 search using Playwright + local Chrome (zero Apify cost).
    Note: 1688 may redirect anonymous sessions to login.taobao.com; in that case returns [] cleanly
    without faking prices.
    """
    chrome_path = _find_local_chrome()
    if not chrome_path:
        return []

    keyword_clean = (keyword or "").strip()
    if not keyword_clean:
        return []

    try:
        from playwright.sync_api import sync_playwright
        results = []
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome_path, headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="zh-CN"
            )
            page = context.new_page()
            search_url = f"https://s.1688.com/selloffer/offer_search.htm?keywords={urllib.parse.quote(keyword_clean)}"
            page.goto(search_url, timeout=14000, wait_until="domcontentloaded")
            page.wait_for_timeout(1800)

            if "login." in page.url.lower():
                logger.info("1688 required login wall on anonymous headless session; skipping direct 1688 scrape cleanly.")
                browser.close()
                return []

            # Extract real offer cards if page loaded
            cards = page.eval_on_selector_all(
                "a[href*='detail.1688.com/offer']",
                """els => els.slice(0, 12).map(el => ({
                    href: el.href || '',
                    text: (el.innerText || '').trim()
                }))"""
            )
            browser.close()

            seen = set()
            for c in cards:
                href = c.get("href", "")
                if not href or href in seen:
                    continue
                seen.add(href)
                txt = c.get("text", "")
                price_match = re.search(r'[¥￥]\s*(\d+(?:\.\d+)?)', txt)
                price_cny = float(price_match.group(1)) if price_match else None
                results.append({
                    "supplier_name": "1688.com Birbaşa İstehsalçı",
                    "platform": "1688.com (Free Direct)",
                    "price_cny": price_cny,
                    "product_name": txt.splitlines()[0][:80] if txt else keyword_clean,
                    "product_url": href,
                    "fulfillment_partner": "HyperSKU / SourcinBox / Private 1688 Agent (YunExpress US/EU)",
                    "moq": 1,
                })
                if len(results) >= max_items:
                    break
        return results
    except Exception as e:
        logger.debug(f"Free Playwright 1688 note: {e}")
        return []


def search_1688_products(keyword: str, image_url: str = None) -> str:
    """
    Searches 1688.com and global dropshipping suppliers with ZERO Apify dependency.
    Priority:
      1. Free Playwright 1688 direct factory search (no fake prices if gated)
      2. Official CJ Dropshipping API 2.0 real-time wholesale catalog
      3. Direct 1688 Factory Search URL + Fulfillment Agent guide (for HyperSKU/SourcinBox/Private Agent)
    """
    keyword_clean = (keyword or "").strip()
    logger.info(f"Searching free wholesale & 1688 channels for '{keyword_clean}'...")
    fx = get_live_fx_rates()

    # 1. Try free local Playwright 1688 search
    direct_1688 = search_1688_free_playwright(keyword_clean, max_items=6)
    if direct_1688:
        for s in direct_1688:
            if s.get("price_cny"):
                s["price_usd"] = round(s["price_cny"] / fx["CNY"], 2)
        return json.dumps({
            "source": "1688.com (Free Playwright)",
            "keyword": keyword_clean,
            "fx_cny_per_usd": fx["CNY"],
            "suppliers": direct_1688
        }, ensure_ascii=False)

    # 2. Try CJ Dropshipping Official API 2.0 (100% Free, Real Stock & US/EU Shipping)
    cj_items = search_cj_products_live(keyword_clean, page=1, size=8, target_country="US")
    if cj_items:
        mapped = []
        for item in cj_items:
            cost_usd = item.get("product_cost_usd", 0.0)
            ship_usd = item.get("estimated_shipping_usd")  # None if unavailable
            mapped.append({
                "supplier_name": f"CJ Verified — {item.get('name', keyword_clean)[:35]}",
                "platform": "CJ Dropshipping (Official API 2.0)",
                "price_usd": cost_usd,
                "price_cny": round(cost_usd * fx["CNY"], 2),
                "shipping_usd": ship_usd,
                "delivery_days": item.get("delivery_days", "7-12 days"),
                "logistic_line": item.get("logistic_line", "CJPacket / YunExpress"),
                "sku": item.get("sku"),
                "product_id": item.get("id"),
                "moq": 1,
                "product_name": item.get("name"),
                "product_url": item.get("order_link"),
                "image_url": item.get("image_url") or image_url or "",
                "listed_count": item.get("listed_count"),
                "is_live_api": True,
            })
        return json.dumps({
            "source": "CJ Dropshipping API 2.0 (Live Free)",
            "keyword": keyword_clean,
            "fx_cny_per_usd": fx["CNY"],
            "suppliers": mapped
        }, ensure_ascii=False)

    # 3. Honest fallback: No fake prices! Return direct 1688 factory search link + Fulfillment Agent channel
    encoded_kw = urllib.parse.quote(keyword_clean)
    search_link_1688 = f"https://s.1688.com/selloffer/offer_search.htm?keywords={encoded_kw}"
    return json.dumps({
        "source": "1688 Direct Search Link (Manual/Agent Sourcing)",
        "keyword": keyword_clean,
        "live_price_available": False,
        "fx_cny_per_usd": fx["CNY"],
        "suppliers": [
            {
                "supplier_name": "1688.com Birbaşa Axtarış + Fulfillment Agent (HyperSKU / SourcinBox)",
                "platform": "1688.com + US/EU Fulfillment Agent",
                "price_cny": None,
                "price_usd": None,
                "moq": 1,
                "product_name": keyword_clean,
                "product_url": search_link_1688,
                "image_url": image_url or "",
                "note": "Canlı 1688 qiyməti login tələb etdiyi üçün açıq mənbədən oxunmadı. 1688 linkini HyperSKU, SourcinBox və ya şəxsi Çin agentinə göndərərək real YunExpress qiyməti ala bilərsiniz.",
                "is_live_api": False,
            }
        ]
    }, ensure_ascii=False)


def search_cj_dropshipping(keyword: str, target_country: str = "US") -> str:
    """
    Searches CJ Dropshipping via their official API 2.0 for real dropship products,
    prices, SKUs, and US/EU shipping options.
    """
    logger.info(f"Searching CJ Dropshipping catalog for '{keyword}' (target={target_country})...")
    try:
        live_products = search_cj_products_live(keyword, page=1, size=8, target_country=target_country)
        if live_products:
            return json.dumps({
                "source": "CJ Dropshipping API 2.0 (Live)",
                "status": "success",
                "keyword": keyword,
                "target_country": target_country,
                "products_count": len(live_products),
                "products": live_products
            }, ensure_ascii=False)

        return json.dumps({
            "source": "CJ Dropshipping API 2.0",
            "status": "no_matches",
            "keyword": keyword,
            "products": [],
            "search_url": f"https://cjdropshipping.com/search/{urllib.parse.quote(keyword)}.html",
            "note": "Bu açar sözlə CJ Dropshipping kataloqunda birbaşa nəticə tapılmadı."
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error in search_cj_dropshipping: {e}")
        return json.dumps({"error": str(e)})


def calculate_unit_economics(
    selling_price: float,
    product_cost: float,
    shipping_cost: float,
    estimated_cpa: float,
    gateway_fee_pct: float = 0.029,
    gateway_fixed_fee: float = 0.30,
    eu_ioss_vat_pct: float = 0.0,
) -> str:
    """
    Calculates global US/EU dropshipping unit economics in USD (including Stripe/Shopify fees & optional EU IOSS VAT).
    """
    logger.info(f"Calculating global unit economics (selling_price=${selling_price})")
    try:
        gateway_fee = (selling_price * gateway_fee_pct) + gateway_fixed_fee
        vat_cost = selling_price * eu_ioss_vat_pct
        total_costs = product_cost + shipping_cost + estimated_cpa + gateway_fee + vat_cost
        net_profit = selling_price - total_costs
        margin = (net_profit / selling_price) * 100 if selling_price > 0 else 0
        break_even_cpa = selling_price - product_cost - shipping_cost - gateway_fee - vat_cost
        break_even_roas = selling_price / break_even_cpa if break_even_cpa > 0 else 0

        response = {
            "currency": "USD",
            "selling_price": round(selling_price, 2),
            "costs": {
                "product": round(product_cost, 2),
                "shipping_us_eu": round(shipping_cost, 2),
                "cpa": round(estimated_cpa, 2),
                "gateway_fee": round(gateway_fee, 2),
                "eu_vat": round(vat_cost, 2),
            },
            "net_profit": round(net_profit, 2),
            "margin_percentage": round(margin, 2),
            "break_even_cpa": round(break_even_cpa, 2),
            "break_even_roas": round(break_even_roas, 2)
        }
        return json.dumps(response)
    except Exception as e:
        return json.dumps({"error": str(e)})


def get_1688_cookies_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "1688_cookies.json")


def _load_1688_cookies() -> List[Dict[str, Any]]:
    path = get_1688_cookies_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            normalized = []
            for c in raw:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                item = {
                    "name": str(c["name"]),
                    "value": str(c.get("value", "")),
                    "domain": str(c.get("domain") or ".1688.com"),
                    "path": str(c.get("path") or "/"),
                }
                if c.get("secure") is not None:
                    item["secure"] = bool(c["secure"])
                if c.get("httpOnly") is not None:
                    item["httpOnly"] = bool(c["httpOnly"])
                same_site = str(c.get("sameSite") or "").strip().capitalize()
                if same_site in ("Strict", "Lax", "None"):
                    item["sameSite"] = same_site
                normalized.append(item)
            return normalized
    except Exception as e:
        logger.debug(f"1688 cookie load error: {e}")
    return []


def get_1688_session_status() -> Dict[str, Any]:
    """Return status of shared server-side 1688 Playwright cookie session."""
    cookies = _load_1688_cookies()
    path = get_1688_cookies_path()
    has_auth = any(
        c.get("name") in ("_tb_token_", "cookie2", "t", "__cn_logon__", "__cn_logon_id__", "cna")
        for c in cookies
    )
    return {
        "session_active": bool(cookies and has_auth),
        "cookies_count": len(cookies),
        "chrome_available": bool(_find_local_chrome()),
        "cookie_file": path,
    }


def save_1688_session_cookies(cookies_input: Any) -> Dict[str, Any]:
    """Validate and save admin 1688 session cookies so end-users search 1688 without login."""
    if isinstance(cookies_input, str):
        parsed = json.loads(cookies_input)
    else:
        parsed = cookies_input
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("Cookie JSON boş olmayan siyahı ([{name, value, domain, ...}]) olmalıdır.")
    path = get_1688_cookies_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)
    return get_1688_session_status()


def _normalize_cookie_path_domain(cookies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [c for c in cookies if c.get("name") and c.get("domain")]


def _download_image_bytes(image_url: str) -> Optional[bytes]:
    if not image_url or not image_url.startswith("http"):
        return None
    try:
        req = urllib.request.Request(
            image_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            if data and len(data) > 256:
                return data
    except Exception as e:
        logger.debug(f"1688 visual search image download note: {e}")
    return None


def search_1688_by_image_playwright(
    image_url: str = "",
    image_bytes: Optional[bytes] = None,
    max_items: int = 8,
) -> Dict[str, Any]:
    """
    Registration-free 1688 Visual Search for dashboard end-users backed by shared server cookies
    (data/1688_cookies.json) + local Chrome Playwright. Never fabricates results if unauthenticated.
    """
    import tempfile

    status = get_1688_session_status()
    if not status["chrome_available"]:
        return {
            "success": False,
            "session_required": True,
            "reason": "Serverdə lokal Chrome/Edge tapılmadı.",
            "suppliers": [],
        }
    cookies = _normalize_cookie_path_domain(_load_1688_cookies())
    if not cookies or not status["session_active"]:
        return {
            "success": False,
            "session_required": True,
            "reason": "1688 Server Admin Sessiyası aktiv deyil (data/1688_cookies.json). Admin bir dəfə 1688 cookie daxil etməlidir ki, saytdakı bütün istifadəçilər qeydiyyatsız axtara bilsin.",
            "suppliers": [],
        }

    raw_bytes = image_bytes or _download_image_bytes(image_url)
    if not raw_bytes:
        return {
            "success": False,
            "session_required": False,
            "reason": "Məhsul şəkli endirilə bilmədi.",
            "suppliers": [],
        }

    tmp_path = None
    try:
        from playwright.sync_api import sync_playwright

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tf.write(raw_bytes)
            tmp_path = tf.name

        fx = get_live_fx_rates()
        results: List[Dict[str, Any]] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=_find_local_chrome(), headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                locale="zh-CN",
            )
            context.add_cookies(cookies)
            page = context.new_page()
            page.goto("https://www.1688.com/", timeout=18000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

            if "login." in page.url.lower():
                browser.close()
                return {
                    "success": False,
                    "session_required": True,
                    "reason": "1688 Admin Cookie sessiyasının müddəti bitib — yenilənməlidir.",
                    "suppliers": [],
                }

            # Locate 1688 image-upload file input (camera upload icon renders <input type="file">)
            uploaded = False
            try:
                file_input = page.locator("input[type='file']").first
                if file_input.count() == 0:
                    cam_btn = page.locator("[class*='img-search'], [class*='camera'], [data-spm*='img']").first
                    if cam_btn.count() > 0:
                        cam_btn.click()
                        page.wait_for_timeout(800)
                file_input = page.locator("input[type='file']").first
                if file_input.count() > 0:
                    file_input.set_input_files(tmp_path)
                    uploaded = True
                    page.wait_for_timeout(3500)
            except Exception as up_err:
                logger.debug(f"1688 image input upload note: {up_err}")

            if not uploaded or "login." in page.url.lower():
                login_hit = "login." in page.url.lower()
                browser.close()
                return {
                    "success": False,
                    "session_required": login_hit,
                    "reason": "1688 cookie sessiyası yenilənməlidir." if login_hit else "1688 şəkil yükləmə xanası tapılmadı.",
                    "suppliers": [],
                }

            cards = page.eval_on_selector_all(
                "a[href*='detail.1688.com/offer']",
                """els => els.slice(0, 16).map(el => {
                    const card = el.closest('[class*="offer"], [class*="card"], li, div') || el;
                    const img = card.querySelector('img');
                    return {
                        href: el.href || '',
                        text: (card.innerText || el.innerText || '').trim(),
                        img: img ? (img.src || img.getAttribute('data-src') || '') : ''
                    };
                })"""
            )
            browser.close()

            seen = set()
            for c in cards:
                href = (c.get("href") or "").split("?")[0]
                if not href or href in seen:
                    continue
                seen.add(href)
                txt = c.get("text", "")
                price_match = re.search(r'[¥￥]\s*(\d+(?:\.\d+)?)', txt)
                price_cny = float(price_match.group(1)) if price_match else None
                moq_match = re.search(r'(\d+)\s*(?:件|个|套|只|pcs)', txt)
                moq = int(moq_match.group(1)) if moq_match else 1
                lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
                title = lines[0][:90] if lines else "1688.com Fabrik Məhsulu"
                results.append({
                    "supplier_name": "1688.com Fabrik Tədarükçü",
                    "platform": "1688.com + YunExpress (3PL)",
                    "channel": "1688_yunexpress",
                    "price_cny": price_cny,
                    "price_usd": round(price_cny / fx["CNY"], 2) if price_cny else None,
                    "product_name": title,
                    "product_url": href,
                    "image_url": c.get("img") or image_url,
                    "moq": moq,
                })
                if len(results) >= max_items:
                    break

        return {
            "success": bool(results),
            "session_required": False,
            "fx_cny_per_usd": fx["CNY"],
            "suppliers": results,
            "reason": "" if results else "1688 şəkil axtarışında uyğun aktiv offer tapılmadı.",
        }
    except Exception as exc:
        logger.debug(f"1688 visual search error: {exc}")
        return {"success": False, "session_required": False, "reason": str(exc), "suppliers": []}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def scrape_1688_link_direct(url: str) -> dict:
    """Scrapes a specific 1688 offer URL using local Chrome (+ server cookies if present)."""
    chrome_path = _find_local_chrome()
    if not chrome_path or not url:
        return {}
    try:
        from playwright.sync_api import sync_playwright
        cookies = _normalize_cookie_path_domain(_load_1688_cookies())
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome_path, headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                locale="zh-CN",
            )
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            page.goto(url, timeout=16000, wait_until="domcontentloaded")
            page.wait_for_timeout(1800)
            if "login." in page.url.lower():
                browser.close()
                return {}
            title = page.title() or ""
            body_text = page.inner_text("body")[:2500]
            first_img = ""
            try:
                first_img = page.eval_on_selector("img[src*='alicdn.com']", "el => el ? el.src : ''") or ""
            except Exception:
                pass
            browser.close()
            lower_body = body_text.lower()
            offline_markers = ("商品已下架", "产品已下架", "已售罄", "out of stock", "no longer available", "offer not found")
            in_stock = not any(m in lower_body or m in body_text for m in offline_markers)
            price_match = re.search(r'[¥￥]\s*(\d+(?:\.\d+)?)', body_text)
            return {
                "supplier_name": "1688.com Birbaşa Fabrik",
                "price_cny": float(price_match.group(1)) if price_match else None,
                "product_name": title[:100],
                "image_url": first_img,
                "in_stock": in_stock,
                "video_url": "",
            }
    except Exception as e:
        logger.debug(f"Direct 1688 link scrape note: {e}")
    return {}


