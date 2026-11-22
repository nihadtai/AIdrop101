"""
E-Commerce Multi-Agent Pipeline — LangGraph Orchestration
==========================================================

Defines the full workflow graph:

  START
    → product_research
    → human_approval (product)
    → sourcing
    → human_approval (supplier)
    → ┌ social_media_setup ┐
      │ store_build        │  (parallel)
      └ creative_production┘
    → human_approval (creatives)
    → media_buying
    → monitoring ↺ (loop or END)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal

from google import genai
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt, Send

from state.graph_state import ECommerceState

from agents.product_research import create_product_research_node
from agents.market_research import create_market_research_node
from agents.sourcing import create_sourcing_node
from agents.social_media import create_social_media_node
from agents.store_builder import create_store_builder_node
from agents.creative import create_creative_node
from agents.media_buyer import create_media_buyer_node

from tools.notification_tools import send_telegram_notification
from tools.catalog_store import save_catalog_item
from tools.design_intel import (
    get_store_design_presets,
    get_store_font_pairs,
    resolve_store_design_system,
)

logger = logging.getLogger(__name__)


# ──────────────────────────── HUMAN APPROVAL NODES ────────────────────────────

def human_approval_product(state: ECommerceState) -> dict:
    """Interrupt the graph so a human can review the 3 discovered winning products and pick one."""
    products = state.get("products", [])
    if not products:
        products = [state.get("selected_product", {})] if state.get("selected_product") else []

    # Build product summaries for the approval message
    product_summaries = []
    for i, product in enumerate(products[:3], 1):
        name = product.get("name", "Bilinməyən Məhsul")
        what_it_does = product.get("what_it_does", product.get("description", "Məlumat yoxdur"))
        wow_factor = product.get("wow_factor", "Məlumat yoxdur")
        image_url = product.get("image_url", "")
        video_ad_url = product.get("video_ad_url", "")
        product_summaries.append(
            f"**{i}. {name}**\n"
            f"   ⚙️ {what_it_does}\n"
            f"   ✨ Wow: {wow_factor}\n"
            f"   🖼️ Şəkil: {image_url}\n"
            f"   🎥 Video: {video_ad_url}"
        )

    message = (
        "🔍 **1-ci Agent: 3 Uğurlu Məhsul Tapıldı!**\n\n"
        + "\n\n".join(product_summaries)
        + "\n\n"
        "👉 **Qərarınız:** Məhsullardan birini seçmək üçün **approved** (təsdiq), hamısını rədd edib yeni axtarış üçün **rejected** (rədd) seçin."
    )

    # Try to get the researched product name from the last decision or current selected product
    sp = state.get("selected_product") or {}
    researched_product = sp.get("name", "")

    decision = interrupt({
        "step": "product_approval",
        "message": message,
        "products": products[:3],
        "market_research": state.get("market_research_report", ""),
        "researched_product": researched_product
    })

    if isinstance(decision, dict):
        decision_str = decision.get("decision", "approved_0").strip().lower()
    else:
        decision_str = str(decision).strip().lower()

    selected_product = None
    if decision_str.startswith("approved") or decision_str.startswith("research"):
        parts = decision_str.split("_")
        idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if idx < len(products):
            selected_product = products[idx]
        else:
            selected_product = products[0] if products else {}
    
    is_rejected = decision_str == "rejected"
    is_research = decision_str.startswith("research")
    rejected_names = [p.get("name", "") for p in products[:3]] if is_rejected else []
    user_id = (state.get("user_id") or "default_user").strip() or "default_user"

    try:
        from tools.discovery_pool import mark_products_for_user
        if is_rejected and products:
            mark_products_for_user(user_id, products[:3], status="rejected")
        elif selected_product and not is_research:
            mark_products_for_user(user_id, [selected_product], status="approved")
    except Exception as pool_err:
        logger.debug(f"Discovery pool status update note: {pool_err}")

    if is_rejected:
        status_text = "rədd edildi (yeni məhsullar axtarılacaq)"
    elif is_research:
        status_text = f"bazar araşdırması tələb edildi: '{selected_product.get('name', '') if selected_product else ''}'"
    else:
        status_text = f"təsdiqləndi: '{selected_product.get('name', '') if selected_product else ''}'"

    result = {
        "pending_approval": None,
        "human_approvals": [{"step": "product_approval", "decision": decision_str, "product_name": selected_product.get("name", "") if selected_product else ""}],
        "logs": [f"[HumanApproval] Məhsul qərarı: {status_text} | Şəkil: {selected_product.get('image_url', '') if selected_product else ''}"],
    }

    if selected_product:
        result["selected_product"] = selected_product

    if is_rejected:
        result["rejected_products"] = rejected_names

    return result


def _build_supplier_from_link(
    link: str,
    product: Dict[str, Any],
    base_supplier: Optional[Dict[str, Any]] = None,
    margin_mult: float = 3.5,
) -> Dict[str, Any]:
    """
    Build normalized supplier + unit_economics record from a user-supplied URL:
      - 1688.com -> routes through 1688 -> HyperSKU/SourcinBox 3PL -> YunExpress -> Customer
      - aliexpress.com -> resolves via AliExpress DS API when configured
      - other URLs -> preserves link with honest unit economics
    """
    from agents.sourcing import compute_unit_economics
    from tools.aliexpress_ds_tool import get_aliexpress_ds_product_detail, parse_aliexpress_url
    from tools.cargo_3pl import calculate_1688_yunexpress_landed_cost, extract_1688_offer_id, is_1688_url
    from tools.sourcing_tools import get_live_fx_rates, scrape_1688_link_direct

    sup = dict(base_supplier or {})
    clean_link = (link or "").strip()
    prod_name = str(product.get("english_name") or product.get("name") or sup.get("product_name") or "Product").strip()
    prod_img = str(product.get("image_url") or sup.get("product_image_url") or "").strip()
    sup["order_link"] = clean_link

    if is_1688_url(clean_link):
        sup["channel"] = "1688_yunexpress"
        sup["platform"] = "1688.com -> YunExpress (3PL)"
        sup["order_link_1688"] = clean_link
        sup["cj_pid"] = ""
        sup["cj_vid"] = ""
        sup["ae_product_id"] = ""
        sup["ae_sku_id"] = ""
        offer_id = extract_1688_offer_id(clean_link)
        if offer_id:
            sup["product_sku"] = f"1688-{offer_id}"

        scraped = scrape_1688_link_direct(clean_link)
        fx = get_live_fx_rates()
        cny_rate = float(fx.get("CNY") or 7.15)
        price_cny = scraped.get("price_cny") if scraped else None
        if scraped:
            sup["supplier_name"] = scraped.get("supplier_name") or "1688.com -> YunExpress (3PL)"
            if scraped.get("image_url"):
                sup["product_image_url"] = scraped["image_url"]
        else:
            sup["supplier_name"] = "1688.com Fabrik + YunExpress (3PL)"
        if not sup.get("product_image_url") and prod_img:
            sup["product_image_url"] = prod_img

        landed = calculate_1688_yunexpress_landed_cost(price_cny=price_cny, fx_cny_per_usd=cny_rate)
        sup["logistic_line"] = landed["logistic_line"]
        sup["delivery_days"] = landed["delivery_days"]
        sup["price_cny"] = price_cny
        sup["cargo_breakdown"] = landed["cost_breakdown"]
        sup["is_live_verified"] = bool(price_cny)
        sup["unit_economics"] = compute_unit_economics(landed["product_cost"], landed["shipping_cost"], margin_mult=margin_mult)
        sup["product_name"] = (scraped or {}).get("product_name") or prod_name
        return sup

    ae_pid = parse_aliexpress_url(clean_link)
    if ae_pid:
        sup["channel"] = "aliexpress_ds"
        sup["platform"] = "AliExpress DS API"
        sup["ae_product_id"] = ae_pid
        sup["cj_pid"] = ""
        sup["cj_vid"] = ""
        detail = get_aliexpress_ds_product_detail(ae_pid, target_country="US")
        if detail:
            sup["supplier_name"] = f"AliExpress DS — {detail.get('store_name') or prod_name[:32]}"
            sup["product_name"] = detail.get("name") or prod_name
            sup["ae_sku_id"] = detail.get("sku_id") or ""
            sup["product_sku"] = detail.get("sku") or f"AE-{ae_pid}"
            sup["logistic_line"] = detail.get("logistic_line") or "AliExpress Selection Standard"
            sup["delivery_days"] = detail.get("delivery_days") or "8-14 days"
            if detail.get("image_url"):
                sup["product_image_url"] = detail["image_url"]
            sup["is_live_verified"] = bool(detail.get("product_cost_usd"))
            sup["unit_economics"] = compute_unit_economics(
                detail.get("product_cost_usd"),
                detail.get("estimated_shipping_usd") or 3.90,
                margin_mult=margin_mult,
            )
            return sup

    if not sup.get("product_image_url") and prod_img:
        sup["product_image_url"] = prod_img
    return sup


def human_approval_supplier(state: ECommerceState) -> dict:
    """
    Interrupt for supplier selection & unified Product-Supplier binding:
      - User can pick AliExpress DS or CJ Dropshipping candidate, OR paste a manual 1688 link (triggers 1688 -> YunExpress 3PL)
      - Optional Backup #2 link for universal stock/price failover
      - Creates unified binding_sku linking Product + Supplier + Logistics method before entering Agent 3
    """
    from tools.binding_store import create_or_update_binding, detect_sourcing_channel
    from tools.sourcing_tools import get_live_fx_rates, search_1688_by_image_playwright
    from tools.supplier_monitor import register_supplier_watch

    supplier = dict(state.get("selected_supplier") or {})
    suppliers_list = [dict(s) for s in (state.get("suppliers") or []) if isinstance(s, dict)]
    if not suppliers_list and supplier:
        suppliers_list = [supplier]
    top_suppliers = suppliers_list[:6]
    economics = dict(state.get("unit_economics") or supplier.get("unit_economics") or {})
    product = dict(state.get("selected_product") or {})
    product_name = product.get("name", "Məhsul")
    margin_mult = float(product.get("estimated_margin_multiplier", 3.5) or 3.5)

    decision = interrupt({
        "step": "supplier_approval",
        "message": (
            f"🏭 **2-ci Mərhələ: Tədarükçü Seçimi & 1688-YunExpress / AliExpress / CJ ({product_name})**\n\n"
            "AliExpress DS və CJ Dropshipping tədarükçülərindən birini seçin, yaxud əl ilə 1688 linki daxil edib təsdiqləyin "
            "(1688 -> HyperSKU/SourcinBox -> YunExpress -> Müştəri sistemi aktivləşəcək)."
        ),
        "supplier": supplier,
        "suppliers": top_suppliers,
        "unit_economics": economics,
        "product": product,
    })

    manual_link = ""
    backup_link = ""
    auto_failover = True
    if isinstance(decision, dict):
        decision_str = str(decision.get("decision", "approved")).strip().lower()
        manual_link = str(decision.get("manual_link") or "").strip()
        backup_link = str(decision.get("backup_link") or "").strip()
        if "auto_failover" in decision:
            auto_failover = bool(decision.get("auto_failover"))
    else:
        decision_str = str(decision).strip().lower()

    selected_idx = None
    if decision_str.startswith("approved_"):
        try:
            selected_idx = int(decision_str.split("_", 1)[1])
        except (ValueError, IndexError):
            selected_idx = None

    if selected_idx is not None and 0 <= selected_idx < len(top_suppliers):
        chosen = dict(top_suppliers[selected_idx])
        supplier = chosen
        if chosen.get("unit_economics"):
            economics = dict(chosen["unit_economics"])

    updates: Dict[str, Any] = {
        "pending_approval": None,
        "human_approvals": [{"step": "supplier_approval", "decision": decision_str}],
        "logs": [f"[HumanApproval] Tədarükçü qərarı: {decision_str}"],
    }

    if decision_str == "search_1688_image":
        from tools.cargo_3pl import calculate_1688_yunexpress_landed_cost
        from agents.sourcing import compute_unit_economics

        img_url = product.get("image_url") or supplier.get("product_image_url") or ""
        updates["logs"].append(f"📸 1688-də şəkil ilə axtarış edilir: {img_url}")
        if img_url:
            vis_res = search_1688_by_image_playwright(image_url=img_url, max_items=6)
            vis_suppliers = vis_res.get("suppliers") or []
            if vis_suppliers:
                best = vis_suppliers[0]
                fx = get_live_fx_rates()
                landed = calculate_1688_yunexpress_landed_cost(best.get("price_cny"), fx_cny_per_usd=fx.get("CNY") or 7.15)
                economics = compute_unit_economics(landed["product_cost"], landed["shipping_cost"], margin_mult=margin_mult)
                supplier = {
                    **supplier,
                    "channel": "1688_yunexpress",
                    "supplier_name": best.get("supplier_name", "1688.com Fabrik"),
                    "platform": "1688.com -> YunExpress (3PL)",
                    "order_link_1688": best.get("product_url") or supplier.get("order_link_1688"),
                    "order_link": best.get("product_url") or supplier.get("order_link"),
                    "product_image_url": best.get("image_url") or img_url,
                    "price_cny": best.get("price_cny"),
                    "logistic_line": landed["logistic_line"],
                    "delivery_days": landed["delivery_days"],
                    "unit_economics": economics,
                    "is_live_verified": bool(best.get("price_cny")),
                }
                updates["logs"].append("✅ 1688 şəkil axtarışı tamamlandı və YunExpress 3PL maya hesablandı.")
            else:
                updates["logs"].append(f"⚠️ {vis_res.get('reason') or 'Şəkil ilə oxşar 1688 tədarükçüsü tapılmadı.'}")
        else:
            updates["logs"].append("⚠️ Axtarış üçün məhsul şəkli tapılmadı.")

    elif manual_link and decision_str.startswith("approved"):
        updates["logs"].append(f"🔗 1-ci Tədarükçü linki emal edilir: {manual_link}")
        supplier = _build_supplier_from_link(manual_link, product, base_supplier=supplier, margin_mult=margin_mult)
        economics = dict(supplier.get("unit_economics") or economics)
        if detect_sourcing_channel(supplier, manual_link) == "1688_yunexpress":
            updates["logs"].append("🚚 1688 -> HyperSKU/SourcinBox 3PL -> YunExpress -> Müştəri sistemi aktivləşdirildi.")

    backup_supplier: Optional[Dict[str, Any]] = None
    if backup_link and decision_str.startswith("approved"):
        updates["logs"].append(f"🛡️ 2-ci Ehtiyat (Failover) Tədarükçü linki qeyd edilir: {backup_link}")
        backup_supplier = _build_supplier_from_link(
            backup_link,
            product,
            base_supplier={"supplier_name": "Ehtiyat Tədarükçü (#2)", "chinese_keywords": supplier.get("chinese_keywords", "")},
            margin_mult=margin_mult,
        )

    all_for_watch = [s for s in suppliers_list]
    if backup_supplier:
        all_for_watch.insert(0, backup_supplier)
    if not backup_supplier and len(all_for_watch) > 1:
        for cand in all_for_watch:
            if cand.get("order_link") != supplier.get("order_link") or cand.get("supplier_name") != supplier.get("supplier_name"):
                backup_supplier = cand
                break

    # Encode unified Product + Supplier + Logistics method before handing off to Agent 3
    if decision_str.startswith("approved") or decision_str == "search_1688_image":
        binding_obj = create_or_update_binding(
            product=product,
            primary_supplier=supplier,
            backup_supplier=backup_supplier,
            unit_economics=economics,
        )
        supplier = dict(binding_obj["primary_supplier"])
        product = dict(binding_obj["product"])
        updates["selected_product"] = product
        updates["selected_supplier"] = supplier
        updates["unit_economics"] = economics
        updates["bound_sourcing"] = binding_obj
        updates["binding_sku"] = binding_obj["binding_sku"]
        updates["suppliers"] = all_for_watch[:6]
        updates["logs"].append(
            f"🔐 Məhsul + Tədarükçü kodlandı: SKU `{binding_obj['binding_sku']}` ({binding_obj['fulfillment_method']})"
        )
        try:
            register_supplier_watch(
                supplier,
                all_for_watch,
                product_name=product_name,
                binding_sku=binding_obj["binding_sku"],
                auto_failover=auto_failover,
            )
        except Exception as watch_err:
            logger.debug(f"Monitor watch registration note: {watch_err}")
    else:
        updates["selected_supplier"] = supplier
        updates["unit_economics"] = economics

    return updates

def _collect_suggested_images(product: Dict[str, Any], supplier: Dict[str, Any], prev_config: Dict[str, Any]) -> List[str]:
    """Collect deduplicated candidate product image URLs from product, supplier, and prior store_config."""
    candidates: List[str] = []
    sources: List[Any] = [
        prev_config.get("product_images"),
        product.get("gallery_images"),
        product.get("images"),
        product.get("image_url"),
        product.get("product_image_url"),
        supplier.get("images"),
        supplier.get("image_url"),
        supplier.get("product_image_url"),
    ]
    for item in sources:
        if isinstance(item, list):
            for u in item:
                if isinstance(u, str) and u.strip() and u.strip() not in candidates:
                    candidates.append(u.strip())
        elif isinstance(item, str) and item.strip() and item.strip() not in candidates:
            candidates.append(item.strip())
    return candidates[:8]


def human_approval_store_config(state: ECommerceState) -> dict:
    """
    HITL interrupt BEFORE Agent 03 builds the storefront.
    Gives the user 2 paths:
      1) Auto (1-click): Build immediately with AI + UI/UX Pro Max defaults.
      2) Manual: Customize brand_name, optional logo_url, colors/typography, selling_price, and >= 4 product_images.
    """
    product = dict(state.get("selected_product") or {})
    supplier = dict(state.get("selected_supplier") or {})
    economics = dict(state.get("unit_economics") or supplier.get("unit_economics") or {})
    brand = dict(state.get("brand_identity") or state.get("brand") or {})
    prev_config = dict(state.get("store_config") or {})
    category = state.get("category", "")

    if supplier.get("product_image_url") and not product.get("image_url"):
        product["image_url"] = supplier.get("product_image_url")

    recommended_design = resolve_store_design_system(
        product=product,
        brand=brand,
        category=category,
        store_config=prev_config if prev_config.get("mode") == "manual" else None,
    )
    suggested_images = _collect_suggested_images(product, supplier, prev_config)

    decision = interrupt({
        "step": "store_config_approval",
        "message": (
            "⚙️ **3-cü Agentdən Əvvəl: Mağaza Konfiqurasiyası**\n\n"
            "Vebsayt hazırlanmazdan əvvəl iki seçim edə bilərsiniz:\n"
            "1) **Avtomatik (1-Klik):** Heç bir parametr dəyişmədən UI/UX Pro Max ilə dərhal hazırlansın.\n"
            "2) **Manual Seçimlər:** Brend adı, loqo (ixtiyari), dizayn (rəng və şrift), qiymət və minimum 4 məhsul şəkli təyin edin."
        ),
        "product": product,
        "supplier": supplier,
        "unit_economics": economics,
        "brand": brand,
        "store_config": prev_config,
        "recommended_design": recommended_design,
        "design_presets": get_store_design_presets(),
        "font_pairs": get_store_font_pairs(),
        "suggested_images": suggested_images,
    })

    decision_str = "approved"
    cfg: Dict[str, Any] = {}
    if isinstance(decision, dict):
        decision_str = str(decision.get("decision", "approved")).strip().lower()
        raw_cfg = decision.get("store_config")
        if isinstance(raw_cfg, dict):
            cfg = dict(raw_cfg)
    else:
        decision_str = str(decision).strip().lower()

    if decision_str.startswith("rejected"):
        return {
            "current_phase": "store_config_approval",
            "pending_approval": None,
            "human_approvals": [{"step": "store_config_approval", "decision": decision_str}],
            "logs": [f"[HumanApproval] Mağaza konfiqurasiyası ləğv edildi, tədarükçü mərhələsinə qayıdılır."],
        }

    mode = str(cfg.get("mode") or "auto").strip().lower()
    if mode not in ("auto", "manual"):
        mode = "auto"

    updates: Dict[str, Any] = {
        "current_phase": "store_config_approval",
        "pending_approval": None,
        "human_approvals": [{"step": "store_config_approval", "decision": decision_str}],
    }

    if mode == "manual":
        clean_images = [
            str(u).strip() for u in (cfg.get("product_images") or []) if isinstance(u, str) and str(u).strip()
        ]
        brand_name = str(cfg.get("brand_name") or "").strip()
        logo_url = str(cfg.get("logo_url") or "").strip()

        normalized_cfg: Dict[str, Any] = {
            "mode": "manual",
            "brand_name": brand_name,
            "logo_url": logo_url,
            "design_preset": str(cfg.get("design_preset") or "auto").strip(),
            "color_mode": str(cfg.get("color_mode") or "auto").strip(),
            "primary_color": str(cfg.get("primary_color") or "").strip(),
            "accent_color": str(cfg.get("accent_color") or "").strip(),
            "font_pair": str(cfg.get("font_pair") or "auto").strip(),
            "product_images": clean_images,
        }

        if cfg.get("selling_price") not in (None, ""):
            try:
                sp = round(float(cfg["selling_price"]), 2)
                if sp > 0:
                    normalized_cfg["selling_price"] = sp
                    economics["selling_price"] = sp
                    cost = float(economics.get("product_cost") or 0)
                    ship = float(economics.get("shipping_cost") or 5.0)
                    cpa = float(economics.get("cpa") or 10.0)
                    economics["net_profit"] = round(sp - cost - ship - cpa, 2)
            except (TypeError, ValueError):
                pass

        if cfg.get("compare_at_price") not in (None, ""):
            try:
                cp = round(float(cfg["compare_at_price"]), 2)
                if cp > 0:
                    normalized_cfg["compare_at_price"] = cp
            except (TypeError, ValueError):
                pass

        if brand_name:
            brand["brand_name"] = brand_name
            brand["name"] = brand_name
        if logo_url:
            brand["logo_url"] = logo_url

        if clean_images:
            product["gallery_images"] = clean_images
            product["image_url"] = clean_images[0]

        updates["store_config"] = normalized_cfg
        updates["brand"] = brand
        updates["brand_identity"] = brand
        updates["unit_economics"] = economics
        updates["selected_product"] = product
        updates["logs"] = [
            f"[HumanApproval] Manual mağaza konfiqurasiyası tətbiq edildi | Brend: {brand_name or 'Avtomatik'} | Şəkil sayı: {len(clean_images)}"
        ]
    else:
        updates["store_config"] = {"mode": "auto"}
        updates["logs"] = ["[HumanApproval] Avtomatik (1-Klik) mağaza hazırlanması seçildi."]

    return updates


def reset_product_cycle(state: ECommerceState) -> dict:
    """
    Reset transient per-product cycle fields in LangGraph checkpoint before returning to Agent 1.
    Preserves accumulated `winning_catalog` and `rejected_products` so prior winning/rejected items are kept.
    """
    logger.info("[Pipeline] 🔄 1-ci Agentə qayıdış: əvvəlki məhsul dövrünün state məlumatları təmizlənir...")
    return {
        "current_phase": "product_research",
        "products": [],
        "selected_product": None,
        "market_research_report": None,
        "suppliers": [],
        "selected_supplier": None,
        "unit_economics": None,
        "bound_sourcing": None,
        "binding_sku": None,
        "brand": None,
        "brand_identity": None,
        "store": None,
        "store_config": None,
        "store_copy": None,
        "wordpress_landing_page_html": None,
        "social_accounts": [],
        "content_plan": [],
        "social_media": None,
        "postiz": None,
        "creatives": [],
        "creative_assets": None,
        "manual_video_links": [],
        "creative_config": {},
        "completed_parallel_nodes": [],
        "pending_approval": None,
        "logs": ["[StateReset] Əvvəlki məhsul dövrünün məlumatları təmizləndi; yeni məhsul axtarışına keçilir."],
    }


def _archive_winning_product(state: ECommerceState) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build and persist a compact 'winning product' catalog item after Agent 3 approval."""
    product = dict(state.get("selected_product") or {})
    supplier = dict(state.get("selected_supplier") or {})
    economics = dict(state.get("unit_economics") or supplier.get("unit_economics") or {})
    bound_sourcing = dict(state.get("bound_sourcing") or {})
    binding_sku = str(state.get("binding_sku") or bound_sourcing.get("binding_sku") or supplier.get("binding_sku") or "").strip()
    store = dict(state.get("store") or {})
    store_config = dict(state.get("store_config") or store.get("store_config") or {})
    brand = dict(state.get("brand_identity") or state.get("brand") or {})

    existing_catalog = [dict(c) for c in (state.get("winning_catalog") or []) if isinstance(c, dict)]
    prod_name = str(product.get("name") or (store.get("copy") or {}).get("product_title") or "Uğurlu Məhsul").strip()
    brand_name = str(
        store_config.get("brand_name")
        or brand.get("brand_name")
        or brand.get("name")
        or prod_name
    ).strip()

    item_id = f"win_{uuid.uuid4().hex[:10]}"
    for cur in existing_catalog:
        if str((cur.get("product") or {}).get("name") or "").strip().lower() == prod_name.lower():
            item_id = cur.get("id") or item_id
            break

    entry = {
        "id": item_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "winning_product",
        "brand_name": brand_name,
        "binding_sku": binding_sku,
        "bound_sourcing": bound_sourcing,
        "product": product,
        "supplier": supplier,
        "unit_economics": economics,
        "store": store,
        "store_config": store_config,
        "brand": brand,
        "supply_summary": {
            "configured": True,
            "binding_sku": binding_sku,
            "channel": bound_sourcing.get("channel") or supplier.get("channel") or "",
            "fulfillment_method": bound_sourcing.get("fulfillment_method") or supplier.get("fulfillment_method") or "",
            "label": "Avtomatlaşdırılmış tədarük konfiqurasiya edildi",
        },
    }

    try:
        saved = save_catalog_item(entry)
        entry["id"] = saved.get("id", item_id)
    except Exception as err:
        logger.warning(f"Catalog disk persistence warning: {err}")

    updated_catalog = [c for c in existing_catalog if c.get("id") != entry["id"]]
    updated_catalog.insert(0, entry)
    return updated_catalog, entry


