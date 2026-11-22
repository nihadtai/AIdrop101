"""
Shopify Store Tracker — Trend Radar module
==========================================

Scans a list of public Shopify stores and detects product-level trend signals by
diffing each scan against the previous snapshot. Runs completely outside the
LangGraph pipeline and uses **no LLM tokens**.

Data source
-----------
Every Shopify storefront exposes a public catalogue endpoint::

    https://<store>/products.json?limit=250

Optionally the "best-selling" collection ordering is requested as well::

    https://<store>/collections/all/products.json?sort_by=best-selling

Many stores rate-limit that second route (HTTP 429). The tracker never retries
or evades a 429 — it simply records that the best-seller rank was unavailable.

What it can and cannot see
--------------------------
Shopify does not publish inventory quantities or order counts, so exact revenue
cannot be computed. Instead the tracker scores these observable signals:

* **new_launch**      – product appeared since the previous scan (or was published
                        within ``new_product_days`` on the very first scan)
* **best_seller**     – position in the store's best-selling ordering
* **rank_climb**      – best-seller position improved vs. the previous scan
* **restock**         – product went from sold-out to available
* **discount**        – compare_at_price vs. price
* **price_fit**       – price inside the impulse-buy sweet spot
* **cross_store**     – similar product found in 2+ tracked stores (convergence)

Usage
-----
    python -m tools.shopify_tracker                 # scan all configured stores
    python -m tools.shopify_tracker --stores ridge.com,lomi.com --top 10
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.radar_common import (
    PoliteSession,
    RadarSignal,
    SnapshotStore,
    contains_any,
    jaccard,
    load_radar_config,
    parse_iso,
    title_tokens,
    utc_now_iso,
    write_report,
)

logger = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "stores": [],
    "max_workers": 4,
    "per_host_delay_s": 1.0,
    "products_per_store": 250,
    "try_best_selling_rank": True,
    "new_product_days": 7,
    "price_sweet_spot": [20, 80],
    "min_score": 35,
    "exclude_terms": ["gift card", "shipping protection", "insurance", "warranty"],
}


# ──────────────────────────── FETCHING ───────────────────────────────────────

def _normalise_product(p: Dict[str, Any], domain: str) -> Dict[str, Any]:
    variants = p.get("variants") or []
    prices = [float(v["price"]) for v in variants if v.get("price") not in (None, "")]
    compares = [
        float(v["compare_at_price"]) for v in variants
        if v.get("compare_at_price") not in (None, "", "0.00")
    ]
    images = p.get("images") or []
    return {
        "id": str(p.get("id")),
        "title": (p.get("title") or "").strip(),
        "handle": p.get("handle", ""),
        "vendor": p.get("vendor", ""),
        "product_type": p.get("product_type", ""),
        "tags": p.get("tags", []),
        "published_at": p.get("published_at"),
        "created_at": p.get("created_at"),
        "price": min(prices) if prices else None,
        "compare_at_price": max(compares) if compares else None,
        "available": any(v.get("available") for v in variants),
        "image": images[0].get("src", "") if images else "",
        "url": f"https://{domain}/products/{p.get('handle', '')}",
    }


def fetch_store(domain: str, http: PoliteSession, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch one store's catalogue (+ optional best-seller order). Never raises."""
    result: Dict[str, Any] = {
        "domain": domain, "ok": False, "status": None, "products": [],
        "best_selling_ids": [], "best_selling_status": None, "error": None,
    }
    limit = int(cfg.get("products_per_store", 250))
    try:
        r = http.get(f"https://{domain}/products.json?limit={limit}")
        result["status"] = r.status_code
        if r.status_code == 429:
            result["error"] = "rate_limited (429) — skipped this run"
            return result
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            result["error"] = f"HTTP {r.status_code} / not a public Shopify catalogue"
            return result
        raw = r.json().get("products", [])
        result["products"] = [_normalise_product(p, domain) for p in raw]
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    if cfg.get("try_best_selling_rank", True):
        try:
            r = http.get(
                f"https://{domain}/collections/all/products.json?limit=50&sort_by=best-selling"
            )
            result["best_selling_status"] = r.status_code
            if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                result["best_selling_ids"] = [str(p.get("id")) for p in r.json().get("products", [])]
        except Exception as e:
            result["best_selling_status"] = f"error: {type(e).__name__}"
    return result


# ──────────────────────────── SCORING ────────────────────────────────────────

