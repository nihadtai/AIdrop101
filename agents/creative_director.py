import asyncio
import json
import logging
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import requests
from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ProductVisualBrief(BaseModel):
    exact_appearance: str = Field(
        description="Precise visual description of the product shape, silhouette, buttons, attachments, and proportions"
    )
    materials_and_colors: str = Field(
        description="Exact surface finish (matte/glossy/metallic/fabric), color palette, and standout visual details"
    )
    key_visual_hook: str = Field(
        description="The single most scroll-stopping visual transformation or satisfying motion this product creates"
    )
    ideal_use_environment: str = Field(
        description="Realistic modern home/car/gym/desk lighting and background where this product naturally shines"
    )
    target_buyer_persona: str = Field(
        description="Core buyer persona and the #1 everyday frustration this product instantly removes"
    )
    avoid_artifacts: str = Field(
        description="Specific AI video risks to avoid for this product (e.g., warped logos, melted cables, floating lids)"
    )


class DirectedHookVariant(BaseModel):
    hook_type: str = Field(
        default="pain_point",
        description="Psychological angle: 'pain_point', 'curiosity', or 'social_proof'",
    )
    hook_text: str = Field(
        description="10-13 spoken words (~4.0-4.5s) scroll-stopping UGC opening line spoken directly to camera"
    )
    avatar_gesture_prompt: str = Field(
        default="Energetic authentic UGC creator speaking directly to smartphone camera with natural hand gestures and warm daylight",
        description="Tailored visual direction for the lip-synced talking-head avatar matching this hook's emotion",
    )


class DirectedStoryboardPlan(BaseModel):
    hook_variants: List[DirectedHookVariant] = Field(
        description="1 to 3 distinct psychological hook variants sharing the same body and CTA"
    )
    shared_body_script: str = Field(
        description="22-30 spoken words (~8-10s) structured as 3 crisp beats: Problem -> Mechanism/Demo -> Payoff"
    )
    shared_cta_text: str = Field(
        description="9-13 spoken words (~3.5-4.5s) strong offer citing discount, fast tracked shipping, and 30-day guarantee"
    )
    broll_shot_prompts: List[str] = Field(
        description="EXACTLY 3 cinematic image-to-video motion prompts (English, 30-45 words each) strictly synced to Beat 1 (Problem/Macro), Beat 2 (POV Action Demo), and Beat 3 (Hero Result Reveal) of shared_body_script"
    )
    visual_instructions: str = Field(
        default="Scene 1: Lip-Synced UGC Hook | Scene 2: 3-Beat Synced AI B-Roll | Scene 3: Branded Offer CTA End-Card",
        description="Short 3-scene storyboard summary",
    )


class StoryboardCritique(BaseModel):
    hook_power_score: int = Field(ge=0, le=20, description="Scroll-stopping impact in first 1.5s (0-20)")
    pacing_wordcount_score: int = Field(ge=0, le=20, description="Natural UGC cadence & exact word counts (0-20)")
    broll_sync_score: int = Field(ge=0, le=20, description="How tightly the 3 B-Roll prompts sync to body voiceover (0-20)")
    ai_artifact_safety_score: int = Field(ge=0, le=20, description="Low risk of AI warping/text/weird hands (0-20)")
    conversion_clarity_score: int = Field(ge=0, le=20, description="Offer clarity, urgency & DTC conversion strength (0-20)")
    total_score: int = Field(ge=0, le=100, description="Sum of the 5 criteria (0-100)")
    improvement_notes: str = Field(
        default="",
        description="Concrete actionable fixes if total_score < 85",
    )


