import base64
import html as html_lib
import logging
import mimetypes
import os
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_ADS_DIR = PROJECT_ROOT / "static" / "media" / "static_ads"
STATIC_ADS_DIR.mkdir(parents=True, exist_ok=True)


_CARD_LOCALES: Dict[str, Dict[str, str]] = {
    "en": {
        "flash_badge": "LIMITED LAUNCH OFFER",
        "save_prefix": "SAVE",
        "shipping_prefix": "Free Tracked Shipping",
        "guarantee": "30-Day Money-Back Guarantee",
        "payment": "Apple Pay • Card • PayPal Express",
        "cta_default": "CLAIM 50% OFF TODAY",
        "verified": "4.9 ★ Verified DTC Bestseller",
        "footer_tag": "Official Store Release",
    },
    "az": {
        "flash_badge": "MƏHDUD KAMPANİYA TƏKLİFİ",
        "save_prefix": "ENDİRİM",
        "shipping_prefix": "Pulsuz Sürətli Çatdırılma",
        "guarantee": "30 Gün Pul Geri Zəmanəti",
        "payment": "Onlayn Kart • Apple Pay • Təhlükəsiz Ödəniş",
        "cta_default": "İNDİ SİFARİŞ VER",
        "verified": "4.9 ★ Təsdiqlənmiş Bestseller",
        "footer_tag": "Rəsmi Mağaza Buraxılışı",
    },
    "de": {
        "flash_badge": "LIMITIERTES ANGEBOT",
        "save_prefix": "SPAREN",
        "shipping_prefix": "Kostenloser Versand mit Tracking",
        "guarantee": "30 Tage Geld-zurück-Garantie",
        "payment": "Apple Pay • Karte • PayPal Express",
        "cta_default": "JETZT 50% RABATT SICHERN",
        "verified": "4.9 ★ Verifizierter Bestseller",
        "footer_tag": "Offizieller Shop-Release",
    },
    "fr": {
        "flash_badge": "OFFRE LIMITÉE",
        "save_prefix": "ÉCONOMISEZ",
        "shipping_prefix": "Livraison Suivie Gratuite",
        "guarantee": "Garantie Satisfait ou Remboursé 30j",
        "payment": "Apple Pay • Carte • PayPal Express",
        "cta_default": "PROFITER DE -50% MAINTENANT",
        "verified": "4.9 ★ Best-seller Vérifié",
        "footer_tag": "Boutique Officielle",
    },
}


def _find_local_chrome() -> Optional[str]:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _image_to_data_uri(img_source: Optional[str], title: str) -> str:
    """Converts a local image file or http URL into an embeddable image source (or sleek SVG fallback)."""
    if img_source and Path(img_source).exists():
        raw = Path(img_source).read_bytes()
        mime, _ = mimetypes.guess_type(img_source)
        mime = mime or "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(raw).decode('utf-8')}"
    if img_source and str(img_source).startswith("http"):
        return str(img_source)
    short_title = html_lib.escape((title or "Official Product")[:28])
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 800" width="800" height="800">'
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0%" stop-color="#0f172a"/><stop offset="100%" stop-color="#1e293b"/>'
        '</linearGradient></defs>'
        '<rect width="800" height="800" fill="url(#bg)"/>'
        '<circle cx="400" cy="360" r="150" fill="none" stroke="#38bdf8" stroke-width="6" opacity="0.7"/>'
        f'<text x="400" y="610" text-anchor="middle" fill="#f8fafc" font-family="sans-serif" font-size="34" font-weight="700">{short_title}</text>'
        '</svg>'
    )
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg)


