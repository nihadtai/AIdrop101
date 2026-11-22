import asyncio
import html as html_lib
import json
import logging
import re
import urllib.parse
from typing import Any, Callable, Dict, List, Optional
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from state.graph_state import ECommerceState
from agents.base import BaseAgent
from tools.design_intel import audit_storefront_html, resolve_store_design_system
from tools.product_image_search import search_product_images

logger = logging.getLogger(__name__)


class FAQItem(BaseModel):
    question: str = Field(description="Most common buyer question in persuasive English (US/EU DTC)")
    answer: str = Field(description="Clear, reassuring answer removing buyer objections in English")


class FeatureBenefit(BaseModel):
    title: str = Field(description="Concise benefit headline (no emojis, clean text, e.g. 'Ultra-Fast USB-C Charging')")
    description: str = Field(description="1-2 sentences explaining how this feature benefits the customer")
    icon_key: str = Field(
        description="Vector SVG icon key: 'zap', 'shield', 'sparkles', 'award', 'truck', 'clock', 'check', 'star', 'heart', 'layers'",
        default="sparkles",
    )
    icon: str = Field(description="Backward-compatibility field (same value as icon_key)", default="sparkles")


class CustomerReview(BaseModel):
    name: str = Field(description="Customer name & last initial (e.g. 'Sarah M.', 'James K.')")
    rating: int = Field(description="Star rating from 1 to 5", default=5)
    review_text: str = Field(description="Authentic, specific, persuasive verified buyer review in English")


class ProductSpecItem(BaseModel):
    label: str = Field(description="Technical specification label (e.g. 'Material', 'Battery Life', 'Dimensions', 'In The Box')")
    value: str = Field(description="Specific technical value for this product (e.g. 'Aerospace Aluminum Alloy', 'Up to 36 Hours')")


class StoreCopy(BaseModel):
    product_title: str = Field(description="Optimized, high-converting commercial product title in English")
    headline: str = Field(description="High-converting landing page hook headline in English (no emojis)")
    subheadline: str = Field(description="Compelling subheadline explaining core value & problem solved in English")
    description_aida: str = Field(description="Rich AIDA (Attention, Interest, Desire, Action) sales copy in English")
    features_benefits: List[FeatureBenefit] = Field(description="At least 4 key features and customer benefits in English")
    specifications: List[ProductSpecItem] = Field(
        description="4-5 product-specific technical specifications & package contents in English",
        default_factory=list,
    )
    faqs: List[FAQItem] = Field(description="At least 4 buyer FAQs in English (shipping, returns, warranty, usage)")
    trust_badges: List[str] = Field(
        description="Trust badges in English (no emojis, e.g. '30-Day Money-Back Guarantee', 'Free Tracked US/EU Shipping', '256-Bit SSL Encrypted Checkout')"
    )
    customer_reviews: List[CustomerReview] = Field(description="3 persuasive verified US/EU buyer reviews in English")
    urgency_text: str = Field(
        description="Limited stock / urgency banner text in English (no emojis)",
        default="Limited Time Offer: 50% OFF + Free Tracked US & EU Shipping Today!",
    )
    cta_button_text: str = Field(
        description="Primary checkout button text in English (no emojis)",
        default="CLAIM 50% OFF NOW — FAST TRACKED SHIPPING",
    )


# Unified Lucide-aligned inline vector SVG paths (stroke-width="1.75", zero structural emojis)
_SVG_PATHS: Dict[str, str] = {
    "zap": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path><path d="m9 12 2 2 4-4"></path>',
    "sparkles": '<path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"></path>',
    "award": '<circle cx="12" cy="8" r="6"></circle><path d="M15.477 12.89 17 22l-5-3-5 3 1.523-9.11"></path>',
    "truck": '<path d="M10 17h4V5H2v12h3"></path><path d="M20 17h2v-3.34a4 4 0 0 0-1.17-2.83L19 9h-5v8h1"></path><circle cx="7.5" cy="17.5" r="2.5"></circle><circle cx="17.5" cy="17.5" r="2.5"></circle>',
    "banknote": '<rect width="20" height="12" x="2" y="6" rx="2"></rect><circle cx="12" cy="12" r="2"></circle><path d="M6 12h.01M18 12h.01"></path>',
    "clock": '<circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline>',
    "check": '<polyline points="20 6 9 17 4 12"></polyline>',
    "check-circle": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>',
    "star": '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon>',
    "heart": '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"></path>',
    "layers": '<polygon points="12 2 2 7 12 12 22 7 12 2"></polygon><polyline points="2 17 12 22 22 17"></polyline><polyline points="2 12 12 17 22 12"></polyline>',
    "shopping-bag": '<path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4Z"></path><line x1="3" y1="6" x2="21" y2="6"></line><path d="M16 10a4 4 0 0 1-8 0"></path>',
    "lock": '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path>',
    "map-pin": '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"></path><circle cx="12" cy="10" r="3"></circle>',
    "thumbs-up": '<path d="M7 10v12"></path><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2h0a3.13 3.13 0 0 1 3 3.88Z"></path>',
    "chevron-down": '<polyline points="6 9 12 15 18 9"></polyline>',
    "camera": '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"></path><circle cx="12" cy="13" r="3"></circle>',
    "package": '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path><polyline points="3.27 6.96 12 12.01 20.73 6.96"></polyline><line x1="12" y1="22.08" x2="12" y2="12"></line>',
    "flame": '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"></path>',
}

_EMOJI_TO_KEY = {
    "⚡": "zap",
    "🔥": "flame",
    "🛡️": "shield",
    "✨": "sparkles",
    "💎": "award",
    "🏆": "award",
    "🚚": "truck",
    "💵": "banknote",
    "⏳": "clock",
    "✓": "check",
    "👍": "thumbs-up",
    "❤️": "heart",
    "📦": "package",
    "📸": "camera",
}

_STRIP_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u26FF]")


def _clean_text(val: Any) -> str:
    """Strips stray decorative emojis from dynamic LLM text before rendering structural UI."""
    cleaned = _STRIP_EMOJI_RE.sub("", str(val or ""))
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _resolve_icon_key(raw_key: Any, fallback: str = "sparkles") -> str:
    s = str(raw_key or "").strip()
    if s in _EMOJI_TO_KEY:
        return _EMOJI_TO_KEY[s]
    low = s.lower()
    if low in _SVG_PATHS:
        return low
    for k in _SVG_PATHS:
        if k in low:
            return k
    return fallback


def _svg_icon(name: str, cls: str = "w-4 h-4", filled: bool = False) -> str:
    body = _SVG_PATHS.get(name, _SVG_PATHS["sparkles"])
    fill_attr = "currentColor" if filled else "none"
    return (
        f'<svg class="{cls} shrink-0" viewBox="0 0 24 24" fill="{fill_attr}" '
        f'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{body}</svg>'
    )


def _build_store_svg_placeholder(title: str) -> str:
    """Builds a sleek inline SVG data URI placeholder (never an unrelated hardcoded watch photo)."""
    short_label = html_lib.escape((title or "Official Product")[:28])
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600" width="800" height="600">'
        '<defs><linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">'
        '<stop offset="0%" stop-color="#0f172a"/><stop offset="100%" stop-color="#1e293b"/>'
        '</linearGradient></defs>'
        '<rect width="800" height="600" fill="url(#g)"/>'
        '<rect x="260" y="140" width="280" height="220" rx="28" fill="none" stroke="#38bdf8" stroke-width="4" opacity="0.85"/>'
        '<circle cx="400" cy="250" r="54" fill="none" stroke="#38bdf8" stroke-width="4" opacity="0.65"/>'
        f'<text x="400" y="430" text-anchor="middle" fill="#f8fafc" font-family="system-ui,sans-serif" font-size="24" font-weight="700">{short_label}</text>'
        '<text x="400" y="468" text-anchor="middle" fill="#94a3b8" font-family="system-ui,sans-serif" font-size="15">Verified Studio Edition</text>'
        '</svg>'
    )
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg)


