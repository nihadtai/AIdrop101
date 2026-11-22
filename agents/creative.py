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
    ELEVENLABS_RATE_PER_1K_CHARS,
    QUALITY_TIERS,
    check_creative_preflight,
    compute_actual_creative_cost,
    estimate_creative_cost,
    generate_hook_lipsync_video,
    generate_product_scene_images,
    generate_shared_ai_broll_clips,
    normalize_quality_tier,
)
from agents.creative_director import CreativeDirectorBrain

logger = logging.getLogger(__name__)


class HookVariant(BaseModel):
    hook_type: str = Field(
        description="Hook strategy angle: 'pain_point', 'curiosity', or 'social_proof'",
        default="pain_point",
    )
    hook_text: str = Field(
        description="4.0-4.5 second scroll-stopping opening hook spoken directly to camera (10-13 words)"
    )
    avatar_gesture_prompt: Optional[str] = Field(
        default=None,
        description="Optional director visual/gesture instruction for lip-sync talking-head",
    )


class HybridCreativeScriptPlan(BaseModel):
    hook_variants: List[HookVariant] = Field(
        description="Distinct 4-second UGC opening hooks (1 to 3 variants sharing the same body and CTA)"
    )
    shared_body_script: str = Field(
        description="8-10 second spoken product demonstration voiceover covering key benefits & problem solved (22-30 words)"
    )
    shared_cta_text: str = Field(
        description="3.5-4.5 second urgency offer & Call-To-Action citing discount & guarantee (9-13 words)"
    )
    visual_instructions: str = Field(
        description="3-scene commercial storyboard summary",
        default="Scene 1: Lip-Synced UGC Hook | Scene 2: Full-Screen Product B-Roll | Scene 3: Branded Offer CTA End-Card",
    )


