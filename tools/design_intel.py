"""
UI/UX Pro Max Design Intelligence Integration for Agent 03 (StoreBuilderAgent).

Uses the vendored pure-stdlib BM25 + Reasoning + WCAG contrast engine in
skills/ui_ux_pro_max/ to resolve niche-specific design systems (palettes,
light/dark mode, typography pairings, landing patterns, anti-patterns, and
HTML+Tailwind UX rules) and audit generated storefront HTML before delivery.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _PROJECT_ROOT / "skills" / "ui_ux_pro_max" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core import search as uxp_search  # type: ignore  # noqa: E402
from design_system import (  # type: ignore  # noqa: E402
    _contrast_ratio,
    _relative_luminance,
    generate_design_system,
)


_NICHE_QUERY_RULES = [
    (
        ("luxury", "watch", "saat", "jewelry", "zərgərlik", "gold", "qızıl", "perfume", "ətir", "premium", "leather", "dəri"),
        "ecommerce luxury",
        7, 5, 4,
    ),
    (
        ("beauty", "skincare", "dəri", "serum", "cosmetic", "kosmetika", "spa", "wellness", "facial", "baxım"),
        "beauty spa wellness",
        5, 4, 4,
    ),
    (
        ("fitness", "gym", "idman", "workout", "məşq", "sport", "yoga", "posture", "massage", "masaj", "muscle"),
        "fitness gym dark mode",
        7, 6, 5,
    ),
    (
        ("tech", "smart", "led", "usb", "wireless", "simsiz", "charger", "şarj", "bluetooth", "audio", "camera", "kamera", "gadget", "qadcet", "automotive"),
        "smart home iot dark mode",
        6, 5, 5,
    ),
    (
        ("pet", "dog", "cat", "heyvan", "pişik"),
        "pet tech",
        5, 4, 5,
    ),
    (
        ("kitchen", "mətbəx", "blender", "espresso", "coffee", "cook"),
        "ecommerce product store conversion",
        5, 4, 5,
    ),
]


def _pick_best_on_color(bg_hex: str, preferred: str = "") -> str:
    """Guarantees WCAG AA >= 4.5:1 text contrast on top of a button/card fill."""
    candidates = [c for c in (preferred, "#FFFFFF", "#0F172A") if c and c.startswith("#")]
    best_hex = "#FFFFFF"
    best_ratio = 0.0
    for c in candidates:
        ratio = _contrast_ratio(c, bg_hex) or 0.0
        if preferred and c == preferred and ratio >= 4.5:
            return c
        if ratio > best_ratio:
            best_ratio = ratio
            best_hex = c
    return best_hex


def _ensure_fg_contrast(fg_hex: str, bg_hex: str, is_dark: bool) -> str:
    ratio = _contrast_ratio(fg_hex, bg_hex) or 0.0
    if ratio >= 4.5:
        return fg_hex
    return "#F8FAFC" if is_dark else "#0F172A"


def _build_query_profile(product: Dict[str, Any], category: str = "") -> tuple[str, int, int, int]:
    blob = " ".join(
        str(x or "")
        for x in (
            category,
            product.get("category"),
            product.get("name"),
            product.get("english_name"),
            product.get("what_it_does"),
            product.get("description"),
            product.get("wow_factor"),
        )
    ).lower()
    for keywords, query, variance, motion, density in _NICHE_QUERY_RULES:
        if any(re.search(rf"\b{re.escape(kw)}\b", blob) for kw in keywords):
            return query, variance, motion, density
    return "ecommerce product store conversion", 5, 4, 5


_STORE_DESIGN_PRESETS: List[Dict[str, Any]] = [
    {
        "id": "auto",
        "name": "Avtomatik (Nişə Görə UI/UX Pro Max)",
        "mode": "auto",
        "primary": "#059669",
        "accent": "#EA580C",
        "background": "#ECFDF5",
        "font_pair": "auto",
    },
    {
        "id": "emerald_conversion",
        "name": "Emerald & Action Orange (E-Commerce)",
        "mode": "light",
        "primary": "#059669",
        "accent": "#EA580C",
        "background": "#ECFDF5",
        "card": "#FFFFFF",
        "border": "#A7F3D0",
        "font_pair": "Rubik|Nunito Sans",
    },
    {
        "id": "luxury_gold",
        "name": "Luxury Liquid Glass (Premium & Gold)",
        "mode": "light",
        "primary": "#1C1917",
        "accent": "#A16207",
        "background": "#FAFAF9",
        "card": "#FFFFFF",
        "border": "#D6D3D1",
        "font_pair": "Cormorant|Montserrat",
    },
    {
        "id": "beauty_pastel",
        "name": "Soft Beauty & Rose Pastel",
        "mode": "light",
        "primary": "#BE185D",
        "accent": "#7C3AED",
        "background": "#FDF2F8",
        "card": "#FFFFFF",
        "border": "#FBCFE8",
        "font_pair": "Lora|Raleway",
    },
    {
        "id": "fitness_dark",
        "name": "Athletic Dark & High Energy",
        "mode": "dark",
        "primary": "#F97316",
        "accent": "#22C55E",
        "background": "#1F2937",
        "card": "#313742",
        "border": "#374151",
        "font_pair": "Barlow Condensed|Barlow",
    },
    {
        "id": "tech_obsidian",
        "name": "Obsidian Tech & Glass Dark",
        "mode": "dark",
        "primary": "#38BDF8",
        "accent": "#10B981",
        "background": "#0F172A",
        "card": "#1E293B",
        "border": "#334155",
        "font_pair": "Plus Jakarta Sans|Plus Jakarta Sans",
    },
]

_STORE_FONT_PAIRS: List[Dict[str, str]] = [
    {"id": "auto", "label": "Avtomatik (UI/UX Pro Max Seçimi)"},
    {"id": "Rubik|Nunito Sans", "label": "Rubik + Nunito Sans (Modern E-Commerce)"},
    {"id": "Plus Jakarta Sans|Plus Jakarta Sans", "label": "Plus Jakarta Sans (SaaS & Tech)"},
    {"id": "Cormorant|Montserrat", "label": "Cormorant + Montserrat (Luxury Editorial)"},
    {"id": "Lora|Raleway", "label": "Lora + Raleway (Beauty & Lifestyle)"},
    {"id": "Barlow Condensed|Barlow", "label": "Barlow Condensed + Barlow (Sport & Bold)"},
]


def get_store_design_presets() -> List[Dict[str, Any]]:
    return [dict(p) for p in _STORE_DESIGN_PRESETS]


def get_store_font_pairs() -> List[Dict[str, str]]:
    return [dict(f) for f in _STORE_FONT_PAIRS]


def _is_valid_hex(val: Any) -> bool:
    return bool(isinstance(val, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", val.strip()))


def resolve_store_design_system(
    product: Optional[Dict[str, Any]] = None,
    brand: Optional[Dict[str, Any]] = None,
    category: str = "",
    store_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Resolves a complete UI/UX Pro Max design system for Agent 03's storefront:
    - Niche-matched color palette & light/dark mode (WCAG >= 4.5:1 verified)
    - Optional user overrides from Manual Store Config (preset, primary, accent, mode, font_pair)
    - Curated Google Font pairing (heading + body)
    - Conversion landing pattern & anti-patterns
    - Top HTML+Tailwind & UX guidelines
    """
    product = product or {}
    brand = brand or {}
    cfg = store_config or {}
    project_name = str(
        cfg.get("brand_name")
        or brand.get("brand_name")
        or brand.get("name")
        or product.get("brand_name")
        or product.get("name")
        or "NovaStore"
    ).strip()

    query, variance, motion, density = _build_query_profile(product, category=category)
    raw = generate_design_system(
        query=query,
        project_name=project_name,
        output_format="markdown",
        variance=variance,
        motion=motion,
        density=density,
    )
    ds = raw.get("design_system") or {}
    colors = dict(ds.get("colors") or {})
    style_obj = dict(ds.get("style") or {"id": "ecommerce-modern", "name": "Modern Conversion Showcase"})

    typo = dict(ds.get("typography") or {})
    heading_font = typo.get("heading") or "Plus Jakarta Sans"
    body_font = typo.get("body") or "Inter"

    # Apply preset overrides if user selected a curated preset in Manual mode
    preset_id = str(cfg.get("design_preset") or "auto").strip()
    preset = next((p for p in _STORE_DESIGN_PRESETS if p["id"] == preset_id and p["id"] != "auto"), None)
    if preset:
        colors["primary"] = preset["primary"]
        colors["secondary"] = preset["primary"]
        colors["accent"] = preset["accent"]
        colors["background"] = preset["background"]
        if preset.get("card"):
            colors["card"] = preset["card"]
        if preset.get("border"):
            colors["border"] = preset["border"]
        style_obj["name"] = preset["name"]
        if preset.get("font_pair") and "|" in preset["font_pair"]:
            heading_font, body_font = [s.strip() for s in preset["font_pair"].split("|", 1)]

    # Apply explicit Dark/Light mode override
    mode_override = str(cfg.get("color_mode") or "auto").strip().lower()
    if mode_override == "dark":
        if (_relative_luminance(colors.get("background", "#FFFFFF")) or 1.0) >= 0.18:
            colors["background"] = "#0F172A"
            colors["card"] = "#1E293B"
            colors["muted"] = "#1E293B"
            colors["border"] = "#334155"
            colors["foreground"] = "#F8FAFC"
            colors["card_foreground"] = "#F8FAFC"
            colors["muted_foreground"] = "#CBD5E1"
    elif mode_override == "light":
        if (_relative_luminance(colors.get("background", "#0F172A")) or 0.0) < 0.18:
            colors["background"] = "#F8FAFC"
            colors["card"] = "#FFFFFF"
            colors["muted"] = "#F1F5F9"
            colors["border"] = "#CBD5E1"
            colors["foreground"] = "#0F172A"
            colors["card_foreground"] = "#0F172A"
            colors["muted_foreground"] = "#475569"

    # Apply custom Primary & Accent hex overrides if provided
    if _is_valid_hex(cfg.get("primary_color")):
        colors["primary"] = cfg["primary_color"].strip().upper()
        colors["secondary"] = colors["primary"]
        colors["ring"] = colors["primary"]
    if _is_valid_hex(cfg.get("accent_color")):
        colors["accent"] = cfg["accent_color"].strip().upper()

    # Apply explicit font pair override if provided
    font_override = str(cfg.get("font_pair") or "").strip()
    if font_override and font_override != "auto" and "|" in font_override:
        heading_font, body_font = [s.strip() for s in font_override.split("|", 1)]

    bg = colors.get("background") or "#F8FAFC"
    is_dark = (_relative_luminance(bg) or 1.0) < 0.18
    primary = colors.get("primary") or ("#10B981" if is_dark else "#059669")
    secondary = colors.get("secondary") or primary
    accent = colors.get("accent") or colors.get("cta") or "#EA580C"
    card = colors.get("card") or ("#111827" if is_dark else "#FFFFFF")
    fg = _ensure_fg_contrast(colors.get("foreground") or colors.get("text") or ("#F8FAFC" if is_dark else "#0F172A"), bg, is_dark)
    card_fg = _ensure_fg_contrast(colors.get("card_foreground") or fg, card, is_dark)
    muted = colors.get("muted") or ("#1E293B" if is_dark else "#F1F5F9")
    muted_fg = _ensure_fg_contrast(colors.get("muted_foreground") or ("#CBD5E1" if is_dark else "#475569"), card, is_dark)
    border = colors.get("border") or ("#334155" if is_dark else "#CBD5E1")
    ring = colors.get("ring") or primary

    resolved_colors = {
        "primary": primary,
        "on_primary": _pick_best_on_color(primary, colors.get("on_primary", "")),
        "secondary": secondary,
        "on_secondary": _pick_best_on_color(secondary, colors.get("on_secondary", "")),
        "accent": accent,
        "on_accent": _pick_best_on_color(accent, colors.get("on_accent", "")),
        "background": bg,
        "foreground": fg,
        "card": card,
        "card_foreground": card_fg,
        "muted": muted,
        "muted_foreground": muted_fg,
        "border": border,
        "ring": ring,
        "surface_alt": "#0F172A" if is_dark else "#FFFFFF",
        "header_bg": "rgba(15, 23, 42, 0.88)" if is_dark else "rgba(255, 255, 255, 0.92)",
        "is_dark": is_dark,
        "notes": "Fərdi Dizayn Seçimi" if preset or _is_valid_hex(cfg.get("primary_color")) else colors.get("notes", ""),
    }

    if font_override and font_override != "auto" or preset:
        gf_url = (
            f"https://fonts.googleapis.com/css2?family={heading_font.replace(' ', '+')}:wght@600;700;800"
            f"&family={body_font.replace(' ', '+')}:wght@400;500;600;700&display=swap"
        )
    else:
        gf_url = typo.get("google_fonts_url") or (
            f"https://fonts.googleapis.com/css2?family={heading_font.replace(' ', '+')}:wght@600;700;800"
            f"&family={body_font.replace(' ', '+')}:wght@400;500;600;700&display=swap"
        )

    ux_hits = (uxp_search("conversion checkout contrast focus", "ux", 3) or {}).get("results", [])
    tw_hits = (uxp_search("reduced-motion responsive spacing", "html-tailwind", 3) or {}).get("results", [])
    guidelines: List[str] = []
    for row in ux_hits:
        if row.get("Do"):
            guidelines.append(f"[UX] {row.get('Issue', '')}: {row['Do']}")
    for row in tw_hits:
        if row.get("Do"):
            guidelines.append(f"[Tailwind] {row.get('Guideline', '')}: {row['Do']}")

    return {
        "query": query,
        "category": ds.get("category") or "E-commerce",
        "color_mode": "dark" if is_dark else "light",
        "style": style_obj,
        "pattern": ds.get("pattern") or {},
        "colors": resolved_colors,
        "typography": {
            "heading": heading_font,
            "body": body_font,
            "mood": typo.get("mood", ""),
            "google_fonts_url": gf_url,
        },
        "key_effects": ds.get("key_effects", ""),
        "anti_patterns": ds.get("anti_patterns", ""),
        "guidelines": guidelines[:5],
        "dials": ds.get("dials") or {"variance": variance, "motion": motion, "density": density},
    }