def _clean_aida_paragraphs(aida_text: str, title: str, subheadline: str) -> List[str]:
    """Cleans raw AIDA text from markdown/labels and returns structured paragraphs."""
    if not aida_text or not aida_text.strip():
        return [
            f"{title} is engineered from the ground up to eliminate everyday friction and deliver studio-grade performance right out of the box.",
            f"{subheadline} Built with precision-tested materials and smart ergonomics for effortless daily use at home, at work, or on the go.",
            "Join thousands of verified US and European customers who upgraded their daily routine. Order today to lock in 50% off plus free tracked delivery.",
        ]

    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", aida_text)
    cleaned = re.sub(r"^#+\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(
        r"^(Attention|Interest|Desire|Action|Diqqət|Maraq|Arzu|Hərəkət)\s*[:\-–—]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    cleaned = _STRIP_EMOJI_RE.sub("", cleaned)

    raw_parts = [p.strip() for p in re.split(r"\n\s*\n|\n", cleaned) if p.strip()]
    return raw_parts or [cleaned.strip()]


def build_wordpress_landing_page(
    copy_data: Dict[str, Any],
    product: Dict[str, Any],
    economics: Dict[str, Any],
    brand: Optional[Dict[str, Any]] = None,
    design_system: Optional[Dict[str, Any]] = None,
    store_config: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Generates a complete, production-ready, token-driven Global US/EU DTC WordPress / WooCommerce
    & Shopify E-Commerce Storefront & Single-Product Landing Page powered by UI/UX Pro Max:
    - Niche-adaptive Light/Dark palettes & WCAG >= 4.5:1 verified semantic CSS variables
    - Curated Google Fonts heading/body pairing
    - Crisp inline SVG vector icons (zero structural emojis)
    - Accessible native <dialog> checkout modal wired to POST /api/store/public-order + <details> FAQ
    - Tabular numerals & prefers-reduced-motion compliance
    """
    brand = brand or {}
    cfg = store_config if isinstance(store_config, dict) else (copy_data.get("store_config") or {})
    if not isinstance(cfg, dict):
        cfg = {}
    binding_sku_val = str(product.get("binding_sku") or cfg.get("binding_sku") or "").strip()
    binding_sku_escaped = html_lib.escape(binding_sku_val)
    sourcing_channel_escaped = html_lib.escape(str(product.get("sourcing_channel") or cfg.get("sourcing_channel") or "").strip())

    ds = (
        design_system
        or copy_data.get("design_system")
        or resolve_store_design_system(product=product, brand=brand, store_config=cfg)
    )
    colors = ds.get("colors") or {}
    typo = ds.get("typography") or {}
    style_meta = ds.get("style") or {}
    is_dark = bool(colors.get("is_dark"))

    c_primary = colors.get("primary", "#059669")
    c_on_primary = colors.get("on_primary", "#FFFFFF")
    c_secondary = colors.get("secondary", c_primary)
    c_accent = colors.get("accent", "#EA580C")
    c_on_accent = colors.get("on_accent", "#FFFFFF")
    c_bg = colors.get("background", "#0F172A" if is_dark else "#ECFDF5")
    c_fg = colors.get("foreground", "#F8FAFC" if is_dark else "#064E3B")
    c_card = colors.get("card", "#1E293B" if is_dark else "#FFFFFF")
    c_card_fg = colors.get("card_foreground", c_fg)
    c_muted = colors.get("muted", "#1E293B" if is_dark else "#E8F1F3")
    c_muted_fg = colors.get("muted_foreground", "#CBD5E1" if is_dark else "#475569")
    c_border = colors.get("border", "#334155" if is_dark else "#A7F3D0")
    c_ring = colors.get("ring", c_primary)
    c_surface_alt = colors.get("surface_alt", "#0B1120" if is_dark else "#FFFFFF")
    c_header_bg = colors.get("header_bg", "rgba(15, 23, 42, 0.90)" if is_dark else "rgba(255, 255, 255, 0.92)")

    heading_font = typo.get("heading", "Rubik")
    body_font = typo.get("body", "Nunito Sans")
    gf_url = typo.get("google_fonts_url") or (
        f"https://fonts.googleapis.com/css2?family={heading_font.replace(' ', '+')}:wght@600;700;800"
        f"&family={body_font.replace(' ', '+')}:wght@400;500;600;700&display=swap"
    )
    style_name_escaped = html_lib.escape(str(style_meta.get("name") or "Conversion Showcase"))

    raw_title = _clean_text(
        copy_data.get("product_title")
        or product.get("english_name")
        or product.get("name")
        or "Premium Smart Gadget"
    )
    title = html_lib.escape(raw_title)

    raw_brand_name = _clean_text(
        cfg.get("brand_name")
        or brand.get("brand_name")
        or brand.get("name")
        or product.get("brand_name")
        or (raw_title.split()[0] if raw_title.split() else "Apex") + "Labs"
    )
    brand_name = html_lib.escape(raw_brand_name)

    raw_logo_url = str(cfg.get("logo_url") or brand.get("logo_url") or "").strip()
    logo_url_escaped = html_lib.escape(raw_logo_url) if raw_logo_url else ""
    if logo_url_escaped:
        header_logo_html = (
            f'<img src="{logo_url_escaped}" alt="{brand_name}" '
            f'class="h-10 w-auto max-w-[160px] object-contain rounded-lg">'
        )
        footer_logo_html = (
            f'<div class="flex items-center gap-3 mb-3">'
            f'<img src="{logo_url_escaped}" alt="{brand_name}" class="h-9 w-auto max-w-[150px] object-contain rounded-lg bg-white/10 p-1">'
            f'<span class="text-white font-extrabold text-lg">{brand_name}</span></div>'
        )
    else:
        header_logo_html = (
            f'<div class="w-10 h-10 rounded-xl flex items-center justify-center font-extrabold text-lg" '
            f'style="background:var(--ds-primary); color:var(--ds-on-primary);">'
            f'{brand_name[:1].upper()}</div>'
        )
        footer_logo_html = f'<div class="text-white font-extrabold text-lg mb-2">{brand_name}</div>'

    headline = html_lib.escape(
        _clean_text(copy_data.get("headline") or f"Upgrade Your Daily Routine With The Original {raw_title}")
    )
    subheadline = html_lib.escape(
        _clean_text(
            copy_data.get("subheadline")
            or product.get("what_it_does")
            or "Precision-engineered performance, modern ergonomic design, and instant results from day one."
        )
    )
    cta_text = html_lib.escape(
        _clean_text(copy_data.get("cta_button_text") or "CLAIM 50% OFF NOW — FAST TRACKED SHIPPING")
    )
    urgency = html_lib.escape(
        _clean_text(copy_data.get("urgency_text") or "Flash Sale Live: 50% OFF + Free Tracked US & EU Shipping Today")
    )

    raw_delivery_days = str(product.get("delivery_days") or cfg.get("delivery_days") or "6-10 Business Days").strip()
    if "day" not in raw_delivery_days.lower():
        raw_delivery_days = f"{raw_delivery_days} Business Days"
    delivery_days_escaped = html_lib.escape(raw_delivery_days)

    default_fallback_img = _build_store_svg_placeholder(raw_title)
    gallery_images: List[str] = []
    for src_list in (
        cfg.get("product_images"),
        copy_data.get("product_images"),
        product.get("gallery_images"),
        product.get("images"),
    ):
        if isinstance(src_list, list):
            for u in src_list:
                if isinstance(u, str) and u.strip() and u.strip() not in gallery_images:
                    gallery_images.append(u.strip())
    single_primary_img = str(
        product.get("image_url") or product.get("product_image_url") or default_fallback_img
    ).strip()
    if single_primary_img and single_primary_img not in gallery_images:
        gallery_images.insert(0, single_primary_img)
    if not gallery_images:
        gallery_images = [default_fallback_img]

    img_url = gallery_images[0]
    img_url_escaped = html_lib.escape(img_url)
    has_multi_gallery = len(gallery_images) >= 2

    try:
        raw_sp = cfg.get("selling_price") if cfg.get("selling_price") not in (None, "") else economics.get("selling_price")
        selling_price = round(float(raw_sp or 39.99), 2)
    except Exception:
        selling_price = 39.99

    try:
        raw_cp = cfg.get("compare_at_price")
        regular_price = round(float(raw_cp), 2) if raw_cp not in (None, "") and float(raw_cp) > selling_price else round(selling_price * 2.0, 2)
    except Exception:
        regular_price = round(selling_price * 2.0, 2)
    savings_amount = round(regular_price - selling_price, 2)
    discount_pct = max(5, int(round(((regular_price - selling_price) / regular_price) * 100))) if regular_price > selling_price else 50

    bundle2_price = round(selling_price * 2 * 0.85, 2)
    bundle2_regular = round(regular_price * 2, 2)
    bundle2_save = round(bundle2_regular - bundle2_price, 2)

    bundle3_price = round(selling_price * 3 * 0.75, 2)
    bundle3_regular = round(regular_price * 3, 2)
    bundle3_save = round(bundle3_regular - bundle3_price, 2)

    if has_multi_gallery:
        gallery_captions = [
            "Studio Front Angle",
            "Close-Up Detail View",
            "In-Use Lifestyle Shot",
            "Complete Package & Accessories",
            "Angle View 5",
            "Angle View 6",
        ]
        thumbs_list = []
        for idx, g_url in enumerate(gallery_images[:6]):
            g_esc = html_lib.escape(g_url)
            cap = gallery_captions[idx] if idx < len(gallery_captions) else f"View {idx + 1}"
            active_cls = "thumb-active " if idx == 0 else ""
            thumbs_list.append(
                f'<button type="button" data-gallery-idx="{idx}" data-img-src="{g_esc}" '
                f'data-caption="{cap}" data-scale="scale-100" id="thumb-{idx}" aria-label="{cap}" '
                f'class="{active_cls}relative rounded-xl overflow-hidden border-2 aspect-square transition-all cursor-pointer" '
                f'style="border-color:var(--ds-border); background:var(--ds-muted);">'
                f'<img src="{g_esc}" alt="{cap}" width="240" height="240" class="w-full h-full object-cover" '
                f'onerror="this.src=\'{default_fallback_img}\'">'
                f'</button>'
            )
        gallery_thumbs_html = "\n                        ".join(thumbs_list)
        gallery_cols_cls = "grid-cols-4 sm:grid-cols-6" if len(gallery_images[:6]) > 4 else "grid-cols-4"
        thumb_count_js = len(gallery_images[:6])
    else:
        gallery_thumbs_html = (
            f'<button type="button" data-gallery-idx="0" data-img-src="{img_url_escaped}" data-caption="Studio Front Angle" data-scale="scale-100" id="thumb-0" aria-label="Primary view" class="thumb-active relative rounded-xl overflow-hidden border-2 aspect-square transition-all cursor-pointer" style="border-color:var(--ds-border); background:var(--ds-muted);">\n'
            f'                            <img src="{img_url_escaped}" alt="View 1" width="240" height="240" class="w-full h-full object-cover" onerror="this.src=\'{default_fallback_img}\'">\n'
            f'                        </button>\n'
            f'                        <button type="button" data-gallery-idx="1" data-img-src="{img_url_escaped}" data-caption="Close-Up Precision Detail" data-scale="scale-125 origin-center" id="thumb-1" aria-label="Detail view" class="relative rounded-xl overflow-hidden border-2 aspect-square transition-all cursor-pointer" style="border-color:var(--ds-border); background:var(--ds-muted);">\n'
            f'                            <img src="{img_url_escaped}" alt="View 2" width="240" height="240" class="w-full h-full object-cover scale-125" onerror="this.src=\'{default_fallback_img}\'">\n'
            f'                        </button>\n'
            f'                        <button type="button" data-gallery-idx="2" data-img-src="{img_url_escaped}" data-caption="Premium Build & Finish" data-scale="scale-110 origin-top" id="thumb-2" aria-label="Build view" class="relative rounded-xl overflow-hidden border-2 aspect-square transition-all cursor-pointer" style="border-color:var(--ds-border); background:var(--ds-muted);">\n'
            f'                            <img src="{img_url_escaped}" alt="View 3" width="240" height="240" class="w-full h-full object-cover scale-110 origin-top" onerror="this.src=\'{default_fallback_img}\'">\n'
            f'                        </button>\n'
            f'                        <button type="button" data-gallery-idx="3" data-img-src="{img_url_escaped}" data-caption="Retail Packaging & Kit" data-scale="scale-105 origin-bottom" id="thumb-3" aria-label="Package view" class="relative rounded-xl overflow-hidden border-2 aspect-square transition-all cursor-pointer" style="border-color:var(--ds-border); background:var(--ds-muted);">\n'
            f'                            <img src="{img_url_escaped}" alt="View 4" width="240" height="240" class="w-full h-full object-cover scale-105 origin-bottom" onerror="this.src=\'{default_fallback_img}\'">\n'
            f'                        </button>'
        )
        gallery_cols_cls = "grid-cols-4"
        thumb_count_js = 4

    # Benefits cards with unified SVG icons
    benefits = copy_data.get("features_benefits") or []
    default_keys = ["zap", "shield", "sparkles", "award"]
    if not benefits or len(benefits) < 3:
        benefits = [
            {
                "icon_key": "zap",
                "title": "Instant High-Efficiency Performance",
                "description": "Engineered to deliver noticeable results from the very first use while saving you time every day.",
            },
            {
                "icon_key": "shield",
                "title": "Certified Studio-Grade Build",
                "description": "Crafted from durable, skin-safe, and precision-tested materials built for years of reliable operation.",
            },
            {
                "icon_key": "sparkles",
                "title": "Compact & Travel-Ready Ergonomics",
                "description": "Lightweight, portable design fits effortlessly into your home, office desk, or carry-on bag.",
            },
            {
                "icon_key": "award",
                "title": "Backed By Our 30-Day Guarantee",
                "description": "Try it risk-free for 30 days with hassle-free returns and dedicated US & EU customer support.",
            },
        ]

    benefits_html = ""
    for idx, b in enumerate(benefits[:4]):
        raw_b_title = b.get("title", "") if isinstance(b, dict) else getattr(b, "title", "")
        raw_b_desc = b.get("description", "") if isinstance(b, dict) else getattr(b, "description", "")
        raw_b_icon = (
            (b.get("icon_key") or b.get("icon"))
            if isinstance(b, dict)
            else (getattr(b, "icon_key", None) or getattr(b, "icon", None))
        )
        icon_key = _resolve_icon_key(raw_b_icon, fallback=default_keys[idx % len(default_keys)])
        b_title = html_lib.escape(_clean_text(raw_b_title))
        b_desc = html_lib.escape(_clean_text(raw_b_desc))
        benefits_html += f"""
        <article class="ds-card rounded-2xl p-6 transition-all duration-200 hover:-translate-y-0.5 flex flex-col justify-between">
            <div>
                <div class="w-11 h-11 rounded-xl flex items-center justify-center mb-4 border" style="background:var(--ds-muted); border-color:var(--ds-border); color:var(--ds-primary);">
                    {_svg_icon(icon_key, "w-5 h-5")}
                </div>
                <h3 class="text-lg font-bold mb-2">{b_title}</h3>
                <p class="text-sm leading-relaxed ds-muted-text">{b_desc}</p>
            </div>
            <div class="mt-4 pt-3 border-t flex items-center text-xs font-semibold gap-1.5" style="border-color:var(--ds-border); color:var(--ds-primary);">
                {_svg_icon("check-circle", "w-4 h-4")}
                <span>Verified Advantage</span>
            </div>
        </article>
        """

    # Clean AIDA storytelling blocks
    aida_paragraphs = _clean_aida_paragraphs(
        str(copy_data.get("description_aida") or ""), raw_title, subheadline
    )
    aida_stages = [
        ("flame", "Why It Matters"),
        ("zap", "How It Solves The Problem"),
        ("sparkles", "Built For Everyday Excellence"),
        ("shield", "Risk-Free 30-Day Trial"),
    ]
    aida_html = ""
    for idx, para in enumerate(aida_paragraphs[:4]):
        st_icon, st_label = aida_stages[idx % len(aida_stages)]
        aida_html += f"""
        <div class="ds-card rounded-2xl p-5 sm:p-6">
            <span class="inline-flex items-center gap-1.5 text-xs font-bold px-3 py-1 rounded-full border mb-3" style="background:var(--ds-muted); border-color:var(--ds-border); color:var(--ds-primary);">
                {_svg_icon(st_icon, "w-3.5 h-3.5")}
                {st_label}
            </span>
            <p class="text-base leading-relaxed ds-muted-text">{html_lib.escape(para)}</p>
        </div>
        """

    # Product-specific Technical Specifications
    raw_specs = copy_data.get("specifications") or []
    specs_items: List[Dict[str, str]] = []
    for sp in raw_specs[:5]:
        lbl = _clean_text(sp.get("label", "") if isinstance(sp, dict) else getattr(sp, "label", ""))
        val = _clean_text(sp.get("value", "") if isinstance(sp, dict) else getattr(sp, "value", ""))
        if lbl and val:
            specs_items.append({"label": lbl, "value": val})
    if len(specs_items) < 3:
        specs_items = [
            {"label": "Model Edition", "value": f"{raw_title[:32]} (2026 Pro Release)"},
            {"label": "Build & Certification", "value": "CE / FCC / RoHS Certified Grade-A Materials"},
            {"label": "Power & Connectivity", "value": "Universal USB-C Fast Charge / Smart Auto-Control"},
            {"label": "Tracked Shipping", "value": f"{raw_delivery_days} (US, UK, EU, CA)"},
            {"label": "In The Box", "value": f"1x {raw_title[:24]}, Charging Cable, User Manual"},
        ]
    specs_html = ""
    for idx, sp in enumerate(specs_items[:5]):
        border_cls = " border-b" if idx < len(specs_items[:5]) - 1 else ""
        specs_html += f"""
                            <li class="flex items-center justify-between py-2{border_cls} gap-3" style="border-color:var(--ds-border);">
                                <span class="ds-muted-text">{html_lib.escape(sp['label'])}:</span>
                                <span class="font-bold text-right">{html_lib.escape(sp['value'])}</span>
                            </li>"""

    # Verified Customer Reviews
    reviews = copy_data.get("customer_reviews") or []
    if not reviews:
        reviews = [
            {
                "name": "Sarah M.",
                "rating": 5,
                "review_text": "Hands down the best purchase I've made this year. Arrived in 7 days with full tracking, and the build quality feels way more expensive than the price tag.",
            },
            {
                "name": "James K.",
                "rating": 5,
                "review_text": "I was skeptical after seeing it on TikTok, but it works exactly as advertised right out of the box. Grabbed the 2-pack so I could keep one at my office.",
            },
            {
                "name": "Elena R.",
                "rating": 5,
                "review_text": "Super sleek design and genuinely solves a daily headache. Packaging was factory-sealed and customer service sent my tracking link within 24 hours.",
            },
        ]

    reviews_html = ""
    review_dates = ["2 days ago", "5 days ago", "1 week ago"]
    review_cities = ["Austin, TX", "London, UK", "Berlin, DE"]
    for idx, r in enumerate(reviews[:3]):
        r_name = html_lib.escape(_clean_text(r.get("name", "Verified Customer") if isinstance(r, dict) else getattr(r, "name", "Verified Customer")))
        r_rating = int(r.get("rating", 5) if isinstance(r, dict) else getattr(r, "rating", 5))
        r_text = html_lib.escape(_clean_text(r.get("review_text", "") if isinstance(r, dict) else getattr(r, "review_text", "")))
        initials = "".join([part[0].upper() for part in r_name.split()[:2] if part]) or "V"
        stars_svg = "".join([_svg_icon("star", "w-4 h-4 text-amber-400", filled=True) for _ in range(max(1, min(5, r_rating)))])
        reviews_html += f"""
        <article class="ds-card rounded-2xl p-6 flex flex-col justify-between">
            <div>
                <div class="flex items-center justify-between mb-4">
                    <div class="flex items-center gap-3">
                        <div class="w-10 h-10 rounded-full font-bold text-sm flex items-center justify-center shrink-0" style="background:var(--ds-primary); color:var(--ds-on-primary);">
                            {initials}
                        </div>
                        <div>
                            <div class="font-bold text-sm flex items-center gap-1.5">
                                <span>{r_name}</span>
                                <span class="text-xs font-medium ds-muted-text">• {review_cities[idx % len(review_cities)]}</span>
                            </div>
                            <div class="text-[11px] font-semibold flex items-center gap-1" style="color:var(--ds-primary);">
                                {_svg_icon("check-circle", "w-3.5 h-3.5")}
                                <span>Verified Buyer</span>
                            </div>
                        </div>
                    </div>
                    <span class="text-xs ds-muted-text">{review_dates[idx % len(review_dates)]}</span>
                </div>
                <div class="flex items-center gap-0.5 mb-3" aria-label="{r_rating} out of 5 stars">
                    {stars_svg}
                </div>
                <p class="text-sm leading-relaxed mb-4 ds-muted-text">"{r_text}"</p>
            </div>
            <div class="pt-3 border-t flex items-center justify-between text-xs ds-muted-text" style="border-color:var(--ds-border);">
                <span class="truncate pr-2">Purchased: {title[:28]}</span>
                <span class="inline-flex items-center gap-1 font-medium shrink-0">{_svg_icon("thumbs-up", "w-3.5 h-3.5")} Helpful</span>
            </div>
        </article>
        """

    # Native Accessible <details> FAQ Accordion
    faqs = copy_data.get("faqs") or []
    if not faqs:
        faqs = [
            {
                "question": "How long does shipping take to the US, UK, and Europe?",
                "answer": f"Orders are processed within 24 hours and delivered via tracked express air freight ({raw_delivery_days}) with real-time tracking updates sent straight to your email.",
            },
            {
                "question": "What payment methods do you accept?",
                "answer": "We accept all major Credit & Debit Cards (Visa, Mastercard, Amex), Apple Pay, Google Pay, and PayPal Express through our 256-bit SSL encrypted checkout.",
            },
            {
                "question": "What if I'm not 100% satisfied with my order?",
                "answer": "Every order is backed by our 30-Day Money-Back Guarantee. If you aren't thrilled with your purchase for any reason, contact our support team for a full replacement or 100% refund.",
            },
            {
                "question": "Can I save more if I order multiple units for family or gifts?",
                "answer": "Yes! Choose our 2-Pack (Extra 15% OFF) or 3-Pack Best Value Bundle (Extra 25% OFF) directly above the checkout button to unlock instant bundle savings.",
            },
        ]

    faqs_html = ""
    for idx, f in enumerate(faqs):
        f_q = html_lib.escape(_clean_text(f.get("question", "") if isinstance(f, dict) else getattr(f, "question", "")))
        f_a = html_lib.escape(_clean_text(f.get("answer", "") if isinstance(f, dict) else getattr(f, "answer", "")))
        open_attr = " open" if idx == 0 else ""
        faqs_html += f"""
        <details name="store-faq" class="ds-card rounded-2xl overflow-hidden transition-colors"{open_attr}>
            <summary class="px-6 py-4 flex justify-between items-center font-bold text-sm sm:text-base cursor-pointer list-none min-h-[44px]">
                <span class="pr-4">{f_q}</span>
                <span class="faq-chevron w-7 h-7 rounded-full flex items-center justify-center shrink-0 transition-transform duration-200" style="background:var(--ds-muted); color:var(--ds-primary);">
                    {_svg_icon("chevron-down", "w-4 h-4")}
                </span>
            </summary>
            <div class="px-6 pb-5 pt-2 text-sm leading-relaxed border-t ds-muted-text" style="border-color:var(--ds-border);">
                {f_a}
            </div>
        </details>
        """

    badges = copy_data.get("trust_badges") or [
        "30-Day Money-Back Guarantee",
        f"Free Tracked Shipping ({raw_delivery_days})",
        "256-Bit SSL Encrypted Checkout",
        "CE & FCC Certified Original Quality",
    ]
    badges_html = "".join(
        [
            f'<span class="inline-flex items-center gap-1.5 border text-xs font-semibold px-3 py-1.5 rounded-full" style="background:var(--ds-muted); border-color:var(--ds-border); color:var(--ds-fg);">'
            f'<span style="color:var(--ds-primary);">{_svg_icon("check", "w-3.5 h-3.5")}</span>'
            f'{html_lib.escape(_clean_text(b))}</span>'
            for b in badges
        ]
    )

    color_scheme_meta = "dark" if is_dark else "light"
    bundle_active_bg = "rgba(255, 255, 255, 0.06)" if is_dark else "rgba(5, 150, 105, 0.06)"
    positive_tint_bg = "rgba(16, 185, 129, 0.14)" if is_dark else "rgba(5, 150, 105, 0.08)"

    html_code = f"""<!DOCTYPE html>
<html lang="en" class="scroll-smooth">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="color-scheme" content="{color_scheme_meta}">
    <title>{title} — {brand_name} Official Online Store</title>
    <meta name="description" content="{subheadline}">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="{html_lib.escape(gf_url)}" rel="stylesheet">
    <style>
        :root {{
            --ds-primary: {c_primary};
            --ds-on-primary: {c_on_primary};
            --ds-secondary: {c_secondary};
            --ds-accent: {c_accent};
            --ds-on-accent: {c_on_accent};
            --ds-bg: {c_bg};
            --ds-fg: {c_fg};
            --ds-card: {c_card};
            --ds-card-fg: {c_card_fg};
            --ds-muted: {c_muted};
            --ds-muted-fg: {c_muted_fg};
            --ds-border: {c_border};
            --ds-ring: {c_ring};
            --ds-surface-alt: {c_surface_alt};
            --ds-header-bg: {c_header_bg};
            --ds-font-heading: '{html_lib.escape(heading_font)}', system-ui, sans-serif;
            --ds-font-body: '{html_lib.escape(body_font)}', system-ui, sans-serif;
        }}
        body {{
            background-color: var(--ds-bg);
            color: var(--ds-fg);
            font-family: var(--ds-font-body);
        }}
        h1, h2, h3, h4 {{
            font-family: var(--ds-font-heading);
        }}
        .ds-card {{
            background-color: var(--ds-card);
            color: var(--ds-card-fg);
            border: 1px solid var(--ds-border);
            box-shadow: 0 4px 20px rgba(0, 0, 0, {"0.28" if is_dark else "0.04"});
        }}
        .ds-surface-alt {{
            background-color: var(--ds-surface-alt);
            border-color: var(--ds-border);
        }}
        .ds-muted-text {{
            color: var(--ds-muted-fg);
        }}
        .ds-btn-accent {{
            background-color: var(--ds-accent);
            color: var(--ds-on-accent);
        }}
        .ds-btn-primary {{
            background-color: var(--ds-primary);
            color: var(--ds-on-primary);
        }}
        .thumb-active {{
            border-color: var(--ds-primary) !important;
            box-shadow: 0 0 0 2px var(--ds-ring);
        }}
        .bundle-active {{
            border-color: var(--ds-primary) !important;
            background-color: {bundle_active_bg} !important;
        }}
        .tabular-nums {{
            font-variant-numeric: tabular-nums;
        }}
        summary::-webkit-details-marker {{ display: none; }}
        details[open] .faq-chevron {{ transform: rotate(180deg); }}
        :focus-visible {{
            outline: 2px solid var(--ds-ring);
            outline-offset: 2px;
        }}
        dialog:modal {{
            overscroll-behavior: contain;
        }}
        dialog::backdrop {{
            background: rgba(2, 6, 23, 0.72);
            backdrop-filter: blur(4px);
            overflow: hidden;
            overscroll-behavior: contain;
        }}
        @media (prefers-reduced-motion: reduce) {{
            *, *::before, *::after {{
                animation-duration: 0.01ms !important;
                animation-iteration-count: 1 !important;
                transition-duration: 0.01ms !important;
                scroll-behavior: auto !important;
            }}
        }}
    </style>
</head>
<body class="antialiased pb-20 sm:pb-0">
<!-- UI/UX Pro Max Engine | Style: {style_name_escaped} | Mode: {color_scheme_meta} | Fonts: {html_lib.escape(heading_font)} / {html_lib.escape(body_font)} -->

<div class="wp-ecommerce-store min-h-screen flex flex-col">

    <!-- 1. TOP ANNOUNCEMENT BAR -->
    <div class="py-2.5 px-4 border-b text-xs sm:text-sm" style="background:var(--ds-primary); color:var(--ds-on-primary); border-color:var(--ds-border);">
        <div class="max-w-7xl mx-auto flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center gap-2 font-bold mx-auto sm:mx-0">
                <span class="inline-block w-2 h-2 rounded-full bg-current animate-ping motion-reduce:animate-none"></span>
                <span>{urgency}</span>
            </div>
            <div class="hidden md:flex items-center gap-5 text-xs font-semibold opacity-95">
                <span class="inline-flex items-center gap-1.5">{_svg_icon("truck", "w-3.5 h-3.5")} Tracked Shipping ({delivery_days_escaped})</span>
                <span>•</span>
                <span class="inline-flex items-center gap-1.5">{_svg_icon("lock", "w-3.5 h-3.5")} Apple Pay / PayPal / Card</span>
                <span>•</span>
                <span class="inline-flex items-center gap-1.5">{_svg_icon("shield", "w-3.5 h-3.5")} 30-Day Money-Back Guarantee</span>
            </div>
        </div>
    </div>

    <!-- 2. STICKY STORE HEADER & NAVIGATION -->
    <header class="sticky top-0 z-40 backdrop-blur-md border-b" style="background:var(--ds-header-bg); border-color:var(--ds-border);">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 sm:h-20 flex items-center justify-between gap-4">
            <a href="#" class="flex items-center gap-2.5">
                {header_logo_html}
                <div>
                    <span class="text-lg sm:text-xl font-extrabold tracking-tight block">{brand_name}</span>
                    <span class="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-wider -mt-0.5" style="color:var(--ds-primary);">
                        {_svg_icon("check", "w-3 h-3")} Official DTC Store
                    </span>
                </div>
            </a>

            <nav class="hidden lg:flex items-center gap-8 text-sm font-semibold ds-muted-text" aria-label="Store navigation">
                <a href="#mehsul" class="hover:opacity-80 transition-opacity">Overview</a>
                <a href="#ustunlukler" class="hover:opacity-80 transition-opacity">Features</a>
                <a href="#haqqinda" class="hover:opacity-80 transition-opacity">Specifications</a>
                <a href="#muqayise" class="hover:opacity-80 transition-opacity">Why Us</a>
                <a href="#reyler" class="hover:opacity-80 transition-opacity inline-flex items-center gap-1">
                    Reviews
                    <span class="inline-flex items-center gap-0.5 text-[11px] font-bold px-2 py-0.5 rounded-full tabular-nums" style="background:var(--ds-muted); color:var(--ds-fg);">
                        4.9 {_svg_icon("star", "w-3 h-3 text-amber-400", filled=True)}
                    </span>
                </a>
                <a href="#faq" class="hover:opacity-80 transition-opacity">FAQ</a>
            </nav>

            <div class="flex items-center gap-3">
                <button type="button" data-open-order class="ds-btn-accent min-h-[44px] inline-flex items-center gap-2 text-xs sm:text-sm font-bold px-4 sm:px-5 py-2.5 rounded-xl shadow-md transition-opacity hover:opacity-95 cursor-pointer">
                    {_svg_icon("shopping-bag", "w-4 h-4")}
                    <span>Order Now</span>
                    <span id="header-price-pill" class="px-2 py-0.5 rounded-md text-xs font-extrabold tabular-nums" style="background:rgba(0,0,0,0.16);">${selling_price}</span>
                </button>
            </div>
        </div>
    </header>

    <!-- 3. HERO PRODUCT SHOWCASE -->
    <main id="mehsul" class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pt-6 sm:pt-10 pb-16 w-full">
        <nav class="flex items-center gap-2 text-xs ds-muted-text mb-6 overflow-x-auto whitespace-nowrap" aria-label="Breadcrumb">
            <a href="#" class="hover:underline">Home</a>
            <span>/</span>
            <a href="#ustunlukler" class="hover:underline">Featured Releases</a>
            <span>/</span>
            <span class="font-semibold truncate" style="color:var(--ds-fg);">{title}</span>
        </nav>

        <div class="grid grid-cols-1 lg:grid-cols-12 gap-8 lg:gap-12 items-start">

            <!-- LEFT COLUMN: INTERACTIVE PRODUCT GALLERY -->
            <div class="lg:col-span-7 lg:sticky lg:top-24">
                <div class="ds-card rounded-3xl p-4 sm:p-6">
                    <div class="relative rounded-2xl overflow-hidden aspect-square sm:aspect-[4/3] flex items-center justify-center group" style="background:var(--ds-muted);">
                        <img id="main-product-image"
                             src="{img_url_escaped}"
                             alt="{title}"
                             fetchpriority="high"
                             width="1000"
                             height="750"
                             class="w-full h-full object-cover object-center transition-transform duration-500 motion-reduce:transition-none group-hover:scale-105"
                             onerror="this.src='{default_fallback_img}'">

                        <div class="absolute top-4 left-4 flex flex-col gap-2">
                            <span class="ds-btn-accent text-xs font-extrabold uppercase px-3 py-1.5 rounded-lg shadow-md tabular-nums">
                                SAVE {discount_pct}%
                            </span>
                            <span class="bg-slate-900/90 backdrop-blur-sm text-white text-xs font-bold px-3 py-1.5 rounded-lg shadow-md inline-flex items-center gap-1.5">
                                {_svg_icon("award", "w-3.5 h-3.5 text-amber-400")}
                                2026 Viral Bestseller
                            </span>
                        </div>

                        <div class="absolute top-4 right-4 ds-card text-xs font-bold px-3 py-1.5 rounded-full inline-flex items-center gap-1.5">
                            <span style="color:var(--ds-primary);">{_svg_icon("check-circle", "w-3.5 h-3.5")}</span>
                            100% Authentic
                        </div>

                        <div class="absolute bottom-4 left-4 bg-slate-900/80 backdrop-blur-sm text-white text-xs font-medium px-3 py-1.5 rounded-lg inline-flex items-center gap-1.5">
                            {_svg_icon("camera", "w-3.5 h-3.5")}
                            <span id="gallery-caption">Studio Front Angle</span>
                        </div>
                    </div>

                    <div class="grid {gallery_cols_cls} gap-3 mt-4">
                        {gallery_thumbs_html}
                    </div>

                    <div class="grid grid-cols-3 gap-3 mt-5 pt-5 border-t text-center" style="border-color:var(--ds-border);">
                        <div class="p-3 rounded-xl" style="background:var(--ds-muted);">
                            <div class="flex justify-center mb-1.5" style="color:var(--ds-primary);">{_svg_icon("truck", "w-5 h-5")}</div>
                            <div class="text-xs font-bold">Tracked Air Freight</div>
                            <div class="text-[11px] ds-muted-text">{delivery_days_escaped}</div>
                        </div>
                        <div class="p-3 rounded-xl" style="background:var(--ds-muted);">
                            <div class="flex justify-center mb-1.5" style="color:var(--ds-primary);">{_svg_icon("lock", "w-5 h-5")}</div>
                            <div class="text-xs font-bold">Encrypted Checkout</div>
                            <div class="text-[11px] ds-muted-text">Card • Apple Pay • PayPal</div>
                        </div>
                        <div class="p-3 rounded-xl" style="background:var(--ds-muted);">
                            <div class="flex justify-center mb-1.5" style="color:var(--ds-primary);">{_svg_icon("shield", "w-5 h-5")}</div>
                            <div class="text-xs font-bold">30-Day Guarantee</div>
                            <div class="text-[11px] ds-muted-text">100% Money-Back Promise</div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- RIGHT COLUMN: BUY BOX & CONVERSION ENGINE -->
            <div class="lg:col-span-5 flex flex-col space-y-5">
                <div class="flex flex-wrap items-center gap-2">
                    <div class="flex items-center gap-0.5 text-amber-400" aria-label="4.9 out of 5 stars">
                        {_svg_icon("star", "w-4 h-4", filled=True)}{_svg_icon("star", "w-4 h-4", filled=True)}{_svg_icon("star", "w-4 h-4", filled=True)}{_svg_icon("star", "w-4 h-4", filled=True)}{_svg_icon("star", "w-4 h-4", filled=True)}
                    </div>
                    <span class="text-sm font-extrabold tabular-nums">4.9</span>
                    <a href="#reyler" class="text-xs font-semibold hover:underline" style="color:var(--ds-primary);">(2,450+ Verified Reviews)</a>
                    <span class="text-xs font-bold px-2.5 py-0.5 rounded-full border" style="background:{positive_tint_bg}; border-color:var(--ds-border); color:var(--ds-primary);">In Stock &amp; Ready To Ship</span>
                </div>

                <div>
                    <h1 class="text-2xl sm:text-3xl font-extrabold tracking-tight leading-snug mb-2">
                        {title}
                    </h1>
                    <p class="font-bold text-sm sm:text-base mb-1.5" style="color:var(--ds-primary);">
                        {headline}
                    </p>
                    <p class="text-sm leading-relaxed ds-muted-text">
                        {subheadline}
                    </p>
                </div>

                <!-- Dynamic Price Card -->
                <div class="ds-card rounded-2xl p-4 sm:p-5">
                    <div class="flex items-center justify-between flex-wrap gap-2">
                        <div>
                            <span class="text-xs font-bold uppercase tracking-wider ds-muted-text block mb-1">Direct-To-Consumer Special Price</span>
                            <div class="flex items-baseline gap-3">
                                <span id="display-selling-price" class="text-3xl sm:text-4xl font-extrabold tabular-nums">${selling_price}</span>
                                <span id="display-regular-price" class="text-lg line-through font-semibold ds-muted-text tabular-nums">${regular_price}</span>
                            </div>
                        </div>
                        <div class="text-right">
                            <span id="display-savings-badge" class="inline-block border text-xs font-extrabold px-3 py-1.5 rounded-full tabular-nums" style="background:{positive_tint_bg}; border-color:var(--ds-border); color:var(--ds-primary);">
                                You Save: ${savings_amount} ({discount_pct}%)
                            </span>
                            <span class="block text-[11px] ds-muted-text mt-1">Taxes Included + Free Tracked Shipping</span>
                        </div>
                    </div>
                </div>

                <!-- INTERACTIVE BUNDLE SELECTOR -->
                <div class="ds-card rounded-2xl p-4 sm:p-5">
                    <div class="flex items-center justify-between mb-3">
                        <span class="inline-flex items-center gap-1.5 text-xs font-extrabold uppercase tracking-wider">
                            {_svg_icon("package", "w-4 h-4")} Select Bundle &amp; Save More:
                        </span>
                        <span class="text-xs font-semibold" style="color:var(--ds-primary);">Free Express Shipping</span>
                    </div>

                    <div class="space-y-2.5">
                        <label id="bundle-card-1" data-qty="1" data-price="{selling_price}" data-regular="{regular_price}" data-label="1x Unit — Standard Pack"
                               class="bundle-active cursor-pointer border-2 rounded-xl p-3.5 transition-all flex items-center justify-between min-h-[44px]" style="border-color:var(--ds-border);">
                            <div class="flex items-center gap-3">
                                <input type="radio" name="bundle_tier" id="bundle-radio-1" checked class="w-4 h-4" style="accent-color:var(--ds-primary);">
                                <div>
                                    <div class="font-bold text-sm">1x Unit — Standard Starter Pack</div>
                                    <div class="text-xs ds-muted-text">Includes {discount_pct}% Launch Discount</div>
                                </div>
                            </div>
                            <div class="text-right tabular-nums">
                                <div class="font-extrabold text-base">${selling_price}</div>
                                <div class="text-xs ds-muted-text line-through">${regular_price}</div>
                            </div>
                        </label>

                        <label id="bundle-card-2" data-qty="2" data-price="{bundle2_price}" data-regular="{bundle2_regular}" data-label="2x Units — Duo Pack (Extra 15% OFF)"
                               class="cursor-pointer border-2 rounded-xl p-3.5 transition-all flex items-center justify-between relative min-h-[44px]" style="border-color:var(--ds-border);">
                            <span class="ds-btn-primary absolute -top-2.5 right-3 text-[10px] font-extrabold uppercase px-2.5 py-0.5 rounded-full shadow-sm">
                                MOST POPULAR (+15% OFF)
                            </span>
                            <div class="flex items-center gap-3">
                                <input type="radio" name="bundle_tier" id="bundle-radio-2" class="w-4 h-4" style="accent-color:var(--ds-primary);">
                                <div>
                                    <div class="font-bold text-sm">2x Units — Duo &amp; Gift Bundle</div>
                                    <div class="text-xs font-semibold tabular-nums" style="color:var(--ds-primary);">Save ${bundle2_save} + Priority Dispatch</div>
                                </div>
                            </div>
                            <div class="text-right tabular-nums">
                                <div class="font-extrabold text-base" style="color:var(--ds-primary);">${bundle2_price}</div>
                                <div class="text-xs ds-muted-text line-through">${bundle2_regular}</div>
                            </div>
                        </label>

                        <label id="bundle-card-3" data-qty="3" data-price="{bundle3_price}" data-regular="{bundle3_regular}" data-label="3x Units — Family Pack (Extra 25% OFF)"
                               class="cursor-pointer border-2 rounded-xl p-3.5 transition-all flex items-center justify-between relative min-h-[44px]" style="border-color:var(--ds-border);">
                            <span class="ds-btn-accent absolute -top-2.5 right-3 text-[10px] font-extrabold uppercase px-2.5 py-0.5 rounded-full shadow-sm">
                                BEST VALUE (+25% OFF)
                            </span>
                            <div class="flex items-center gap-3">
                                <input type="radio" name="bundle_tier" id="bundle-radio-3" class="w-4 h-4" style="accent-color:var(--ds-primary);">
                                <div>
                                    <div class="font-bold text-sm">3x Units — Ultimate Family Bundle</div>
                                    <div class="text-xs font-semibold tabular-nums" style="color:var(--ds-primary);">Save ${bundle3_save} + VIP Insured Shipping</div>
                                </div>
                            </div>
                            <div class="text-right tabular-nums">
                                <div class="font-extrabold text-base" style="color:var(--ds-primary);">${bundle3_price}</div>
                                <div class="text-xs ds-muted-text line-through">${bundle3_regular}</div>
                            </div>
                        </label>
                    </div>
                </div>

                <!-- Live Stock & Countdown Bar -->
                <div class="ds-card rounded-2xl p-4 space-y-2">
                    <div class="flex items-center justify-between text-xs font-bold">
                        <span class="flex items-center gap-2">
                            <span class="inline-block w-2 h-2 rounded-full bg-rose-500 animate-ping motion-reduce:animate-none"></span>
                            <span><strong id="live-viewers" class="tabular-nums">24</strong> shoppers viewing this offer right now</span>
                        </span>
                        <span class="text-rose-500 tabular-nums">Only 7 units left at 50% OFF</span>
                    </div>
                    <div class="w-full rounded-full h-2 overflow-hidden" style="background:var(--ds-muted);">
                        <div class="h-2 rounded-full w-[84%]" style="background:var(--ds-accent);"></div>
                    </div>
                    <div class="flex items-center justify-between text-[11px] ds-muted-text pt-0.5">
                        <span class="inline-flex items-center gap-1">{_svg_icon("clock", "w-3.5 h-3.5")} Flash Sale Ends In:</span>
                        <span id="countdown-timer" class="font-mono font-extrabold px-2 py-0.5 rounded border tabular-nums" style="background:var(--ds-surface-alt); border-color:var(--ds-border); color:var(--ds-accent);">02:14:59</span>
                    </div>
                </div>

                <!-- PRIMARY CTA BUTTONS -->
                <div class="space-y-3 pt-1">
                    <button type="button"
                            data-open-order
                            class="ds-btn-accent min-h-[52px] w-full py-4 px-6 rounded-2xl font-extrabold text-base sm:text-lg shadow-xl transition-opacity hover:opacity-95 flex items-center justify-center gap-2 cursor-pointer">
                        {_svg_icon("shopping-bag", "w-5 h-5")}
                        <span id="main-cta-label">{cta_text}</span>
                    </button>

                    <div class="grid grid-cols-2 gap-2.5">
                        <a id="wc-direct-link"
                           href="?add-to-cart=[PRODUCT_ID]&quantity=1&checkout=true"
                           class="ds-card min-h-[44px] text-center py-2.5 px-4 rounded-xl font-bold text-xs flex items-center justify-center gap-1.5 transition-opacity hover:opacity-85">
                            {_svg_icon("zap", "w-3.5 h-3.5")} Express Cart Checkout
                        </a>
                        <button type="button"
                                data-open-order
                                class="min-h-[44px] text-center py-2.5 px-4 rounded-xl border font-bold text-xs flex items-center justify-center gap-1.5 transition-opacity hover:opacity-90 cursor-pointer" style="background:{positive_tint_bg}; border-color:var(--ds-border); color:var(--ds-primary);">
                            {_svg_icon("lock", "w-3.5 h-3.5")} Pay With Apple Pay / Card
                        </button>
                    </div>
                </div>

                <!-- Trust Badges Strip -->
                <div class="pt-1 flex flex-wrap gap-2">
                    {badges_html}
                </div>
            </div>
        </div>
    </main>

    <!-- 4. KEY BENEFITS & FEATURES SECTION -->
    <section id="ustunlukler" class="ds-surface-alt py-14 sm:py-20 border-y">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="text-center max-w-2xl mx-auto mb-12">
                <span class="inline-block text-xs font-extrabold uppercase tracking-widest px-3.5 py-1.5 rounded-full border mb-3" style="background:var(--ds-muted); border-color:var(--ds-border); color:var(--ds-primary);">
                    Engineered To Outperform
                </span>
                <h2 class="text-2xl sm:text-4xl font-extrabold tracking-tight mb-3">
                    Why Thousands Of Customers Are Switching To {brand_name}
                </h2>
                <p class="text-sm sm:text-base ds-muted-text">
                    Designed for maximum durability, effortless portability, and instant day-one performance.
                </p>
            </div>

            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6">
                {benefits_html}
            </div>
        </div>
    </section>

    <!-- 5. DETAILED AIDA STORYTELLING & SPECIFICATIONS -->
    <section id="haqqinda" class="py-14 sm:py-20">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="grid grid-cols-1 lg:grid-cols-12 gap-10 items-center">
                <div class="lg:col-span-7 space-y-5">
                    <div>
                        <span class="inline-block text-xs font-extrabold uppercase tracking-widest px-3.5 py-1 rounded-full border mb-3" style="background:var(--ds-muted); border-color:var(--ds-border); color:var(--ds-primary);">
                            Inside The Innovation
                        </span>
                        <h2 class="text-2xl sm:text-3xl font-extrabold tracking-tight">
                            {title} — Built For Modern Everyday Living
                        </h2>
                    </div>
                    <div class="space-y-4">
                        {aida_html}
                    </div>
                </div>

                <div class="lg:col-span-5">
                    <div class="ds-card rounded-3xl p-6 sm:p-8 shadow-lg">
                        <div class="relative rounded-2xl overflow-hidden mb-6 aspect-[16/10]" style="background:var(--ds-muted);">
                            <img src="{img_url_escaped}" alt="{title}" width="800" height="500" loading="lazy" class="w-full h-full object-cover" onerror="this.src='{default_fallback_img}'">
                            <div class="absolute inset-0 bg-gradient-to-t from-slate-900/75 via-transparent to-transparent flex items-end p-4">
                                <span class="text-white font-bold text-sm inline-flex items-center gap-1.5">
                                    {_svg_icon("sparkles", "w-4 h-4")} Quality-Inspected &amp; Factory Sealed
                                </span>
                            </div>
                        </div>

                        <h3 class="text-lg font-extrabold mb-4 inline-flex items-center gap-2">
                            {_svg_icon("layers", "w-5 h-5")} Technical Specifications &amp; Package Info
                        </h3>
                        <ul class="space-y-3 text-sm mb-6">
                            {specs_html}
                        </ul>

                        <button type="button" data-open-order class="ds-btn-primary min-h-[44px] w-full py-3.5 px-5 rounded-xl font-bold text-sm transition-opacity hover:opacity-95 shadow-md tabular-nums cursor-pointer">
                            Claim Yours Today — ${selling_price}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <!-- 6. COMPARISON TABLE -->
    <section id="muqayise" class="ds-surface-alt py-14 sm:py-20 border-t">
        <div class="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="text-center mb-10">
                <span class="inline-block text-xs font-extrabold uppercase tracking-widest px-3.5 py-1 rounded-full border mb-3" style="background:var(--ds-muted); border-color:var(--ds-border); color:var(--ds-primary);">
                    The Clear Difference
                </span>
                <h2 class="text-2xl sm:text-3xl font-extrabold tracking-tight mb-2">
                    Original {brand_name} vs. Cheap Generic Imitations
                </h2>
                <p class="text-sm ds-muted-text">Don't settle for underpowered knockoffs — invest in certified studio performance.</p>
            </div>

            <div class="ds-card rounded-3xl overflow-hidden shadow-md">
                <div class="grid grid-cols-3 text-xs sm:text-sm font-bold py-4 px-4 sm:px-6" style="background:var(--ds-primary); color:var(--ds-on-primary);">
                    <div>Feature</div>
                    <div class="text-center">{brand_name} (Official)</div>
                    <div class="text-center opacity-80">Generic Knockoffs</div>
                </div>
                <div class="divide-y text-xs sm:text-sm" style="border-color:var(--ds-border);">
                    <div class="grid grid-cols-3 py-4 px-4 sm:px-6 items-center gap-2">
                        <div class="font-semibold">Material &amp; Engineering</div>
                        <div class="text-center font-bold py-1.5 px-2 rounded-lg inline-flex items-center justify-center gap-1" style="background:{positive_tint_bg}; color:var(--ds-primary);">{_svg_icon("check", "w-3.5 h-3.5")} Certified Grade-A Build</div>
                        <div class="text-center text-rose-500 inline-flex items-center justify-center gap-1">{_svg_icon("x", "w-3.5 h-3.5")} Flimsy low-grade plastic</div>
                    </div>
                    <div class="grid grid-cols-3 py-4 px-4 sm:px-6 items-center gap-2">
                        <div class="font-semibold">Tracked US &amp; EU Delivery</div>
                        <div class="text-center font-bold py-1.5 px-2 rounded-lg inline-flex items-center justify-center gap-1" style="background:{positive_tint_bg}; color:var(--ds-primary);">{_svg_icon("check", "w-3.5 h-3.5")} {delivery_days_escaped} Air Freight</div>
                        <div class="text-center text-rose-500 inline-flex items-center justify-center gap-1">{_svg_icon("x", "w-3.5 h-3.5")} 30-45 days untracked</div>
                    </div>
                    <div class="grid grid-cols-3 py-4 px-4 sm:px-6 items-center gap-2">
                        <div class="font-semibold">30-Day Money-Back Guarantee</div>
                        <div class="text-center font-bold py-1.5 px-2 rounded-lg inline-flex items-center justify-center gap-1" style="background:{positive_tint_bg}; color:var(--ds-primary);">{_svg_icon("check", "w-3.5 h-3.5")} 100% Hassle-Free Refund</div>
                        <div class="text-center text-rose-500 inline-flex items-center justify-center gap-1">{_svg_icon("x", "w-3.5 h-3.5")} No returns accepted</div>
                    </div>
                    <div class="grid grid-cols-3 py-4 px-4 sm:px-6 items-center gap-2">
                        <div class="font-semibold">Buyer Protection &amp; Support</div>
                        <div class="text-center font-bold py-1.5 px-2 rounded-lg inline-flex items-center justify-center gap-1" style="background:{positive_tint_bg}; color:var(--ds-primary);">{_svg_icon("check", "w-3.5 h-3.5")} 24/7 Concierge Support</div>
                        <div class="text-center text-rose-500 inline-flex items-center justify-center gap-1">{_svg_icon("x", "w-3.5 h-3.5")} No customer support</div>
                    </div>
                </div>
            </div>
        </div>
    </section>

    <!-- 7. VERIFIED CUSTOMER REVIEWS -->
    <section id="reyler" class="py-14 sm:py-20 border-t" style="border-color:var(--ds-border);">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="ds-card flex flex-col md:flex-row items-center justify-between gap-6 mb-10 rounded-3xl p-6 sm:p-8">
                <div>
                    <span class="inline-block text-xs font-extrabold uppercase tracking-widest px-3 py-1 rounded-full border mb-2" style="background:var(--ds-muted); border-color:var(--ds-border); color:var(--ds-primary);">
                        Verified Customer Feedback
                    </span>
                    <h2 class="text-2xl sm:text-3xl font-extrabold">What Real Buyers Are Saying</h2>
                    <p class="text-sm mt-1 ds-muted-text">Unfiltered reviews from verified customers across the US, UK, and Europe</p>
                </div>
                <div class="flex items-center gap-5 px-6 py-4 rounded-2xl border" style="background:var(--ds-muted); border-color:var(--ds-border);">
                    <div class="text-center">
                        <div class="text-3xl sm:text-4xl font-extrabold tabular-nums">4.9</div>
                        <div class="flex items-center justify-center gap-0.5 text-amber-400 my-0.5">
                            {_svg_icon("star", "w-3.5 h-3.5", filled=True)}{_svg_icon("star", "w-3.5 h-3.5", filled=True)}{_svg_icon("star", "w-3.5 h-3.5", filled=True)}{_svg_icon("star", "w-3.5 h-3.5", filled=True)}{_svg_icon("star", "w-3.5 h-3.5", filled=True)}
                        </div>
                        <div class="text-[11px] ds-muted-text font-medium tabular-nums">2,450+ Reviews</div>
                    </div>
                    <div class="h-12 w-px" style="background:var(--ds-border);"></div>
                    <div class="text-xs space-y-1.5 ds-muted-text">
                        <div class="font-bold inline-flex items-center gap-1" style="color:var(--ds-primary);">{_svg_icon("check", "w-3.5 h-3.5")} 98% Recommend</div>
                        <div class="inline-flex items-center gap-1">{_svg_icon("check-circle", "w-3.5 h-3.5")} Verified Purchases</div>
                    </div>
                </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
                {reviews_html}
            </div>
        </div>
    </section>

    <!-- 8. HOW TO ORDER STEPS -->
    <section class="ds-surface-alt py-12 border-t">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="text-center mb-10">
                <h2 class="text-2xl sm:text-3xl font-extrabold">How Your Order Works (4 Simple Steps)</h2>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6 text-center">
                <div class="ds-card p-6 rounded-2xl">
                    <div class="w-10 h-10 rounded-full font-extrabold flex items-center justify-center mx-auto mb-3 tabular-nums" style="background:var(--ds-primary); color:var(--ds-on-primary);">1</div>
                    <h3 class="font-bold mb-1">Choose Your Bundle</h3>
                    <p class="text-xs ds-muted-text">Select your preferred bundle tier and click "Order Now" to open secure checkout.</p>
                </div>
                <div class="ds-card p-6 rounded-2xl">
                    <div class="w-10 h-10 rounded-full font-extrabold flex items-center justify-center mx-auto mb-3 tabular-nums" style="background:var(--ds-primary); color:var(--ds-on-primary);">2</div>
                    <h3 class="font-bold mb-1">24h Priority Dispatch</h3>
                    <p class="text-xs ds-muted-text">Our automated fulfillment center inspects, packs, and dispatches your order within 24 hours.</p>
                </div>
                <div class="ds-card p-6 rounded-2xl">
                    <div class="w-10 h-10 rounded-full font-extrabold flex items-center justify-center mx-auto mb-3 tabular-nums" style="background:var(--ds-primary); color:var(--ds-on-primary);">3</div>
                    <h3 class="font-bold mb-1">Live Tracked Transit</h3>
                    <p class="text-xs ds-muted-text">Receive your real-time tracking number via email as your parcel ships ({delivery_days_escaped}).</p>
                </div>
                <div class="ds-card p-6 rounded-2xl">
                    <div class="w-10 h-10 rounded-full font-extrabold flex items-center justify-center mx-auto mb-3 tabular-nums" style="background:var(--ds-accent); color:var(--ds-on-accent);">4</div>
                    <h3 class="font-bold mb-1">30-Day Risk-Free Trial</h3>
                    <p class="text-xs ds-muted-text">Test it at home for 30 full days backed by our 100% money-back satisfaction guarantee.</p>
                </div>
            </div>
        </div>
    </section>

    <!-- 9. NATIVE ACCESSIBLE FAQ ACCORDION -->
    <section id="faq" aria-labelledby="faqHeading" class="py-14 sm:py-20 border-t" style="border-color:var(--ds-border);">
        <div class="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="text-center mb-10">
                <span class="inline-block text-xs font-extrabold uppercase tracking-widest px-3.5 py-1 rounded-full border mb-2" style="background:var(--ds-muted); border-color:var(--ds-border); color:var(--ds-primary);">
                    Got Questions?
                </span>
                <h2 id="faqHeading" class="text-2xl sm:text-3xl font-extrabold">Frequently Asked Questions</h2>
                <p class="text-sm mt-1 ds-muted-text">Everything you need to know about shipping, returns, and warranty</p>
            </div>
            <div class="space-y-3">
                {faqs_html}
            </div>
        </div>
    </section>

    <!-- 10. 30-DAY GUARANTEE CTA BANNER -->
    <section class="ds-surface-alt py-12 px-4 sm:px-6 lg:px-8 border-t">
        <div class="max-w-5xl mx-auto rounded-3xl p-8 sm:p-12 text-center shadow-xl border" style="background:var(--ds-primary); color:var(--ds-on-primary); border-color:var(--ds-border);">
            <div class="w-16 h-16 rounded-2xl flex items-center justify-center mx-auto mb-4" style="background:rgba(255,255,255,0.16);">
                {_svg_icon("shield", "w-8 h-8")}
            </div>
            <h2 class="text-2xl sm:text-3xl font-extrabold mb-3">100% Risk-Free 30-Day Money-Back Guarantee</h2>
            <p class="text-sm sm:text-base max-w-2xl mx-auto mb-6 leading-relaxed opacity-90">
                Experience {title} for yourself. If you're not completely blown away within 30 days of delivery, we'll refund 100% of your order — no hoops, no hassle.
            </p>
            <button type="button" data-open-order class="ds-btn-accent min-h-[52px] inline-flex items-center gap-2 font-extrabold text-base px-8 py-4 rounded-2xl shadow-lg transition-opacity hover:opacity-95 tabular-nums cursor-pointer">
                {_svg_icon("shopping-bag", "w-5 h-5")}
                <span>CLAIM 50% OFF NOW — ${selling_price}</span>
            </button>
        </div>
    </section>

    <!-- 11. PROFESSIONAL STORE FOOTER -->
    <footer class="bg-slate-950 text-slate-300 py-12 border-t border-slate-800 text-xs sm:text-sm mt-auto">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 grid grid-cols-1 md:grid-cols-4 gap-8 mb-8">
            <div class="md:col-span-2">
                {footer_logo_html}
                <p class="text-slate-400 text-xs leading-relaxed max-w-sm">
                    Official Direct-To-Consumer flagship store. Every order ships factory-sealed with tracked air freight and our 30-Day Money-Back Guarantee.
                </p>
            </div>
            <div>
                <div class="text-white font-bold mb-3">Quick Links</div>
                <ul class="space-y-2 text-xs">
                    <li><a href="#mehsul" class="hover:text-white">Shop Offer</a></li>
                    <li><a href="#ustunlukler" class="hover:text-white">Key Features</a></li>
                    <li><a href="#reyler" class="hover:text-white">Verified Reviews</a></li>
                    <li><a href="#faq" class="hover:text-white">Shipping &amp; Returns FAQ</a></li>
                </ul>
            </div>
            <div>
                <div class="text-white font-bold mb-3">Global Fulfillment</div>
                <ul class="space-y-2 text-xs">
                    <li class="inline-flex items-center gap-1.5">{_svg_icon("map-pin", "w-3.5 h-3.5")} Tracked Shipping to US, UK, EU, CA &amp; AU</li>
                    <li class="inline-flex items-center gap-1.5">{_svg_icon("clock", "w-3.5 h-3.5")} 24/7 Customer Support Concierge</li>
                    <li class="inline-flex items-center gap-1.5">{_svg_icon("lock", "w-3.5 h-3.5")} 256-Bit SSL Encrypted Checkout</li>
                </ul>
            </div>
        </div>
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pt-6 border-t border-slate-800 flex flex-wrap items-center justify-between gap-4 text-xs text-slate-400">
            <span>© 2026 {brand_name}. All rights reserved.</span>
            <span>Shopify &amp; WooCommerce Global DTC Ready</span>
        </div>
    </footer>

    <!-- 12. MOBILE STICKY BOTTOM CHECKOUT BAR -->
    <div class="sm:hidden fixed bottom-0 left-0 right-0 backdrop-blur-md border-t p-3 z-40 flex items-center justify-between gap-3 shadow-2xl" style="background:var(--ds-header-bg); border-color:var(--ds-border);">
        <div class="tabular-nums">
            <div class="text-[10px] font-semibold ds-muted-text uppercase">Selected Bundle:</div>
            <div class="flex items-baseline gap-1.5">
                <span id="mobile-bar-price" class="text-lg font-extrabold">${selling_price}</span>
                <span id="mobile-bar-regular" class="text-xs ds-muted-text line-through">${regular_price}</span>
            </div>
        </div>
        <button type="button" data-open-order class="ds-btn-accent min-h-[44px] flex-1 text-center font-extrabold text-sm py-3 px-4 rounded-xl shadow-md inline-flex items-center justify-center gap-2 cursor-pointer">
            {_svg_icon("shopping-bag", "w-4 h-4")}
            <span>ORDER NOW</span>
        </button>
    </div>

    <!-- 13. ACCESSIBLE NATIVE <dialog> CHECKOUT MODAL -->
    <dialog id="order-modal" closedby="any" aria-labelledby="orderModalTitle" class="ds-card rounded-3xl max-w-lg w-[calc(100%-2rem)] p-0 overflow-hidden shadow-2xl m-auto">
        <div class="px-6 py-4 flex items-center justify-between" style="background:var(--ds-primary); color:var(--ds-on-primary);">
            <div class="flex items-center gap-2">
                {_svg_icon("shopping-bag", "w-5 h-5")}
                <h2 id="orderModalTitle" class="font-extrabold text-base sm:text-lg">Express Encrypted Checkout</h2>
            </div>
            <button type="button" id="close-order-modal" aria-label="Close checkout dialog" class="min-w-[44px] min-h-[44px] inline-flex items-center justify-center rounded-xl hover:opacity-80 cursor-pointer">
                {_svg_icon("x", "w-5 h-5")}
            </button>
        </div>

        <div id="order-form-container" class="p-6 space-y-4">
            <div class="flex items-center gap-4 border p-3.5 rounded-2xl" style="background:var(--ds-muted); border-color:var(--ds-border);">
                <img src="{img_url_escaped}" alt="{title}" width="64" height="64" class="w-16 h-16 rounded-xl object-cover border shrink-0" style="border-color:var(--ds-border);" onerror="this.src='{default_fallback_img}'">
                <div class="flex-1 min-w-0">
                    <div class="font-bold text-sm truncate">{title}</div>
                    <div id="modal-bundle-name" class="text-xs font-semibold" style="color:var(--ds-primary);">1x Unit — Standard Pack</div>
                    <div class="flex items-baseline gap-2 mt-0.5 tabular-nums">
                        <span id="modal-bundle-price" class="text-lg font-extrabold" style="color:var(--ds-primary);">${selling_price}</span>
                        <span class="text-[11px] ds-muted-text">+ Free Tracked Shipping ({delivery_days_escaped})</span>
                    </div>
                </div>
            </div>

            <form id="quick-order-form" data-binding-sku="{binding_sku_escaped}" data-sourcing-channel="{sourcing_channel_escaped}" class="space-y-3">
                <input type="hidden" id="product-binding-sku" name="binding_sku" value="{binding_sku_escaped}">
                <input type="hidden" id="product-sourcing-channel" name="sourcing_channel" value="{sourcing_channel_escaped}">
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                        <label for="cust-name" class="block text-xs font-bold mb-1">Full Name *</label>
                        <input type="text" id="cust-name" name="name" autocomplete="name" autofocus required placeholder="Sarah Jenkins" class="w-full min-h-[44px] px-3.5 py-2 rounded-xl border text-sm" style="background:var(--ds-surface-alt); color:var(--ds-fg); border-color:var(--ds-border);">
                    </div>
                    <div>
                        <label for="cust-email" class="block text-xs font-bold mb-1">Email For Tracking *</label>
                        <input type="email" id="cust-email" name="email" autocomplete="email" required placeholder="sarah@example.com" class="w-full min-h-[44px] px-3.5 py-2 rounded-xl border text-sm" style="background:var(--ds-surface-alt); color:var(--ds-fg); border-color:var(--ds-border);">
                    </div>
                </div>
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                        <label for="cust-phone" class="block text-xs font-bold mb-1">Phone Number *</label>
                        <input type="tel" id="cust-phone" name="phone" autocomplete="tel" required placeholder="+1 (555) 234-5678" class="w-full min-h-[44px] px-3.5 py-2 rounded-xl border text-sm" style="background:var(--ds-surface-alt); color:var(--ds-fg); border-color:var(--ds-border);">
                    </div>
                    <div>
                        <label for="cust-country" class="block text-xs font-bold mb-1">Destination Country *</label>
                        <select id="cust-country" name="country_code" class="w-full min-h-[44px] px-3.5 py-2 rounded-xl border text-sm" style="background:var(--ds-surface-alt); color:var(--ds-fg); border-color:var(--ds-border);">
                            <option value="US">United States (US)</option>
                            <option value="GB">United Kingdom (UK)</option>
                            <option value="DE">Germany (EU)</option>
                            <option value="FR">France (EU)</option>
                            <option value="CA">Canada (CA)</option>
                            <option value="AU">Australia (AU)</option>
                        </select>
                    </div>
                </div>
                <div>
                    <label for="cust-address" class="block text-xs font-bold mb-1">Street Address *</label>
                    <input type="text" id="cust-address" name="address" autocomplete="street-address" required placeholder="742 Evergreen Terrace, Apt 4B" class="w-full min-h-[44px] px-3.5 py-2 rounded-xl border text-sm" style="background:var(--ds-surface-alt); color:var(--ds-fg); border-color:var(--ds-border);">
                </div>
                <div class="grid grid-cols-3 gap-2.5">
                    <div>
                        <label for="cust-city" class="block text-xs font-bold mb-1">City *</label>
                        <input type="text" id="cust-city" name="city" autocomplete="address-level2" required placeholder="Austin" class="w-full min-h-[44px] px-3 py-2 rounded-xl border text-sm" style="background:var(--ds-surface-alt); color:var(--ds-fg); border-color:var(--ds-border);">
                    </div>
                    <div>
                        <label for="cust-state" class="block text-xs font-bold mb-1">State / Prov *</label>
                        <input type="text" id="cust-state" name="province" autocomplete="address-level1" required placeholder="TX" class="w-full min-h-[44px] px-3 py-2 rounded-xl border text-sm" style="background:var(--ds-surface-alt); color:var(--ds-fg); border-color:var(--ds-border);">
                    </div>
                    <div>
                        <label for="cust-zip" class="block text-xs font-bold mb-1">ZIP / Postal *</label>
                        <input type="text" id="cust-zip" name="zip" autocomplete="postal-code" required placeholder="78701" class="w-full min-h-[44px] px-3 py-2 rounded-xl border text-sm" style="background:var(--ds-surface-alt); color:var(--ds-fg); border-color:var(--ds-border);">
                    </div>
                </div>
                <div>
                    <label for="cust-payment" class="block text-xs font-bold mb-1">Payment Method</label>
                    <select id="cust-payment" class="w-full min-h-[44px] px-3.5 py-2 rounded-xl border text-sm" style="background:var(--ds-surface-alt); color:var(--ds-fg); border-color:var(--ds-border);">
                        <option value="card_express">Credit / Debit Card (Visa, Mastercard, Amex)</option>
                        <option value="apple_pay">Apple Pay / Google Pay Express</option>
                        <option value="paypal">PayPal Express Checkout</option>
                        <option value="wc_online">WooCommerce / Shopify Direct Checkout</option>
                    </select>
                </div>

                <button type="submit" id="quick-order-submit-btn" class="ds-btn-accent min-h-[48px] w-full py-3.5 rounded-xl font-extrabold text-base shadow-lg transition-opacity hover:opacity-95 inline-flex items-center justify-center gap-2 cursor-pointer">
                    {_svg_icon("lock", "w-4 h-4")}
                    <span>COMPLETE SECURE ORDER</span>
                </button>
            </form>

            <p class="text-[11px] text-center ds-muted-text inline-flex items-center justify-center gap-1 w-full">
                {_svg_icon("lock", "w-3.5 h-3.5")} 256-Bit SSL Encrypted • 30-Day Money-Back Guarantee
            </p>
        </div>

        <div id="order-success-container" class="hidden p-8 text-center space-y-4">
            <div class="w-16 h-16 rounded-full flex items-center justify-center mx-auto font-bold" style="background:{positive_tint_bg}; color:var(--ds-primary);">
                {_svg_icon("check-circle", "w-8 h-8")}
            </div>
            <h3 class="text-xl font-extrabold">Order Confirmed &amp; Dispatched To Fulfillment</h3>
            <p id="order-success-msg" class="text-sm ds-muted-text leading-relaxed"></p>
            <button type="button" id="done-order-modal" class="ds-btn-primary min-h-[44px] mt-2 px-6 py-3 rounded-xl font-bold text-sm cursor-pointer">
                Done
            </button>
        </div>
    </dialog>

    <!-- STORE INTERACTIVE JAVASCRIPT -->
    <script>
        (function () {{
            window.PRODUCT_BINDING_SKU = "{binding_sku_escaped}";
            window.PRODUCT_SOURCING_CHANNEL = "{sourcing_channel_escaped}";
            let currentBundle = {{
                qty: 1,
                price: {selling_price},
                regular: {regular_price},
                label: '1x Unit — Standard Pack'
            }};

            const modal = document.getElementById('order-modal');
            const formBox = document.getElementById('order-form-container');
            const successBox = document.getElementById('order-success-container');

            function openOrderModal() {{
                const bundleName = document.getElementById('modal-bundle-name');
                const bundlePrice = document.getElementById('modal-bundle-price');
                if (bundleName) bundleName.textContent = currentBundle.label;
                if (bundlePrice) bundlePrice.textContent = '$' + currentBundle.price.toFixed(2);
                if (formBox) formBox.classList.remove('hidden');
                if (successBox) successBox.classList.add('hidden');
                if (modal && !modal.open) modal.showModal();
            }}

            function closeOrderModal() {{
                if (modal && modal.open) modal.close();
            }}

            modal?.addEventListener('close', () => {{
                modal.returnValue = '';
            }});

            if (modal && !('closedBy' in HTMLDialogElement.prototype)) {{
                const outside = (e, d) => {{
                    const r = d.getBoundingClientRect();
                    return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
                }};
                let pressedOutside = false;
                modal.addEventListener('pointerdown', (e) => {{ pressedOutside = outside(e, modal); }});
                modal.addEventListener('pointerup', (e) => {{ if (pressedOutside && outside(e, modal)) closeOrderModal(); }});
            }}

            document.querySelectorAll('[data-open-order]').forEach((btn) => {{
                btn.addEventListener('click', openOrderModal);
            }});
            document.getElementById('close-order-modal')?.addEventListener('click', closeOrderModal);
            document.getElementById('done-order-modal')?.addEventListener('click', closeOrderModal);

            document.querySelectorAll('[data-gallery-idx]').forEach((btn) => {{
                btn.addEventListener('click', () => {{
                    const idx = Number(btn.getAttribute('data-gallery-idx'));
                    const cap = btn.getAttribute('data-caption') || '';
                    const newSrc = btn.getAttribute('data-img-src');
                    const scaleClass = btn.getAttribute('data-scale') || 'scale-100';
                    const mainImg = document.getElementById('main-product-image');
                    const caption = document.getElementById('gallery-caption');
                    if (mainImg) {{
                        if (newSrc && mainImg.getAttribute('src') !== newSrc) {{
                            mainImg.src = newSrc;
                        }}
                        mainImg.className = 'w-full h-full object-cover object-center transition-transform duration-500 motion-reduce:transition-none ' + scaleClass;
                    }}
                    if (caption) caption.textContent = cap;
                    for (let i = 0; i < {thumb_count_js}; i++) {{
                        document.getElementById('thumb-' + i)?.classList.toggle('thumb-active', i === idx);
                    }}
                }});
            }});

            document.querySelectorAll('[data-qty]').forEach((card) => {{
                card.addEventListener('click', () => {{
                    const qty = Number(card.getAttribute('data-qty'));
                    const price = Number(card.getAttribute('data-price'));
                    const regular = Number(card.getAttribute('data-regular'));
                    const label = card.getAttribute('data-label') || '';
                    currentBundle = {{ qty, price, regular, label }};
                    const savings = (regular - price).toFixed(2);
                    const pct = Math.round(((regular - price) / regular) * 100);

                    for (let i = 1; i <= 3; i++) {{
                        document.getElementById('bundle-card-' + i)?.classList.toggle('bundle-active', i === qty);
                        const radio = document.getElementById('bundle-radio-' + i);
                        if (radio) radio.checked = (i === qty);
                    }}

                    const sellEl = document.getElementById('display-selling-price');
                    const regEl = document.getElementById('display-regular-price');
                    const savEl = document.getElementById('display-savings-badge');
                    const headPill = document.getElementById('header-price-pill');
                    const mobPrice = document.getElementById('mobile-bar-price');
                    const mobReg = document.getElementById('mobile-bar-regular');
                    const wcLink = document.getElementById('wc-direct-link');

                    if (sellEl) sellEl.textContent = '$' + price.toFixed(2);
                    if (regEl) regEl.textContent = '$' + regular.toFixed(2);
                    if (savEl) savEl.textContent = 'You Save: $' + savings + ' (' + pct + '%)';
                    if (headPill) headPill.textContent = '$' + price.toFixed(2);
                    if (mobPrice) mobPrice.textContent = '$' + price.toFixed(2);
                    if (mobReg) mobReg.textContent = '$' + regular.toFixed(2);
                    if (wcLink) wcLink.href = '?add-to-cart=[PRODUCT_ID]&quantity=' + qty + '&checkout=true';
                }});
            }});

            document.getElementById('wc-direct-link')?.addEventListener('click', (e) => {{
                const href = e.currentTarget.getAttribute('href') || '';
                if (href.includes('[PRODUCT_ID]')) {{
                    e.preventDefault();
                    openOrderModal();
                }}
            }});

            document.getElementById('quick-order-form')?.addEventListener('submit', async (e) => {{
                e.preventDefault();
                const name = (document.getElementById('cust-name')?.value || '').trim();
                const email = (document.getElementById('cust-email')?.value || '').trim();
                const phone = (document.getElementById('cust-phone')?.value || '').trim();
                const address = (document.getElementById('cust-address')?.value || '').trim();
                const city = (document.getElementById('cust-city')?.value || '').trim();
                const province = (document.getElementById('cust-state')?.value || '').trim();
                const zip = (document.getElementById('cust-zip')?.value || '').trim();
                const country_code = (document.getElementById('cust-country')?.value || 'US').trim();
                const payment = document.getElementById('cust-payment')?.value || 'card_express';

                if (payment === 'wc_online' && window.WC_PRODUCT_ID) {{
                    window.location.href = '?add-to-cart=' + window.WC_PRODUCT_ID + '&quantity=' + currentBundle.qty + '&checkout=true';
                    return;
                }}

                const submitBtn = document.getElementById('quick-order-submit-btn');
                if (submitBtn) {{
                    submitBtn.disabled = true;
                    submitBtn.style.opacity = '0.7';
                }}

                let orderIdText = 'ORD-' + Math.floor(100000 + Math.random() * 900000);
                let channelNote = window.PRODUCT_SOURCING_CHANNEL || 'Auto-Fulfillment';
                try {{
                    const resp = await fetch('/api/store/public-order', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{
                            binding_sku: window.PRODUCT_BINDING_SKU || '',
                            sourcing_channel: window.PRODUCT_SOURCING_CHANNEL || '',
                            product_name: {json.dumps(raw_title)},
                            quantity: currentBundle.qty,
                            total_price_usd: currentBundle.price,
                            customer: {{
                                name: name,
                                email: email,
                                phone: phone,
                                address1: address,
                                city: city,
                                province: province,
                                zip: zip,
                                country_code: country_code,
                                payment_method: payment
                            }}
                        }})
                    }});
                    if (resp.ok) {{
                        const data = await resp.json();
                        if (data.order_id) orderIdText = data.order_id;
                        if (data.channel) channelNote = data.channel;
                    }}
                }} catch (err) {{
                    console.debug('Offline/static preview order fallback:', err);
                }} finally {{
                    if (submitBtn) {{
                        submitBtn.disabled = false;
                        submitBtn.style.opacity = '1';
                    }}
                }}

                const msg = document.getElementById('order-success-msg');
                if (msg) {{
                    msg.replaceChildren();
                    const s1 = document.createElement('strong');
                    s1.textContent = name;
                    const s2 = document.createElement('strong');
                    s2.textContent = currentBundle.label + ' ($' + currentBundle.price.toFixed(2) + ')';
                    const s3 = document.createElement('strong');
                    s3.textContent = orderIdText;
                    const s4 = document.createElement('strong');
                    s4.textContent = address + ', ' + city + ' ' + province + ' ' + zip + ' (' + country_code + ')';
                    msg.append('Thank you, ', s1, '! Your order ', s3, ' for ', s2, ' has been queued for priority dispatch (' + channelNote + ') to ', s4, '. Real-time tracking updates will be sent to ' + (email || phone) + '.');
                }}
                if (formBox) formBox.classList.add('hidden');
                if (successBox) successBox.classList.remove('hidden');
            }});

            let totalSeconds = 2 * 3600 + 14 * 60 + 59;
            const timerEl = document.getElementById('countdown-timer');
            const viewersEl = document.getElementById('live-viewers');
            setInterval(() => {{
                if (totalSeconds > 0) totalSeconds--;
                const hrs = String(Math.floor(totalSeconds / 3600)).padStart(2, '0');
                const mins = String(Math.floor((totalSeconds % 3600) / 60)).padStart(2, '0');
                const secs = String(totalSeconds % 60).padStart(2, '0');
                if (timerEl) timerEl.textContent = hrs + ':' + mins + ':' + secs;
            }}, 1000);
            setInterval(() => {{
                if (viewersEl) viewersEl.textContent = String(18 + Math.floor(Math.random() * 11));
            }}, 7000);
        }})();
    </script>
