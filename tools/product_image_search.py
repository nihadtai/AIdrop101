import html as html_lib
import logging
import urllib.parse
import urllib.request
import re
from typing import Optional

logger = logging.getLogger(__name__)

def _is_reachable_image(url: str, timeout: float = 4.0) -> bool:
    """Verify via HTTP HEAD/GET that an image URL actually resolves with image content."""
    if not url or not url.startswith("http"):
        return False
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        req = urllib.request.Request(url, headers=req_headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as res:
            ctype = (res.headers.get("Content-Type") or "").lower()
            return res.status == 200 and (ctype.startswith("image/") or "octet-stream" in ctype or not ctype)
    except Exception:
        try:
            req_get = urllib.request.Request(url, headers={**req_headers, "Range": "bytes=0-1024"}, method="GET")
            with urllib.request.urlopen(req_get, timeout=timeout) as res:
                ctype = (res.headers.get("Content-Type") or "").lower()
                return res.status in (200, 206) and (ctype.startswith("image/") or not ctype)
        except Exception:
            return False


_TRUSTED_ECOM_DOMAINS = (
    "m.media-amazon.com",
    "images-na.ssl-images-amazon.com",
    "cdn.shopify.com",
    "i.ebayimg.com",
    "ae01.alicdn.com",
    "cbu01.alicdn.com",
    "mm.bing.net",
)
_BAD_TOKENS = ("logo", "icon", "avatar", "sprite", "1x1", "placeholder", "favicon", "badge")


def _extract_bing_candidates(html_text: str) -> list[str]:
    """Extract ordered image candidates from Bing Images HTML, putting trusted CDNs and Bing CDN turls first."""
    murls = re.findall(r'&quot;murl&quot;:&quot;(https?://[^&]+)&quot;', html_text)
    if not murls:
        murls = re.findall(r'"murl":"(https?://[^"]+)"', html_text)
    turls = re.findall(r'&quot;turl&quot;:&quot;(https?://[^&]+)&quot;', html_text)
    if not turls:
        turls = re.findall(r'"turl":"(https?://[^"]+)"', html_text)

    trusted: list[str] = []
    other_direct: list[str] = []
    bing_cdn: list[str] = []

    for raw in murls:
        u = html_lib.unescape(raw)
        low = u.lower()
        if any(bad in low for bad in _BAD_TOKENS):
            continue
        if any(dom in low for dom in _TRUSTED_ECOM_DOMAINS):
            trusted.append(u)
        elif any(ext in low for ext in (".jpg", ".jpeg", ".png", ".webp")):
            other_direct.append(u)

    for raw_t in turls:
        t = html_lib.unescape(raw_t)
        if "mm.bing.net/th" in t.lower():
            # Request crisp high-res square crop from Bing's reliable CDN
            upgraded = re.sub(r'([?&])w=\d+', r'\g<1>w=800', t)
            upgraded = re.sub(r'([?&])h=\d+', r'\g<1>h=800', upgraded)
            if "w=" not in upgraded:
                sep = "&" if "?" in upgraded else "?"
                upgraded = f"{upgraded}{sep}w=800&h=800&c=7&rs=1&p=0"
            bing_cdn.append(upgraded)

    # Prefer reliable trusted e-commerce CDNs -> Bing CDN turl (never hotlink-blocked) -> other direct murls
    return trusted[:6] + bing_cdn[:6] + other_direct[:6]


def search_product_images(product_name: str, fallback_query: str = "", max_images: int = 4) -> list[str]:
    """
    Searches the web for up to `max_images` distinct, HTTP-reachable high-resolution product images.
    Prioritizes reliable e-commerce CDNs and Bing CDN thumbnails.
    """
    clean_name = (product_name or "").strip() or (fallback_query or "").strip()
    if not clean_name or max_images <= 0:
        return []

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    queries = [clean_name]
    if fallback_query and fallback_query.strip().lower() != clean_name.lower():
        queries.append(fallback_query.strip())

    found: list[str] = []
    for q in queries:
        encoded = urllib.parse.quote_plus(q)
        try:
            b_url = f"https://www.bing.com/images/search?q={encoded}&first=1"
            b_req = urllib.request.Request(b_url, headers=headers)
            with urllib.request.urlopen(b_req, timeout=8) as b_res:
                bing_content = b_res.read().decode("utf-8", errors="ignore")
                candidates = _extract_bing_candidates(bing_content)
                for cand in candidates:
                    if cand not in found and _is_reachable_image(cand):
                        found.append(cand)
                        if len(found) >= max_images:
                            return found
        except Exception as be:
            logger.warning(f"Bing image search error for '{q}': {be}")

    if len(found) < max_images:
        try:
            encoded = urllib.parse.quote_plus(clean_name)
            url = f"https://yandex.com/images/search?text={encoded}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=8) as res:
                res_content = res.read().decode("utf-8", errors="ignore")
                raw_urls = re.findall(r'img_url=(https?%3A%2F%2F[^&]+)', res_content)
                for r in raw_urls[:8]:
                    decoded = html_lib.unescape(urllib.parse.unquote(r))
                    lower = decoded.lower()
                    if (
                        decoded not in found
                        and any(ext in lower for ext in (".jpg", ".jpeg", ".png", ".webp"))
                        and not any(bad in lower for bad in _BAD_TOKENS)
                        and _is_reachable_image(decoded)
                    ):
                        found.append(decoded)
                        if len(found) >= max_images:
                            return found
        except Exception as e:
            logger.warning(f"Secondary image search error for '{clean_name}': {e}")

    return found


def search_product_image(product_name: str, fallback_query: str = "") -> str:
    """Return a single verified product image URL (or empty string if none reachable)."""
    imgs = search_product_images(product_name, fallback_query=fallback_query, max_images=1)
    if imgs:
        logger.info(f"Found verified product image for '{product_name or fallback_query}': {imgs[0]}")
        return imgs[0]
    return ""
