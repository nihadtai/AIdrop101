"""
Cross-Platform Radar Orchestrator (Shopify & Amazon Asynchronous Convergence)
=============================================================================

Runs tools/shopify_tracker.py and tools/amazon_tracker.py asynchronously in parallel.
When a product or concept appears in BOTH Shopify competitor stores AND Amazon
Movers & Shakers / Hot New Releases, it is identified as a high-confidence
"Qalib Məhsul" (Cross-Platform Winning Product) and boosted to the top of the
daily digest report (Top 10-20 products).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from tools.radar_common import (
    jaccard,
    title_tokens,
    utc_now_iso,
    write_report,
)
from tools.shopify_tracker import run_scan as run_shopify_scan
from tools.amazon_tracker import run_scan as run_amazon_scan

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = PROJECT_ROOT / "data" / "radar" / "reports"


# ──────────────────────────── CONVERGENCE MATCHER ────────────────────────────

def compute_title_similarity(title_a: str, title_b: str) -> Tuple[float, Set[str]]:
    """
    Computes lexical similarity between two product titles using Jaccard token
    overlap and significant noun keyword matching.
    """
    tok_a = title_tokens(title_a)
    tok_b = title_tokens(title_b)
    if not tok_a or not tok_b:
        return 0.0, set()

    sim = jaccard(tok_a, tok_b)
    overlap = tok_a & tok_b

    # Weight boost if multi-word specific product nouns match (e.g. 'charger', 'thermometer', 'mat')
    long_words = [w for w in overlap if len(w) >= 4]
    if len(long_words) >= 2:
        sim = max(sim, 0.35 + len(long_words) * 0.08)
    elif len(long_words) == 1 and len(tok_a) <= 3 and len(tok_b) <= 3:
        sim = max(sim, 0.30)

    return sim, overlap


def match_cross_platform_signals(
    shopify_signals: List[Dict[str, Any]],
    amazon_signals: List[Dict[str, Any]],
    threshold: float = 0.30,
) -> Tuple[List[Dict[str, Any]], Set[int], Set[int]]:
    """
    Finds products that appear in both Shopify stores and Amazon breakout lists.
    Returns:
      (winning_matches, matched_shopify_indices, matched_amazon_indices)
    """
    matches: List[Dict[str, Any]] = []
    matched_s_idx: Set[int] = set()
    matched_a_idx: Set[int] = set()

    for s_idx, s in enumerate(shopify_signals):
        best_sim = 0.0
        best_a_idx = -1
        best_overlap: Set[str] = set()

        for a_idx, a in enumerate(amazon_signals):
            sim, overlap = compute_title_similarity(s.get("title", ""), a.get("title", ""))
            if sim > best_sim:
                best_sim = sim
                best_a_idx = a_idx
                best_overlap = overlap

        if best_sim >= threshold and best_a_idx != -1:
            a = amazon_signals[best_a_idx]
            matched_s_idx.add(s_idx)
            matched_a_idx.add(best_a_idx)

            s_origin = s.get("origin", "Shopify Store")
            a_origin = a.get("origin", "Amazon")
            s_price = s.get("price")
            a_price = a.get("price")
            a_metrics = a.get("metrics", {})

            # Composite Winning Score: highest individual score + 40 bonus points
            base_score = max(s.get("score", 0), a.get("score", 0))
            winning_score = base_score + 40

            overlap_words = ", ".join(sorted(best_overlap)[:3])
            reasons = [
                f"🏆 DUAL-MARKET QALİB MƏHSUL: Həm {s_origin} (Shopify), həm də {a_origin} siyahısında təsdiqlənib!",
                f"🔗 Oxşar açar anlayış: [{overlap_words}]",
            ]
            if s_price and a_price:
                margin = round(s_price - a_price, 2)
                reasons.append(
                    f"💰 Qiymət marjası: Amazon ${a_price:.2f} ⇄ Shopify ${s_price:.2f} (Fərq: +${margin:.2f})"
                )
            if a_metrics.get("rating"):
                reasons.append(
                    f"⭐ Amazon Reytinqi: {a_metrics.get('rating')}/5.0 ({a_metrics.get('reviews', 0)} rəy)"
                )
            reasons.extend(s.get("reasons", [])[:2])

            unified_title = s.get("title") or a.get("title")

            matches.append({
                "source": "cross_platform",
                "is_cross_match": True,
                "title": unified_title,
                "score": winning_score,
                "similarity": round(best_sim, 2),
                "reasons": reasons,
                "price": s_price or a_price,
                "currency": "USD",
                "image": s.get("image") or a.get("image") or "",
                "url": s.get("url") or a.get("url") or "#",
                "shopify": {
                    "origin": s_origin,
                    "title": s.get("title"),
                    "price": s_price,
                    "url": s.get("url"),
                    "image": s.get("image"),
                    "score": s.get("score"),
                },
                "amazon": {
                    "origin": a_origin,
                    "title": a.get("title"),
                    "price": a_price,
                    "url": a.get("url"),
                    "image": a.get("image"),
                    "score": a.get("score"),
                    "rank": a_metrics.get("rank"),
                    "reviews": a_metrics.get("reviews"),
                    "rating": a_metrics.get("rating"),
                },
                "detected_at": utc_now_iso(),
            })

    return matches, matched_s_idx, matched_a_idx


def build_unified_report(
    shopify_report: Dict[str, Any],
    amazon_report: Dict[str, Any],
    top_n: int = 20,
) -> Dict[str, Any]:
    """
    Merges Shopify and Amazon signals into a unified Top 10-20 daily ranking.
    Dual-market confirmed products are placed at the very top.
    """
    s_signals = shopify_report.get("signals", [])
    a_signals = amazon_report.get("signals", [])

    cross_matches, matched_s, matched_a = match_cross_platform_signals(s_signals, a_signals)

    unified_signals: List[Dict[str, Any]] = list(cross_matches)

    # Add remaining unmatched Shopify signals
    for idx, s in enumerate(s_signals):
        if idx not in matched_s:
            s_copy = dict(s)
            s_copy["is_cross_match"] = False
            unified_signals.append(s_copy)

    # Add remaining unmatched Amazon signals
    for idx, a in enumerate(a_signals):
        if idx not in matched_a:
            a_copy = dict(a)
            a_copy["is_cross_match"] = False
            unified_signals.append(a_copy)

    # Sort descending by score
    unified_signals.sort(key=lambda x: x.get("score", 0), reverse=True)
    top_signals = unified_signals[:top_n]

    report = {
        "source": "unified",
        "generated_at": utc_now_iso(),
        "stats": {
            "cross_matches_count": len(cross_matches),
            "shopify_signals_count": len(s_signals),
            "amazon_signals_count": len(a_signals),
            "top_signals_count": len(top_signals),
            "shopify_stores_ok": shopify_report.get("stats", {}).get("stores_ok", 0),
            "shopify_stores_total": shopify_report.get("stats", {}).get("catalog_stores_total", 172),
            "amazon_pages_ok": amazon_report.get("stats", {}).get("pages_ok", 0),
        },
        "cross_matches": cross_matches,
        "signals": top_signals,
    }

    # Persist report to data/radar/reports/unified_latest.json
    write_report("unified", report)
    logger.info(
        f"Unified Radar report generated: {len(cross_matches)} cross-matches, "
        f"{len(top_signals)} top signals."
    )
    return report


# ──────────────────────────── ASYNC PARALLEL EXECUTION ───────────────────────

async def run_cross_radar_scan_async(
    category: Optional[str] = None,
    top_n: int = 20,
) -> Dict[str, Any]:
    """
    Executes Shopify and Amazon tracker scans concurrently in parallel threads
    and generates the unified daily digest report.
    """
    logger.info("🚀 Starting asynchronous parallel scan of Shopify & Amazon trackers...")
    started = datetime.now()

    # Run both blocking scans concurrently in thread pool
    shopify_future = asyncio.to_thread(run_shopify_scan, category=category, top_n=top_n)
    amazon_cats = [category] if category and category != "all" else None
    amazon_future = asyncio.to_thread(run_amazon_scan, categories=amazon_cats, top_n=top_n)

    shopify_report, amazon_report = await asyncio.gather(shopify_future, amazon_future)

    elapsed_s = (datetime.now() - started).total_seconds()
    logger.info(f"✅ Both scans completed in parallel in {elapsed_s:.1f}s.")

    unified_report = build_unified_report(shopify_report, amazon_report, top_n=top_n)
    unified_report["elapsed_seconds"] = round(elapsed_s, 1)
    return unified_report


def run_cross_radar_scan(
    category: Optional[str] = None,
    top_n: int = 20,
) -> Dict[str, Any]:
    """Synchronous entry point that runs the async parallel loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Inside an existing event loop (e.g. inside FastAPI / Jupyter)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(run_shopify_scan, category=category, top_n=top_n)
            amazon_cats = [category] if category and category != "all" else None
            f2 = executor.submit(run_amazon_scan, categories=amazon_cats, top_n=top_n)
            s_rep = f1.result()
            a_rep = f2.result()
            return build_unified_report(s_rep, a_rep, top_n=top_n)
    else:
        return asyncio.run(run_cross_radar_scan_async(category=category, top_n=top_n))