</div>
</body>
</html>"""
    return html_code.strip()


class StoreBuilderAgent(BaseAgent):
    """Agent responsible for writing UI/UX Pro Max tailored Global US/EU English DTC store copy and generating storefronts."""

    def __init__(
        self,
        client: genai.Client,
        model: str = "gemini-3.5-flash-lite",
        tools: Optional[List[Callable]] = None,
    ):
        super().__init__(client=client, model=model, tools=tools)
        self.system_instruction = (
            "You are a world-class Direct-To-Consumer (DTC) E-Commerce Creative Director, UI/UX Conversion Architect, and Copywriter for US, UK, and European markets. "
            "Your mission is to craft high-converting, compliant Global English DTC product landing page copy aligned with the UI/UX Pro Max design system.\n\n"
            "STRICT RULES:\n"
            "1. Write 100% of the storefront copy in persuasive, native Global English (US/EU DTC standard). Do NOT use Azerbaijani or Cash-On-Delivery local text.\n"
            "2. Structure description_aida using the AIDA framework (Attention, Interest, Desire, Action) with clean paragraphs.\n"
            "3. Do NOT use emojis in headlines, buttons, or benefit titles — vector SVG icons are rendered automatically via 'icon_key' ('zap', 'shield', 'sparkles', 'award', 'truck', 'clock', 'check', 'star', 'heart', 'layers').\n"
            "4. Provide 4-5 accurate, product-specific technical specifications in 'specifications' (e.g. Material, Power/Battery, Dimensions, Certifications, In The Box).\n"
            "5. Highlight Tracked Air Shipping to US/UK/EU, 256-Bit Encrypted Checkout (Card / Apple Pay / PayPal), and a 30-Day Money-Back Guarantee."
        )

    async def run(self, state: ECommerceState) -> Dict[str, Any]:
        """Resolve UI/UX Pro Max design system, auto-populate multi-angle product gallery, generate Global English DTC copy, render HTML storefront, and run UX audit."""
        logger.info("Starting Store Builder Phase (Global US/EU English DTC + UI/UX Pro Max Storefront)...")
        product = dict(state.get("selected_product") or {})
        supplier = state.get("selected_supplier") or {}
        economics = dict(state.get("unit_economics") or supplier.get("unit_economics") or {})
        store_config = dict(state.get("store_config") or {})
        bound_sourcing = dict(state.get("bound_sourcing") or {})
        binding_sku = str(
            state.get("binding_sku")
            or bound_sourcing.get("binding_sku")
            or supplier.get("binding_sku")
            or product.get("binding_sku")
            or ""
        ).strip()
        sourcing_channel = str(
            bound_sourcing.get("channel")
            or supplier.get("channel")
            or product.get("sourcing_channel")
            or ""
        ).strip()
        if binding_sku:
            product["binding_sku"] = binding_sku
        if sourcing_channel:
            product["sourcing_channel"] = sourcing_channel

        primary_agent1_img = str(product.get("image_url") or "").strip()
        if supplier:
            product["supplier_link"] = supplier.get("order_link")
            if supplier.get("delivery_days"):
                product["delivery_days"] = supplier.get("delivery_days")
            if supplier.get("supplier_name"):
                product["supplier_name"] = supplier.get("supplier_name")
            # Protect Agent 1's verified product image from low-confidence supplier image overwrite
            sup_img = str(supplier.get("product_image_url") or "").strip()
            sup_match = int(supplier.get("match_confidence") or 0)
            if not primary_agent1_img and sup_img:
                product["image_url"] = sup_img
            elif primary_agent1_img:
                product["image_url"] = primary_agent1_img

        brand = dict(state.get("brand_identity") or state.get("brand") or {})
        category = state.get("category", "")

        if store_config.get("mode") == "manual":
            if store_config.get("brand_name"):
                brand["brand_name"] = store_config["brand_name"]
                brand["name"] = store_config["brand_name"]
            if store_config.get("logo_url"):
                brand["logo_url"] = store_config["logo_url"]
            if store_config.get("selling_price") not in (None, ""):
                try:
                    economics["selling_price"] = round(float(store_config["selling_price"]), 2)
                except (TypeError, ValueError):
                    pass
            manual_imgs = [
                str(u).strip() for u in (store_config.get("product_images") or []) if isinstance(u, str) and str(u).strip()
            ]
            if manual_imgs:
                product["gallery_images"] = manual_imgs
                product["image_url"] = manual_imgs[0]

        # Auto-populate 3-4 distinct product gallery images when in Auto mode (or when manual images weren't provided)
        existing_gallery = [
            str(u).strip() for u in (product.get("gallery_images") or []) if isinstance(u, str) and str(u).strip()
        ]
        if len(existing_gallery) < 3:
            en_query = str(
                product.get("sourcing_search_query")
                or product.get("english_name")
                or product.get("name")
                or ""
            ).strip()
            auto_gallery: List[str] = []
            if product.get("image_url") and str(product["image_url"]).startswith("http"):
                auto_gallery.append(str(product["image_url"]).strip())
            sup_img = str(supplier.get("product_image_url") or "").strip()
            if sup_img.startswith("http") and int(supplier.get("match_confidence") or 0) >= 55 and sup_img not in auto_gallery:
                auto_gallery.append(sup_img)
            if en_query:
                try:
                    fetched_imgs = await asyncio.to_thread(
                        search_product_images,
                        en_query,
                        str(product.get("english_name") or product.get("name") or ""),
                        4,
                    )
                    for u in fetched_imgs:
                        if u and u.startswith("http") and u not in auto_gallery:
                            auto_gallery.append(u)
                            if len(auto_gallery) >= 4:
                                break
                except Exception as g_err:
                    logger.debug(f"Auto multi-image gallery lookup note: {g_err}")
            if auto_gallery:
                product["gallery_images"] = auto_gallery
                if not product.get("image_url"):
                    product["image_url"] = auto_gallery[0]

        design_system = resolve_store_design_system(
            product=product,
            brand=brand,
            category=category,
            store_config=store_config if store_config.get("mode") == "manual" else None,
        )
        ds_style = design_system.get("style", {})
        ds_pattern = design_system.get("pattern", {})
        ds_colors = design_system.get("colors", {})
        ds_typo = design_system.get("typography", {})
        ds_rules = "\n".join(f"  - {g}" for g in design_system.get("guidelines", []))
        delivery_days_for_prompt = str(product.get("delivery_days") or "6-10 Business Days")

        try:
            prompt = (
                f"Create high-converting Global US/EU English Direct-To-Consumer (DTC) storefront copy for the following winning product and UI/UX Pro Max design system:\n\n"
                f"Product Details: {json.dumps(product, ensure_ascii=False)}\n"
                f"Brand Identity: {json.dumps(brand, ensure_ascii=False)}\n"
                f"Unit Economics (USD): {json.dumps(economics, ensure_ascii=False)}\n"
                f"Tracked Shipping Estimate: {delivery_days_for_prompt}\n\n"
                f"UI/UX PRO MAX DESIGN CONTEXT:\n"
                f"- Niche Category: {design_system.get('category')} ({design_system.get('color_mode')} mode)\n"
                f"- Selected Style: {ds_style.get('name')} ({ds_colors.get('notes', '')})\n"
                f"- Font Pairing: {ds_typo.get('heading')} + {ds_typo.get('body')} ({ds_typo.get('mood', '')})\n"
                f"- Section Flow: {ds_pattern.get('sections', 'Hero > Features > Benefits > Specs > Reviews > FAQ > CTA')}\n"
                f"- Conversion Focus: {ds_pattern.get('conversion', '')}\n"
                f"- Anti-Patterns to Avoid: {design_system.get('anti_patterns', '')}\n"
                f"- UX Guidelines:\n{ds_rules}\n\n"
                f"REQUIREMENTS:\n"
                f"1. Write 100% of fields in persuasive, native Global English for US, UK & EU shoppers (USD $ pricing, Card / Apple Pay / PayPal checkout, {delivery_days_for_prompt} tracked air shipping).\n"
                f"2. Compelling commercial product_title, viral hook headline, and benefit-driven subheadline (NO emojis).\n"
                f"3. Full 3-4 paragraph AIDA sales story (description_aida).\n"
                f"4. 4 distinct features_benefits items with appropriate 'icon_key' ('zap', 'shield', 'sparkles', 'award', 'truck', 'clock', 'check', 'star', 'heart', 'layers').\n"
                f"5. 4-5 accurate, product-specific technical specifications in 'specifications' (e.g. Material, Power/Battery, Dimensions, Certifications, In The Box).\n"
                f"6. 4 objection-busting FAQs (shipping time: {delivery_days_for_prompt}, encrypted payment methods, 30-day money-back guarantee, product usage).\n"
                f"7. 3 authentic verified US/EU buyer reviews (customer_reviews).\n"
                f"8. Trust badges, urgency_text, and cta_button_text in English (NO emojis)."
            )

            logger.info(
                f"Generating Global English UI/UX Pro Max copy ({design_system.get('category')} | "
                f"{ds_style.get('name')} | {design_system.get('color_mode')})..."
            )
            schema = StoreCopy.model_json_schema()

            response_text = await self._call_gemini(
                prompt=prompt,
                system_instruction=self.system_instruction,
                response_schema=schema,
            )

            copy_data = json.loads(response_text)
            store_copy = StoreCopy(**copy_data)
            copy_dict = store_copy.model_dump()

            landing_html = build_wordpress_landing_page(
                copy_dict, product, economics, brand=brand, design_system=design_system, store_config=store_config
            )
            audit = audit_storefront_html(landing_html)

            copy_dict["design_system"] = design_system
            copy_dict["store_config"] = store_config
            copy_dict["binding_sku"] = binding_sku
            copy_dict["sourcing_channel"] = sourcing_channel
            copy_dict["ux_audit"] = audit
            copy_dict["wordpress_landing_page_html"] = landing_html

            log_msg = (
                f"✅ 3-cü Agent (UI/UX Pro Max Global DTC): '{copy_dict.get('product_title')}' mağazası yaradıldı | "
                f"SKU: {binding_sku or 'N/A'} | Qalereya: {len(product.get('gallery_images') or [1])} şəkil | "
                f"Stil: {ds_style.get('name')} ({design_system.get('color_mode')}) | "
                f"UX Audit: {audit['score']}/100 ({'PASS' if audit['passed'] else ', '.join(audit['issues'])})"
            )
            logger.info(log_msg)

            return {
                "store": {
                    "platform": "wordpress",
                    "store_url": "https://mystore.com/landing-page",
                    "status": "ready",
                    "binding_sku": binding_sku,
                    "sourcing_channel": sourcing_channel,
                    "fulfillment_method": bound_sourcing.get("fulfillment_method", ""),
                    "gallery_images": product.get("gallery_images") or [],
                    "copy": copy_dict,
                    "design_system": design_system,
                    "store_config": store_config,
                    "ux_audit": audit,
                    "wordpress_landing_page_html": landing_html,
                },
                "selected_product": product,
                "store_copy": copy_dict,
                "wordpress_landing_page_html": landing_html,
                "logs": [log_msg],
            }

        except Exception as e:
            logger.error(f"Error in Store Builder Agent: {str(e)}", exc_info=True)
            en_title = product.get("english_name") or product.get("name") or "Winning Smart Product"
            fallback_copy = {
                "product_title": en_title,
                "headline": f"Upgrade Your Daily Routine With The Next-Gen {en_title}",
                "subheadline": product.get("what_it_does", "Studio-grade engineering built for effortless everyday results."),
                "description_aida": product.get("description", ""),
                "features_benefits": [],
                "specifications": [],
                "faqs": [],
                "trust_badges": [
                    "30-Day Money-Back Guarantee",
                    f"Free Tracked Shipping ({delivery_days_for_prompt})",
                    "256-Bit SSL Encrypted Checkout",
                    "CE & FCC Certified Original Quality",
                ],
                "customer_reviews": [],
                "urgency_text": "Limited Time Offer: 50% OFF + Free Tracked US & EU Shipping Today!",
                "cta_button_text": "CLAIM 50% OFF NOW — FAST TRACKED SHIPPING",
            }
            landing_html = build_wordpress_landing_page(
                fallback_copy, product, economics, brand=brand, design_system=design_system, store_config=store_config
            )
            audit = audit_storefront_html(landing_html)
            fallback_copy["design_system"] = design_system
            fallback_copy["store_config"] = store_config
            fallback_copy["binding_sku"] = binding_sku
            fallback_copy["sourcing_channel"] = sourcing_channel
            fallback_copy["ux_audit"] = audit
            fallback_copy["wordpress_landing_page_html"] = landing_html
            return {
                "store": {
                    "platform": "wordpress",
                    "status": "ready",
                    "binding_sku": binding_sku,
                    "sourcing_channel": sourcing_channel,
                    "fulfillment_method": bound_sourcing.get("fulfillment_method", ""),
                    "gallery_images": product.get("gallery_images") or [],
                    "copy": fallback_copy,
                    "design_system": design_system,
                    "store_config": store_config,
                    "ux_audit": audit,
                    "wordpress_landing_page_html": landing_html,
                },
                "selected_product": product,
                "store_copy": fallback_copy,
                "wordpress_landing_page_html": landing_html,
                "logs": [
                    f"⚠️ Mağaza Qurulumunda ehtiyat Global DTC şablonu + UI/UX Pro Max ({ds_style.get('name')}) istifadə edildi: {str(e)}"
                ],
            }


def create_store_builder_node(
    client: genai.Client,
    model: str = "gemini-3.5-flash-lite",
    tools: Optional[List[Callable]] = None,
) -> Callable[[ECommerceState], Dict[str, Any]]:
    """Factory function to create the store builder node."""
    agent = StoreBuilderAgent(client=client, model=model, tools=tools)

    async def node(state: ECommerceState) -> Dict[str, Any]:
        result = await agent.run(state)
        result["completed_parallel_nodes"] = ["store_build"]
        return result

    return node
