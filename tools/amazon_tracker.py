"""
Amazon Movers & Bestsellers Tracker — Trend Radar module
========================================================

Scans public Amazon category rankings (``bestsellers``, ``new-releases``, and
``movers-and-shakers`` when served) and detects breakout, dropshipping-friendly
products by diffing each scan against the previous snapshot.

Runs completely outside the LangGraph pipeline and uses **no LLM tokens**.

How it works
------------
1. Fetches public category pages on ``amazon.com`` using ``PoliteSession``
   (honest browser User-Agent, 4s delay between requests, no CAPTCHA bypass).
2. Parses product cards (``div#gridItemRoot`` + ``[data-client-recs-list]``):
   ASIN, rank, title, price, star rating, review count, image, and URL.
3. Filters out big household brands (Apple, Amazon Basics, Dove, etc.) and
   non-dropshippable consumables (food, supplements, books, toilet paper, etc.).
4. Computes a composite **Breakout Score** from:
   * **new_release_top**   – ranked in Amazon *Hot New Releases* (#1–#30)
   * **mover_spike**       – explicit % sales-rank surge (when Movers & Shakers is served)
   * **rank_jump**         – rank climbed vs. the previous snapshot (self-computed Mover)
   * **new_entry**         – entered Top-30 since the previous snapshot
   * **low_reviews_gem**   – Top-30 rank with < 500 reviews (early unsaturated product)
   * **high_rating**       – rating >= 4.3 stars (low return-rate risk)
   * **price_sweet_spot**  – Amazon price in $8–$45 (ideal for $29–$69 DTC markup)

Usage
-----
    python -m tools.amazon_tracker
    python -m tools.amazon_tracker --categories home-garden,kitchen,beauty --top 15
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from tools.radar_common import (
    PoliteSession,
    RadarSignal,
    SnapshotStore,
    contains_any,
    load_radar_config,
    utc_now_iso,
    write_report,
)

logger = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "marketplace": "https://www.amazon.com",
    "categories": [
        "home-garden",
        "kitchen",
        "beauty",
        "pet-supplies",
        "sporting-goods",
        "automotive",
    ],
    "lists": ["bestsellers", "new-releases"],
    "try_movers_and_shakers": True,
    "per_request_delay_s": 4.0,
    "max_requests_per_run": 20,
    "price_range": [8, 45],
    "early_trend_max_reviews": 500,
    "min_rating": 4.0,
    "min_score": 40,
    "exclude_brands": [],
    "exclude_terms": [],
}


# ──────────────────────────── HTML PARSING ───────────────────────────────────

def _parse_metadata_map(soup: BeautifulSoup) -> Dict[str, Dict[str, Any]]:
    """Extract ASIN → metadata from ``[data-client-recs-list]`` if present."""
    meta_by_asin: Dict[str, Dict[str, Any]] = {}
    for el in soup.select("[data-client-recs-list]"):
        raw = el.get("data-client-recs-list") or ""
        if not raw:
            continue
        try:
            items = json.loads(raw)
            for item in items:
                asin = item.get("id")
                if asin:
                    meta_by_asin[asin] = item.get("metadataMap") or {}
        except Exception:
            pass
    return meta_by_asin


def _parse_grid_card(card: Any, marketplace: str, meta_map: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Parse a single ``div#gridItemRoot`` card into a clean product dict."""
    # 1. ASIN
    asin = None
    asin_el = card.select_one("[data-asin]")
    if asin_el and asin_el.get("data-asin"):
        asin = asin_el["data-asin"].strip()
    if not asin:
        link_el = card.select_one("a[href*='/dp/']")
        if link_el:
            m = re.search(r"/dp/([A-Z0-9]{10})", link_el.get("href", ""))
            if m:
                asin = m.group(1)
    if not asin:
        return None

    # 2. Rank
    rank: Optional[int] = None
    badge = card.select_one("span.zg-bdg-text")
    if badge:
        m = re.search(r"(\d+)", badge.get_text())
        if m:
            rank = int(m.group(1))

    # 3. Title & Image
    title = ""
    img_url = ""
    img_el = card.select_one("img")
    if img_el:
        title = (img_el.get("alt") or "").strip()
        img_url = (img_el.get("src") or "").strip()
        dyn = img_el.get("data-a-dynamic-image")
        if dyn:
            try:
                urls = list(json.loads(dyn).keys())
                if urls:
                    img_url = urls[-1]  # highest resolution
            except Exception:
                pass

    if not title:
        for clamp in card.select("div[class*='line-clamp']"):
            t = clamp.get_text(" ", strip=True)
            if len(t) > 5:
                title = t
                break

    if not title:
        return None

    # 4. Rating & Reviews
    rating: Optional[float] = None
    reviews: Optional[int] = None
    star_link = card.select_one("a[aria-label*='out of 5 stars']")
    if star_link:
        label = star_link.get("aria-label", "")
        m_r = re.search(r"([\d.]+)\s+out of 5 stars", label)
        if m_r:
            rating = float(m_r.group(1))
        m_c = re.search(r"([\d,]+)\s+rating", label)
        if m_c:
            reviews = int(m_c.group(1).replace(",", ""))

    card_text = " | ".join(card.stripped_strings)
    if rating is None:
        m_r = re.search(r"([\d.]+)\s+out of 5 stars", card_text)
        if m_r:
            rating = float(m_r.group(1))
    if reviews is None:
        m_c = re.search(r"out of 5 stars\s*\|\s*([\d,]+)", card_text)
        if m_c:
            try:
                reviews = int(m_c.group(1).replace(",", ""))
            except ValueError:
                pass

    # 5. Price
    price: Optional[float] = None
    m_p = re.search(r"\$([\d,]+\.\d{2})", card_text)
    if m_p:
        try:
            price = float(m_p.group(1).replace(",", ""))
        except ValueError:
            pass

    # 6. Explicit Mover % change (if present on page or metadataMap)
    pct_change: Optional[int] = None
    meta = meta_map.get(asin) or {}
    raw_pct = str(meta.get("render.zg.bsms.percentageChange", "")).strip()
    if raw_pct and raw_pct.isdigit():
        pct_change = int(raw_pct)
    else:
        pct_el = card.select_one("span.zg-percent-change")
        if pct_el:
            m_pct = re.search(r"([\d,]+)%", pct_el.get_text())
            if m_pct:
                pct_change = int(m_pct.group(1).replace(",", ""))

    return {
        "asin": asin,
        "rank": rank,
        "title": title,
        "price": price,
        "rating": rating,
        "reviews": reviews,
        "pct_change": pct_change,
        "image": img_url,
        "url": f"{marketplace}/dp/{asin}",
    }