def human_approval_store(state: ECommerceState) -> dict:
    """Interrupt for reviewing the Agent 3 storefront before saving as winning product & entering Stage 1 decision."""
    store = state.get("store") or {}
    product = state.get("selected_product") or {}

    decision = interrupt({
        "step": "store_approval",
        "message": (
            "🛍️ **3-cü Mərhələ: Mağaza Mətni və Dizaynı Hazırdır**\n\n"
            "Mağazanı təsdiqlədikdən sonra məhsul 'Uğurlu Məhsul' kimi yadda saxlanacaq və "
            "növbəti məhsul əlavə etmək / 2-ci mərhələyə keçmək pəncərəsi açılacaq."
        ),
        "store": store,
        "product": product,
    })

    if isinstance(decision, dict):
        decision_str = decision.get("decision", "approved").strip().lower()
    else:
        decision_str = str(decision).strip().lower()

    updates: Dict[str, Any] = {
        "current_phase": "store_approval",
        "pending_approval": None,
        "human_approvals": [{"step": "store_approval", "decision": decision_str}],
        "logs": [f"[HumanApproval] Mağaza qərarı: {decision_str}"],
    }

    if decision_str.startswith("approved"):
        catalog, saved_entry = _archive_winning_product(state)
        updates["winning_catalog"] = catalog
        updates["active_catalog_id"] = saved_entry["id"]
        prod_name = str((saved_entry.get("product") or {}).get("name") or "").strip()
        if prod_name:
            updates["rejected_products"] = [prod_name]  # avoid re-suggesting same product on loop back
        updates["logs"].append(f"🏆 '{prod_name}' uğurlu məhsul kimi kataloqa əlavə edildi.")

    return updates


