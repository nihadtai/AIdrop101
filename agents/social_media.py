import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from state.graph_state import ECommerceState
from agents.base import BaseAgent

logger = logging.getLogger(__name__)

class BrandIdentity(BaseModel):
    name: str = Field(description="The brand name")
    colors: List[str] = Field(description="List of hex color codes for brand palette")
    slogan: str = Field(description="Catchy brand slogan")
    bio: str = Field(description="Social media bio (Instagram/TikTok style)")

class ContentPiece(BaseModel):
    day: int = Field(description="Day number (1-7)")
    platform: str = Field(description="Platform (TikTok, Instagram Reels, etc.)")
    content_type: str = Field(description="Type of content (e.g., UGC, Behind the scenes, Product Demo)")
    caption: str = Field(description="Post caption including hashtags")

class SocialMediaStrategy(BaseModel):
    brand_identity: BrandIdentity = Field(description="The visual and textual brand identity")
    content_plan: List[ContentPiece] = Field(description="7-day content plan")

class SocialMediaAgent(BaseAgent):
    """Agent responsible for brand strategy and social media planning."""
    
    def __init__(
        self,
        client: genai.Client,
        model: str = 'gemini-3.8-flash',
        tools: Optional[List[Callable]] = None
    ):
        super().__init__(client=client, model=model, tools=tools)
        self.system_instruction = (
            "You are an expert social media manager and brand strategist for e-commerce. "
            "Your goal is to create compelling brand identities and viral content plans that resonate "
            "with target audiences and drive engagement and conversions."
        )

    async def run(self, state: ECommerceState) -> Dict[str, Any]:
        """Execute the social media strategy phase (runs concurrently with CreativeAgent in Stage 2)."""
        logger.info("Starting Social Media Phase (Agent 5 — Parallel with Agent 4)...")
        product = dict(state.get("selected_product") or {})
        supplier = state.get("selected_supplier") or {}
        if supplier:
            product["supplier_link"] = supplier.get("order_link")
            if supplier.get("product_image_url") and not product.get("image_url"):
                product["image_url"] = supplier.get("product_image_url")
            if supplier.get("supplier_name"):
                product["supplier_name"] = supplier.get("supplier_name")

        store = state.get("store") or {}
        store_copy = store.get("copy") or {}
        store_config = state.get("store_config") or {}
        existing_brand = state.get("brand_identity") or state.get("brand") or {}
        preferred_brand_name = (
            store_config.get("brand_name")
            or existing_brand.get("brand_name")
            or existing_brand.get("name")
            or ""
        )
        preferred_logo_url = store_config.get("logo_url") or existing_brand.get("logo_url") or ""

        try:
            creatives = state.get("creatives") or []
            creative_cfg = state.get("creative_config") or {}
            prompt = (
                f"Sən 5-ci Agent — Sosial Media İdarəetmə və Paylaşım Cədvəli Agentisən.\n"
                f"Aşağıdakı uğurlu məhsul, qurulmuş mağaza kopiraytinqi və reklam konfiqurasiyası əsasında "
                f"brend kimliyi və həftəlik (7 günlük) dəqiq paylaşım qrafiki hazırla. Hər gün üçün kreativ mövzu, "
                f"platforma (TikTok, Instagram Reels), başlıq (caption) və viral heşteqlər daxil olsun:\n"
                f"Məhsul: {json.dumps(product, ensure_ascii=False)}\n"
                f"Mağaza Başlığı & Alt Başlığı: {json.dumps({'headline': store_copy.get('headline', ''), 'subheadline': store_copy.get('subheadline', '')}, ensure_ascii=False)}\n"
                f"Mövcud Brend Adı (əgər varsa saxla): {preferred_brand_name or 'Avtomatik yarat'}\n"
                f"AI UGC Konfiqurasiyası: {json.dumps(creative_cfg, ensure_ascii=False)}\n"
                f"Hazır Kreativlər (varsa): {json.dumps(creatives, ensure_ascii=False)}"
            )
            
            logger.info("Generating social media weekly content schedule via Gemini...")
            schema = SocialMediaStrategy.model_json_schema()
            
            response_text = await self._call_gemini(
                prompt=prompt,
                system_instruction=self.system_instruction,
                response_schema=schema
            )
            
            strategy_data = json.loads(response_text)
            strategy = SocialMediaStrategy(**strategy_data)
            
            brand_dict = strategy.brand_identity.model_dump()
            if preferred_brand_name:
                brand_dict["name"] = preferred_brand_name
                brand_dict["brand_name"] = preferred_brand_name
            if preferred_logo_url:
                brand_dict["logo_url"] = preferred_logo_url
            content_list = [piece.model_dump() for piece in strategy.content_plan]

            # ── Postiz Self-Hosted API Integration ──
            postiz_info = {}
            postiz_logs = []
            try:
                from tools.postiz_tool import get_postiz_status, schedule_content_plan_to_postiz

                video_url = None
                if creatives and isinstance(creatives, list):
                    for c in creatives:
                        if isinstance(c, dict) and c.get("video_url"):
                            video_url = c["video_url"]
                            break

                sched_res = schedule_content_plan_to_postiz(
                    content_plan=content_list,
                    image_url=product.get("image_url"),
                    video_url=video_url,
                    product_name=product.get("name", "Seçilmiş Məhsul"),
                )
                status_res = get_postiz_status()
                postiz_info = {
                    **status_res,
                    "scheduling_result": sched_res,
                }
                postiz_logs.append(f"🚀 Postiz (Self-Hosted): {sched_res.get('message')}")
            except Exception as p_err:
                logger.warning(f"Postiz integration notice: {p_err}")
                postiz_info = {"connected": False, "error": str(p_err)}

            agent_logs = [
                "✅ 5-ci Agent (Sosial Media): 7 günlük paylaşım qrafiki və brend planı uğurla tərtib edildi."
            ] + postiz_logs

            return {
                "brand": brand_dict,
                "brand_identity": brand_dict,
                "content_plan": content_list,
                "postiz": postiz_info,
                "social_media": {
                    "brand": brand_dict,
                    "content_plan": content_list,
                    "postiz": postiz_info,
                },
                "logs": agent_logs
            }
            
        except Exception as e:
            logger.error(f"Error in Social Media Agent: {str(e)}")
            return {
                "logs": [f"Error in Social Media: {str(e)}"]
            }

def create_social_media_node(
    client: genai.Client,
    model: str = 'gemini-3.8-flash',
    tools: Optional[List[Callable]] = None
) -> Callable[[ECommerceState], Dict[str, Any]]:
    """Factory function to create the social media node."""
    agent = SocialMediaAgent(client=client, model=model, tools=tools)
    
    async def node(state: ECommerceState) -> Dict[str, Any]:
        result = await agent.run(state)
        result["completed_parallel_nodes"] = ["social_media_setup"]
        return result
        
    return node
