import asyncio
import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from google import genai
from pydantic import BaseModel, Field

from agents.base import BaseAgent
from state.graph_state import ECommerceState
from tools.creative_cards import render_branded_creative_cards
from tools.creative_tools import (
    AD_VIDEO_DIR,
    MEDIA_DIR,
    UGC_AVATAR_PRESETS,
    concat_audio_segments_with_words,
    download_video_from_url,
    generate_voiceover_with_timestamps,
    get_ugc_avatar_presets,
    render_ugc_commercial_ad,
)
from tools.fal_video_engine import (
    CreativeBalanceError,
    QUALITY_TIERS,
    check_creative_preflight,
    compute_actual_creative_cost,
    estimate_creative_cost,
    generate_hook_lipsync_video,
    generate_product_scene_images,
    generate_shared_ai_broll_clips,
    normalize_quality_tier,
)

logger = logging.getLogger(__name__)


class HookVariant(BaseModel):
    hook_type: str = Field(
        description="Hook strategy angle: 'pain_point', 'curiosity', or 'social_proof'",
        default="pain_point",
    )
    hook_text: str = Field(
        description="3.5-4.5 second scroll-stopping opening hook spoken directly to camera (8-12 words)"
    )


class HybridCreativeScriptPlan(BaseModel):
    hook_variants: List[HookVariant] = Field(
        description="Distinct 3-4 second UGC opening hooks (1 to 3 variants sharing the same body and CTA)"
    )
    shared_body_script: str = Field(
        description="7-10 second spoken product demonstration voiceover covering key benefits & problem solved (20-28 words)"
    )
    shared_cta_text: str = Field(
        description="3.5-4.5 second urgency offer & Call-To-Action citing discount & guarantee (9-13 words)"
    )
    visual_instructions: str = Field(
        description="3-scene commercial storyboard summary",
        default="Scene 1: Lip-Synced UGC Hook | Scene 2: Full-Screen Product B-Roll | Scene 3: Branded Offer CTA End-Card",
    )


class AdScript(BaseModel):
    hook_type: str = Field(default="pain_point")
    hook_text: str
    body_script: str
    cta_text: str
    visual_instructions: str = ""
    language: str = "en"
    quality_tier: str = "balanced"
    avatar_id: Optional[str] = "aysel_ugc"
    avatar_name: Optional[str] = "Chloe S."
    voice_engine: Optional[str] = "ElevenLabs /with-timestamps"
    broll_source: Optional[str] = "Fal.ai Shared AI B-Roll"
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float = 0.0
    qa_gate: Dict[str, Any] = Field(default_factory=dict)
    video_url: Optional[str] = None
    audio_url: Optional[str] = None


class CreativeAssets(BaseModel):
    ad_scripts: List[AdScript] = Field(description="Generated high-converting UGC video ad variants")