def human_approval_stage_one(state: ECommerceState) -> dict:
    """
    Stage 1 Completion Window (shown right after Agent 3 approval):
      - Displays saved winning products catalog & minimal automated supply status
      - Option A: Return to Agent 1 (`add_another_product`) with state reset to discover another product
      - Option B: Proceed to Stage 2 (`approved` / `proceed_stage_2`) with Agents 4 & 5 parallel -> Agent 6
    """
    catalog = list(state.get("winning_catalog") or [])
    active_id = state.get("active_catalog_id") or (catalog[0]["id"] if catalog else None)
    latest_item = next((c for c in catalog if c.get("id") == active_id), catalog[0] if catalog else None)
    product = (latest_item or {}).get("product") or state.get("selected_product") or {}

    decision = interrupt({
        "step": "stage_one_decision",
        "message": (
            "🏆 **1-ci Mərhələ Tamamlandı: Məhsul Uğurla Hazırlandı!**\n\n"
            "Məhsul, avtomatlaşdırılmış tədarük konfiqurasiyası və mağaza saytı kataloqa yadda saxlanıldı.\n"
            "İstəsəniz **1-ci Agentə Qayıt** seçib növbəti məhsulu əlavə edə və ya **2-ci Mərhələyə Keç** "
            "düyməsi ilə 4-cü & 5-ci Agentləri (paralel) və 6-cı Agenti işə sala bilərsiniz."
        ),
        "winning_catalog": catalog,
        "active_catalog_id": active_id,
        "latest_item": latest_item,
        "product": product,
        "supply_summary": {
            "configured": True,
            "binding_sku": (latest_item or {}).get("binding_sku") or state.get("binding_sku") or "",
            "label": "Avtomatlaşdırılmış tədarük konfiqurasiya edildi",
        },
    })

    selected_catalog_id = active_id
    if isinstance(decision, dict):
        decision_str = str(decision.get("decision", "approved")).strip().lower()
        if decision.get("selected_catalog_id"):
            selected_catalog_id = str(decision["selected_catalog_id"]).strip()
    else:
        decision_str = str(decision).strip().lower()

    updates: Dict[str, Any] = {
        "current_phase": "stage_one_decision",
        "pending_approval": None,
        "active_catalog_id": selected_catalog_id,
        "human_approvals": [{"step": "stage_one_decision", "decision": decision_str}],
    }

    if decision_str == "add_another_product":
        updates["logs"] = ["[Stage1] İstifadəçi yeni məhsul əlavə etmək üçün 1-ci Agentə qayıdır..."]
        return updates

    # Load chosen winning product entry into active Stage 2 state
    chosen_item = next((c for c in catalog if c.get("id") == selected_catalog_id), latest_item)
    if isinstance(chosen_item, dict):
        if chosen_item.get("product"):
            updates["selected_product"] = chosen_item["product"]
        if chosen_item.get("supplier"):
            updates["selected_supplier"] = chosen_item["supplier"]
        if chosen_item.get("bound_sourcing"):
            updates["bound_sourcing"] = chosen_item["bound_sourcing"]
        if chosen_item.get("binding_sku"):
            updates["binding_sku"] = chosen_item["binding_sku"]
        if chosen_item.get("unit_economics"):
            updates["unit_economics"] = chosen_item["unit_economics"]
        if chosen_item.get("store"):
            updates["store"] = chosen_item["store"]
        if chosen_item.get("store_config"):
            updates["store_config"] = chosen_item["store_config"]
        if chosen_item.get("brand"):
            updates["brand"] = chosen_item["brand"]
            updates["brand_identity"] = chosen_item["brand"]

    chosen_name = ((chosen_item or {}).get("product") or {}).get("name") or product.get("name", "Seçilmiş Məhsul")
    updates["logs"] = [f"[Stage1 -> Stage2] '{chosen_name}' üçün 2-ci Mərhələ (4-cü & 5-ci Agent paralel → 6-cı Agent) başladılır."]
    return updates