def fetch_category_list(
    category: str,
    list_type: str,
    http: PoliteSession,
    marketplace: str = "https://www.amazon.com",
) -> Dict[str, Any]:
    """Fetch a single Amazon category list page (`bestsellers`, `new-releases`, `movers-and-shakers`)."""
    url = f"{marketplace}/gp/{list_type}/{category}"
    out: Dict[str, Any] = {
        "category": category,
        "list_type": list_type,
        "url": url,
        "ok": False,
        "status": None,
        "items": [],
        "error": None,
    }
    try:
        r = http.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
        out["status"] = r.status_code
        html = r.text or ""
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}"
            return out
        if "captcha" in html.lower() or "api-services-support@amazon.com" in html:
            out["error"] = "captcha_or_throttled — skipped"
            return out

        soup = BeautifulSoup(html, "html.parser")
        meta_map = _parse_metadata_map(soup)
        cards = soup.select("div[id^='gridItemRoot']")
        items: List[Dict[str, Any]] = []
        for card in cards:
            parsed = _parse_grid_card(card, marketplace, meta_map)
            if parsed:
                items.append(parsed)

        out["items"] = items
        out["ok"] = len(items) > 0
        if not out["ok"]:
            out["error"] = "empty_grid (SSR list not populated)"
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out


# ──────────────────────────── SCORING ────────────────────────────────────────

