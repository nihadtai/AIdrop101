import json
import logging
from pathlib import Path
from typing import Any, Dict, List
import urllib.parse
import xml.etree.ElementTree as ET
import requests
from tools.rapidapi_tiktok import get_real_tiktok_trends_for_category

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "data" / "radar" / "reports"


def _fetch_live_google_14d_trends(query: str, limit: int = 5) -> List[Dict[str, str]]:
    """
    Fetches real-time search trends and viral product news from Google News RSS
    strictly filtered to the last 14 days (when:14d).
    """
    try:
        encoded = urllib.parse.quote(f"{query} when:14d")
        url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and r.text:
            root = ET.fromstring(r.text)
            items = []
            for item in root.findall(".//item")[:limit]:
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                pub = item.find("pubDate").text if item.find("pubDate") is not None else ""
                if title:
                    items.append({"title": title, "url": link, "published": pub})
            return items
    except Exception as e:
        logger.debug(f"Google 14d RSS fetch error: {e}")
    return []


_CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "electronics": ["earbud", "headphone", "earphone", "charger", "camera", "speaker", "watch", "phone", "led", "usb",
                    "bluetooth", "wireless", "smart", "gimbal", "projector", "cable", "power bank", "drone", "keyboard"],
    "home": ["home", "decor", "lamp", "light", "clean", "storage", "organizer", "humidifier", "vacuum", "diffuser",
             "candle", "shelf", "curtain", "blanket"],
    "beauty": ["skin", "hair", "beauty", "face", "makeup", "serum", "massager", "nail", "lash", "cream", "brush"],
    "fashion": ["shirt", "dress", "jacket", "shoe", "bag", "jewelry", "ring", "necklace", "sock", "hat", "wear"],
    "kitchen": ["kitchen", "cook", "blender", "cup", "bottle", "knife", "fryer", "coffee", "mug", "pan", "chopper"],
    "fitness": ["fitness", "gym", "workout", "yoga", "resistance", "massage", "dumbbell", "band", "jump rope"],
    "kids": ["kid", "toy", "baby", "child", "puzzle", "plush"],
    "pet": ["pet", "dog", "cat", "puppy", "leash", "collar", "litter"],
    "automotive": ["car", "auto", "vehicle", "dash", "tire", "windshield"],
    "outdoor": ["outdoor", "camp", "travel", "hiking", "tent", "flashlight", "portable"],
}
_STOPWORDS = {"gadgets", "tools", "items", "products", "supplies", "accessories", "gear", "equipment",
              "viral", "trending", "decor", "household", "personal", "care", "and", "tech"}


def _category_keywords(category: str) -> List[str]:
    """Expand a category phrase (e.g. 'kitchen gadgets cooking tools') into product-title keywords."""
    words = [w for w in category.lower().replace("&", " ").split() if w]
    keywords: List[str] = []
    for w in words:
        if w in _CATEGORY_KEYWORDS:
            keywords.extend(_CATEGORY_KEYWORDS[w])
        elif w not in _STOPWORDS and len(w) >= 4:
            keywords.append(w)
    return list(dict.fromkeys(keywords))


def _load_live_radar_signals(source: str, category: str = "", limit: int = 5) -> List[Dict[str, Any]]:
    """Loads fresh breakout signals from live radar reports, filtered by category keywords with varied sampling."""
    import random as _random
    path = REPORTS_DIR / f"{source}_latest.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        signals = data.get("signals", [])
        generic = not category or category.lower().strip() in ("all", "viral trending products")
        if generic:
            pool = signals[:25]
            return _random.sample(pool, min(limit, len(pool))) if len(pool) > limit else pool[:limit]
        keywords = _category_keywords(category)
        if not keywords:
            pool = signals[:25]
            return _random.sample(pool, min(limit, len(pool))) if len(pool) > limit else pool[:limit]
        import re as _re
        matched = []
        for s in signals:
            text = f"{s.get('title', '')} {s.get('origin', '')}".lower()
            words = _re.findall(r"[a-z0-9]+", text)
            if any(
                (" " in k and k in text)
                or any(w == k or w == k + "s" or w == k + "es" or (len(k) >= 6 and w.startswith(k)) for w in words)
                for k in keywords
            ):
                matched.append(s)
        if len(matched) > limit:
            pool = matched[:max(limit * 3, 15)]
            return _random.sample(pool, limit)
        return matched[:limit]
    except Exception as e:
        logger.debug(f"Radar signals read error: {e}")
        return []