def human_approval_video_sources(state: ECommerceState) -> dict:
    """Interrupt to let user configure Stage 2 AI UGC Studio (Language, Tier, Variant Count, Cost Estimator, Avatar, B-Roll)."""
    from tools.creative_tools import get_ugc_avatar_presets
    from tools.fal_video_engine import check_creative_preflight

    product = state.get("selected_product") or {}
    supplier = state.get("selected_supplier") or {}
    store = state.get("store") or {}
    store_config = state.get("store_config") or store.get("store_config") or {}

    from agents.creative import PLACEHOLDER_IMAGE_PATTERNS
    gallery_images: List[str] = []
    for src in [
        store.get("gallery_images"),
        store_config.get("product_images"),
        product.get("gallery_images"),
        [product.get("image_url")] if product.get("image_url") else [],
        [supplier.get("product_image_url")] if supplier.get("product_image_url") else [],
    ]:
        if isinstance(src, list):
            for u in src:
                s = str(u).strip() if isinstance(u, str) else ""
                if (
                    s
                    and (s.startswith("http") or s.startswith("/static/"))
                    and not any(p in s.lower() for p in PLACEHOLDER_IMAGE_PATTERNS)
                    and s not in gallery_images
                ):
                    gallery_images.append(s)

    prev_cfg = dict(state.get("creative_config") or {})
    prev_feedback = (state.get("creative_feedback") or "").strip()
    init_tier = str(prev_cfg.get("quality_tier") or "balanced").strip().lower()
    init_variants = 3 if int(prev_cfg.get("variant_count") or 3) >= 3 else 1
    init_lang = str(prev_cfg.get("ad_language") or "en").strip().lower()
    init_broll_mode = str(prev_cfg.get("broll_mode") or "auto").strip().lower()
    if isinstance(prev_cfg.get("broll_image_urls"), list) and prev_cfg["broll_image_urls"]:
        for u in reversed(prev_cfg["broll_image_urls"]):
            s = str(u).strip() if isinstance(u, str) else ""
            if s and s not in gallery_images:
                gallery_images.insert(0, s)

    has_sup_vid = bool(supplier.get("video_url") or supplier.get("product_video_url"))
    try:
        preflight = check_creative_preflight(
            quality_tier=init_tier,
            variant_count=init_variants,
            has_supplier_video=has_sup_vid,
            broll_mode=init_broll_mode,
            strict=False,
            image_count=len(gallery_images),
        )
    except Exception:
        preflight = {}

    decision = interrupt({
        "step": "video_source_approval",
        "message": (
            "🎬 **2-ci Mərhələ (4-cü & 5-ci Agent): Hybrid AI UGC Studio & Sosial Media**\n\n"
            "Dil, Keyfiyyət Rejimi (Economy / Balanced / Cinema) və A/B Test Sayını seçib **Təsdiq Et** klikləyin."
        ),
        "product": product,
        "supplier": supplier,
        "gallery_images": gallery_images,
        "avatar_presets": get_ugc_avatar_presets(language=init_lang),
        "preflight_status": preflight,
        "previous_config": prev_cfg,
        "creative_feedback": prev_feedback,
        "social_already_ready": bool(state.get("content_plan")),
    })

    creative_cfg: Dict[str, Any] = {
        "ad_language": init_lang,
        "quality_tier": init_tier,
        "variant_count": init_variants,
    }
    if prev_feedback:
        creative_cfg["rejection_feedback"] = prev_feedback
    if isinstance(decision, dict):
        decision_str = decision.get("decision", "approved").strip().lower()
        v1 = (decision.get("video_url_1") or "").strip()
        v2 = (decision.get("video_url_2") or "").strip()
        if decision.get("ad_language"):
            creative_cfg["ad_language"] = str(decision["ad_language"]).strip().lower()
        if decision.get("quality_tier"):
            creative_cfg["quality_tier"] = str(decision["quality_tier"]).strip().lower()
        if decision.get("variant_count") not in (None, ""):
            try:
                creative_cfg["variant_count"] = 3 if int(decision["variant_count"]) >= 3 else 1
            except (TypeError, ValueError):
                pass
        if decision.get("avatar_id"):
            creative_cfg["avatar_id"] = decision.get("avatar_id").strip()
        if decision.get("custom_avatar_url"):
            creative_cfg["custom_avatar_url"] = decision.get("custom_avatar_url").strip()
        if decision.get("broll_mode"):
            creative_cfg["broll_mode"] = decision.get("broll_mode").strip()
        if decision.get("custom_script"):
            creative_cfg["custom_script"] = decision.get("custom_script").strip()
        if decision.get("feedback"):
            creative_cfg["rejection_feedback"] = str(decision["feedback"]).strip()
        if isinstance(decision.get("broll_image_urls"), list) and decision.get("broll_image_urls"):
            creative_cfg["broll_image_urls"] = [
                str(u).strip() for u in decision["broll_image_urls"] if isinstance(u, str) and str(u).strip()
            ]
    else:
        decision_str = str(decision).strip().lower()
        v1 = ""
        v2 = ""

    updates = {
        "current_phase": "video_source_approval",
        "pending_approval": None,
        "creative_config": creative_cfg,
        "creative_feedback": creative_cfg.get("rejection_feedback") or prev_feedback or None,
        "completed_parallel_nodes": [],
        "human_approvals": [{"step": "video_source_approval", "decision": decision_str}],
        "logs": [
            f"[HumanApproval] Stage 2 konfiqurasiyası təsdiqləndi "
            f"(Dil={creative_cfg['ad_language']}, Rejim={creative_cfg['quality_tier']}, Variant={creative_cfg['variant_count']}, Avatar={creative_cfg.get('avatar_id', 'auto')})"
            + (" | 5-ci Agentin hazır planı qorunur" if state.get("content_plan") else "")
        ],
    }

    video_links = []
    if v1:
        video_links.append(v1)
    if v2:
        video_links.append(v2)
    if video_links:
        updates["manual_video_links"] = video_links
        updates["logs"].append(f"📹 B-Roll video mənbələri qəbul edildi: {len(video_links)} ədəd")

    return updates