def _build_card_html(
    width: int,
    height: int,
    mode: str,
    product_name: str,
    brand_name: str,
    img_src: str,
    logo_src: str,
    selling_price: float,
    compare_price: float,
    discount_pct: int,
    delivery_days: str,
    cta_text: str,
    hook_headline: str,
    design_system: Dict[str, Any],
    language: str = "en",
) -> str:
    loc = _CARD_LOCALES.get(language, _CARD_LOCALES["en"])
    colors = (design_system or {}).get("colors") or {}
    typo = (design_system or {}).get("typography") or {}
    c_primary = colors.get("primary") or "#059669"
    c_accent = colors.get("accent") or "#F97316"
    c_bg = "#070B16" if mode == "endcard" else (colors.get("background") if colors.get("is_dark") else "#0B1120")
    h_font = html_lib.escape(str(typo.get("heading") or "Plus Jakarta Sans"))
    b_font = html_lib.escape(str(typo.get("body") or "Inter"))

    is_square = width == height
    p_name_esc = html_lib.escape(product_name[:56])
    brand_esc = html_lib.escape(brand_name[:28])
    hook_esc = html_lib.escape((hook_headline or product_name)[:92])
    cta_esc = html_lib.escape((cta_text or loc["cta_default"])[:40])
    deliv_esc = html_lib.escape(delivery_days or "6-10 Business Days")

    logo_block = (
        f'<img src="{html_lib.escape(logo_src)}" style="height:44px;max-width:160px;object-fit:contain;border-radius:10px;background:rgba(255,255,255,0.08);padding:4px 8px;">'
        if logo_src
        else f'<div style="display:inline-flex;align-items:center;gap:10px;"><div style="width:42px;height:42px;border-radius:12px;background:{c_primary};color:#fff;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:22px;">{brand_esc[:1].upper()}</div><span style="font-weight:800;font-size:24px;letter-spacing:-0.02em;color:#fff;">{brand_esc}</span></div>'
    )

    if is_square:
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family={h_font.replace(' ', '+')}:wght@700;800&family={b_font.replace(' ', '+')}:wght@500;600;700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{width:{width}px;height:{height}px;overflow:hidden;background:radial-gradient(circle at 80% 20%, {c_primary}35, transparent 52%), linear-gradient(155deg,#070b16 0%,{c_bg} 60%,#0f172a 100%);color:#f8fafc;font-family:'{b_font}',system-ui,sans-serif;padding:54px;display:flex;flex-direction:column;justify-content:space-between;}}
h1,h2,.head{{font-family:'{h_font}',system-ui,sans-serif;}}
</style></head><body>
<div style="display:flex;justify-content:space-between;align-items:center;">
  {logo_block}
  <span style="background:{c_accent};color:#fff;font-weight:800;font-size:22px;padding:12px 24px;border-radius:999px;box-shadow:0 8px 25px {c_accent}55;">{loc['save_prefix']} {discount_pct}% OFF</span>
</div>
<div style="display:grid;grid-template-columns:1.05fr 0.95fr;gap:36px;align-items:center;margin:20px 0;">
  <div style="display:flex;flex-direction:column;gap:18px;">
    <span style="display:inline-flex;width:fit-content;background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.16);color:#38bdf8;font-weight:700;font-size:18px;padding:8px 16px;border-radius:999px;">{loc['verified']}</span>
    <h1 class="head" style="font-size:48px;line-height:1.12;font-weight:800;letter-spacing:-0.02em;">{hook_esc}</h1>
    <div style="display:flex;align-items:baseline;gap:18px;margin-top:4px;">
      <span style="font-size:58px;font-weight:800;color:#10b981;">${selling_price:.2f}</span>
      <span style="font-size:30px;text-decoration:line-through;color:#94a3b8;font-weight:600;">${compare_price:.2f}</span>
    </div>
    <div style="display:flex;flex-direction:column;gap:8px;font-size:20px;color:#cbd5e1;font-weight:600;">
      <div>✓ {loc['shipping_prefix']} ({deliv_esc})</div>
      <div>✓ {loc['guarantee']}</div>
    </div>
  </div>
  <div style="position:relative;width:100%;aspect-ratio:1/1;border-radius:34px;overflow:hidden;border:4px solid {c_primary};background:#fff;box-shadow:0 24px 60px rgba(0,0,0,0.55);">
    <img src="{html_lib.escape(img_src)}" style="width:100%;height:100%;object-fit:cover;">
  </div>
</div>
<div style="display:flex;align-items:center;justify-content:space-between;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.14);border-radius:26px;padding:18px 26px;">
  <div>
    <div style="font-size:18px;font-weight:700;color:#e2e8f0;">{p_name_esc}</div>
    <div style="font-size:16px;color:#94a3b8;margin-top:2px;">{loc['payment']}</div>
  </div>
  <div style="background:linear-gradient(135deg,{c_primary},{c_accent});color:#fff;font-weight:800;font-size:24px;padding:18px 34px;border-radius:18px;box-shadow:0 12px 30px {c_primary}66;">{cta_esc} →</div>
</div>
</body></html>"""

    # 1080x1920 Vertical Story / End-Card layout
    top_label = loc["flash_badge"] if mode == "endcard" else loc["verified"]
    headline_display = p_name_esc if mode == "endcard" else hook_esc
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family={h_font.replace(' ', '+')}:wght@700;800&family={b_font.replace(' ', '+')}:wght@500;600;700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{width:{width}px;height:{height}px;overflow:hidden;background:radial-gradient(circle at 50% 18%, {c_primary}44, transparent 54%), linear-gradient(180deg,#060913 0%,{c_bg} 55%,#091326 100%);color:#f8fafc;font-family:'{b_font}',system-ui,sans-serif;padding:86px 64px 96px 64px;display:flex;flex-direction:column;justify-content:space-between;align-items:center;text-align:center;}}
h1,h2,.head{{font-family:'{h_font}',system-ui,sans-serif;}}
</style></head><body>
<div style="width:100%;display:flex;justify-content:space-between;align-items:center;">
  {logo_block}
  <span style="background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.18);padding:10px 20px;border-radius:999px;font-size:20px;font-weight:700;color:#cbd5e1;">{loc['footer_tag']}</span>
</div>

<div style="display:flex;flex-direction:column;align-items:center;gap:22px;width:100%;">
  <span style="background:linear-gradient(135deg,#e11d48,{c_accent});color:#fff;font-weight:800;font-size:28px;letter-spacing:0.04em;padding:16px 40px;border-radius:999px;box-shadow:0 12px 32px rgba(225,29,72,0.45);">{top_label} • {loc['save_prefix']} {discount_pct}%</span>
  <h1 class="head" style="font-size:62px;line-height:1.12;font-weight:800;letter-spacing:-0.02em;max-width:940px;">{headline_display}</h1>
</div>

<div style="position:relative;width:740px;height:740px;border-radius:48px;overflow:hidden;border:6px solid {c_primary};background:#fff;box-shadow:0 30px 80px rgba(0,0,0,0.65);">
  <img src="{html_lib.escape(img_src)}" style="width:100%;height:100%;object-fit:cover;">
  <div style="position:absolute;bottom:24px;right:24px;background:rgba(7,11,22,0.92);backdrop-filter:blur(8px);border:2px solid {c_primary};border-radius:24px;padding:14px 28px;display:flex;align-items:baseline;gap:14px;">
    <span style="font-size:52px;font-weight:800;color:#10b981;">${selling_price:.2f}</span>
    <span style="font-size:28px;text-decoration:line-through;color:#94a3b8;font-weight:600;">${compare_price:.2f}</span>
  </div>
</div>

<div style="width:100%;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.14);border-radius:34px;padding:30px 34px;display:flex;flex-direction:column;gap:14px;font-size:30px;font-weight:700;">
  <div style="color:#38bdf8;">✓ {loc['shipping_prefix']} ({deliv_esc})</div>
  <div style="color:#10b981;">✓ {loc['guarantee']}</div>
  <div style="color:#cbd5e1;font-size:24px;font-weight:600;">{loc['payment']}</div>
</div>

<div style="width:100%;background:linear-gradient(135deg,{c_primary},{c_accent});color:#fff;font-weight:800;font-size:38px;padding:32px 36px;border-radius:36px;box-shadow:0 18px 45px {c_primary}66;border:3px solid rgba(255,255,255,0.35);">
  {cta_esc} →
</div>
</body></html>"""


def _render_html_to_png_playwright(html_content: str, width: int, height: int, dest_path: Path) -> bool:
    chrome_path = _find_local_chrome()
    if not chrome_path:
        return False
    try:
        from playwright.sync_api import sync_playwright
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chrome_path, headless=True)
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            page.set_content(html_content, wait_until="load", timeout=15000)
            page.wait_for_timeout(450)
            page.screenshot(path=str(dest_path), type="png")
            browser.close()
        return dest_path.exists() and dest_path.stat().st_size > 15000
    except Exception as exc:
        logger.debug(f"Playwright HTML->PNG fallback note: {exc}")
        return False


def _render_pillow_fallback(
    width: int,
    height: int,
    product_img_path: Optional[Path],
    product_name: str,
    brand_name: str,
    selling_price: float,
    compare_price: float,
    discount_pct: int,
    delivery_days: str,
    cta_text: str,
    dest_path: Path,
    language: str = "en",
) -> Path:
    """Clean emoji-free Pillow fallback renderer when local Chrome/Playwright is unavailable."""
    from PIL import Image, ImageDraw, ImageFont

    loc = _CARD_LOCALES.get(language, _CARD_LOCALES["en"])
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (width, height), (9, 14, 28))
    draw = ImageDraw.Draw(canvas)

    def _font(sz: int):
        for fp in ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            if os.path.exists(fp):
                try:
                    return ImageFont.truetype(fp, sz)
                except Exception:
                    pass
        return ImageFont.load_default()

    draw.rounded_rectangle([(60, 75), (width - 60, 175)], radius=32, fill=(225, 29, 72))
    draw.text((width // 2, 125), f"{loc['flash_badge']} - {discount_pct}% OFF", font=_font(38), fill=(255, 255, 255), anchor="mm")

    img_box_size = min(width - 260, int(height * 0.42))
    x0 = (width - img_box_size) // 2
    y0 = 230
    draw.rounded_rectangle([(x0 - 8, y0 - 8), (x0 + img_box_size + 8, y0 + img_box_size + 8)], radius=32, fill=(255, 255, 255), outline=(16, 185, 129), width=6)
    if product_img_path and product_img_path.exists():
        try:
            p_img = Image.open(product_img_path).convert("RGB").resize((img_box_size, img_box_size), Image.Resampling.LANCZOS)
            canvas.paste(p_img, (x0, y0))
        except Exception:
            pass

    ty = y0 + img_box_size + 65
    draw.text((width // 2, ty), f"{brand_name} | {product_name[:28]}", font=_font(42), fill=(255, 255, 255), anchor="mm")
    draw.text((width // 2, ty + 70), f"${selling_price:.2f}   (Was ${compare_price:.2f})", font=_font(44), fill=(16, 185, 129), anchor="mm")
    draw.text((width // 2, ty + 140), f"{loc['shipping_prefix']} ({delivery_days})", font=_font(32), fill=(56, 189, 248), anchor="mm")

    btn_y0 = height - 210
    draw.rounded_rectangle([(60, btn_y0), (width - 60, height - 80)], radius=36, fill=(16, 185, 129))
    draw.text((width // 2, (btn_y0 + height - 80) // 2), (cta_text or loc["cta_default"])[:34], font=_font(42), fill=(255, 255, 255), anchor="mm")

    canvas.save(dest_path, format="PNG")
    return dest_path


def render_branded_creative_cards(
    product: Dict[str, Any],
    supplier: Dict[str, Any],
    economics: Dict[str, Any],
    brand: Dict[str, Any],
    store_config: Dict[str, Any],
    cta_text: str,
    hook_headline: str,
    run_uid: str,
    local_product_img_path: Optional[Path] = None,
    language: str = "en",
) -> Dict[str, str]:
    """
    Renders 3 zero-cost ($0) 2x-crisp branded PNG creative assets via Playwright HTML->PNG:
      1) Scene 3 CTA End-Card (1080x1920)
      2) Meta/TikTok Story & Reels Static Ad (1080x1920)
      3) Meta/IG Feed Static Retargeting Ad (1080x1080)
    """
    from tools.design_intel import resolve_store_design_system

    lang = (language or "en").strip().lower()
    if lang not in _CARD_LOCALES:
        lang = "en"

    product_name = str(
        (product.get("english_name") if lang != "az" else product.get("name"))
        or product.get("name")
        or "Featured Product"
    ).strip()
    brand_name = str(
        store_config.get("brand_name")
        or brand.get("brand_name")
        or brand.get("name")
        or (product_name.split()[0] if product_name.split() else "Apex") + "Labs"
    ).strip()

    try:
        sp = round(float(store_config.get("selling_price") or economics.get("selling_price") or 39.99), 2)
    except Exception:
        sp = 39.99
    try:
        raw_cp = store_config.get("compare_at_price")
        cp = round(float(raw_cp), 2) if raw_cp and float(raw_cp) > sp else round(sp * 2.0, 2)
    except Exception:
        cp = round(sp * 2.0, 2)
    disc_pct = max(5, int(round(((cp - sp) / cp) * 100))) if cp > sp else 50
    delivery_days = str(supplier.get("delivery_days") or product.get("delivery_days") or "6-10 Business Days").strip()
    logo_src = str(store_config.get("logo_url") or brand.get("logo_url") or "").strip()

    ds = resolve_store_design_system(
        product=product,
        brand=brand,
        store_config=store_config if store_config.get("mode") == "manual" else None,
    )
    img_src = _image_to_data_uri(
        str(local_product_img_path) if (local_product_img_path and local_product_img_path.exists()) else product.get("image_url"),
        product_name,
    )

    endcard_path = STATIC_ADS_DIR / f"scene3_endcard_{run_uid}.png"
    story_path = STATIC_ADS_DIR / f"static_story_{run_uid}.png"
    feed_path = STATIC_ADS_DIR / f"static_feed_{run_uid}.png"

    specs = [
        ("endcard", 1080, 1920, endcard_path),
        ("story", 1080, 1920, story_path),
        ("feed", 1080, 1080, feed_path),
    ]
    for mode, w, h, dest in specs:
        html_doc = _build_card_html(
            width=w,
            height=h,
            mode=mode,
            product_name=product_name,
            brand_name=brand_name,
            img_src=img_src,
            logo_src=logo_src,
            selling_price=sp,
            compare_price=cp,
            discount_pct=disc_pct,
            delivery_days=delivery_days,
            cta_text=cta_text,
            hook_headline=hook_headline,
            design_system=ds,
            language=lang,
        )
        ok = _render_html_to_png_playwright(html_doc, w, h, dest)
        if not ok:
            _render_pillow_fallback(
                width=w,
                height=h,
                product_img_path=local_product_img_path,
                product_name=product_name,
                brand_name=brand_name,
                selling_price=sp,
                compare_price=cp,
                discount_pct=disc_pct,
                delivery_days=delivery_days,
                cta_text=cta_text,
                dest_path=dest,
                language=lang,
            )

    return {
        "cta_endcard_path": str(endcard_path),
        "cta_endcard_url": f"/static/media/static_ads/{endcard_path.name}",
        "static_story_path": str(story_path),
        "static_story_url": f"/static/media/static_ads/{story_path.name}",
        "static_feed_path": str(feed_path),
        "static_feed_url": f"/static/media/static_ads/{feed_path.name}",
    }