def _score_product(
    prod: Dict[str, Any],
    prev: Optional[Dict[str, Any]],
    prev_exists: bool,
    best_rank: Optional[int],
    cfg: Dict[str, Any],
) -> Tuple[int, List[str], Dict[str, Any]]:
    score, reasons, metrics = 0, [], {}
    now = datetime.now(timezone.utc)

    # 1) New launch
    published = parse_iso(prod.get("published_at")) or parse_iso(prod.get("created_at"))
    age_days = (now - published).days if published else None
    metrics["age_days"] = age_days
    if prev_exists and prev is None:
        score += 30
        reasons.append("🆕 Son skandan sonra mağazaya yeni əlavə olunub")
    elif age_days is not None and age_days <= int(cfg.get("new_product_days", 7)):
        score += 25
        reasons.append(f"🆕 {age_days} gün əvvəl satışa çıxarılıb")

    # 2) Best-seller position & climb
    if best_rank is not None:
        metrics["best_seller_rank"] = best_rank
        if best_rank <= 10:
            score += 25
            reasons.append(f"🏆 Mağazanın Top-10 bestseller-i (#{best_rank})")
        elif best_rank <= 30:
            score += 15
            reasons.append(f"🏆 Mağazanın Top-30 bestseller-i (#{best_rank})")
        prev_rank = (prev or {}).get("best_rank")
        if prev_rank and best_rank < prev_rank:
            climb = prev_rank - best_rank
            metrics["rank_climb"] = climb
            if climb >= 5:
                score += 20
                reasons.append(f"📈 Bestseller sırasında {climb} pillə yüksəlib (#{prev_rank} → #{best_rank})")

    # 3) Restock
    if prev is not None and prev.get("available") is False and prod.get("available"):
        score += 10
        reasons.append("🔄 Tükənmişdi, yenidən stoka gəlib (tələbat siqnalı)")

    # 4) Discount
    price, cmp = prod.get("price"), prod.get("compare_at_price")
    if price and cmp and cmp > price:
        disc = round((1 - price / cmp) * 100)
        metrics["discount_pct"] = disc
        if disc >= 30:
            score += 10
            reasons.append(f"🏷️ {disc}% endirimlə satılır (aqressiv təklif)")

    # 5) Price fit
    lo, hi = cfg.get("price_sweet_spot", [20, 80])
    if price is not None:
        if lo <= price <= hi:
            score += 10
            reasons.append(f"💵 Qiymət impulsiv alış diapazonundadır (${price:.2f})")
        elif price > hi * 2:
            score -= 10

    if not prod.get("available"):
        score -= 15
        reasons.append("⚠️ Hazırda stokda yoxdur")

    return score, reasons, metrics


def _apply_cross_store(signals: List[RadarSignal], threshold: float = 0.5) -> None:
    """Boost products that have look-alikes in other tracked stores."""
    tokens = [title_tokens(s.title) for s in signals]
    for i, s in enumerate(signals):
        others = {
            signals[j].origin for j in range(len(signals))
            if j != i and signals[j].origin != s.origin and jaccard(tokens[i], tokens[j]) >= threshold
        }
        if others:
            s.score += 20
            s.metrics["cross_store_matches"] = sorted(others)
            s.reasons.append(f"🔗 Oxşar məhsul digər mağazalarda da var: {', '.join(sorted(others)[:3])}")


# ──────────────────────────── MAIN SCAN ──────────────────────────────────────