def human_approval_creatives(state: ECommerceState) -> dict:
    """Interrupt for reviewing parallel Agent 4 (Creatives + Static Ads) + Agent 5 (Social Media) before Agent 6 (Media Buyer)."""
    creatives = state.get("creatives", [])
    creative_assets = state.get("creative_assets") or {}
    brand = state.get("brand") or {}
    product = state.get("selected_product") or {}
    content_plan = state.get("content_plan", [])
    social_media = state.get("social_media") or {
        "brand": brand,
        "content_plan": content_plan,
        "postiz": state.get("postiz", {}),
    }
    ok_count = sum(1 for c in creatives if c.get("video_url") and c.get("status", "completed") == "completed")

    decision = interrupt({
        "step": "creatives_approval",
        "message": (
            "🎬 **4-cü və 5-ci Agentlər Hazırdır (Videolar + Statik Reklamlar + Sosial Media)**\n\n"
            f"🎨 Hazır reklam videoları: {ok_count}/{len(creatives)} | 📅 Paylaşım günü: {len(content_plan)}\n\n"
            "**6-cı Agenti (Meta Media Buyer)** başlatmaq üçün **Təsdiq Et**, reklam ayarlarına qayıtmaq üçün **Rədd Et** seçin."
        ),
        "creatives": creatives,
        "creative_assets": creative_assets,
        "brand": brand,
        "product": product,
        "social_media": social_media,
        "content_plan": content_plan,
    })
    if isinstance(decision, dict):
        decision_str = str(decision.get("decision", "approved")).strip().lower()
        feedback_txt = str(decision.get("feedback") or decision.get("rejection_reason") or "").strip()
    else:
        decision_str = str(decision).strip().lower()
        feedback_txt = ""

    updates: Dict[str, Any] = {
        "current_phase": "creatives_approval",
        "pending_approval": None,
        "human_approvals": [{"step": "creatives_approval", "decision": decision_str}],
        "logs": [
            f"[HumanApproval] Kreativlər və Sosial Media qərarı: {decision_str}"
            + (f" | Rəy: {feedback_txt}" if feedback_txt else "")
        ],
    }
    if not decision_str.startswith("approved"):
        updates["creative_feedback"] = feedback_txt or "İstifadəçi əvvəlki reklamı rədd etdi — daha güclü hook və fərqli vizual yanaşma tətbiq et."
        updates["completed_parallel_nodes"] = []
    return updates