# ──────────────────────────── CLI ────────────────────────────────────────────

def _print_unified_report(rep: Dict[str, Any]) -> None:
    st = rep["stats"]
    signals = rep["signals"]
    cross = rep["cross_matches"]

    print("\n" + "=" * 95)
    print("  🛰️ UNIFIED TREND RADAR: SHOPIFY & AMAZON DUAL-MARKET RAPORU")
    print(f"  Qalib Məhsullar (İkili Təsdiq): {st['cross_matches_count']} | Top Reytinq: {len(signals)} məhsul")
    print(f"  Shopify: {st['shopify_signals_count']} siqnal ({st['shopify_stores_total']} mağaza) | Amazon: {st['amazon_signals_count']} siqnal")
    print("=" * 95 + "\n")

    if cross:
        print("🏆 DUAL-MARKET QALİB MƏHSULLAR (Həm Shopify, həm Amazon-da tapılanlar):")
        print("-" * 95)
        for idx, c in enumerate(cross, 1):
            sh = c.get("shopify", {})
            am = c.get("amazon", {})
            print(f"🔥 #{idx} [{c['score']} BAL] {c['title']}")
            print(f"   ↳ Shopify: {sh.get('origin')} (${sh.get('price') or '—'}) ↗ {sh.get('url')}")
            print(f"   ↳ Amazon:  #{am.get('rank') or '?'} (${am.get('price') or '—'}, ⭐ {am.get('rating')}) ↗ {am.get('url')}")
            for r in c["reasons"]:
                print(f"      • {r}")
            print()

    print("\n📊 ƏN YÜKSƏK BALA SAHİB TOP MƏHSULLARIN ÜMUMİ SİYAHISI:")
    print("-" * 95)
    for idx, s in enumerate(signals, 1):
        badge = "🏆 [DUAL-MARKET]" if s.get("is_cross_match") else f"[{s.get('source').upper()}]"
        price_str = f"${s['price']:.2f}" if s.get("price") is not None else "—"
        origin = s.get("origin") or s.get("shopify", {}).get("origin") or "Dual"
        print(f"{idx:2d}. {badge} [{s.get('score', 0)} bal] {origin} | {s['title'][:55]} | {price_str}")
        for r in s.get("reasons", [])[:2]:
            print(f"    ↳ {r}")


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")

    ap = argparse.ArgumentParser(description="Cross-platform Shopify & Amazon Radar Orchestrator")
    ap.add_argument("--category", help="Category filter (kitchen, electronics, beauty, etc.)")
    ap.add_argument("--top", type=int, default=20, help="Number of top products in final report (10-20)")
    args = ap.parse_args()

    unified = run_cross_radar_scan(category=args.category, top_n=args.top)
    _print_unified_report(unified)