def _score_item(
    item: Dict[str, Any],
    list_type: str,
    prev: Optional[Dict[str, Any]],
    prev_exists: bool,
    cfg: Dict[str, Any],
) -> Tuple[int, List[str], Dict[str, Any]]:
    score = 0
    reasons: List[str] = []
    rank = item.get("rank")
    price = item.get("price")
    rating = item.get("rating")
    reviews = item.get("reviews")
    pct_change = item.get("pct_change")

    metrics: Dict[str, Any] = {
        "asin": item["asin"],
        "rank": rank,
        "list_type": list_type,
        "rating": rating,
        "reviews": reviews,
    }

    # 1. List type & rank position
    if list_type == "new-releases":
        if rank and rank <= 10:
            score += 30
            reasons.append(f"🔥 Amazon Hot New Releases Top-10 (#{rank})")
        elif rank and rank <= 30:
            score += 20
            reasons.append(f"🆕 Amazon Hot New Releases Top-30 (#{rank})")
    elif list_type == "movers-and-shakers":
        score += 30
        reasons.append(f"🚀 Amazon Movers & Shakers siyahısında (#{rank or '?'})")
    elif list_type == "bestsellers":
        if rank and rank <= 10:
            score += 20
            reasons.append(f"🏆 Amazon Best Sellers Top-10 (#{rank})")
        elif rank and rank <= 30:
            score += 12
            reasons.append(f"🏆 Amazon Best Sellers Top-30 (#{rank})")

    # 2. Explicit % surge
    if pct_change and pct_change >= 100:
        score += 25
        metrics["pct_change"] = pct_change
        reasons.append(f"📈 Son 24 saatda satış reytinqi +{pct_change}% sıçrayıb")

    # 3. Snapshot diff: self-computed Mover (rank climb or new entry into Top-30)
    if prev_exists:
        if prev is None:
            score += 25
            reasons.append("⚡ Son skandan bəri Top-30 siyahısına yeni daxil olub")
        else:
            prev_rank = prev.get("rank")
            if prev_rank and rank and rank < prev_rank:
                jump = prev_rank - rank
                metrics["rank_jump"] = jump
                if jump >= 3:
                    score += min(30, 10 + jump * 2)
                    reasons.append(f"📈 Reytinqdə {jump} pillə yüksəlib (#{prev_rank} → #{rank})")

    # 4. Early / unsaturated trend signal (high rank but low review count)
    max_rev = int(cfg.get("early_trend_max_reviews", 500))
    if reviews is not None:
        if reviews <= 150:
            score += 25
            reasons.append(f"💎 Erkən trend: cəmi {reviews} rəy ilə Top siyahıdadır (bazar doymayıb)")
        elif reviews <= max_rev:
            score += 15
            reasons.append(f"🌱 Yeni yüksələn məhsul ({reviews} rəy — aşağı rəqabət)")
        elif reviews >= 25000:
            score -= 15
            reasons.append(f"⚠️ Çox köhnə/doymuş məhsul ({reviews:,} rəy)")

    # 5. Price window ($8–$45 on Amazon -> $29.99–$69.99 DTC retail)
    lo, hi = cfg.get("price_range", [8, 45])
    if price is not None:
        if lo <= price <= hi:
            score += 15
            est_retail = round(max(price * 2.5, price + 22), 2)
            metrics["suggested_dtc_price"] = est_retail
            reasons.append(
                f"💰 Dropshipping marjası üçün uyğun qiymət (Amazon: ${price:.2f} → tövsiyə satış: ~${est_retail:.2f})"
            )
        elif price < lo:
            score -= 5
        elif price > hi * 1.8:
            score -= 10

    # 6. Customer satisfaction (low return-rate indicator)
    min_rating = float(cfg.get("min_rating", 4.0))
    if rating is not None:
        if rating >= 4.4:
            score += 10
            reasons.append(f"⭐ Yüksək müştəri məmnuniyyəti ({rating}/5.0)")
        elif rating < min_rating:
            score -= 20
            reasons.append(f"⚠️ Aşağı reytinq ({rating}/5.0 — geri qaytarma riski)")

    return score, reasons, metrics


# ──────────────────────────── MAIN SCAN ──────────────────────────────────────