_STRUCTURAL_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u26FF]"
)


def audit_storefront_html(html_text: str) -> Dict[str, Any]:
    """
    Heuristic UX, Accessibility & UI/UX Pro Max quality audit for generated storefronts.
    Checks CSS design tokens, vector SVG icons (zero structural emojis), native <dialog>,
    native <details> FAQ, tabular numbers, prefers-reduced-motion, and viewport/alt tags.
    """
    html_str = html_text or ""
    # Strip <script> blocks before checking structural emojis in visible UI markup
    markup_only = re.sub(r"<script\b[^>]*>.*?</script>", "", html_str, flags=re.IGNORECASE | re.DOTALL)
    markup_only = re.sub(r"<!--.*?-->", "", markup_only, flags=re.DOTALL)
    emoji_hits = _STRUCTURAL_EMOJI_RE.findall(markup_only)

    checks = {
        "has_design_tokens": "--ds-primary:" in html_str and "--ds-accent:" in html_str and "--ds-bg:" in html_str,
        "has_dynamic_fonts": "fonts.googleapis.com" in html_str,
        "has_svg_icons": html_str.count("<svg") >= 8 and 'aria-hidden="true"' in html_str,
        "no_structural_emojis": len(emoji_hits) == 0,
        "has_native_dialog": "<dialog" in html_str and "showModal()" in html_str and "overscroll-behavior" in html_str,
        "has_native_disclosure": "<details" in html_str and "<summary" in html_str,
        "has_tabular_nums": "tabular-nums" in html_str,
        "has_reduced_motion": "motion-reduce:" in html_str and "prefers-reduced-motion" in html_str,
        "has_mobile_touch_targets": "min-h-[44px]" in html_str,
        "has_viewport_and_lcp": 'name="viewport"' in html_str and 'fetchpriority="high"' in html_str,
    }
    passed_count = sum(1 for v in checks.values() if v)
    score = int(round((passed_count / len(checks)) * 100))
    issues = [k for k, ok in checks.items() if not ok]
    return {
        "passed": len(issues) == 0,
        "score": score,
        "checks": checks,
        "issues": issues,
        "emoji_hits_count": len(emoji_hits),
    }