PLACEHOLDER_IMAGE_PATTERNS = (
    "1523275335684",
    "placeholder",
    "no-image",
    "default-product",
    "via.placeholder.com",
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
    shared_cost_usd: float = 0.0
    status: str = "completed"
    error: Optional[str] = None
    retryable: bool = False
    variant_index: int = 0
    run_uid: Optional[str] = None
    qa_gate: Dict[str, Any] = Field(default_factory=dict)
    video_url: Optional[str] = None
    audio_url: Optional[str] = None


class CreativeAssets(BaseModel):
    ad_scripts: List[AdScript] = Field(description="Generated high-converting UGC video ad variants")


class CreativeAgent(BaseAgent):
    """
    4th Agent: Hybrid AI UGC & Commercial Video Studio v2 (Creative Director AI Brain + EL v3/v4 + OmniHuman v2 / Seedance 2.0).
      - Default 100% Global US/EU English DTC (with EN/AZ/DE/FR language switcher)
      - 3 Quality/Cost Tiers: Economy (EL v3 + Seedance Lite) / Balanced (EL v4 Turbo + OmniHuman v2 + Seedance 1.5 Pro) / Cinema (EL v4 + OmniHuman v2 + Seedance 2.0 Fast)
      - 3-Stage Creative Director AI Brain (Vision Product Analysis -> Synced 3-Beat Storyboard -> Pre-Spend Self-Critic)
      - 12 Realistic UGC Human Avatars + Upload + 1-Click AI Avatar Generation
      - Per-variant isolation, step-level retries, +25% Fal retry budget guard, and run-level asset cache
      - Clean Voice-Only Post-Production (xfade transitions, -14 LUFS loudness, NO BGM/SFX) + Post-Render FFmpeg QA Gate
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
            "and 3 distinct scroll-stopping 4.0-4.5 second opening hooks (pain_point, curiosity, social_proof).\n\n"
            "STRICT RULES:\n"
            "1. Write 100% in the requested target language (default: natural American/Global English for US/EU DTC shoppers).\n"
            "2. Keep each hook_text strictly 10-13 spoken words (~4.0-4.5 seconds) so AI lip-sync stays razor-sharp and OmniHuman v2 ready.\n"
            "3. Keep shared_body_script strictly 22-30 spoken words (~8-10 seconds) describing how this exact product solves the buyer's friction.\n"
            "4. Keep shared_cta_text strictly 9-13 spoken words (~3.5-4 seconds) mentioning the discount offer, fast tracked shipping, and 30-day guarantee.\n"
            "5. NEVER include emojis or hashtags in spoken script lines."
        )

    def _auto_select_avatar_id(self, product: Dict[str, Any], requested_id: Optional[str] = None) -> str:
        if requested_id and requested_id in UGC_AVATAR_PRESETS:
            return requested_id
        text_blob = f"{product.get('english_name', '')} {product.get('name', '')} {product.get('category', '')} {product.get('what_it_does', '')}".lower()
        if any(k in text_blob for k in ["skincare", "serum", "beauty", "face", "makeup", "hair", "glow", "dəri", "gözəllik"]):
            return "sofia_ugc"
        if any(k in text_blob for k in ["fashion", "jewelry", "watch", "luxury", "bag", "perfume", "moda"]):
            return "claire_ugc"
        if any(k in text_blob for k in ["yoga", "pilates", "wellness", "posture", "stretch"]):
            return "maya_ugc"
        if any(k in text_blob for k in ["baby", "kid", "family", "pet", "organizer", "ailə"]):
            return "rachel_ugc"
        if any(k in text_blob for k in ["auto", "car", "avto", "dashcam", "trunk", "vehicle"]):
            return "noah_ugc"
        if any(k in text_blob for k in ["business", "travel", "office", "luggage", "desk", "executive", "səyahət"]):
            return "david_ugc"
        if any(k in text_blob for k in ["diy", "tool", "drill", "repair", "garden", "alət"]):
            return "daniel_ugc"
        if any(k in text_blob for k in ["fitness", "gym", "sport", "massage", "outdoor", "idman"]):
            return "emin_ugc"
        if any(k in text_blob for k in ["tech", "smart", "led", "adapter", "audio", "earbud", "wireless", "charger", "phone"]):
            return "resad_ugc"
        if any(k in text_blob for k in ["kitchen", "home", "clean", "espresso", "coffee", "ultrasonic", "mətbəx", "ev"]):
            return "leyla_ugc"
        return "aysel_ugc"

    @staticmethod
    def _is_placeholder_image(url: str) -> bool:
        low = (url or "").strip().lower()
        return any(pat in low for pat in PLACEHOLDER_IMAGE_PATTERNS)

    @staticmethod
    def _is_valid_video_url(u: Optional[str]) -> bool:
        """Accept ONLY direct video files or real video-hosting URLs (reject HTML offer/search pages)."""
        if not u:
            return False
        s = str(u).strip()
        low = s.lower()
        if not low.startswith("http"):
            return False
        if "/search?" in low or "/search/" in low or "google.com" in low:
            return False
        # Reject 1688/Alibaba HTML product offer pages (not direct videos)
        if "1688.com/offer/" in low or "alibaba.com/product-detail/" in low:
            return False
        video_markers = (
            ".mp4", ".mov", ".webm", ".m3u8",
            "cloud.video.taobao.com", "aliyuncs.com", "fal.media",
            "tiktok.com", "vm.tiktok.com", "douyin.com", "vimeo.com", "v.redd.it",
        )
        return any(m in low for m in video_markers)

    async def _download_supplier_videos(self, target_video_urls: List[str], supplier: Dict[str, Any], run_uid: str) -> List[str]:
        if not target_video_urls:
            return []
        loop = asyncio.get_running_loop()

        async def _dl(v_idx: int, v_url: str) -> Optional[str]:
            try:
                dl_raw = await loop.run_in_executor(
                    None, download_video_from_url, v_url, f"raw_source_{v_idx+1}_{run_uid}.mp4"
                )
                dl_res = json.loads(dl_raw)
                if dl_res.get("status") == "success" and dl_res.get("file_path"):
                    return str(dl_res["file_path"])
            except Exception as exc:
                logger.warning(f"Supplier video #{v_idx+1} download note: {exc}")
            return None

        results = await asyncio.gather(*[_dl(i, u) for i, u in enumerate(target_video_urls[:2])], return_exceptions=True)
        return [r for r in results if isinstance(r, str) and Path(r).exists()]

    @staticmethod
    async def _tts_with_retry(
        text: str,
        avatar_id: str,
        output_filename: str,
        language: str,
        quality_tier: str,
        cache_dir: Optional[Path] = None,
        cache_key: Optional[str] = None,
        max_attempts: int = 2,
    ) -> Dict[str, Any]:
        """Generate ElevenLabs voiceover with timestamp caching and exponential backoff retry."""
        if cache_dir and cache_key:
            meta_p = cache_dir / f"{cache_key}.tts.json"
            if meta_p.exists():
                try:
                    cached = json.loads(meta_p.read_text(encoding="utf-8"))
                    fp = Path(cached.get("file_path") or "")
                    if fp.exists() and fp.stat().st_size > 1000:
                        return cached
                except Exception:
                    pass

        last_exc: Optional[Exception] = None
        for attempt in range(max_attempts):
            try:
                seg = await generate_voiceover_with_timestamps(
                    text=text,
                    avatar_id=avatar_id,
                    output_filename=output_filename,
                    language=language,
                    quality_tier=quality_tier,
                )
                if cache_dir and cache_key:
                    try:
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        (cache_dir / f"{cache_key}.tts.json").write_text(
                            json.dumps(seg, ensure_ascii=False), encoding="utf-8"
                        )
                    except Exception:
                        pass
                return seg
            except CreativeBalanceError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < max_attempts:
                    delay = 1.5 * (2 ** attempt)
                    logger.warning(f"TTS '{output_filename}' cəhd {attempt+1}/{max_attempts} uğursuz ({exc}); {delay:.1f}s sonra retry...")
                    await asyncio.sleep(delay)
        raise RuntimeError(f"ElevenLabs TTS uğursuz oldu: {last_exc}")

    async def _ensure_unique_hooks(
        self,
        hooks: List[HookVariant],
        variant_count: int,
        product_name: str,
        ad_language: str,
    ) -> List[HookVariant]:
        """Ensure every hook variant has non-empty, distinct spoken text BEFORE paying for lip-sync."""
        angles = ["pain_point", "curiosity", "social_proof"]
        prefixes_by_lang = {
            "en": [
                f"Stop struggling every day when this {product_name} fixes it fast",
                f"Wait until you see what this {product_name} actually does",
                f"Everyone keeps asking where I got this {product_name}",
            ],
            "az": [
                f"Hər gün vaxt itirməyə son, bu {product_name} işini asanlaşdırır",
                f"Bu {product_name} məhsulunun nə etdiyini görəndə təəccüblənəcəksən",
                f"Hamı bu {product_name} məhsulunu haradan aldığımı soruşur",
            ],
            "de": [
                f"Schluss mit dem Stress, dieses {product_name} löst es sofort",
                f"Warte bis du siehst was dieses {product_name} wirklich kann",
                f"Alle fragen mich woher ich dieses {product_name} habe",
            ],
            "fr": [
                f"Fini la galère, ce {product_name} règle tout en quelques secondes",
                f"Attends de voir ce que ce {product_name} fait vraiment",
                f"Tout le monde me demande où j'ai trouvé ce {product_name}",
            ],
        }
        fallbacks = prefixes_by_lang.get(ad_language, prefixes_by_lang["en"])

        cleaned: List[HookVariant] = []
        seen_norm: set[str] = set()
        for hv in hooks[:variant_count]:
            txt = (hv.hook_text or "").strip()
            norm = re.sub(r"\W+", " ", txt.lower()).strip()
            if txt and norm not in seen_norm:
                seen_norm.add(norm)
                cleaned.append(
                    HookVariant(
                        hook_type=hv.hook_type or angles[len(cleaned) % 3],
                        hook_text=txt,
                        avatar_gesture_prompt=getattr(hv, "avatar_gesture_prompt", None),
                    )
                )

        missing = variant_count - len(cleaned)
        if missing > 0:
            # Ask LLM once for missing unique hooks before falling back deterministically
            try:
                needed_angles = [angles[(len(cleaned) + i) % 3] for i in range(missing)]
                existing_txt = [h.hook_text for h in cleaned]
                regen_prompt = (
                    f"Generate {missing} NEW, distinct 8-12 word opening hooks in language '{ad_language}' "
                    f"for product '{product_name}' covering angles {needed_angles}. "
                    f"Do NOT repeat or paraphrase these existing hooks: {existing_txt}. "
                    f"Return JSON with hook_variants, shared_body_script='ok', shared_cta_text='ok'."
                )
                resp_txt = await self._call_gemini(
                    prompt=regen_prompt,
                    system_instruction=self.system_instruction,
                    response_schema=HybridCreativeScriptPlan.model_json_schema(),
                )
                extra_plan = HybridCreativeScriptPlan(**json.loads(resp_txt))
                for eh in extra_plan.hook_variants:
                    if len(cleaned) >= variant_count:
                        break
                    txt = (eh.hook_text or "").strip()
                    norm = re.sub(r"\W+", " ", txt.lower()).strip()
                    if txt and norm not in seen_norm:
                        seen_norm.add(norm)
                        cleaned.append(HookVariant(hook_type=eh.hook_type or angles[len(cleaned) % 3], hook_text=txt))
            except Exception as exc:
                logger.warning(f"Duplicate hook LLM replenishment note: {exc}")

        while len(cleaned) < variant_count:
            idx = len(cleaned)
            cand = fallbacks[idx % len(fallbacks)]
            norm = re.sub(r"\W+", " ", cand.lower()).strip()
            if norm in seen_norm:
                cand = f"{cand} right now"
            seen_norm.add(norm)
            cleaned.append(HookVariant(hook_type=angles[idx % 3], hook_text=cand))

        return cleaned[:variant_count]

    async def run(self, state: ECommerceState) -> Dict[str, Any]:
        """Execute Stage 2 Agent 4 Hybrid Studio with strict Fail-Fast, per-variant isolation, cache, and Post-Render QA."""
        logger.info("🎬 Starting 4th Agent (Hybrid AI UGC Studio: Pre-Flight + Cache + /with-timestamps + Fal Queue + Playwright PNG)...")
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
        run_uid = (creative_cfg.get("resume_run_uid") or "").strip() or uuid.uuid4().hex[:6]
        cache_dir = AD_VIDEO_DIR / "cache" / run_uid
        cache_dir.mkdir(parents=True, exist_ok=True)
        progress_logs: List[str] = []

        # Collect Stage 2 confirmed + Stage 1 verified gallery images (filtering placeholders)
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
                    if s and (s.startswith("http") or s.startswith("/static/")) and not self._is_placeholder_image(s) and s not in gallery_urls:
                        gallery_urls.append(s)
        primary_product_img = gallery_urls[0] if gallery_urls else ""

        # Avatar persona localized to chosen language
        avatar_id = self._auto_select_avatar_id(product, creative_cfg.get("avatar_id"))
        localized_presets = {p["id"]: p for p in get_ugc_avatar_presets(language=ad_language)}
        avatar_preset = localized_presets.get(avatar_id) or next(iter(localized_presets.values()))
        avatar_image_url = (creative_cfg.get("custom_avatar_url") or "").strip() or avatar_preset["avatar_url"]

        # 1. Collect ONLY real direct/host video URLs (never 1688 HTML order_link or /search?)
        manual_videos = state.get("manual_video_links") or []
        target_video_urls: List[str] = [str(u).strip() for u in manual_videos if self._is_valid_video_url(u)]
        if not target_video_urls and broll_mode in ("auto", "supplier_video", "1688_clean"):
            for fb in [supplier.get("video_url"), supplier.get("product_video_url")]:
                if self._is_valid_video_url(fb):
                    target_video_urls.append(str(fb).strip())
                    break

        downloaded_supplier_clips: List[str] = []
        if target_video_urls and broll_mode not in ("studio_only", "ai_broll", "ai_minimax", "ai_i2v"):
            downloaded_supplier_clips = await self._download_supplier_videos(target_video_urls, supplier, run_uid)

        has_supplier_video = len(downloaded_supplier_clips) > 0

        # 2. Strict Pre-Flight Gate (checks ElevenLabs quota + scoped TTS probe + FAL_KEY + exact $/s cost estimate BEFORE spending)
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
        per_variant_lipsync_cap = float(est_info.get("per_variant_lipsync_cap_usd") or 0.5)

        # 3. 3-Stage Creative Director AI Brain (Vision Analysis -> Synced Storyboard + 3 B-Roll Prompts -> Self-Critic)
        selling_price = round(float(store_config.get("selling_price") or economics.get("selling_price") or 39.99), 2)
        delivery_days = str(supplier.get("delivery_days") or product.get("delivery_days") or "6-10 Business Days")
        rejection_feedback = (
            str(state.get("creative_feedback") or creative_cfg.get("rejection_feedback") or "").strip()
        )
        lang_names = {"en": "Global US/EU English", "az": "Azərbaycan dili", "de": "Deutsch (German)", "fr": "Français (French)"}

        director_broll_prompts: List[str] = []
        director_report: Dict[str, Any] = {}
        try:
            brain = CreativeDirectorBrain(
                client=self.client,
                model=self.model,
                call_gemini_fn=self._call_gemini,
            )
            directed_plan, director_report = await brain.direct(
                image_urls=gallery_urls,
                product=product,
                product_name=product_name_display,
                selling_price=selling_price,
                delivery_days=delivery_days,
                avatar_preset=avatar_preset,
                variant_count=variant_count,
                ad_language=ad_language,
                rejection_feedback=rejection_feedback,
                custom_script_text=custom_script_text,
            )
            script_plan = HybridCreativeScriptPlan(
                hook_variants=[
                    HookVariant(
                        hook_type=h.hook_type,
                        hook_text=h.hook_text,
                        avatar_gesture_prompt=h.avatar_gesture_prompt,
                    )
                    for h in directed_plan.hook_variants
                ],
                shared_body_script=directed_plan.shared_body_script,
                shared_cta_text=directed_plan.shared_cta_text,
                visual_instructions=directed_plan.visual_instructions,
            )
            director_broll_prompts = list(directed_plan.broll_shot_prompts or [])
            crit_score = (director_report.get("critique") or {}).get("total_score")
            if crit_score is not None:
                progress_logs.append(
                    f"🧠 AI Creative Director Beyni: Vision={'ON' if director_report.get('vision_used') else 'Text'} | "
                    f"Self-Critic={crit_score}/100{' (Auto-Refined)' if director_report.get('refined') else ''}"
                )
        except Exception as brain_err:
            logger.warning(f"🧠 CreativeDirectorBrain fallback to single-pass script plan: {brain_err}")
            if custom_script_text:
                parts = [p.strip() for p in re.split(r"[.!?]+", custom_script_text) if p.strip()]
                base_hook = parts[0] if parts else f"Wait until you see what this {product_name_display} can do"
                base_cta = parts[-1] if len(parts) > 2 else f"Grab fifty percent off today for ${selling_price:.2f} with a thirty day guarantee"
                base_body = ". ".join(parts[1:-1]) if len(parts) > 2 else custom_script_text
                script_plan = HybridCreativeScriptPlan(
                    hook_variants=[HookVariant(hook_type="pain_point", hook_text=base_hook)],
                    shared_body_script=base_body,
                    shared_cta_text=base_cta,
                )
            else:
                prompt = (
                    f"Create a high-converting Hybrid AI UGC script plan in **{lang_names[ad_language]}**.\n"
                    f"PRODUCT: {product_name_display}\n"
                    f"PRODUCT CONTEXT: {json.dumps(product, ensure_ascii=False)}\n"
                    f"OFFER DETAILS: Price ${selling_price:.2f} (50% OFF launch offer), Tracked Shipping ({delivery_days}), 30-Day Money-Back Guarantee\n"
                    f"CREATOR PERSONA: {avatar_preset['name']} ({avatar_preset['style']})\n"
                    f"{f'USER FEEDBACK TO APPLY: {rejection_feedback}' if rejection_feedback else ''}\n\n"
                    f"OUTPUT EXACTLY {variant_count} distinct hook_variants (covering: pain_point, curiosity, social_proof) "
                    f"that ALL seamlessly flow into 1 shared_body_script (22-30 words) and 1 shared_cta_text (9-13 words).\n"
                    f"CRITICAL SPEECH RULES:\n"
                    f"- Each hook_text MUST be 10-13 spoken words (~4.0-4.5s) directly addressing the viewer on camera.\n"
                    f"- Spell out numbers naturally in {lang_names[ad_language]} where helpful, and do NOT use emojis or hashtags."
                )
                response_text = await self._call_gemini(
                    prompt=prompt,
                    system_instruction=self.system_instruction,
                    response_schema=HybridCreativeScriptPlan.model_json_schema(),
                )
                script_plan = HybridCreativeScriptPlan(**json.loads(response_text))

        hooks = await self._ensure_unique_hooks(
            script_plan.hook_variants,
            variant_count=variant_count,
            product_name=product_name_display,
            ad_language=ad_language,
        )

        # 4. Generate SHARED Assets ONCE: first TTS body/CTA + Playwright cards so we know exact Scene 2 duration for B-Roll
        logger.info(
            f"Generating shared creative assets [{quality_tier} | {ad_language} | {variant_count} variants | est ${est_info['estimated_cost_usd']:.2f} | cap ${est_info.get('retry_fal_budget_cap_usd', est_info['estimated_fal_usd']):.2f}]..."
        )
        shared_body_task = self._tts_with_retry(
            text=script_plan.shared_body_script,
            avatar_id=avatar_id,
            output_filename=f"shared_body_{run_uid}.mp3",
            language=ad_language,
            quality_tier=quality_tier,
            cache_dir=cache_dir,
            cache_key="shared_body",
        )
        shared_cta_task = self._tts_with_retry(
            text=script_plan.shared_cta_text,
            avatar_id=avatar_id,
            output_filename=f"shared_cta_{run_uid}.mp3",
            language=ad_language,
            quality_tier=quality_tier,
            cache_dir=cache_dir,
            cache_key="shared_cta",
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
        shared_body_seg, shared_cta_seg, cards_info = await asyncio.gather(
            shared_body_task,
            shared_cta_task,
            cards_task,
        )
        target_scene2_dur = float(shared_body_seg.get("duration") or 0.0)

        # Shared Scene 2 B-Roll (Real supplier video $0 OR 3 parallel cached AI I2V clips driven by Director Brain prompts)
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
                    target_scene2_dur=target_scene2_dur,
                    cache_dir=cache_dir,
                    progress_cb=lambda msg: progress_logs.append(msg),
                    custom_broll_prompts=director_broll_prompts or None,
                ),
            )
            tier_label = QUALITY_TIERS[quality_tier]["broll_model"].split("/")[-1]
            gen_cnt = int(scene_res.get("generated_count") or 0)
            src_note = f"+{gen_cnt} AI Scene" if gen_cnt > 0 else f"{len(scene_img_paths)} Qalereya Şəkli"
            return (
                broll_details["clip_paths"],
                f"Fal.ai {len(broll_details['clip_paths'])}-Klip AI B-Roll ({tier_label} | {src_note})",
                broll_details["clips_meta"],
                float(scene_res.get("cost_usd") or 0.0),
                gen_cnt,
            )

        shared_broll_clips, broll_label, broll_clips_meta, scene_gen_usd, scene_gen_cnt = await _prepare_shared_broll()

        # Persist run manifest so any failed variant can be retried later at $0 shared cost
        manifest_data = {
            "run_uid": run_uid,
            "product_name_display": product_name_display,
            "ad_language": ad_language,
            "quality_tier": quality_tier,
            "avatar_id": avatar_id,
            "avatar_name": avatar_preset["name"],
            "avatar_image_url": avatar_image_url,
            "primary_product_img": primary_product_img,
            "gallery_urls": gallery_urls,
            "shared_body_script": script_plan.shared_body_script,
            "shared_cta_text": script_plan.shared_cta_text,
            "visual_instructions": script_plan.visual_instructions,
            "hooks": [h.model_dump() for h in hooks],
            "shared_body_seg": shared_body_seg,
            "shared_cta_seg": shared_cta_seg,
            "cards_info": cards_info,
            "shared_broll_clips": shared_broll_clips,
            "broll_label": broll_label,
            "broll_clips_meta": broll_clips_meta,
            "scene_gen_usd": scene_gen_usd,
            "est_info": est_info,
            "director_report": director_report,
        }
        try:
            (cache_dir / "manifest.json").write_text(
                json.dumps(manifest_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as m_err:
            logger.debug(f"Cache manifest write note: {m_err}")

        # 5. Build each Hook Variant independently (return_exceptions=True so 1 failure NEVER wastes other variants or B-Roll)
        per_variant_est = round(est_info["estimated_cost_usd"] / max(len(hooks), 1), 2)
        shared_broll_and_scene_usd = round(
            sum(float(c.get("cost_usd") or 0.0) for c in broll_clips_meta) + scene_gen_usd, 3
        )
        el_rate_1k = ELEVENLABS_RATE_PER_1K_CHARS.get(QUALITY_TIERS[quality_tier]["elevenlabs_model"], 0.18)
        shared_el_chars = len(script_plan.shared_body_script) + len(script_plan.shared_cta_text)

        async def _build_variant(idx: int, hv: HookVariant) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
            try:
                hook_seg = await self._tts_with_retry(
                    text=hv.hook_text,
                    avatar_id=avatar_id,
                    output_filename=f"hook_{idx+1}_{run_uid}.mp3",
                    language=ad_language,
                    quality_tier=quality_tier,
                    cache_dir=cache_dir,
                    cache_key=f"hook_tts_v{idx+1}",
                )

                lipsync_dest = AD_VIDEO_DIR / f"hook_lipsync_{idx+1}_{run_uid}.mp4"
                hook_details = await loop.run_in_executor(
                    None,
                    lambda: generate_hook_lipsync_video(
                        avatar_image_url=avatar_image_url,
                        hook_audio_path=hook_seg["file_path"],
                        dest_video_path=lipsync_dest,
                        quality_tier=quality_tier,
                        return_details=True,
                        cache_dir=cache_dir,
                        cache_key=f"lipsync_v{idx+1}",
                        max_fal_budget_usd=per_variant_lipsync_cap,
                        progress_cb=lambda msg: progress_logs.append(f"Variant #{idx+1}: {msg}"),
                        avatar_direction_prompt=hv.avatar_gesture_prompt,
                    ),
                )
                hook_video_path = hook_details["file_path"]

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

                variant_el_chars = len(hv.hook_text) + (shared_el_chars / max(len(hooks), 1))
                variant_el_usd = (variant_el_chars / 1000.0) * el_rate_1k
                shared_share_usd = round(shared_broll_and_scene_usd / max(len(hooks), 1), 2)
                var_actual_usd = round(
                    float(hook_details.get("cost_usd") or 0.0) + shared_share_usd + variant_el_usd,
                    2,
                )

                script_obj = AdScript(
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
                    shared_cost_usd=shared_share_usd,
                    status="completed",
                    error=None,
                    retryable=False,
                    variant_index=idx,
                    run_uid=run_uid,
                    qa_gate=render_res.get("qa_gate") or {},
                    video_url=render_res.get("web_url"),
                    audio_url=merged_audio.get("web_url"),
                ).model_dump()
                return script_obj, hook_details
            except CreativeBalanceError:
                raise
            except Exception as var_exc:
                logger.error(f"❌ Variant #{idx+1} ({hv.hook_type}) uğursuz oldu: {var_exc}")
                failed_obj = AdScript(
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
                    actual_cost_usd=0.0,
                    shared_cost_usd=0.0,
                    status="failed",
                    error=str(var_exc)[:260],
                    retryable=True,
                    variant_index=idx,
                    run_uid=run_uid,
                    qa_gate={"passed": False, "issues": [str(var_exc)[:180]]},
                    video_url=None,
                    audio_url=None,
                ).model_dump()
                return failed_obj, None

        raw_variant_results = await asyncio.gather(
            *[_build_variant(i, hooks[i]) for i in range(len(hooks))],
            return_exceptions=True,
        )

        processed_scripts: List[Dict[str, Any]] = []
        lipsync_records: List[Dict[str, Any]] = []
        for idx, r in enumerate(raw_variant_results):
            if isinstance(r, CreativeBalanceError):
                raise r
            if isinstance(r, Exception):
                hv = hooks[idx]
                processed_scripts.append(
                    AdScript(
                        hook_type=hv.hook_type,
                        hook_text=hv.hook_text,
                        body_script=script_plan.shared_body_script,
                        cta_text=script_plan.shared_cta_text,
                        language=ad_language,
                        quality_tier=quality_tier,
                        avatar_id=avatar_id,
                        avatar_name=avatar_preset["name"],
                        broll_source=broll_label,
                        status="failed",
                        error=str(r)[:260],
                        retryable=True,
                        variant_index=idx,
                        run_uid=run_uid,
                        qa_gate={"passed": False, "issues": [str(r)[:180]]},
                    ).model_dump()
                )
            else:
                script_obj, hook_det = r
                processed_scripts.append(script_obj)
                if hook_det:
                    lipsync_records.append(hook_det)

        succeeded_scripts = [s for s in processed_scripts if s.get("status") == "completed" and s.get("video_url")]
        if not succeeded_scripts:
            first_err = next((s.get("error") for s in processed_scripts if s.get("error")), "Naməlum render xətası")
            raise RuntimeError(f"Bütün reklam variantları uğursuz oldu: {first_err}")

        succeeded_chars = sum(len(s.get("hook_text") or "") for s in succeeded_scripts)
        total_el_chars = shared_el_chars + succeeded_chars
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

        qa_all_pass = len(succeeded_scripts) == len(processed_scripts) and all(
            (s.get("qa_gate") or {}).get("passed") is True for s in succeeded_scripts
        )
        qa_summary_tag = "PASS" if qa_all_pass else f"PARTIAL ({len(succeeded_scripts)}/{len(processed_scripts)})"

        brain_score_val = (director_report.get("critique") or {}).get("total_score")
        brain_tag = f" | AI Beyin: {brain_score_val}/100" if brain_score_val is not None else ""

        return {
            "creatives": processed_scripts,
            "creative_feedback": None,
            "creative_assets": {
                "run_uid": run_uid,
                "ad_scripts": processed_scripts,
                "static_ads": static_ads,
                "cta_endcard_url": cards_info["cta_endcard_url"],
                "preflight_estimate": est_info,
                "actual_cost_summary": actual_summary,
                "scene_gen_count": scene_gen_cnt,
                "language": ad_language,
                "quality_tier": quality_tier,
                "director_report": director_report,
            },
            "logs": [
                *progress_logs[-6:],
                f"✅ 4-cü Agent ({QUALITY_TIERS[quality_tier]['label']} | {ad_language.upper()}): '{product_name_display}' üçün "
                f"{len(succeeded_scripts)}/{len(processed_scripts)} video + 2 statik reklam hazırlandı | "
                f"Təxmini: ${est_info['estimated_cost_usd']:.2f} → Faktiki: ${actual_summary['actual_cost_usd']:.2f} (Fal: ${actual_summary['actual_fal_usd']:.2f}){brain_tag} | QA Gate: {qa_summary_tag}.",
            ],
        }


async def retry_cached_creative_variant(run_uid: str, variant_index: int) -> Dict[str, Any]:
    """
    Re-build ONLY a single failed variant using the run's cached shared assets (B-Roll, Body/CTA audio, CTA card)
    at $0 extra shared Fal cost, respecting the +25% per-variant retry budget cap.
    """
    cache_dir = AD_VIDEO_DIR / "cache" / run_uid
    manifest_p = cache_dir / "manifest.json"
    if not manifest_p.exists():
        raise FileNotFoundError(f"Run cache manifest tapılmadı: {run_uid}")
    m = json.loads(manifest_p.read_text(encoding="utf-8"))
    hooks = m.get("hooks") or []
    if variant_index < 0 or variant_index >= len(hooks):
        raise IndexError(f"Yanlış variant indeksi: {variant_index}")

    hv = HookVariant(**hooks[variant_index])
    ad_language = m["ad_language"]
    quality_tier = normalize_quality_tier(m["quality_tier"])
    avatar_id = m["avatar_id"]
    avatar_image_url = m["avatar_image_url"]
    est_info = m.get("est_info") or {}
    per_variant_lipsync_cap = float(est_info.get("per_variant_lipsync_cap_usd") or 0.5)
    loop = asyncio.get_running_loop()

    hook_seg = await CreativeAgent._tts_with_retry(
        text=hv.hook_text,
        avatar_id=avatar_id,
        output_filename=f"hook_{variant_index+1}_{run_uid}.mp3",
        language=ad_language,
        quality_tier=quality_tier,
        cache_dir=cache_dir,
        cache_key=f"hook_tts_v{variant_index+1}",
    )

    lipsync_dest = AD_VIDEO_DIR / f"hook_lipsync_{variant_index+1}_{run_uid}.mp4"
    hook_details = await loop.run_in_executor(
        None,
        lambda: generate_hook_lipsync_video(
            avatar_image_url=avatar_image_url,
            hook_audio_path=hook_seg["file_path"],
            dest_video_path=lipsync_dest,
            quality_tier=quality_tier,
            return_details=True,
            cache_dir=cache_dir,
            cache_key=f"lipsync_v{variant_index+1}",
            max_fal_budget_usd=per_variant_lipsync_cap,
            avatar_direction_prompt=hv.avatar_gesture_prompt,
        ),
    )

    merged_audio = await loop.run_in_executor(
        None,
        lambda: concat_audio_segments_with_words(
            [hook_seg, m["shared_body_seg"], m["shared_cta_seg"]],
            output_filename=f"ad_voice_master_{variant_index+1}_{run_uid}.mp3",
        ),
    )

    unique_video_filename = f"rendered_ugc_ad_{variant_index+1}_{run_uid}.mp4"
    full_text = f"{hv.hook_text}. {m['shared_body_script']}. {m['shared_cta_text']}"
    render_raw = await loop.run_in_executor(
        None,
        lambda: render_ugc_commercial_ad(
            product_image_url=m["primary_product_img"],
            avatar_image_url=avatar_image_url,
            voiceover_path=merged_audio["file_path"],
            hook_text=hv.hook_text,
            full_script_text=full_text,
            cta_text=m["shared_cta_text"],
            ugc_avatar_video_path=hook_details["file_path"],
            output_filename=unique_video_filename,
            product_name=m["product_name_display"],
            words=merged_audio["words"],
            scene1_dur=merged_audio["scene1_dur"],
            scene2_dur=merged_audio["scene2_dur"],
            scene3_dur=merged_audio["scene3_dur"],
            broll_clip_paths=m["shared_broll_clips"],
            product_gallery_urls=m["gallery_urls"],
            cta_png_path=m["cards_info"]["cta_endcard_path"],
            language=ad_language,
        ),
    )
    render_res = json.loads(render_raw)
    el_rate_1k = ELEVENLABS_RATE_PER_1K_CHARS.get(QUALITY_TIERS[quality_tier]["elevenlabs_model"], 0.12)
    var_actual_usd = round(
        float(hook_details.get("cost_usd") or 0.0) + (len(hv.hook_text) / 1000.0) * el_rate_1k,
        2,
    )
    return AdScript(
        hook_type=hv.hook_type,
        hook_text=hv.hook_text,
        body_script=m["shared_body_script"],
        cta_text=m["shared_cta_text"],
        visual_instructions=m.get("visual_instructions") or "",
        language=ad_language,
        quality_tier=quality_tier,
        avatar_id=avatar_id,
        avatar_name=m.get("avatar_name"),
        voice_engine=f"ElevenLabs /with-timestamps ({QUALITY_TIERS[quality_tier]['elevenlabs_model']})",
        broll_source=m.get("broll_label"),
        estimated_cost_usd=round(float(est_info.get("estimated_cost_usd") or 0.0) / max(len(hooks), 1), 2),
        actual_cost_usd=var_actual_usd,
        shared_cost_usd=0.0,
        status="completed",
        error=None,
        retryable=False,
        variant_index=variant_index,
        run_uid=run_uid,
        qa_gate=render_res.get("qa_gate") or {},
        video_url=render_res.get("web_url"),
        audio_url=merged_audio.get("web_url"),
    ).model_dump()


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