def run_scan(categories: Optional[List[str]] = None, top_n: int = 20) -> Dict[str, Any]:
    """
    Scan configured Amazon categories across Best Sellers & Hot New Releases,
    diff against previous snapshots, score breakout items, and save report to
    ``data/radar/reports/amazon_latest.json``.
    """
    cfg = {**DEFAULTS, **load_radar_config().get("amazon", {})}
    categories = categories or cfg["categories"]
    marketplace = cfg.get("marketplace", "https://www.amazon.com").rstrip("/")
    started = utc_now_iso()

    http = PoliteSession(min_delay_s=float(cfg.get("per_request_delay_s", 4.0)), timeout_s=20.0)
    snapshots = SnapshotStore("amazon")

    exclude_brands = cfg.get("exclude_brands", [])
    exclude_terms = cfg.get("exclude_terms", [])
    lists_to_scan = list(cfg.get("lists", ["bestsellers", "new-releases"]))

    max_reqs = int(cfg.get("max_requests_per_run", 20))
    reqs_done = 0

    signals_by_asin: Dict[str, RadarSignal] = {}
    page_errors: Dict[str, str] = {}
    stats = {
        "categories_total": len(categories),
        "pages_fetched": 0,
        "pages_ok": 0,
        "items_scanned": 0,
        "filtered_out": 0,
        "first_run_pages": 0,
    }

    for cat in categories:
        for list_type in lists_to_scan:
            if reqs_done >= max_reqs:
                break
            reqs_done += 1
            stats["pages_fetched"] += 1
            key = f"{cat}__{list_type}"
            res = fetch_category_list(cat, list_type, http, marketplace)
            if not res["ok"]:
                page_errors[key] = res["error"] or "unknown"
                continue

            stats["pages_ok"] += 1
            stats["items_scanned"] += len(res["items"])

            prev_snap = snapshots.load(key)
            prev_items: Dict[str, Any] = (prev_snap or {}).get("items", {})
            prev_exists = prev_snap is not None
            if not prev_exists:
                stats["first_run_pages"] += 1

            new_snap_items: Dict[str, Any] = {}
            for item in res["items"]:
                asin = item["asin"]
                new_snap_items[asin] = {
                    "rank": item["rank"],
                    "price": item["price"],
                    "reviews": item["reviews"],
                    "title": item["title"][:120],
                }

                # Filter out household megabrands and non-dropshippable consumables
                if contains_any(item["title"], exclude_brands) or contains_any(item["title"], exclude_terms):
                    stats["filtered_out"] += 1
                    continue

                score, reasons, metrics = _score_item(
                    item, list_type, prev_items.get(asin), prev_exists, cfg
                )
                if score <= 0:
                    continue

                origin = f"amazon:{cat}:{list_type}"
                existing = signals_by_asin.get(asin)
                if existing:
                    # Product appears in both Best Sellers AND New Releases -> extra boost!
                    existing.score += 15
                    for r in reasons:
                        if r not in existing.reasons:
                            existing.reasons.append(r)
                    existing.reasons.append(f"🔥 Həmçinin {list_type} siyahısında yer alır")
                else:
                    signals_by_asin[asin] = RadarSignal(
                        source="amazon",
                        origin=origin,
                        title=item["title"],
                        url=item["url"],
                        score=score,
                        reasons=reasons,
                        image=item.get("image") or "",
                        price=item.get("price"),
                        metrics={**metrics, "category": cat},
                    )

            snapshots.save(key, {"fetched_at": utc_now_iso(), "items": new_snap_items})

    min_score = int(cfg.get("min_score", 40))
    ranked = sorted(
        (s for s in signals_by_asin.values() if s.score >= min_score),
        key=lambda s: s.score,
        reverse=True,
    )

    report = {
        "source": "amazon",
        "started_at": started,
        "finished_at": utc_now_iso(),
        "baseline_run": stats["first_run_pages"] == stats["pages_ok"] and stats["pages_ok"] > 0,
        "stats": {**stats, "signals_above_threshold": len(ranked)},
        "signals": [s.to_dict() for s in ranked[:top_n]],
        "page_errors": page_errors,
    }
    path = write_report("amazon", report)
    logger.info(f"Amazon scan done: {len(ranked)} signals, report → {path}")
    return report


# ──────────────────────────── CLI ────────────────────────────────────────────

def _print_report(report: Dict[str, Any]) -> None:
    st = report["stats"]
    print(
        f"\n=== Amazon Radar ===\n"
        f"Səhifələr: {st['pages_ok']}/{st['pages_fetched']} | "
        f"Skan edilən: {st['items_scanned']} | "
        f"Filtrlənən (brend/qida): {st['filtered_out']} | "
        f"Siqnallar: {st['signals_above_threshold']}"
    )
    if report["baseline_run"]:
        print("ℹ İlk skan (baseline): növbəti skanlarda reytinq sıçrayışları (Movers) avtomatik hesablanacaq.")
    print("-" * 95)
    for idx, s in enumerate(report["signals"], 1):
        price = f"${s['price']:.2f}" if s.get("price") is not None else "—"
        m = s.get("metrics") or {}
        rr = f"⭐ {m.get('rating') or '—'} ({m.get('reviews') or 0} rəy)"
        reasons = "; ".join(s["reasons"])
        print(f"{idx:2d}. [{s['score']} bal] {s['origin']} | {s['title'][:55]} | {price} | {rr}")
        print(f"    ↳ {reasons}")
    if report["page_errors"]:
        print("\nXəta olan səhifələr:")
        for k, e in report["page_errors"].items():
            print(f"  • {k}: {e}")


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    ap = argparse.ArgumentParser(description="Amazon Movers & Bestsellers trend radar")
    ap.add_argument("--categories", help="Comma-separated category slugs (default: config)")
    ap.add_argument("--top", type=int, default=20, help="Number of signals to keep")
    args = ap.parse_args()
    rep = run_scan(
        categories=args.categories.split(",") if args.categories else None,
        top_n=args.top,
    )
    _print_report(rep)