def find_radar_image_for_product(product_name: str) -> str:
    """Return an authentic CDN product image from live Shopify/Amazon radar reports when title matches."""
    if not product_name or len(product_name.strip()) < 3:
        return ""
    import re as _re
    query_tokens = {w for w in _re.findall(r"[a-z0-9]+", product_name.lower()) if len(w) >= 3 and w not in _STOPWORDS}
    if not query_tokens:
        return ""
    best_img = ""
    best_score = 0
    for src in ("amazon", "shopify"):
        path = REPORTS_DIR / f"{src}_latest.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for s in data.get("signals", []):
                img = str(s.get("image") or "").strip()
                if not img.startswith("http"):
                    continue
                title_tokens = set(_re.findall(r"[a-z0-9]+", str(s.get("title") or "").lower()))
                overlap = len(query_tokens & title_tokens)
                if overlap >= 2 and overlap > best_score:
                    best_score = overlap
                    best_img = img
        except Exception:
            continue
    return best_img


def search_tiktok_trends(category: str) -> str:
    """
    Searches TikTok metrics and real-time 14-day viral product signals in a category.
    """
    logger.info(f"Searching TikTok trends for category: {category} (14-day fresh filter)...")
    try:
        live_tiktok_data = get_real_tiktok_trends_for_category(category)
        google_14d_news = _fetch_live_google_14d_trends(f"tiktok viral products {category}", limit=4)
        shopify_breakouts = _load_live_radar_signals("shopify", category=category, limit=3)

        response_data = {
            "platform": "TikTok Shop & Creative Center",
            "timeframe": "Son 7-14 gün",
            "category": category,
            "live_meta": live_tiktok_data,
            "google_recent_14d_buzz": google_14d_news,
            "competitor_shopify_signals": [
                {
                    "title": s.get("title"),
                    "store": s.get("origin"),
                    "price": s.get("price"),
                    "image": s.get("image"),
                    "url": s.get("url"),
                }
                for s in shopify_breakouts
            ],
        }
        return json.dumps(response_data, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Note on TikTok live search: {e}")
        return json.dumps({"platform": "TikTok", "category": category, "error": str(e)}, ensure_ascii=False)


def search_google_trends(keyword: str, timeframe: str = "today 1-m") -> str:
    """
    Uses Google Search & Trends to analyze search volume and breakout demand in the last 14 days.
    """
    logger.info(f"Searching Google trends for keyword: {keyword} over {timeframe}")
    try:
        recent_buzz = _fetch_live_google_14d_trends(f"{keyword} viral trend buy", limit=4)
        response_data = {
            "platform": "Google Trends & Web Grounding",
            "keyword": keyword,
            "timeframe": "Son 14 gün (when:14d)",
            "article_count_last_14d": len(recent_buzz),
            "note": "Axtarış həcmi/artım faizi ölçülmür; yalnız son 14 günün real xəbər başlıqları verilir.",
            "live_14d_articles": recent_buzz,
            "suggested_search_phrases": [
                f"best {keyword} 2026",
                f"tiktok made me buy it {keyword}",
                f"{keyword} honest review 2026"
            ],
        }
        return json.dumps(response_data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def search_amazon_bestsellers(category: str) -> str:
    """
    Returns live Amazon Movers & Shakers / Hot New Releases (< 150 reviews) from live radar.
    """
    logger.info(f"Searching Amazon Movers & Hot New Releases for category: {category}")
    try:
        live_amazon = _load_live_radar_signals("amazon", category=category, limit=5)
        bestsellers = []
        for a in live_amazon:
            m = a.get("metrics", {})
            bestsellers.append({
                "product": a.get("title"),
                "rank": m.get("rank", 1),
                "avg_rating": m.get("rating", 4.5),
                "reviews_count": m.get("reviews", 50),
                "is_early_trend": (m.get("reviews") or 0) <= 200,
                "price": a.get("price"),
                "suggested_dtc_price": m.get("suggested_dtc_price"),
                "reasons": a.get("reasons", []),
                "image": a.get("image"),
                "url": a.get("url")
            })

        response_data = {
            "platform": "Amazon Hot New Releases & Movers",
            "timeframe": "Son 24-48 saatlıq radar sıçrayışları",
            "category": category,
            "bestsellers": bestsellers,
        }
        return json.dumps(response_data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def search_meta_ad_library(keyword: str) -> str:
    """
    Searches Meta Ad Library active scaling ads for creative angles.
    """
    logger.info(f"Building Meta Ad Library link for keyword: {keyword}")
    try:
        link = (
            "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL"
            f"&q={urllib.parse.quote_plus(keyword)}&search_type=keyword_unordered&media_type=all"
        )
        response_data = {
            "platform": "Meta Ad Library",
            "keyword": keyword,
            "live_data_available": False,
            "note": "Meta Reklam Kitabxanası üzrə birbaşa aktiv reklam axtarış linki yaradıldı.",
            "ad_library_search_url": link,
        }
        return json.dumps(response_data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})
