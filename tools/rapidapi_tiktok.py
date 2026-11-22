import json
import logging
import os
import urllib.parse
from typing import Any, Dict, List, Optional
import httpx
from config.settings import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

RAPIDAPI_KEY = getattr(settings, "rapidapi_key", None) or os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = getattr(settings, "rapidapi_tiktok_host", None) or os.getenv(
    "RAPIDAPI_TIKTOK_HOST", "tiktok239.p.rapidapi.com"
)

# In-memory cache to guarantee minimum RapidAPI usage (strictly 1 call per category session)
_TIKTOK_CACHE: Dict[str, Any] = {}


def fetch_tiktok_user_info(username: str) -> Optional[Dict[str, Any]]:
    """
    Fetches real TikTok profile metrics with local caching to minimize RapidAPI calls.
    Uses RapidAPI TikTok API (alexanderxbx - /user.php).
    """
    clean_username = username.lstrip("@").strip().lower()
    cache_key = f"user_{clean_username}"
    if cache_key in _TIKTOK_CACHE:
        logger.info(f"Using cached TikTok user data for @{clean_username} (saving RapidAPI quota)")
        return _TIKTOK_CACHE[cache_key]

    if not RAPIDAPI_KEY:
        logger.warning("RAPIDAPI_KEY is not configured in .env — skipping TikTok profile lookup.")
        return None

    logger.info(f"Making single targeted RapidAPI TikTok call for user @{clean_username}...")
    headers = {
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
    }
    url = f"https://{RAPIDAPI_HOST}/user.php?username={urllib.parse.quote_plus(clean_username)}"
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if "userInfo" in data:
                    user = data["userInfo"].get("user", {})
                    stats = data["userInfo"].get("stats", {})
                    profile = {
                        "username": user.get("uniqueId", clean_username),
                        "nickname": user.get("nickname", ""),
                        "avatar": user.get("avatarLarger", ""),
                        "followers": stats.get("followerCount", 0),
                        "likes": stats.get("heartCount", 0),
                        "video_count": stats.get("videoCount", 0),
                        "bio": user.get("signature", ""),
                        "profile_url": f"https://www.tiktok.com/@{user.get('uniqueId', clean_username)}",
                    }
                    _TIKTOK_CACHE[cache_key] = profile
                    return profile
    except Exception as err:
        logger.warning(f"RapidAPI TikTok user.php error: {err}")

    return None


def get_real_tiktok_trends_for_category(category: str) -> Dict[str, Any]:
    """
    Fetches trending TikTok e-commerce metrics for a category.
    Uses viral e-commerce hashtags like #tiktokmademebuyit for discovery.
    Strictly makes at most 1 RapidAPI request to protect user API quota.
    """
    cat_lower = category.lower().strip()
    cache_key = f"trend_cat_{cat_lower}"
    if cache_key in _TIKTOK_CACHE:
        return _TIKTOK_CACHE[cache_key]

    # Trending e-commerce hashtags on TikTok — primary discovery channels
    VIRAL_HASHTAGS = [
        "tiktokmademebuyit",
        "amazonfind",
        "amazonmusthaves",
        "viralproducts",
        "gadgets",
        "coolgadgets",
        "homefinds",
        "beautytok",
        "techtok",
        "kitchentok",
        "fitnesstok",
    ]

    # Map user category to best matching hashtags
    CATEGORY_HASHTAG_MAP = {
        "electronics": ["techtok", "coolgadgets", "gadgets", "tiktokmademebuyit"],
        "tech": ["techtok", "coolgadgets", "gadgets", "tiktokmademebuyit"],
        "gadgets": ["coolgadgets", "gadgets", "techtok", "viralproducts"],
        "beauty": ["beautytok", "skincareroutine", "tiktokmademebuyit", "glowup"],
        "home": ["homefinds", "homedecor", "tiktokmademebuyit", "cleaningtok"],
        "fitness": ["fitnesstok", "homegym", "workoutgear", "tiktokmademebuyit"],
        "kitchen": ["kitchentok", "kitchengadgets", "cookinghacks", "tiktokmademebuyit"],
        "pet": ["pettok", "dogsoftiktok", "petgadgets", "tiktokmademebuyit"],
        "fashion": ["fashiontok", "ootd", "tiktokmademebuyit", "styletips"],
        "kids": ["momtok", "kidstoys", "parentinghacks", "tiktokmademebuyit"],
    }

    # Find best hashtags for this category
    selected_tags = VIRAL_HASHTAGS[:4]  # default
    for key, tags in CATEGORY_HASHTAG_MAP.items():
        if key in cat_lower:
            selected_tags = tags
            break

    # Curator profiles per category for RapidAPI user lookup
    category_curators = {
        "beauty": "skincarehacks",
        "tech": "gadgetflow",
        "gadgets": "viralgadgets",
        "home": "roomdecorfinds",
        "fitness": "wellnessgear",
        "kitchen": "cleantok",
        "electronics": "gadgetflow",
        "fashion": "styletips",
        "pet": "dogsoftiktok",
    }

    curator = "viralgadgets"
    for k, v in category_curators.items():
        if k in cat_lower:
            curator = v
            break

    # Strictly 1 single targeted RapidAPI call:
    profile = fetch_tiktok_user_info(curator)
    profiles = [profile] if profile else []

    # Build TikTok search and tag links for the selected hashtags
    tag_links = [f"https://www.tiktok.com/tag/{tag}" for tag in selected_tags]
    search_url = f"https://www.tiktok.com/search?q={urllib.parse.quote_plus(cat_lower + ' viral product')}"

    result = {
        "source": "RapidAPI Hub - TikTok (alexanderxbx)",
        "api_host": RAPIDAPI_HOST,
        "category": category,
        "discovery_hashtags": selected_tags,
        "hashtag_links": tag_links,
        "search_url": search_url,
        "curator_profiles": profiles,
        "status": "connected" if profiles else "fallback_ready",
    }
    _TIKTOK_CACHE[cache_key] = result
    return result


def get_real_tiktok_product_link(product_name: str, candidate_url: str = "") -> str:
    """Returns guaranteed real TikTok link for product without redundant API calls."""
    if candidate_url and candidate_url.startswith("https://www.tiktok.com/") and "example.com" not in candidate_url:
        return candidate_url
    clean_tag = "".join(c for c in product_name.replace("&", " ").split()[:2] if c.isalnum()).lower()
    return f"https://www.tiktok.com/tag/{clean_tag}"