# ──────────────────────────── ROUTING FUNCTIONS ───────────────────────────────

def _last_decision(state: ECommerceState) -> str:
    """Return the most recent human-approval decision string."""
    approvals = state.get("human_approvals", [])
    if approvals:
        return str(approvals[-1].get("decision", "")).strip().lower()
    return ""


def route_after_product_approval(state: ECommerceState) -> Literal["market_research", "reset_product_cycle", "sourcing"]:
    decision = _last_decision(state)
    if decision.startswith("research"):
        return "market_research"
    if decision.startswith("approved"):
        return "sourcing"
    return "reset_product_cycle"


def route_after_supplier_approval(
    state: ECommerceState,
) -> Literal["human_approval_store_config", "human_approval_supplier", "human_approval_product"]:
    decision = _last_decision(state)
    if decision == "search_1688_image":
        return "human_approval_supplier"
    return "human_approval_store_config" if decision.startswith("approved") else "human_approval_product"


def route_after_store_config(state: ECommerceState) -> Literal["store_build", "human_approval_supplier"]:
    decision = _last_decision(state)
    if decision.startswith("rejected"):
        return "human_approval_supplier"
    return "store_build"


def route_after_store_approval(state: ECommerceState) -> Literal["human_approval_stage_one", "human_approval_store_config"]:
    return "human_approval_stage_one" if _last_decision(state).startswith("approved") else "human_approval_store_config"


