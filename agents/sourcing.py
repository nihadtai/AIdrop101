import asyncio
import json
import logging
import math
import urllib.parse
from typing import Any, Callable, Dict, List, Optional
from google import genai
from pydantic import BaseModel, Field

from state.graph_state import ECommerceState
from agents.base import BaseAgent
from tools.aliexpress_ds_tool import search_aliexpress_ds_products
from tools.binding_store import detect_sourcing_channel
from tools.cj_dropshipping_tool import enrich_cj_top_candidates
from tools.sourcing_tools import (
    search_cj_dropshipping,
    get_live_fx_rates,
)
from tools.supplier_scorer import score_and_rank_suppliers

logger = logging.getLogger(__name__)


def compute_unit_economics(
    cost_usd: Optional[float],
    ship_usd: float,
    margin_mult: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Compute honest dropshipping unit economics using tiered retail markup:
      - Landed <= $12.00 -> 3.2x
      - Landed <= $28.00 -> 2.6x
      - Landed >  $28.00 -> 2.1x
    Returns None cost fields when wholesale price isn't verified.
    """
    if cost_usd is None or float(cost_usd) <= 0:
        return {
            "product_cost": None,
            "shipping_cost": round(float(ship_usd or 5.0), 2),
            "selling_price": None,
            "estimated_cpa": None,
            "payment_gateway_fee": None,
            "net_profit": None,
            "break_even_roas": None,
        }
    c_usd = round(float(cost_usd), 2)
    s_usd = round(float(ship_usd or 0.0), 2)
    landed = c_usd + s_usd
    if landed <= 12.0:
        tiered_mult = 3.2
    elif landed <= 28.0:
        tiered_mult = 2.6
    else:
        tiered_mult = 2.1
    raw_sell = landed * tiered_mult
    selling_price = max(19.99, round(math.floor(raw_sell) + 0.99, 2))
    est_cpa = round(selling_price * 0.28, 2)
    gw_fee = round(selling_price * 0.029 + 0.30, 2)
    net_profit = round(selling_price - (landed + est_cpa + gw_fee), 2)
    be_cpa = selling_price - landed - gw_fee
    be_roas = round(selling_price / be_cpa, 2) if be_cpa > 0 else 0.0
    return {
        "product_cost": c_usd,
        "shipping_cost": s_usd,
        "selling_price": selling_price,
        "estimated_cpa": est_cpa,
        "payment_gateway_fee": gw_fee,
        "net_profit": net_profit,
        "break_even_roas": be_roas,
    }


class UnitEconomics(BaseModel):
    product_cost: Optional[float] = Field(description="Verified wholesale cost in USD (None if manual check needed)", default=None)
    shipping_cost: Optional[float] = Field(description="US/EU ePacket/CJPacket/YunExpress shipping cost in USD", default=None)
    selling_price: Optional[float] = Field(description="Proposed retail selling price in USD", default=None)
    estimated_cpa: Optional[float] = Field(description="Estimated Cost Per Acquisition for US/EU ads", default=None)
    payment_gateway_fee: Optional[float] = Field(description="Stripe/Shopify gateway fee (2.9% + $0.30)", default=None)
    net_profit: Optional[float] = Field(description="Net profit per unit in USD", default=None)
    break_even_roas: Optional[float] = Field(description="Minimum ROAS required to break even", default=None)


class SupplierInfo(BaseModel):
    supplier_name: str = Field(description="Name of the supplier")
    platform: str = Field(description="Platform (e.g., AliExpress DS API, CJ Dropshipping, 1688 -> YunExpress)")
    rating: Optional[float] = Field(description="Verified supplier rating (None if unknown)", default=None)
    product_sku: str = Field(description="SKU code of the product", default="N/A")
    product_image_url: str = Field(description="URL to the product image", default="")
    order_link: str = Field(description="Direct link to view or order the product", default="")
    price_cny: Optional[float] = Field(description="Wholesale price in CNY", default=None)
    delivery_days: str = Field(description="Estimated delivery time to US/EU", default="7-12 days")
    logistic_line: str = Field(description="Shipping method (e.g. AliExpress Standard, CJPacket, YunExpress)", default="YunExpress Tracked")
    is_live_verified: bool = Field(description="True if backed by live API/scraper price", default=False)
    unit_economics: UnitEconomics = Field(description="Calculated unit economics for US/EU dropshipping")


class SourcingAgent(BaseAgent):
    """
    Agent 2: Multi-source Global US/EU Sourcing & Fulfillment Specialist.
    Automatically queries AliExpress Dropshipping (DS) API and CJ Dropshipping API 2.0 in parallel,
    while allowing manual 1688.com links routed through 1688 -> HyperSKU/SourcinBox 3PL -> YunExpress -> Customer.
    """

    def __init__(
        self,
        client: genai.Client,
        model: str = "gemini-3.5-flash-lite",
        tools: Optional[List[Callable]] = None,
    ):
        super().__init__(client=client, model=model, tools=tools)

    async def run(self, state: ECommerceState) -> Dict[str, Any]:
        """Execute multi-source sourcing via AliExpress DS API + CJ Dropshipping API in parallel."""
        logger.info("Starting Agent 2 (Parallel AliExpress DS API + CJ Dropshipping API 2.0 + 1688-YunExpress ready)...")
        products = state.get("products", [])
        target_product = state.get("selected_product") or (products[0] if products else None)

        if not target_product:
            raise ValueError("Tədarük üçün seçilmiş məhsul tapılmadı.")

        product_name = target_product.get("name", "Viral Product")
        english_name = (target_product.get("english_name") or product_name).strip()
        sourcing_query = (target_product.get("sourcing_search_query") or english_name).strip()
        image_url = (target_product.get("image_url") or "").strip()
        margin_mult = float(target_product.get("estimated_margin_multiplier", 3.2) or 3.2)

        chinese_kw = (target_product.get("chinese_keywords") or "").strip()
        for ch in [",", "，", "、", ";", "；", ".", "。", "!", "?", '"', "'", "：", ":", "\n", "\t"]:
            chinese_kw = chinese_kw.replace(ch, " ")
        clean_chinese_kw = " ".join([t.strip() for t in chinese_kw.split() if t.strip()][:2])
        search_kw_1688 = clean_chinese_kw or sourcing_query
        search_url_1688 = f"https://s.1688.com/selloffer/offer_search.htm?keywords={urllib.parse.quote(search_kw_1688)}"

        # Query Live FX + AliExpress DS API + CJ Dropshipping API 2.0 in parallel using concise sourcing_query
        fx_task = asyncio.to_thread(get_live_fx_rates)
        ae_task = asyncio.to_thread(search_aliexpress_ds_products, sourcing_query, "US", "USD", 8)
        cj_task = asyncio.to_thread(search_cj_dropshipping, sourcing_query, "US")

        fx, raw_ae, raw_cj = await asyncio.gather(fx_task, ae_task, cj_task, return_exceptions=True)

        if isinstance(fx, Exception) or not isinstance(fx, dict):
            fx = {"CNY": 7.15, "EUR": 0.92, "live": False}
        cny_rate = fx["CNY"]

        candidates: List[Dict[str, Any]] = []
        ae_status_note = ""

        # 1. Parse AliExpress Dropshipping API results
        if isinstance(raw_ae, dict):
            if not raw_ae.get("configured"):
                ae_status_note = raw_ae.get("note") or "AliExpress DS API konfiqurasiya olunmayıb."
            for p in raw_ae.get("products", []):
                cost_usd = p.get("product_cost_usd")
                if cost_usd and float(cost_usd) > 0:
                    ship_usd = p.get("estimated_shipping_usd")
                    candidates.append({
                        "channel": "aliexpress_ds",
                        "supplier_name": f"AliExpress DS — {p.get('store_name') or p.get('name', english_name)[:36]}",
                        "product_name": p.get("name", english_name),
                        "platform": "AliExpress DS API",
                        "product_cost_usd": round(float(cost_usd), 2),
                        "price_cny": round(float(cost_usd) * cny_rate, 2),
                        "shipping_usd": round(float(ship_usd), 2) if ship_usd is not None else 3.90,
                        "shipping_verified": ship_usd is not None,
                        "delivery_days": p.get("delivery_days", "8-14 days"),
                        "logistic_line": p.get("logistic_line", "AliExpress Selection Standard"),
                        "product_sku": p.get("sku") or f"AE-{p.get('id', 'SKU')}",
                        "ae_product_id": str(p.get("id") or ""),
                        "ae_sku_id": str(p.get("sku_id") or ""),
                        "cj_pid": "",
                        "cj_vid": "",
                        "product_image_url": p.get("image_url") or image_url,
                        "order_link": p.get("order_link") or "",
                        "listed_count": p.get("listed_count") or 0,
                        "is_live_verified": True,
                    })

        # 2. Parse CJ Dropshipping API 2.0 results
        if isinstance(raw_cj, str):
            try:
                cj_data = json.loads(raw_cj)
                for p in cj_data.get("products", []):
                    cost_usd = p.get("product_cost_usd")
                    if cost_usd and float(cost_usd) > 0:
                        ship_usd = p.get("estimated_shipping_usd")
                        candidates.append({
                            "channel": "cj_dropshipping",
                            "supplier_name": f"CJ Dropshipping — {p.get('name', english_name)[:36]}",
                            "product_name": p.get("name", english_name),
                            "platform": "CJ Dropshipping",
                            "product_cost_usd": round(float(cost_usd), 2),
                            "price_cny": round(float(cost_usd) * cny_rate, 2),
                            "shipping_usd": round(float(ship_usd), 2) if ship_usd is not None else 5.20,
                            "shipping_verified": ship_usd is not None,
                            "delivery_days": p.get("delivery_days", "7-12 days"),
                            "logistic_line": p.get("logistic_line", "CJPacket / YunExpress"),
                            "product_sku": p.get("sku") or f"CJ-{p.get('id', 'SKU')}",
                            "ae_product_id": "",
                            "ae_sku_id": "",
                            "cj_pid": str(p.get("id") or ""),
                            "cj_vid": str(p.get("vid") or ""),
                            "product_image_url": p.get("image_url") or image_url,
                            "order_link": p.get("order_link") or "",
                            "listed_count": p.get("listed_count") or 0,
                            "is_live_verified": True,
                        })
            except Exception as e:
                logger.warning(f"CJ parse note: {e}")

        def _build_1688_fallback_supplier() -> Dict[str, Any]:
            ue_fallback = compute_unit_economics(None, 6.62, margin_mult=margin_mult)
            return {
                "channel": "1688_yunexpress",
                "supplier_name": "1688.com -> YunExpress (3PL Manual Link)",
                "product_name": english_name,
                "platform": "1688.com -> YunExpress (3PL)",
                "rating": None,
                "product_sku": f"1688-{abs(hash(search_kw_1688)) % 1000000}",
                "ae_product_id": "",
                "ae_sku_id": "",
                "cj_pid": "",
                "cj_vid": "",
                "product_image_url": image_url,
                "order_link": search_url_1688,
                "order_link_1688": search_url_1688,
                "price_cny": None,
                "chinese_keywords": clean_chinese_kw,
                "delivery_days": "6-10 days (YunExpress)",
                "logistic_line": "YunExpress Standard US (HyperSKU / SourcinBox)",
                "is_live_verified": False,
                "listed_count": 0,
                "match_confidence": 0,
                "supplier_score": 15.0,
                "unit_economics": ue_fallback,
            }

        enriched_suppliers: List[Dict[str, Any]] = []
        for c in candidates:
            ue = compute_unit_economics(c["product_cost_usd"], c["shipping_usd"], margin_mult=margin_mult)
            enriched_suppliers.append({
                "channel": c.get("channel") or detect_sourcing_channel(c),
                "supplier_name": c["supplier_name"],
                "product_name": c.get("product_name", c["supplier_name"]),
                "platform": c["platform"],
                "rating": None,
                "product_sku": c["product_sku"],
                "ae_product_id": c.get("ae_product_id", ""),
                "ae_sku_id": c.get("ae_sku_id", ""),
                "cj_pid": c.get("cj_pid", ""),
                "cj_vid": c.get("cj_vid", ""),
                "product_image_url": c["product_image_url"] or image_url,
                "order_link": c["order_link"],
                "order_link_1688": search_url_1688,
                "price_cny": c["price_cny"],
                "chinese_keywords": clean_chinese_kw,
                "delivery_days": c["delivery_days"],
                "logistic_line": c["logistic_line"],
                "is_live_verified": c["is_live_verified"],
                "listed_count": c["listed_count"],
                "unit_economics": ue,
            })

        # 3. Score & filter candidates with the >=45% Relevance Gate
        if enriched_suppliers:
            enriched_suppliers = await asyncio.to_thread(
                score_and_rank_suppliers,
                enriched_suppliers,
                sourcing_query or english_name,
                image_url,
            )

        # 4. Post-ranking live CJ variant (vid) + freight calculation on Top 3 matching CJ candidates
        if enriched_suppliers:
            enriched_suppliers = await asyncio.to_thread(
                enrich_cj_top_candidates,
                enriched_suppliers,
                "US",
                3,
            )
            for s in enriched_suppliers:
                if s.get("estimated_shipping_usd") is not None and s.get("unit_economics", {}).get("product_cost"):
                    s["unit_economics"] = compute_unit_economics(
                        s["unit_economics"]["product_cost"],
                        float(s["estimated_shipping_usd"]),
                        margin_mult=margin_mult,
                    )

        # 5. If 0 live candidates passed the >=45% relevance gate, fall back cleanly to 1688 -> YunExpress (3PL)
        if not enriched_suppliers:
            enriched_suppliers = [_build_1688_fallback_supplier()]

        best_supplier = enriched_suppliers[0]
        ue = best_supplier["unit_economics"]
        score_val = best_supplier.get("supplier_score", 0.0)
        ae_cnt = sum(1 for s in enriched_suppliers if s.get("channel") == "aliexpress_ds")
        cj_cnt = sum(1 for s in enriched_suppliers if s.get("channel") == "cj_dropshipping")

        logs_out: List[str] = []
        if ae_status_note:
            logs_out.append(f"ℹ️ AliExpress DS API: {ae_status_note}")

        if best_supplier["is_live_verified"]:
            log_msg = (
                f"✅ 2-ci Agent: '{sourcing_query}' üçün {len(enriched_suppliers)} uyğun tədarükçü tapıldı (AliExpress DS: {ae_cnt}, CJ: {cj_cnt}). "
                f"Lider: '{best_supplier['supplier_name']}' ({best_supplier['platform']}, Uyğunluq: {best_supplier.get('match_confidence', 0)}%) | "
                f"Skor: {score_val}/100 | Maya: ${ue['product_cost']:.2f} + Çatdırılma: ${ue['shipping_cost']:.2f} | "
                f"Satış: ${ue['selling_price']:.2f} | Xalis Qazanc: +${ue['net_profit']:.2f}"
            )
        else:
            log_msg = (
                f"⚠️ 2-ci Agent: '{sourcing_query}' üzrə kataloqda >=45% uyğunluq həddini keçən birbaşa məhsul tapılmadı — "
                "1688 Şəkil Axtarışı və ya Manual 1688 -> YunExpress (3PL) linki ilə davam edin."
            )
        logs_out.append(log_msg)
        logger.info(log_msg)

        return {
            "selected_supplier": best_supplier,
            "suppliers": enriched_suppliers[:6],
            "unit_economics": ue,
            "current_phase": "human_approval_supplier",
            "logs": logs_out,
        }


def create_sourcing_node(
    client: genai.Client,
    model: str = "gemini-3.5-flash-lite",
    tools: Optional[List[Callable]] = None,
) -> Callable[[ECommerceState], Dict[str, Any]]:
    """Factory function to create the sourcing node."""
    agent = SourcingAgent(client=client, model=model, tools=tools)

    async def node(state: ECommerceState) -> Dict[str, Any]:
        return await agent.run(state)

    return node
