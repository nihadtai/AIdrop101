import logging
from typing import Any, Callable, Dict, List, Optional
from google import genai

from state.graph_state import ECommerceState
from agents.base import BaseAgent

logger = logging.getLogger(__name__)

class MarketResearchAgent(BaseAgent):
    """
    Subagent that performs deep market research and feasibility due diligence
    on the user-approved product.
    """
    def __init__(
        self,
        client: genai.Client,
        model: str = "gemini-3.8-flash",
        tools: Optional[List[Callable]] = None,
    ):
        super().__init__(
            name="MarketResearchAgent",
            role="Autonomous Market Research & Due Diligence Analyst",
            goal="Perform grounded web research on selected product",
            client=client,
            model=model,
            tools=tools,
            use_google_search=True,
        )
        
        # We integrate the exact prompt requested by the user.
        self.system_instruction = (
            "You are an Autonomous Market Research Analyst and E-Commerce Due Diligence Specialist. "
            "Your mission is to perform comprehensive web research, gather real-world data, "
            "evaluate market potential, uncover regulatory and supply risks, and deliver a "
            "publication-grade feasibility report document. \n\n"
            "IMPORTANT: The entire final report MUST be written in Azerbaijani language (Azərbaycan dilində), "
            "even though the instructions are in English. Use professional, analytical language."
        )

    async def run(self, state: ECommerceState) -> Dict[str, Any]:
        logger.info("Starting Deep Market Research (Subagent)...")
        product = state.get("selected_product", {})
        product_name = product.get("name", "Bilinməyən Məhsul")
        
        # 1. Gather trend data using tools
        gathered_data = []
        if self.tools:
            logger.info(f"Gathering real-world market data for '{product_name}'...")
            for tool in self.tools:
                try:
                    res = tool(product_name)
                    gathered_data.append(f"Data from {getattr(tool, '__name__', 'tool')}:\n{res}")
                except Exception as te:
                    logger.warning(f"Tool execution note: {te}")
                    
        market_context = "\n".join(gathered_data) if gathered_data else "No live data available. Rely on internal knowledge."
        
        prompt = f"""
Zəhmət olmasa "{product_name}" məhsulu üçün QISA və KANKRET (Bazar Araşdırması) hesabatı hazırlayın. 
Mətni həddindən artıq uzatmayın, yalnız ən vacib detalları və əsas başlıqları saxlayın.

Real-world market data (if any):
{market_context}

Hesabatı bu əsas başlıqlar üzrə, Azərbaycan dilində yazın:

1. 🎯 Məhsulun İdentifikasiyası və Həll Etdiyi Problem
   - Məhsul nədir və hansı əsas problemi həll edir?

2. ⚔️ Rəqabət Analizi
   - Bazar doyması və əsas alternativlər (qısa məlumat).

3. 👥 Hədəf Kütlə (Target Audience)
   - İdeal müştəri profili kimlərdir?

4. 🛡️ Risklər və Tədarük Zənciri
   - Olası geri qaytarma səbəbləri, və sertifikat ehtiyacları (qısa).

5. ✨ Fərqləndirici Üstünlük (USP)
   - Bu məhsulu necə fərqli satmaq olar?

Mətni oxunaqlı və yığcam Markdown formatında təqdim edin.
"""
        try:
            # Single concise call for market research with Google Search Grounding enabled
            response_text = await self._call_gemini(
                prompt=prompt,
                system_instruction=self.system_instruction,
                use_google_search=True,
            )

            if self.last_grounding_info.get("sources"):
                src_lines = [
                    f"- [{s['title']}]({s['url']})"
                    for s in self.last_grounding_info["sources"][:5]
                ]
                response_text += "\n\n**🌐 Google Search Grounding Mənbələri:**\n" + "\n".join(src_lines)
            
            logger.info("Concise Market Research Report generated successfully.")
            
            # Save the report into the state
            return {
                "market_research_report": response_text,
                "current_phase": "market_research",
                "logs": [f"✅ Subagent: '{product_name}' üçün Qısa Bazar Araşdırması tamamlandı."]
            }

        except Exception as e:
            logger.error(f"Error in Market Research Subagents: {str(e)}", exc_info=True)
            return {
                "errors": [f"Bazar araşdırması xətası: {str(e)}"],
                "logs": [f"❌ Bazar araşdırması xətası: {str(e)}"]
            }

def create_market_research_node(
    client: genai.Client,
    model: str = "gemini-3.8-flash",
    tools: Optional[List[Callable]] = None,
) -> Callable[[ECommerceState], Dict[str, Any]]:
    """Factory function to create the market research subagent node."""
    agent = MarketResearchAgent(client=client, model=model, tools=tools)

    async def node(state: ECommerceState) -> Dict[str, Any]:
        return await agent.run(state)

    return node