def run_scan(stores: Optional[List[str]] = None, category: Optional[str] = None, top_n: int = 20) -> Dict[str, Any]:
    """
    Scan Shopify stores, diff against previous snapshots, score signals and
    write a report to ``data/radar/reports/shopify_latest.json``.

    Returns a dict: ``{source, started_at, finished_at, stats, signals, store_errors}``.
    """
    cfg = {**DEFAULTS, **load_radar_config().get("shopify", {})}

    # Domain to category mapping
    categories_cfg = cfg.get("categories", {})
    domain_to_category: Dict[str, str] = {}
    for cat_name, cat_stores in categories_cfg.items():
        for st in cat_stores:
            domain_to_category[st.lower().strip()] = cat_name

    if stores:
        selected_stores = stores
    elif category and category.lower() != "all" and category.lower() in categories_cfg:
        selected_stores = categories_cfg[category.lower()]
    else:
        selected_stores = cfg["stores"]

    started = utc_now_iso()
    http = PoliteSession(
        min_delay_s=float(cfg["per_host_delay_s"]),
        global_delay_s=0.6,
        use_browser_ua=False,
    )
    snapshots = SnapshotStore("shopify")
    exclude = cfg.get("exclude_terms", [])

    fetched: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=int(cfg["max_workers"])) as pool:
        futures = {pool.submit(fetch_store, d, http, cfg): d for d in selected_stores}
        for fut in as_completed(futures):
            fetched.append(fut.result())

    all_signals: List[RadarSignal] = []
    store_errors: Dict[str, str] = {}
    stats = {"stores_total": len(selected_stores), "stores_ok": 0, "products_scanned": 0,
             "best_seller_rank_available": 0, "first_run_stores": 0}

    for res in fetched:
        domain = res["domain"]
        if not res["ok"]:
            store_errors[domain] = res["error"] or "unknown error"
            continue
        stats["stores_ok"] += 1
        stats["products_scanned"] += len(res["products"])

        prev_snap = snapshots.load(domain)
        prev_products: Dict[str, Any] = (prev_snap or {}).get("products", {})
        prev_exists = prev_snap is not None
        if not prev_exists:
            stats["first_run_stores"] += 1

        rank_map = {pid: i + 1 for i, pid in enumerate(res["best_selling_ids"])}
        if rank_map:
            stats["best_seller_rank_available"] += 1

        cat = domain_to_category.get(domain.lower(), "general")
        new_snapshot: Dict[str, Any] = {}
        for prod in res["products"]:
            pid = prod["id"]
            best_rank = rank_map.get(pid)
            new_snapshot[pid] = {
                "title": prod["title"], "price": prod["price"],
                "available": prod["available"], "best_rank": best_rank,
            }
            if contains_any(f"{prod['title']} {prod['product_type']}", exclude):
                continue
            score, reasons, metrics = _score_product(
                prod, prev_products.get(pid), prev_exists, best_rank, cfg
            )
            if score <= 0:
                continue
            all_signals.append(RadarSignal(
                source="shopify", origin=domain, title=prod["title"], url=prod["url"],
                score=score, reasons=reasons, image=prod["image"], price=prod["price"],
                metrics={**metrics, "vendor": prod["vendor"], "product_type": prod["product_type"], "category": cat},
            ))

        snapshots.save(domain, {"fetched_at": utc_now_iso(), "products": new_snapshot})

    _apply_cross_store(all_signals)
    min_score = int(cfg["min_score"])
    new_dicts = [s.to_dict() for s in all_signals if s.score >= min_score]

    # Merge with existing latest report signals from other stores (for drip/category scans)
    scanned_ok_domains = {r["domain"] for r in fetched if r.get("ok")}
    merged_signals = list(new_dicts)
    latest_path = Path(__file__).resolve().parent.parent / "data" / "radar" / "reports" / "shopify_latest.json"
    if latest_path.exists():
        try:
            import json as _json
            prev_rep = _json.loads(latest_path.read_text(encoding="utf-8"))
            for old_s in prev_rep.get("signals", []):
                if old_s.get("origin") not in scanned_ok_domains:
                    merged_signals.append(old_s)
        except Exception:
            pass

    merged_signals.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Diversity cap: max 3 signals per store domain in top_n so one store doesn't flood the digest
    diverse_signals: List[Dict[str, Any]] = []
    domain_counts: Dict[str, int] = {}
    for sig in merged_signals:
        dom = sig.get("origin", "")
        if domain_counts.get(dom, 0) < 3:
            diverse_signals.append(sig)
            domain_counts[dom] = domain_counts.get(dom, 0) + 1
            if len(diverse_signals) >= top_n:
                break

    report = {
        "source": "shopify",
        "started_at": started,
        "finished_at": utc_now_iso(),
        "baseline_run": stats["first_run_stores"] == stats["stores_ok"] and stats["stores_ok"] > 0,
        "stats": {
            **stats,
            "catalog_stores_total": len(cfg.get("stores", [])),
            "categories_total": len(categories_cfg),
            "signals_above_threshold": len(merged_signals),
        },
        "signals": diverse_signals,
        "store_errors": store_errors,
    }
    path = write_report("shopify", report)
    logger.info(f"Shopify scan done: {len(merged_signals)} signals, report → {path}")
    return report


# ──────────────────────────── CLI ────────────────────────────────────────────

def _print_report(report: Dict[str, Any]) -> None:
    st = report["stats"]
    print(
        f"\n=== Shopify Radar ===\n"
        f"Mağazalar: {st['stores_ok']}/{st['stores_total']} | "
        f"Skan edilən məhsullar: {st['products_scanned']} | "
        f"Bestseller sırası: {st['best_seller_rank_available']} mağazada | "
        f"Siqnallar: {st['signals_above_threshold']}"
    )
    if report["baseline_run"]:
        print("ℹ İlk skan (baseline): növbəti skanlarda yeni əlavələr və sıra dəyişiklikləri aşkarlanacaq.")
    print("-" * 95)
    for idx, s in enumerate(report["signals"], 1):
        price = f"${s['price']:.2f}" if s.get("price") is not None else "—"
        reasons = "; ".join(s["reasons"])
        print(f"{idx:2d}. [{s['score']} bal] {s['origin']} | {s['title'][:55]} | {price}")
        print(f"    ↳ {reasons}")
    if report["store_errors"]:
        print("\nƏlçatmaz mağazalar:")
        for d, e in report["store_errors"].items():
            print(f"  • {d}: {e}")


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    ap = argparse.ArgumentParser(description="Shopify trend radar")
    ap.add_argument("--stores", help="Comma-separated store domains (default: config)")
    ap.add_argument("--category", help="Category to filter (e.g. electronics, home, kitchen, fitness, etc.)")
    ap.add_argument("--top", type=int, default=20, help="Number of signals to keep")
    args = ap.parse_args()
    rep = run_scan(
        stores=args.stores.split(",") if args.stores else None,
        category=args.category,
        top_n=args.top
    )
    _print_report(rep)