def route_after_stage_one(state: ECommerceState) -> Literal["reset_product_cycle", "human_approval_video_sources"]:
    if _last_decision(state) == "add_another_product":
        return "reset_product_cycle"
    return "human_approval_video_sources"


def route_after_video_source_approval(
    state: ECommerceState,
) -> Literal["parallel_fanout_creative_social", "creative_production", "human_approval_video_sources"]:
    if not _last_decision(state).startswith("approved"):
        return "human_approval_video_sources"
    # Preserve Agent 5 (Social Media) if content_plan is already ready (e.g. after rejecting creatives)
    if state.get("content_plan"):
        return "creative_production"
    return "parallel_fanout_creative_social"


def route_after_creative_approval(state: ECommerceState) -> Literal["media_buying", "human_approval_video_sources"]:
    return "media_buying" if _last_decision(state).startswith("approved") else "human_approval_video_sources"


def route_monitoring(state: ECommerceState) -> Literal["monitoring", "__end__"]:
    """Keep monitoring loop going while ROAS is being tracked."""
    metrics = state.get("ad_metrics", [])
    if metrics:
        latest = metrics[-1] if isinstance(metrics[-1], dict) else {}
        roas = latest.get("roas", 0)
        if roas > 0:
            return "__end__"
    return "__end__"


# ──────────────────────────── PIPELINE BUILDER ────────────────────────────────