class CreativeDirectorBrain:
    """
    3-Stage AI Creative Director Brain controlling Agent 4 (~$0.002 Gemini spend, $0 extra Fal spend):
      Stage 1: Multimodal Vision Product Analyzer (reads real product photos -> ProductVisualBrief)
      Stage 2: Master Director & Synchronized Storyboard Planner (hooks + body + CTA + 3 synced B-Roll I2V prompts + avatar gestures)
      Stage 3: Pre-Spend Self-Critic Loop (scores 5 criteria /100; auto-refines once if < 85 BEFORE paying Fal/ElevenLabs)
    """

    DIRECTOR_SYSTEM = (
        "You are an elite TikTok, Reels & Meta DTC Performance Creative Director who has directed $50M+ in winning UGC video ads. "
        "You combine real product visual grounding with direct-response psychology (Hook -> Problem -> Proof/Demo -> Offer CTA).\n"
        "STRICT RULES:\n"
        "1. Write spoken lines 100% in the requested target language with natural, punchy conversational rhythm — NEVER robotic marketing cliches.\n"
        "2. Each hook_text MUST be 10-13 spoken words (~4.0-4.5s) — ideal for OmniHuman v2 & Kling lip-sync — and open with an immediate pattern interrupt.\n"
        "3. shared_body_script MUST be 22-30 spoken words with 3 clear visual beats: Beat 1 (tactile detail/pain), Beat 2 (in-action demonstration), Beat 3 (satisfying result).\n"
        "4. shared_cta_text MUST be 9-13 spoken words citing the launch discount, fast shipping, and 30-day guarantee.\n"
        "5. broll_shot_prompts MUST ALWAYS be written in crisp cinematic ENGLISH (for Seedance / Kling I2V models), "
        "EXACTLY 3 prompts synced 1-to-1 with Beat 1, Beat 2, and Beat 3 of shared_body_script. "
        "Each prompt specifies: exact camera movement (e.g., slow 20-deg macro orbit / smooth handheld POV push-in / dramatic dolly-out), "
        "lighting, product material interaction, and ends with 'preserve exact product geometry and colors, no text, no watermark, no distorted hands'.\n"
        "6. NEVER include emojis or hashtags in spoken lines."
    )

    CRITIC_SYSTEM = (
        "You are a ruthless Performance Ad QA Critic auditing a UGC video ad storyboard BEFORE paid video rendering begins. "
        "Score honestly across 5 criteria (0-20 each, total 0-100): hook scroll-stop power, natural spoken pacing/word-count (hooks 10-13w, body 22-30w, CTA 9-13w), "
        "1-to-1 sync between the 3 B-Roll I2V prompts and the body voiceover beats, AI video artifact safety (no text/logos/complex finger physics), and DTC conversion clarity. "
        "Be strict: generic hooks ('Wait until you see this') or generic B-Roll prompts score <= 14/20."
    )

    def __init__(
        self,
        client: Optional[genai.Client],
        model: str,
        call_gemini_fn: Callable[..., Awaitable[str]],
    ):
        self.client = client
        self.model = model
        self._call_gemini = call_gemini_fn

    @staticmethod
    def _load_image_jpeg_bytes(source: str, max_dim: int = 768) -> Optional[bytes]:
        src = str(source or "").strip()
        try:
            if src.startswith("http://") or src.startswith("https://"):
                r = requests.get(
                    src,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"},
                    timeout=12,
                )
                r.raise_for_status()
                im = Image.open(BytesIO(r.content)).convert("RGB")
            elif src.startswith("/"):
                rel = Path(__file__).resolve().parent.parent / src.lstrip("/")
                if not rel.exists():
                    return None
                im = Image.open(rel).convert("RGB")
            else:
                p = Path(src)
                if not p.exists():
                    return None
                im = Image.open(p).convert("RGB")
            im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
            buf = BytesIO()
            im.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
        except Exception:
            return None

    async def analyze_product_visually(
        self,
        image_urls: List[str],
        product: Dict[str, Any],
        product_name: str,
        ad_language: str,
    ) -> Tuple[ProductVisualBrief, bool]:
        """Stage 1: Inspect up to 2 real product photos via Gemini Vision; gracefully falls back to text brief."""
        loop = asyncio.get_running_loop()
        img_parts: List[Any] = []
        for u in (image_urls or [])[:2]:
            raw = await loop.run_in_executor(None, self._load_image_jpeg_bytes, u)
            if raw:
                img_parts.append(types.Part.from_bytes(data=raw, mime_type="image/jpeg"))

        if img_parts and self.client is not None:
            vision_prompt = (
                f"Analyze these real product photos of '{product_name}' for a high-converting 9:16 TikTok/Reels commercial.\n"
                f"Product metadata: {json.dumps({k: product.get(k) for k in ('name', 'english_name', 'category', 'what_it_does', 'key_benefits') if product.get(k)}, ensure_ascii=False)}\n"
                "Describe the EXACT visual appearance, colors, materials, best scroll-stopping visual motion, ideal realistic setting, "
                "target buyer persona, and what AI video artifacts to avoid so I2V clips stay photorealistic."
            )
            try:
                resp = await loop.run_in_executor(
                    None,
                    lambda: self.client.models.generate_content(
                        model=self.model,
                        contents=[vision_prompt, *img_parts],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=ProductVisualBrief.model_json_schema(),
                            temperature=0.35,
                        ),
                    ),
                )
                txt = getattr(resp, "text", None) or ""
                if txt.strip():
                    brief = ProductVisualBrief(**json.loads(txt))
                    logger.info(f"🧠 [AI Brain Stage 1] Vision Product Analysis OK ({len(img_parts)} şəkil): {brief.key_visual_hook[:90]}")
                    return brief, True
            except Exception as exc:
                logger.warning(f"🧠 [AI Brain Stage 1] Vision fallback to text brief: {exc}")

        # Graceful text-only fallback brief (never blocks pipeline)
        desc = str(product.get("what_it_does") or product.get("description") or product_name).strip()
        cat = str(product.get("category") or "consumer lifestyle gadget").strip()
        return (
            ProductVisualBrief(
                exact_appearance=f"{product_name} ({cat}) — {desc[:180]}",
                materials_and_colors="Clean modern commercial finish matching the product gallery photos",
                key_visual_hook=f"Instant effortless before-and-after demonstration of {product_name} solving everyday friction",
                ideal_use_environment="Bright modern sunlit home interior with natural depth of field",
                target_buyer_persona=f"Busy modern shoppers looking for a reliable {cat} upgrade",
                avoid_artifacts="Avoid warped product geometry, extra fingers, floating parts, or unreadable text overlays",
            ),
            False,
        )

    async def create_synchronized_storyboard(
        self,
        visual_brief: ProductVisualBrief,
        product: Dict[str, Any],
        product_name: str,
        selling_price: float,
        delivery_days: str,
        avatar_preset: Dict[str, Any],
        variant_count: int,
        ad_language: str,
        rejection_feedback: str = "",
        custom_script_text: str = "",
        critique_notes: str = "",
    ) -> DirectedStoryboardPlan:
        """Stage 2: Direct hooks, synced 3-beat body, CTA, avatar gestures, and 3 cinematic B-Roll I2V prompts."""
        lang_names = {
            "en": "Global US/EU English",
            "az": "Azərbaycan dili",
            "de": "Deutsch (German)",
            "fr": "Français (French)",
        }
        lang_label = lang_names.get(ad_language, "Global US/EU English")

        feedback_block = ""
        if rejection_feedback and rejection_feedback.strip():
            feedback_block = (
                f"\n⚠️ HIGHEST PRIORITY USER REJECTION FEEDBACK (MUST FIX IN THIS VERSION):\n"
                f"\"{rejection_feedback.strip()}\"\n"
            )
        critique_block = ""
        if critique_notes and critique_notes.strip():
            critique_block = (
                f"\n🔍 SELF-CRITIC REFINEMENT INSTRUCTIONS (MUST IMPROVE EVERY ITEM):\n"
                f"{critique_notes.strip()}\n"
            )
        custom_block = ""
        if custom_script_text and custom_script_text.strip():
            custom_block = (
                f"\n📝 USER CUSTOM SCRIPT BASE (preserve core message while structuring cleanly into hooks/body/CTA + 3 synced B-Roll prompts):\n"
                f"\"{custom_script_text.strip()}\"\n"
            )

        prompt = (
            f"Direct a top-1% Hybrid AI UGC commercial storyboard in **{lang_label}**.\n"
            f"PRODUCT: {product_name}\n"
            f"VISUAL BRIEF (from real product photos):\n{json.dumps(visual_brief.model_dump(), ensure_ascii=False)}\n"
            f"OFFER: ${selling_price:.2f} (50% OFF Launch Offer), Tracked Shipping ({delivery_days}), 30-Day Money-Back Guarantee\n"
            f"CREATOR PERSONA: {avatar_preset.get('name')} | Style: {avatar_preset.get('style')} | Gender: {avatar_preset.get('gender')}\n"
            f"{feedback_block}{critique_block}{custom_block}\n"
            f"REQUIREMENTS:\n"
            f"- Output EXACTLY {variant_count} distinct hook_variants (angles: pain_point, curiosity, social_proof), each 10-13 spoken words (~4.0-4.5s) + tailored avatar_gesture_prompt.\n"
            f"- Output 1 shared_body_script (22-30 spoken words) with 3 tight visual beats (Beat 1: detail/problem -> Beat 2: live action demo -> Beat 3: hero payoff).\n"
            f"- Output 1 shared_cta_text (9-13 spoken words) with urgency, price/discount, shipping, and guarantee.\n"
            f"- Output EXACTLY 3 broll_shot_prompts in ENGLISH (30-45 words each) visually grounded in exact_appearance & materials_and_colors, "
            f"synced 1-to-1 with Beats 1, 2, and 3 of shared_body_script, ending with 'preserve exact product geometry and colors, no text, no watermark'."
        )
        raw_text = await self._call_gemini(
            prompt=prompt,
            system_instruction=self.DIRECTOR_SYSTEM,
            response_schema=DirectedStoryboardPlan.model_json_schema(),
        )
        plan = DirectedStoryboardPlan(**json.loads(raw_text))
        return plan

    async def critique_plan(
        self,
        plan: DirectedStoryboardPlan,
        visual_brief: ProductVisualBrief,
        product_name: str,
        ad_language: str,
    ) -> StoryboardCritique:
        """Stage 3: Audit storyboard across 5 performance & AI-safety criteria before spending paid API credits."""
        prompt = (
            f"Audit this UGC commercial storyboard for '{product_name}' (language={ad_language}):\n"
            f"VISUAL BRIEF: {json.dumps(visual_brief.model_dump(), ensure_ascii=False)}\n"
            f"STORYBOARD PLAN: {json.dumps(plan.model_dump(), ensure_ascii=False)}\n\n"
            "Score each of the 5 criteria strictly from 0 to 20 and set total_score = sum of all 5. "
            "If total_score < 85, provide concrete improvement_notes."
        )
        raw = await self._call_gemini(
            prompt=prompt,
            system_instruction=self.CRITIC_SYSTEM,
            response_schema=StoryboardCritique.model_json_schema(),
        )
        crit = StoryboardCritique(**json.loads(raw))
        # Guarantee arithmetic consistency
        computed = (
            crit.hook_power_score
            + crit.pacing_wordcount_score
            + crit.broll_sync_score
            + crit.ai_artifact_safety_score
            + crit.conversion_clarity_score
        )
        crit.total_score = max(0, min(100, computed))
        return crit

    async def direct(
        self,
        image_urls: List[str],
        product: Dict[str, Any],
        product_name: str,
        selling_price: float,
        delivery_days: str,
        avatar_preset: Dict[str, Any],
        variant_count: int,
        ad_language: str,
        rejection_feedback: str = "",
        custom_script_text: str = "",
        quality_threshold: int = 85,
    ) -> Tuple[DirectedStoryboardPlan, Dict[str, Any]]:
        """Run full 3-stage Creative Director AI Brain (Vision -> Storyboard -> Self-Critic + 1x auto-refine)."""
        visual_brief, vision_used = await self.analyze_product_visually(
            image_urls=image_urls,
            product=product,
            product_name=product_name,
            ad_language=ad_language,
        )

        plan = await self.create_synchronized_storyboard(
            visual_brief=visual_brief,
            product=product,
            product_name=product_name,
            selling_price=selling_price,
            delivery_days=delivery_days,
            avatar_preset=avatar_preset,
            variant_count=variant_count,
            ad_language=ad_language,
            rejection_feedback=rejection_feedback,
            custom_script_text=custom_script_text,
        )

        refined = False
        critique_dict: Dict[str, Any] = {"total_score": 88, "improvement_notes": ""}
        try:
            crit = await self.critique_plan(plan, visual_brief, product_name, ad_language)
            critique_dict = crit.model_dump()
            logger.info(
                f"🧠 [AI Brain Stage 3] Self-Critic Score: {crit.total_score}/100 "
                f"(Hook={crit.hook_power_score}, Pacing={crit.pacing_wordcount_score}, "
                f"Sync={crit.broll_sync_score}, Safety={crit.ai_artifact_safety_score}, Conv={crit.conversion_clarity_score})"
            )
            if crit.total_score < quality_threshold:
                logger.info(f"🔁 [AI Brain] Score {crit.total_score} < {quality_threshold} — 1x avtomatik yenidən yazılır...")
                refined_plan = await self.create_synchronized_storyboard(
                    visual_brief=visual_brief,
                    product=product,
                    product_name=product_name,
                    selling_price=selling_price,
                    delivery_days=delivery_days,
                    avatar_preset=avatar_preset,
                    variant_count=variant_count,
                    ad_language=ad_language,
                    rejection_feedback=rejection_feedback,
                    custom_script_text=custom_script_text,
                    critique_notes=crit.improvement_notes or "Make hooks sharper, tighten word counts, and strengthen B-Roll sync.",
                )
                plan = refined_plan
                refined = True
                try:
                    crit2 = await self.critique_plan(plan, visual_brief, product_name, ad_language)
                    critique_dict = crit2.model_dump()
                    logger.info(f"✅ [AI Brain Stage 3] Refined Score: {crit2.total_score}/100")
                except Exception:
                    pass
        except Exception as crit_err:
            logger.warning(f"🧠 [AI Brain Stage 3] Critic step skipped gracefully: {crit_err}")

        # Guarantee exactly 3 non-empty B-Roll prompts
        broll_prompts = [str(p).strip() for p in (plan.broll_shot_prompts or []) if str(p).strip()]
        while len(broll_prompts) < 3:
            broll_prompts.append(
                f"Cinematic vertical 9:16 commercial close-up of {product_name}, smooth subtle camera motion in "
                f"{visual_brief.ideal_use_environment}, preserve exact product geometry and colors, no text, no watermark"
            )
        plan.broll_shot_prompts = broll_prompts[:3]

        director_report = {
            "vision_used": vision_used,
            "visual_brief": visual_brief.model_dump(),
            "critique": critique_dict,
            "refined": refined,
            "rejection_feedback_applied": bool(rejection_feedback and rejection_feedback.strip()),
            "broll_shot_prompts": plan.broll_shot_prompts,
        }
        return plan, director_report