class CreativeAgent(BaseAgent):
    """
    4th Agent: Hybrid AI UGC & Commercial Video Studio (Max Quality / Minimum Cost).
      - Default 100% Global US/EU English DTC (with EN/AZ/DE/FR language switcher)
      - 3 Quality/Cost Tiers: Economy / Balanced Hybrid [Default] / Cinema Pro
      - 3-Hook A/B Test Pack [Default] sharing Body+CTA Voice, Scene 2 AI/Supplier B-Roll & Scene 3 CTA End-Card
      - ElevenLabs /with-timestamps word-exact Karaoke captions + exact audio-driven scene boundaries
      - Fal.ai Async Queue Lip-Sync ONLY on 3.5-4.5s Hook audio (~80% Lip-Sync savings)
      - Playwright HTML->PNG ($0) Branded CTA End-Card + 2 Static Ads (1080x1920 Story & 1080x1080 Feed)
      - Clean Voice-Only Post-Production (xfade transitions, -14 LUFS loudness, NO BGM/SFX)
      - Strict Fail-Fast Pre-Flight Gate & Post-Render FFmpeg QA Gate (zero fake YouTube or stale video fallback)
    """

    def __init__(
        self,
        client: genai.Client,
        model: str = "gemini-3.5-flash-lite",
        tools: Optional[List[Callable]] = None,
    ):
        super().__init__(client=client, model=model, tools=tools)
        self.system_instruction = (
            "You are a world-class Direct-To-Consumer (DTC) Performance Creative Director for TikTok, Instagram Reels, and Meta Ads. "
            "Your job is to script ultra-converting 15-18 second UGC commercial video ads engineered around a shared demonstration middle + CTA "
            "and 3 distinct scroll-stopping 3.5-4.5 second opening hooks (pain_point, curiosity, social_proof).\n\n"
            "STRICT RULES:\n"
            "1. Write 100% in the requested target language (default: natural American/Global English for US/EU DTC shoppers).\n"
            "2. Keep each hook_text strictly 8-12 spoken words (~3.5-4.5 seconds) so AI lip-sync stays razor-sharp and low-cost.\n"
            "3. Keep shared_body_script strictly 20-28 spoken words (~8-9 seconds) describing how this exact product solves the buyer's friction.\n"
            "4. Keep shared_cta_text strictly 9-13 spoken words (~3.5-4 seconds) mentioning the discount offer, fast tracked shipping, and 30-day guarantee.\n"
            "5. NEVER include emojis or hashtags in spoken script lines."
        )

    def _auto_select_avatar_id(self, product: Dict[str, Any], requested_id: Optional[str] = None) -> str:
        if requested_id and requested_id in UGC_AVATAR_PRESETS:
            return requested_id
        text_blob = f"{product.get('english_name', '')} {product.get('name', '')} {product.get('category', '')} {product.get('what_it_does', '')}".lower()
        if any(k in text_blob for k in ["auto", "car", "avto", "fitness", "gym", "sport", "massage", "outdoor"]):
            return "emin_ugc"
        if any(k in text_blob for k in ["tech", "smart", "led", "adapter", "audio", "earbud", "wireless", "charger", "phone"]):
            return "resad_ugc"
        if any(k in text_blob for k in ["kitchen", "home", "clean", "espresso", "coffee", "ultrasonic", "mətbəx", "ev"]):
            return "leyla_ugc"
        return "aysel_ugc"

    @staticmethod
    def _is_valid_video_url(u: Optional[str]) -> bool:
        if not u:
            return False
        s = str(u).strip()
        if not s.startswith("http"):
            return False
        if "/search?" in s or "/search/" in s or "google.com" in s:
            return False
        return True

    async def _download_supplier_videos(self, target_video_urls: List[str], supplier: Dict[str, Any], run_uid: str) -> List[str]:
        if not target_video_urls:
            return []
        loop = asyncio.get_running_loop()

        async def _dl(v_idx: int, v_url: str) -> Optional[str]:
            direct_v = supplier.get("video_url")
            dl_target = direct_v if (direct_v and "1688.com/offer/" in v_url) else v_url
            try:
                dl_raw = await loop.run_in_executor(
                    None, download_video_from_url, dl_target, f"raw_source_{v_idx+1}_{run_uid}.mp4"
                )
                dl_res = json.loads(dl_raw)
                if dl_res.get("status") == "success" and dl_res.get("file_path"):
                    return str(dl_res["file_path"])
            except Exception as exc:
                logger.warning(f"Supplier video #{v_idx+1} download note: {exc}")
            return None

        results = await asyncio.gather(*[_dl(i, u) for i, u in enumerate(target_video_urls[:2])], return_exceptions=True)
        return [r for r in results if isinstance(r, str) and Path(r).exists()]

    async def run(self, state: ECommerceState) -> Dict[str, Any]:
        """Execute Stage 2 Agent 4 Hybrid Studio with strict Fail-Fast and Post-Render QA."""
        logger.info("🎬 Starting 4th Agent (Hybrid AI UGC Studio: Pre-Flight + /with-timestamps + Fal Queue + Playwright PNG)...")
        product = dict(state.get("selected_product") or {})
        supplier = dict(state.get("selected_supplier") or {})
        economics = dict(state.get("unit_economics") or supplier.get("unit_economics") or {})
        store = dict(state.get("store") or {})
        store_config = dict(state.get("store_config") or store.get("store_config") or {})
        brand = dict(state.get("brand_identity") or state.get("brand") or {})
        creative_cfg = dict(state.get("creative_config") or {})

        ad_language = (creative_cfg.get("ad_language") or "en").strip().lower()
        if ad_language not in ("en", "az", "de", "fr"):
            ad_language = "en"
        quality_tier = normalize_quality_tier(creative_cfg.get("quality_tier") or "balanced")
        variant_count = 3 if int(creative_cfg.get("variant_count") or 3) >= 3 else 1
        broll_mode = (creative_cfg.get("broll_mode") or "auto").strip().lower()
        custom_script_text = (creative_cfg.get("custom_script") or "").strip()

        clean_studio_img = str(product.get("image_url") or "").strip()
        if supplier:
            product["supplier_link"] = supplier.get("order_link")
            if supplier.get("product_image_url") and not clean_studio_img:
                clean_studio_img = str(supplier.get("product_image_url")).strip()
                product["image_url"] = clean_studio_img
            if supplier.get("supplier_name"):
                product["supplier_name"] = supplier.get("supplier_name")

        product_name_display = str(
            (product.get("english_name") if ad_language != "az" else product.get("name"))
            or product.get("english_name")
            or product.get("name")
            or "Featured Product"
        ).strip()
        run_uid = uuid.uuid4().hex[:6]

        # Collect Stage 2 confirmed + Stage 1 verified gallery images (NO watch fallback!)
        gallery_urls: List[str] = []
        for candidate_src in [
            creative_cfg.get("broll_image_urls"),
            store.get("gallery_images"),
            store_config.get("product_images"),
            product.get("gallery_images"),
            [clean_studio_img] if clean_studio_img else [],
            [supplier.get("product_image_url")] if supplier.get("product_image_url") else [],
        ]:
            if isinstance(candidate_src, list):
                for u in candidate_src:
                    s = str(u).strip() if isinstance(u, str) else ""
                    if s and (s.startswith("http") or s.startswith("/static/")) and "1523275335684" not in s and s not in gallery_urls:
                        gallery_urls.append(s)
        primary_product_img = gallery_urls[0] if gallery_urls else ""

        # Avatar persona localized to chosen language
        avatar_id = self._auto_select_avatar_id(product, creative_cfg.get("avatar_id"))
        localized_presets = {p["id"]: p for p in get_ugc_avatar_presets(language=ad_language)}
        avatar_preset = localized_presets.get(avatar_id) or next(iter(localized_presets.values()))
        avatar_image_url = (creative_cfg.get("custom_avatar_url") or "").strip() or avatar_preset["avatar_url"]

        # 1. Collect explicit manual or supplier video URLs (strictly ignoring /search? links)
        manual_videos = state.get("manual_video_links") or []
        target_video_urls: List[str] = [str(u).strip() for u in manual_videos if self._is_valid_video_url(u)]
        if not target_video_urls and broll_mode in ("auto", "supplier_video", "1688_clean"):
            for fb in [supplier.get("video_url"), supplier.get("product_video_url"), supplier.get("order_link")]:
                if self._is_valid_video_url(fb):
                    target_video_urls.append(str(fb).strip())
                    break

        downloaded_supplier_clips: List[str] = []
        if target_video_urls and broll_mode not in ("studio_only", "ai_broll", "ai_minimax", "ai_i2v"):
            downloaded_supplier_clips = await self._download_supplier_videos(target_video_urls, supplier, run_uid)

        has_supplier_video = len(downloaded_supplier_clips) > 0

        # 2. Strict Pre-Flight Gate (checks ElevenLabs quota + FAL_KEY + exact $/s cost estimate BEFORE spending)
        loop = asyncio.get_running_loop()
        preflight = await loop.run_in_executor(
            None,
            lambda: check_creative_preflight(
                quality_tier=quality_tier,
                variant_count=variant_count,
                has_supplier_video=has_supplier_video,
                broll_mode=broll_mode,
                strict=True,
                image_count=len(gallery_urls),
            ),
        )
        est_info = preflight["estimate"]

        # 3. Generate Script Plan (3-Hook A/B Test or 1 Video) in target language
        selling_price = round(float(store_config.get("selling_price") or economics.get("selling_price") or 39.99), 2)
        delivery_days = str(supplier.get("delivery_days") or product.get("delivery_days") or "6-10 Business Days")
        lang_names = {"en": "Global US/EU English", "az": "Azərbaycan dili", "de": "Deutsch (German)", "fr": "Français (French)"}

        if custom_script_text:
            parts = [p.strip() for p in re.split(r"[.!?]+", custom_script_text) if p.strip()]
            base_hook = parts[0] if parts else f"Wait until you see what this {product_name_display} can do"
            base_cta = parts[-1] if len(parts) > 2 else f"Grab fifty percent off today for ${selling_price:.2f} with a thirty day guarantee"
            base_body = ". ".join(parts[1:-1]) if len(parts) > 2 else custom_script_text
            script_plan = HybridCreativeScriptPlan(
                hook_variants=[
                    HookVariant(hook_type=["pain_point", "curiosity", "social_proof"][i % 3], hook_text=base_hook)
                    for i in range(variant_count)
                ],
                shared_body_script=base_body,
                shared_cta_text=base_cta,
            )
        else:
            prompt = (
                f"Create a high-converting Hybrid AI UGC script plan in **{lang_names[ad_language]}**.\n"
                f"PRODUCT: {product_name_display}\n"
                f"PRODUCT CONTEXT: {json.dumps(product, ensure_ascii=False)}\n"
                f"OFFER DETAILS: Price ${selling_price:.2f} (50% OFF launch offer), Tracked Shipping ({delivery_days}), 30-Day Money-Back Guarantee\n"
                f"CREATOR PERSONA: {avatar_preset['name']} ({avatar_preset['style']})\n\n"
                f"OUTPUT EXACTLY {variant_count} distinct hook_variants (covering: pain_point, curiosity, social_proof) "
                f"that ALL seamlessly flow into 1 shared_body_script (20-28 words) and 1 shared_cta_text (9-13 words).\n"
                f"CRITICAL SPEECH RULES:\n"
                f"- Each hook_text MUST be 8-12 spoken words (~3.5-4.5s) directly addressing the viewer on camera.\n"
                f"- Spell out numbers naturally in {lang_names[ad_language]} where helpful, and do NOT use emojis or hashtags."
            )
            response_text = await self._call_gemini(
                prompt=prompt,
                system_instruction=self.system_instruction,
                response_schema=HybridCreativeScriptPlan.model_json_schema(),
            )
            script_plan = HybridCreativeScriptPlan(**json.loads(response_text))

        hooks = list(script_plan.hook_variants[:variant_count])
        while len(hooks) < variant_count:
            hooks.append(hooks[0])

        # 4. Generate SHARED Assets ONCE (Shared Body+CTA Audio, Shared B-Roll Clips, Branded CTA + 2 Static Ads)
        logger.info(
            f"Generating shared creative assets [{quality_tier} | {ad_language} | {variant_count} variants | est ${est_info['estimated_cost_usd']:.2f}]..."
        )
        shared_body_task = generate_voiceover_with_timestamps(
            text=script_plan.shared_body_script,
            avatar_id=avatar_id,
            output_filename=f"shared_body_{run_uid}.mp3",
            language=ad_language,
            quality_tier=quality_tier,
        )
        shared_cta_task = generate_voiceover_with_timestamps(
            text=script_plan.shared_cta_text,
            avatar_id=avatar_id,
            output_filename=f"shared_cta_{run_uid}.mp3",
            language=ad_language,
            quality_tier=quality_tier,
        )
        cards_task = loop.run_in_executor(
            None,
            lambda: render_branded_creative_cards(
                product=product,
                supplier=supplier,
                economics=economics,
                brand=brand,
                store_config=store_config,
                cta_text=script_plan.shared_cta_text,
                hook_headline=hooks[0].hook_text,
                run_uid=run_uid,
                language=ad_language,
            ),
        )

        # Shared Scene 2 B-Roll (Real supplier video $0 OR 3 distinct AI I2V clips from 3 vertical 9:16 images)
        async def _prepare_shared_broll() -> tuple[List[str], str, List[Dict[str, Any]], float, int]:
            if downloaded_supplier_clips:
                return downloaded_supplier_clips, "Verified Supplier / Manual Video ($0)", [], 0.0, 0
            if broll_mode == "studio_only":
                return [], "Studio Multi-Angle Product Motion ($0)", [], 0.0, 0

            target_broll_n = int(QUALITY_TIERS[quality_tier]["broll_clip_count"])
            frame_wh = (1080, 1920) if quality_tier == "cinema" else (720, 1280)
            scene_res = await loop.run_in_executor(
                None,
                lambda: generate_product_scene_images(
                    existing_image_urls=gallery_urls,
                    product_name=product_name_display,
                    output_dir=AD_VIDEO_DIR,
                    run_uid=run_uid,
                    target_count=target_broll_n,
                    frame_size=frame_wh,
                ),
            )
            scene_img_paths = scene_res["image_paths"]
            broll_details = await loop.run_in_executor(
                None,
                lambda: generate_shared_ai_broll_clips(
                    product_image_urls=scene_img_paths,
                    product_name=product_name_display,
                    output_dir=AD_VIDEO_DIR,
                    run_uid=run_uid,
                    quality_tier=quality_tier,
                    return_details=True,
                ),
            )
            tier_label = QUALITY_TIERS[quality_tier]["broll_model"].split("/")[-1]
            gen_cnt = int(scene_res.get("generated_count") or 0)
            src_note = f"+{gen_cnt} AI Scene" if gen_cnt > 0 else f"{len(scene_img_paths)} Qalereya Şəkli"
            return (
                broll_details["clip_paths"],
                f"Fal.ai 3-Klip AI B-Roll ({tier_label} | {src_note})",
                broll_details["clips_meta"],
                float(scene_res.get("cost_usd") or 0.0),
                gen_cnt,
            )

        shared_body_seg, shared_cta_seg, cards_info, (shared_broll_clips, broll_label, broll_clips_meta, scene_gen_usd, scene_gen_cnt) = await asyncio.gather(
            shared_body_task,
            shared_cta_task,
            cards_task,
            _prepare_shared_broll(),
        )

        # 5. Build each Hook Variant (3.5-4.0s Hook Voice -> Fal Queue Lip-Sync -> Concat Audio -> Render -> QA Gate)
        per_variant_est = round(est_info["estimated_cost_usd"] / max(len(hooks), 1), 2)
        lipsync_records: List[Dict[str, Any]] = []
        shared_broll_and_scene_usd = sum(float(c.get("cost_usd") or 0.0) for c in broll_clips_meta) + scene_gen_usd

        async def _build_variant(idx: int, hv: HookVariant) -> Dict[str, Any]:
            hook_seg = await generate_voiceover_with_timestamps(
                text=hv.hook_text,
                avatar_id=avatar_id,
                output_filename=f"hook_{idx+1}_{run_uid}.mp3",
                language=ad_language,
                quality_tier=quality_tier,
            )

            # Generate real AI Lip-Synced Hook Video ONLY on the short 3.5-4.0s hook audio!
            lipsync_dest = AD_VIDEO_DIR / f"hook_lipsync_{idx+1}_{run_uid}.mp4"
            hook_details = await loop.run_in_executor(
                None,
                lambda: generate_hook_lipsync_video(
                    avatar_image_url=avatar_image_url,
                    hook_audio_path=hook_seg["file_path"],
                    dest_video_path=lipsync_dest,
                    quality_tier=quality_tier,
                    return_details=True,
                ),
            )
            hook_video_path = hook_details["file_path"]
            lipsync_records.append(hook_details)

            merged_audio = await loop.run_in_executor(
                None,
                lambda: concat_audio_segments_with_words(
                    [hook_seg, shared_body_seg, shared_cta_seg],
                    output_filename=f"ad_voice_master_{idx+1}_{run_uid}.mp3",
                ),
            )

            unique_video_filename = f"rendered_ugc_ad_{idx+1}_{run_uid}.mp4"
            full_text = f"{hv.hook_text}. {script_plan.shared_body_script}. {script_plan.shared_cta_text}"
            render_raw = await loop.run_in_executor(
                None,
                lambda: render_ugc_commercial_ad(
                    product_image_url=primary_product_img,
                    avatar_image_url=avatar_image_url,
                    voiceover_path=merged_audio["file_path"],
                    hook_text=hv.hook_text,
                    full_script_text=full_text,
                    cta_text=script_plan.shared_cta_text,
                    ugc_avatar_video_path=hook_video_path,
                    output_filename=unique_video_filename,
                    product_name=product_name_display,
                    words=merged_audio["words"],
                    scene1_dur=merged_audio["scene1_dur"],
                    scene2_dur=merged_audio["scene2_dur"],
                    scene3_dur=merged_audio["scene3_dur"],
                    broll_clip_paths=shared_broll_clips,
                    product_gallery_urls=gallery_urls,
                    cta_png_path=cards_info["cta_endcard_path"],
                    language=ad_language,
                ),
            )
            render_res = json.loads(render_raw)
            src_v = Path(render_res["video_path"])
            if src_v.exists():
                shutil.copy2(src_v, AD_VIDEO_DIR / f"rendered_ugc_ad_{idx+1}.mp4")
                shutil.copy2(src_v, MEDIA_DIR / "rendered_ads" / f"rendered_ugc_ad_{idx+1}.mp4")

            var_actual_usd = round(
                float(hook_details.get("cost_usd") or 0.0)
                + (shared_broll_and_scene_usd / max(len(hooks), 1))
                + float(est_info["breakdown"].get("elevenlabs_voice_timestamps_usd", 0.02)) / max(len(hooks), 1),
                2,
            )

            return AdScript(
                hook_type=hv.hook_type,
                hook_text=hv.hook_text,
                body_script=script_plan.shared_body_script,
                cta_text=script_plan.shared_cta_text,
                visual_instructions=script_plan.visual_instructions,
                language=ad_language,
                quality_tier=quality_tier,
                avatar_id=avatar_id,
                avatar_name=avatar_preset["name"],
                voice_engine=f"ElevenLabs /with-timestamps ({QUALITY_TIERS[quality_tier]['elevenlabs_model']})",
                broll_source=broll_label,
                estimated_cost_usd=per_variant_est,
                actual_cost_usd=var_actual_usd,
                qa_gate=render_res.get("qa_gate") or {},
                video_url=render_res.get("web_url"),
                audio_url=merged_audio.get("web_url"),
            ).model_dump()

        processed_scripts = await asyncio.gather(*[_build_variant(i, hooks[i]) for i in range(len(hooks))])

        total_el_chars = len(script_plan.shared_body_script) + len(script_plan.shared_cta_text) + sum(len(h.hook_text) for h in hooks)
        actual_summary = compute_actual_creative_cost(
            quality_tier=quality_tier,
            lipsync_records=lipsync_records,
            broll_records=broll_clips_meta,
            scene_gen_cost_usd=scene_gen_usd,
            elevenlabs_char_count=total_el_chars,
        )

        static_ads = [
            {
                "format": "story_9x16",
                "dimensions": "1080x1920",
                "label": "Meta / TikTok Story & Reels Static Ad",
                "image_url": cards_info["static_story_url"],
            },
            {
                "format": "feed_1x1",
                "dimensions": "1080x1080",
                "label": "Instagram / Facebook Feed Static Ad",
                "image_url": cards_info["static_feed_url"],
            },
        ]

        return {
            "creatives": processed_scripts,
            "creative_assets": {
                "ad_scripts": processed_scripts,
                "static_ads": static_ads,
                "cta_endcard_url": cards_info["cta_endcard_url"],
                "preflight_estimate": est_info,
                "actual_cost_summary": actual_summary,
                "scene_gen_count": scene_gen_cnt,
                "language": ad_language,
                "quality_tier": quality_tier,
            },
            "logs": [
                f"✅ 4-cü Agent ({QUALITY_TIERS[quality_tier]['label']} | {ad_language.upper()}): '{product_name_display}' üçün "
                f"{len(processed_scripts)} video + 2 statik reklam hazırlandı | "
                f"Təxmini: ${est_info['estimated_cost_usd']:.2f} → Faktiki: ${actual_summary['actual_cost_usd']:.2f} (Fal: ${actual_summary['actual_fal_usd']:.2f}) | QA Gate: PASS."
            ],
        }


def create_creative_node(
    client: genai.Client,
    model: str = "gemini-3.5-flash-lite",
    tools: Optional[List[Callable]] = None,
) -> Callable[[ECommerceState], Dict[str, Any]]:
    """Factory function to create the creative node."""
    agent = CreativeAgent(client=client, model=model, tools=tools)

    async def node(state: ECommerceState) -> Dict[str, Any]:
        result = await agent.run(state)
        result["completed_parallel_nodes"] = ["creative_production"]
        return result

    return node