def build_pipeline(
    client: genai.Client | None = None,
    tools_map: Dict[str, list] | None = None,
    settings: Any = None,
    model: str = "gemini-3.5-flash-lite",
) -> Any:
    """
    Build and compile the 2-Stage LangGraph pipeline:
      - Stage 1: Product Research -> Sourcing (Automated) -> Store Config -> Store Build -> Winning Catalog Decision
                 (loop back to Product Research via `reset_product_cycle` OR advance to Stage 2)
      - Stage 2: Video/UGC Config -> Parallel (Agent 4 Creative ║ Agent 5 Social Media) -> Fan-in -> Approval -> Agent 6 Media Buyer -> Monitoring
    """
    tools_map = tools_map or {}

    # ── Create agent node functions ──
    product_research_node = create_product_research_node(
        client=client, model=model, tools=tools_map.get("product_research", [])
    )
    market_research_node = create_market_research_node(
        client=client, model=model, tools=tools_map.get("market_research", [])
    )
    sourcing_node = create_sourcing_node(
        client=client, model=model, tools=tools_map.get("sourcing", [])
    )
    social_media_node = create_social_media_node(
        client=client, model=model, tools=tools_map.get("social_media", [])
    )
    store_builder_node = create_store_builder_node(
        client=client, model=model, tools=tools_map.get("store_builder", [])
    )
    creative_node = create_creative_node(
        client=client, model=model, tools=tools_map.get("creative", [])
    )
    media_buyer_node = create_media_buyer_node(
        client=client, model=model, tools=tools_map.get("media_buyer", [])
    )

    # ── Phase 4 Monitoring Node (Supply price/stock + Order tracking) ──
    async def monitoring_node(state: ECommerceState) -> dict:
        logger.info("[Monitoring] Running Phase 4 supplier price/stock & order tracking check...")
        try:
            from tools.supplier_monitor import run_supply_monitor_cycle
            res = await asyncio.to_thread(run_supply_monitor_cycle)
            alerts = res.get("new_alerts", [])
            updates = res.get("order_updates", [])
            msg = (
                f"[Monitoring] {res.get('checked_watches', 0)} tədarükçü yoxlanıldı | "
                f"{len(alerts)} yeni xəbərdarlıq | {len(updates)} sifariş yeniləndi."
            )
            logs = [msg] + [f"⚠️ {a.get('message')}" for a in alerts]
        except Exception as e:
            logger.warning(f"[Monitoring] Note: {e}")
            logs = [f"[Monitoring] Yoxlama tamamlandı (qeyd: {e})."]
        return {
            "current_phase": "monitoring",
            "logs": logs,
        }

    # ── Fan-out node: triggers Agent 4 (Creative) AND Agent 5 (Social Media) in parallel ──
    def parallel_fanout_creative_social(state: ECommerceState) -> dict:
        """Reset parallel tracking and fan out to Creative (Agent 4) + Social Media (Agent 5) concurrently."""
        logger.info("[Pipeline] Fanning out Stage 2: creative_production ║ social_media_setup (parallel)")
        return {
            "logs": ["[Pipeline] ⚡ Paralel başladı: 4-cü Agent (Kreativ Video) + 5-ci Agent (Sosial Media) eyni anda işlənir..."],
            "completed_parallel_nodes": [],  # Reset before fan-out
        }

    # ── Fan-in node: waits until BOTH Agent 4 AND Agent 5 finish ──
    def fan_in_creative_social(state: ECommerceState) -> dict:
        completed = state.get("completed_parallel_nodes", [])
        logger.info(f"[Pipeline] Stage 2 Fan-in check: {len(completed)}/2 paralel nod tamamlandı: {completed}")
        return {}

    def route_fan_in_creative_social(state: ECommerceState) -> Literal["human_approval_creatives", "__end__"]:
        completed = state.get("completed_parallel_nodes", [])
        has_creative = any("creative" in c for c in completed)
        has_social = any("social" in c for c in completed) or bool(state.get("content_plan"))
        if has_creative and has_social:
            logger.info("[Pipeline] ✅ 4-cü və 5-ci Agentlər hazırdır → human_approval_creatives")
            return "human_approval_creatives"
        logger.info(f"[Pipeline] Stage 2 paralel nod gözlənilir: completed={completed}")
        return "__end__"

    # ── Build the graph ──
    graph = StateGraph(ECommerceState)

    # --- Nodes ---
    graph.add_node("reset_product_cycle", reset_product_cycle)
    graph.add_node("product_research", product_research_node)
    graph.add_node("human_approval_product", human_approval_product)
    graph.add_node("market_research", market_research_node)
    graph.add_node("sourcing", sourcing_node)
    graph.add_node("human_approval_supplier", human_approval_supplier)
    graph.add_node("human_approval_store_config", human_approval_store_config)
    graph.add_node("store_build", store_builder_node)
    graph.add_node("human_approval_store", human_approval_store)
    graph.add_node("human_approval_stage_one", human_approval_stage_one)

    # Stage 2: Parallel fan-out/fan-in for Agent 4 (Creative) + Agent 5 (Social Media) → Agent 6 (Media Buyer)
    graph.add_node("human_approval_video_sources", human_approval_video_sources)
    graph.add_node("parallel_fanout_creative_social", parallel_fanout_creative_social)
    graph.add_node("creative_production", creative_node)
    graph.add_node("social_media_setup", social_media_node)
    graph.add_node("fan_in_creative_social", fan_in_creative_social)
    graph.add_node("human_approval_creatives", human_approval_creatives)
    graph.add_node("media_buying", media_buyer_node)
    graph.add_node("monitoring", monitoring_node)

    # --- Edges ---
    # Stage 1 Phase 1: Product Research → Approval (loop back via reset_product_cycle on reject)
    graph.add_edge(START, "product_research")
    graph.add_edge("reset_product_cycle", "product_research")
    graph.add_edge("product_research", "human_approval_product")
    graph.add_conditional_edges(
        "human_approval_product",
        route_after_product_approval,
        {
            "market_research": "market_research",
            "reset_product_cycle": "reset_product_cycle",
            "sourcing": "sourcing",
        },
    )
    graph.add_edge("market_research", "human_approval_product")

    # Stage 1 Phase 2: Sourcing (Automated) → Approval
    graph.add_edge("sourcing", "human_approval_supplier")
    graph.add_conditional_edges(
        "human_approval_supplier",
        route_after_supplier_approval,
        {
            "human_approval_store_config": "human_approval_store_config",
            "human_approval_supplier": "human_approval_supplier",
            "human_approval_product": "human_approval_product",
        },
    )

    # Stage 1 Phase 3: Store Config HITL → Agent 3 Store Build → Store Approval → Stage 1 Decision Window
    graph.add_conditional_edges(
        "human_approval_store_config",
        route_after_store_config,
        {
            "store_build": "store_build",
            "human_approval_supplier": "human_approval_supplier",
        },
    )
    graph.add_edge("store_build", "human_approval_store")
    graph.add_conditional_edges(
        "human_approval_store",
        route_after_store_approval,
        {
            "human_approval_stage_one": "human_approval_stage_one",
            "human_approval_store_config": "human_approval_store_config",
        },
    )
    graph.add_conditional_edges(
        "human_approval_stage_one",
        route_after_stage_one,
        {
            "reset_product_cycle": "reset_product_cycle",
            "human_approval_video_sources": "human_approval_video_sources",
        },
    )

    # Stage 2: Video Config → Parallel Fan-out (first run) OR Agent 4 only (after Reject) → Fan-in → Approval → Agent 6
    graph.add_conditional_edges(
        "human_approval_video_sources",
        route_after_video_source_approval,
        {
            "parallel_fanout_creative_social": "parallel_fanout_creative_social",
            "creative_production": "creative_production",
            "human_approval_video_sources": "human_approval_video_sources",
        },
    )
    graph.add_edge("parallel_fanout_creative_social", "creative_production")
    graph.add_edge("parallel_fanout_creative_social", "social_media_setup")
    graph.add_edge("creative_production", "fan_in_creative_social")
    graph.add_edge("social_media_setup", "fan_in_creative_social")
    graph.add_conditional_edges(
        "fan_in_creative_social",
        route_fan_in_creative_social,
        {"human_approval_creatives": "human_approval_creatives", "__end__": END},
    )
    graph.add_conditional_edges(
        "human_approval_creatives",
        route_after_creative_approval,
        {
            "media_buying": "media_buying",
            "human_approval_video_sources": "human_approval_video_sources",
        },
    )

    # Stage 2 Final: Agent 6 Media Buying → Monitoring → END
    graph.add_edge("media_buying", "monitoring")
    graph.add_conditional_edges(
        "monitoring",
        route_monitoring,
        {"monitoring": "monitoring", "__end__": END},
    )

    # ── Compile with checkpointer ──
    checkpointer = MemorySaver()
    compiled = graph.compile(checkpointer=checkpointer)

    logger.info("✅ E-Commerce 2-Stage pipeline compiled successfully (Stage 1 Catalog Loop + Stage 2 Parallel Agent 4+5 → Agent 6).")
    return compiled
