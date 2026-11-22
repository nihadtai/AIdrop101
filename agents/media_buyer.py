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

class AudienceTargeting(BaseModel):
    name: str = Field(description="Name of the audience (e.g., 'Broad', 'Interests: Fitness')")
    details: str = Field(description="Detailed targeting criteria")

class CampaignStructure(BaseModel):
    campaign_objective: str = Field(description="e.g., Sales / Conversions")
    budget_strategy: str = Field(description="CBO or ABO")
    daily_budget: float = Field(description="Total daily budget for the test")
    audiences: List[AudienceTargeting] = Field(description="List of audiences to test")
    automated_rules: List[str] = Field(description="Rules for killing/scaling ads (e.g., 'Kill ad if Spend > $15 and ROAS < 1.0')")

class MediaBuyerAgent(BaseAgent):
    """Agent responsible for planning and executing ad campaigns."""
    
    def __init__(
        self,
        client: genai.Client,
        model: str = 'gemini-3.8-flash',
        tools: Optional[List[Callable]] = None
    ):
        super().__init__(client=client, model=model, tools=tools)
        self.system_instruction = (
            "You are an elite Meta Ads media buyer and performance marketer. "
            "Your objective is to design profitable A/B testing structures for dropshipping products. "
            "Focus on ROAS optimization, select appropriate CBO/ABO strategies, define audiences, "
            "and establish strict automated rules for killing losing ads and scaling winners."
        )

    async def run(self, state: ECommerceState) -> Dict[str, Any]:
        """Execute the media buying planning phase."""
        logger.info("Starting Media Buyer Phase...")
        product = state.get("selected_product") or {}
        supplier = state.get("selected_supplier") or {}
        if supplier:
            product["supplier_link"] = supplier.get("order_link")
            if supplier.get("product_image_url"):
                product["image_url"] = supplier.get("product_image_url")
            if supplier.get("supplier_name"):
                product["supplier_name"] = supplier.get("supplier_name")
        creatives = state.get("creative_assets", {})
        
        try:
            prompt = (
                f"Design a Meta Ads testing campaign structure for:\n"
                f"Product: {json.dumps(product)}\n"
                f"Using these creative concepts: {json.dumps(creatives)}"
            )
            
            if self.tools:
                logger.info("Calling Gemini with tools to setup campaigns...")
                # e.g., meta_ads_tools
                pass

            logger.info("Generating structured campaign plan via Gemini 3.8 Flash...")
            schema = CampaignStructure.model_json_schema()
            
            response_text = await self._call_gemini(
                prompt=prompt,
                system_instruction=self.system_instruction,
                response_schema=schema
            )
            
            campaign_data = json.loads(response_text)
            campaign = CampaignStructure(**campaign_data)
            
            campaign_dict = campaign.model_dump()
            return {
                "ad_campaigns": [campaign_dict],
                "campaign_plan": campaign_dict,
                "current_phase": "complete",
                "logs": ["Completed Media Buying phase. Ready for launch."]
            }
            
        except Exception as e:
            logger.error(f"Error in Media Buyer Agent: {str(e)}")
            return {
                "logs": [f"Error in Media Buying: {str(e)}"]
            }

def create_media_buyer_node(
    client: genai.Client,
    model: str = 'gemini-3.8-flash',
    tools: Optional[List[Callable]] = None
) -> Callable[[ECommerceState], Dict[str, Any]]:
    """Factory function to create the media buyer node."""
    agent = MediaBuyerAgent(client=client, model=model, tools=tools)
    
    async def node(state: ECommerceState) -> Dict[str, Any]:
        return await agent.run(state)
        
    return node
