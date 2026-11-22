import concurrent.futures
import json
import base64
import logging
import mimetypes
import os
import re
import subprocess
import time
import urllib.parse
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import imageio_ffmpeg
import requests
from PIL import Image, ImageFilter

from config.settings import get_settings

logger = logging.getLogger(__name__)


class CreativeBalanceError(RuntimeError):
    """Raised on strict Fail-Fast when ElevenLabs or Fal.ai API key/balance/quota is missing or exhausted."""

    def __init__(self, provider: str, message: str, topup_url: str = ""):
        self.provider = provider
        self.topup_url = topup_url
        full_msg = f"[{provider.upper()}] {message}"
        if topup_url:
            full_msg += f" (Balans artırma: {topup_url})"
        super().__init__(full_msg)


class FalQueueTimeoutError(TimeoutError):
    """Timeout error carrying Fal queue request_id/urls so caller can resume without paying twice."""

    def __init__(
        self,
        message: str,
        model_id: str = "",
        request_id: Optional[str] = None,
        status_url: Optional[str] = None,
        response_url: Optional[str] = None,
    ):
        super().__init__(message)
        self.model_id = model_id
        self.request_id = request_id
        self.status_url = status_url
        self.response_url = response_url


# Official Fal.ai per-second video billing rates (verified 2026) & per-image scene edit cost
FAL_MODEL_RATES_PER_SEC: Dict[str, float] = {
    "fal-ai/kling-video/ai-avatar/v2/standard": 0.0562,
    "fal-ai/kling-video/ai-avatar/v2/pro": 0.115,
    "fal-ai/kling-video/v1/pro/ai-avatar": 0.115,
    "bytedance/omnihuman/v2": 0.105,
    "fal-ai/bytedance/omnihuman/v2": 0.105,
    "fal-ai/bytedance/omnihuman": 0.140,
    "fal-ai/bytedance/omnihuman/v1.5:720p": 0.145,
    "fal-ai/bytedance/omnihuman/v1.5:1080p": 0.210,
    "fal-ai/bytedance/omnihuman/v1.5": 0.145,
    "fal-ai/bytedance/seedance/v1/lite/image-to-video:480p": 0.014,
    "fal-ai/bytedance/seedance/v1/lite/image-to-video:720p": 0.030,
    "fal-ai/bytedance/seedance/v1/lite/image-to-video:1080p": 0.068,
    "fal-ai/bytedance/seedance/v1.5/pro/image-to-video:720p": 0.052,
    "fal-ai/bytedance/seedance/v1.5/pro/image-to-video": 0.052,
    "bytedance/seedance-2.0/fast/image-to-video:720p": 0.2419,
    "bytedance/seedance-2.0/fast/image-to-video": 0.2419,
    "bytedance/seedance-2.0/image-to-video:720p": 0.3034,
    "bytedance/seedance-2.0/image-to-video:1080p": 0.6820,
    "bytedance/seedance-2.0/image-to-video": 0.3034,
    "bytedance/seedance-2.5/us/image-to-video:480p": 0.2205,
    "bytedance/seedance-2.5/us/image-to-video:720p": 0.4730,
    "bytedance/seedance-2.5/us/image-to-video": 0.4730,
    "fal-ai/minimax/hailuo-02/standard/image-to-video:512p": 0.017,
    "fal-ai/minimax/hailuo-02/standard/image-to-video:768p": 0.045,
    "fal-ai/kling-video/v1.6/standard/image-to-video": 0.045,
    "fal-ai/kling-video/v2.1/pro/image-to-video": 0.120,
    "fal-ai/wan-25-preview/image-to-video:480p": 0.050,
    "fal-ai/wan-25-preview/image-to-video:720p": 0.100,
    "fal-ai/wan-25-preview/image-to-video:1080p": 0.150,
    "fal-ai/wan/v2.7/image-to-video:720p": 0.100,
    "fal-ai/wan/v2.7/image-to-video:1080p": 0.150,
}

SCENE_GEN_COST_PER_IMAGE_USD: float = 0.039  # fal-ai/nano-banana/edit (~$0.039/img)
AI_AVATAR_GEN_COST_USD: float = 0.04        # fal-ai/flux-pro/v1.1 or flux/schnell portrait
RETRY_FAL_BUDGET_CAP_MULTIPLIER: float = 1.25  # Max +25% extra Fal budget on retries


def is_known_fal_rate(model_id: str, resolution: Optional[str] = None) -> bool:
    mid = (model_id or "").strip().lower()
    res_tag = (resolution or "").strip().lower()
    return (bool(res_tag) and f"{mid}:{res_tag}" in FAL_MODEL_RATES_PER_SEC) or (mid in FAL_MODEL_RATES_PER_SEC)


def get_fal_rate_per_sec(model_id: str, resolution: Optional[str] = None) -> float:
    mid = (model_id or "").strip().lower()
    res_tag = (resolution or "").strip().lower()
    if res_tag and f"{mid}:{res_tag}" in FAL_MODEL_RATES_PER_SEC:
        return FAL_MODEL_RATES_PER_SEC[f"{mid}:{res_tag}"]
    if mid in FAL_MODEL_RATES_PER_SEC:
        return FAL_MODEL_RATES_PER_SEC[mid]
    logger.warning(
        f"⚠️ Naməlum Fal.ai model tarifi ('{model_id}', res='{resolution}') — default $0.05/s təxmini istifadə olunur."
    )
    return 0.05


QUALITY_TIERS: Dict[str, Dict[str, Any]] = {
    "economy": {
        "id": "economy",
        "label": "Economy (EL v3 + Kling Avatar v2 Std + Seedance Lite)",
        "elevenlabs_model": "eleven_v3",
        "lipsync_model": "fal-ai/kling-video/ai-avatar/v2/standard",
        "lipsync_fallback_model": "bytedance/omnihuman/v2",
        "lipsync_resolution": "720p",
        "lipsync_target_sec": 3.5,
        "broll_model": "fal-ai/bytedance/seedance/v1/lite/image-to-video",
        "broll_fallback_model": "fal-ai/minimax/hailuo-02/standard/image-to-video",
        "broll_resolution": "480p",
        "broll_clip_dur_sec": 4,
        "broll_clip_count": 3,
        "elevenlabs_base_usd": 0.025,
        "elevenlabs_per_hook_usd": 0.012,
    },
    "balanced": {
        "id": "balanced",
        "label": "Balanced Hybrid (EL v4 Turbo + OmniHuman v2 + Seedance v1.5 Pro)",
        "elevenlabs_model": "eleven_v4_turbo",
        "lipsync_model": "bytedance/omnihuman/v2",
        "lipsync_fallback_model": "fal-ai/kling-video/ai-avatar/v2/standard",
        "lipsync_resolution": "720p",
        "lipsync_target_sec": 4.1,
        "broll_model": "fal-ai/bytedance/seedance/v1.5/pro/image-to-video",
        "broll_fallback_model": "fal-ai/kling-video/v1.6/standard/image-to-video",
        "broll_resolution": "720p",
        "broll_clip_dur_sec": 5,
        "broll_clip_count": 3,
        "elevenlabs_base_usd": 0.025,
        "elevenlabs_per_hook_usd": 0.012,
    },
    "cinema": {
        "id": "cinema",
        "label": "Cinema Pro (EL v4 + OmniHuman v2 + Seedance 2.0 Fast)",
        "elevenlabs_model": "eleven_v4",
        "lipsync_model": "bytedance/omnihuman/v2",
        "lipsync_fallback_model": "fal-ai/kling-video/ai-avatar/v2/pro",
        "lipsync_resolution": "720p",
        "lipsync_target_sec": 4.2,
        "broll_model": "bytedance/seedance-2.0/fast/image-to-video",
        "broll_fallback_model": "bytedance/seedance-2.0/image-to-video",
        "broll_secondary_fallback_model": "fal-ai/bytedance/seedance/v1.5/pro/image-to-video",
        "broll_resolution": "720p",
        "broll_clip_dur_sec": 5,
        "broll_clip_count": 3,
        "elevenlabs_base_usd": 0.035,
        "elevenlabs_per_hook_usd": 0.018,
    },
}


def normalize_quality_tier(tier: Optional[str]) -> str:
    t = (tier or "balanced").strip().lower()
    return t if t in QUALITY_TIERS else "balanced"


def get_elevenlabs_api_key() -> str:
    settings = get_settings()
    key = (
        settings.elevenlabs_api_key
        or settings.creative.elevenlabs_api_key
        or os.environ.get("ELEVENLABS_API_KEY")
        or ""
    ).strip()
    return key


def get_fal_api_keys() -> List[str]:
    settings = get_settings()
    raw = (
        settings.fal_api_key
        or settings.creative.fal_api_key
        or os.environ.get("FAL_KEY")
        or ""
    ).strip()
    if len(raw) < 10:
        return []
    keys = [raw]
    if raw.startswith("fal_sk_"):
        keys.append(raw[len("fal_sk_"):])
    return keys


def estimate_creative_cost(
    quality_tier: str = "balanced",
    variant_count: int = 3,
    has_supplier_video: bool = False,
    broll_mode: str = "auto",
    image_count: int = 3,
) -> Dict[str, Any]:
    """
    Compute transparent, official per-second unit-cost breakdown before running paid AI generation:
      - Lip-Sync Hook: variant_count x hook_target_sec x Fal $/sec
      - Shared 3-Clip AI B-Roll: 3 x clip_dur_sec x Fal $/sec (for locked resolution & 9:16 framing)
      - AI Scene Variation (if image_count < 3 and AI B-Roll is active): (3 - image_count) x $0.039
      - ElevenLabs /with-timestamps + $0 Playwright CTA End-Card & 2 Static PNG Ads
    """
    tier_id = normalize_quality_tier(quality_tier)
    cfg = QUALITY_TIERS[tier_id]
    n_vars = 3 if int(variant_count or 3) >= 3 else 1
    need_ai_broll = (not has_supplier_video) and (broll_mode not in ("studio_only", "1688_clean"))

    lipsync_rate = get_fal_rate_per_sec(cfg["lipsync_model"], cfg.get("lipsync_resolution"))
    hook_sec = float(cfg["lipsync_target_sec"])
    lipsync_cost = round(n_vars * hook_sec * lipsync_rate, 3)

    broll_rate = get_fal_rate_per_sec(cfg["broll_model"], cfg.get("broll_resolution"))
    broll_clips = int(cfg["broll_clip_count"])
    broll_dur = int(cfg["broll_clip_dur_sec"])
    broll_cost = round(broll_clips * broll_dur * broll_rate, 3) if need_ai_broll else 0.0

    known_imgs = max(int(image_count if image_count is not None else 3), 0)
    missing_scenes = max(0, broll_clips - known_imgs) if need_ai_broll else 0
    scene_gen_cost = round(missing_scenes * SCENE_GEN_COST_PER_IMAGE_USD, 3)

    voice_cost = round(cfg["elevenlabs_base_usd"] + n_vars * cfg["elevenlabs_per_hook_usd"], 3)
    cards_cost = 0.0
    fal_total = round(lipsync_cost + broll_cost + scene_gen_cost, 2)
    total = round(voice_cost + lipsync_cost + broll_cost + scene_gen_cost + cards_cost, 2)

    retry_buffer_usd = round(fal_total * 0.10, 2)
    retry_fal_budget_cap_usd = round(fal_total * RETRY_FAL_BUDGET_CAP_MULTIPLIER, 2)
    per_variant_lipsync_cap_usd = round((hook_sec * lipsync_rate) * RETRY_FAL_BUDGET_CAP_MULTIPLIER, 3)
    rate_estimated = (
        not is_known_fal_rate(cfg["lipsync_model"], cfg.get("lipsync_resolution"))
        or (need_ai_broll and not is_known_fal_rate(cfg["broll_model"], cfg.get("broll_resolution")))
    )

    return {
        "quality_tier": tier_id,
        "tier_label": cfg["label"],
        "variant_count": n_vars,
        "broll_clip_count": broll_clips if need_ai_broll else 0,
        "broll_resolution": cfg["broll_resolution"],
        "scene_gen_count": missing_scenes,
        "estimated_fal_usd": fal_total,
        "estimated_cost_usd": total,
        "retry_buffer_usd": retry_buffer_usd,
        "retry_fal_budget_cap_usd": retry_fal_budget_cap_usd,
        "per_variant_lipsync_cap_usd": per_variant_lipsync_cap_usd,
        "rate_estimated": rate_estimated,
        "breakdown": {
            "elevenlabs_voice_timestamps_usd": round(voice_cost, 2),
            "fal_lipsync_hooks_usd": round(lipsync_cost, 2),
            "fal_shared_ai_broll_usd": round(broll_cost, 2),
            "fal_scene_gen_usd": round(scene_gen_cost, 2),
            "playwright_cta_and_static_ads_usd": cards_cost,
        },
        "rate_details": {
            "lipsync_rate_per_sec": lipsync_rate,
            "lipsync_est_sec": round(n_vars * hook_sec, 1),
            "broll_rate_per_sec": broll_rate if need_ai_broll else 0.0,
            "broll_est_sec": (broll_clips * broll_dur) if need_ai_broll else 0,
            "scene_gen_rate_per_img": SCENE_GEN_COST_PER_IMAGE_USD if missing_scenes else 0.0,
        },
        "models": {
            "elevenlabs": cfg["elevenlabs_model"],
            "lipsync": cfg["lipsync_model"],
            "broll": cfg["broll_model"] if need_ai_broll else ("supplier_video" if has_supplier_video else "studio_motion"),
            "scene_gen": "fal-ai/nano-banana/edit" if missing_scenes > 0 else "none",
        },
    }


def _verify_scoped_elevenlabs_tts(api_key: str) -> Tuple[bool, str]:
    """Tiny ~8-char TTS probe to confirm a scoped ElevenLabs key has active v3/v4 quota BEFORE Fal spend."""
    last_err = ""
    for probe_model in ("eleven_v3", "eleven_v4_turbo"):
        try:
            body: Dict[str, Any] = {"text": "Test ok.", "model_id": probe_model}
            if probe_model != "eleven_v3":
                body["voice_settings"] = {"stability": 0.5, "similarity_boost": 0.75}
            probe_resp = requests.post(
                "https://api.elevenlabs.io/v1/text-to-speech/EXAVITQu4vr4xnSDxMaL",
                headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
                json=body,
                timeout=15,
            )
            if probe_resp.status_code == 200 and len(probe_resp.content) > 200:
                return True, ""
            if probe_resp.status_code in (401, 402, 403, 429):
                return False, f"ElevenLabs scoped TTS test uğursuz (HTTP {probe_resp.status_code}): {probe_resp.text[:160]}"
            last_err = f"ElevenLabs TTS yoxlama xətası ({probe_model}, HTTP {probe_resp.status_code}): {probe_resp.text[:140]}"
        except Exception as exc:
            last_err = f"ElevenLabs scoped TTS bağlantı xətası: {exc}"
    return False, last_err


def check_elevenlabs_balance(required_chars: int = 750, verify_scoped_tts: bool = False) -> Dict[str, Any]:
    """Verify ElevenLabs API key validity and remaining character quota."""
    key = get_elevenlabs_api_key()
    if len(key) < 10:
        return {
            "ok": False,
            "configured": False,
            "remaining_chars": 0,
            "error": "ELEVENLABS_API_KEY .env faylında təyin edilməyib.",
            "topup_url": "https://elevenlabs.io/app/subscription",
        }
    try:
        resp = requests.get(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": key},
            timeout=10,
        )
        if resp.status_code in (401, 403):
            err_body = resp.text or ""
            if "missing_permissions" in err_body and "user_read" in err_body:
                scoped_verified = False
                if verify_scoped_tts:
                    tts_ok, tts_err = _verify_scoped_elevenlabs_tts(key)
                    if not tts_ok:
                        return {
                            "ok": False,
                            "configured": True,
                            "tier": "scoped_tts_key",
                            "remaining_chars": 0,
                            "required_chars": required_chars,
                            "error": tts_err,
                            "topup_url": "https://elevenlabs.io/app/subscription",
                        }
                    scoped_verified = True
                return {
                    "ok": True,
                    "configured": True,
                    "tier": "scoped_tts_key",
                    "scoped_verified": scoped_verified,
                    "character_limit": None,
                    "character_used": None,
                    "remaining_chars": None,
                    "required_chars": required_chars,
                    "error": "",
                    "note": (
                        "Scoped TTS açarı canlı test sintezi ilə təsdiqləndi."
                        if scoped_verified
                        else "Scoped TTS açarı aktivdir (user_read icazəsi olmadan TTS işləyir)."
                    ),
                    "topup_url": "https://elevenlabs.io/app/subscription",
                }
            return {
                "ok": False,
                "configured": True,
                "remaining_chars": 0,
                "error": f"ElevenLabs API açarı rədd edildi (HTTP {resp.status_code}).",
                "topup_url": "https://elevenlabs.io/app/subscription",
            }
        if resp.status_code != 200:
            return {
                "ok": False,
                "configured": True,
                "remaining_chars": 0,
                "error": f"ElevenLabs quota yoxlaması xətası (HTTP {resp.status_code}).",
                "topup_url": "https://elevenlabs.io/app/subscription",
            }
        data = resp.json()
        limit = int(data.get("character_limit") or 0)
        used = int(data.get("character_count") or 0)
        remaining = max(0, limit - used)
        tier = str(data.get("tier") or "unknown")
        ok = remaining >= required_chars
        return {
            "ok": ok,
            "configured": True,
            "tier": tier,
            "character_limit": limit,
            "character_used": used,
            "remaining_chars": remaining,
            "required_chars": required_chars,
            "error": "" if ok else f"ElevenLabs simvol balansı çatmır ({remaining} qalıb, tələb olunan: {required_chars}).",
            "topup_url": "https://elevenlabs.io/app/subscription",
        }
    except Exception as exc:
        return {
            "ok": False,
            "configured": True,
            "remaining_chars": 0,
            "error": f"ElevenLabs bağlantı xətası: {exc}",
            "topup_url": "https://elevenlabs.io/app/subscription",
        }


def check_fal_key_configured() -> Dict[str, Any]:
    keys = get_fal_api_keys()
    if not keys:
        return {
            "ok": False,
            "configured": False,
            "error": "FAL_KEY .env faylında təyin edilməyib.",
            "topup_url": "https://fal.ai/dashboard/billing",
        }
    return {
        "ok": True,
        "configured": True,
        "error": "",
        "topup_url": "https://fal.ai/dashboard/billing",
    }


def check_creative_preflight(
    quality_tier: str = "balanced",
    variant_count: int = 3,
    has_supplier_video: bool = False,
    broll_mode: str = "auto",
    strict: bool = False,
    image_count: int = 3,
) -> Dict[str, Any]:
    """
    Pre-flight gate before spending any rendering time:
      - Checks ElevenLabs quota (and runs tiny TTS probe for scoped keys when strict=True) & Fal.ai key configuration
      - Calculates exact estimated cost breakdown + retry budget cap (+25%) for all 3 tiers + active selection
      - Raises CreativeBalanceError immediately when strict=True and a required check fails
    """
    n_vars = 3 if int(variant_count or 3) >= 3 else 1
    required_chars = 350 + n_vars * 130
    el_status = check_elevenlabs_balance(required_chars=required_chars, verify_scoped_tts=bool(strict))
    fal_status = check_fal_key_configured()
    estimate = estimate_creative_cost(
        quality_tier=quality_tier,
        variant_count=n_vars,
        has_supplier_video=has_supplier_video,
        broll_mode=broll_mode,
        image_count=image_count,
    )
    all_tiers = {
        tid: estimate_creative_cost(
            tid,
            variant_count=n_vars,
            has_supplier_video=has_supplier_video,
            broll_mode=broll_mode,
            image_count=image_count,
        )
        for tid in QUALITY_TIERS
    }

    ok = bool(el_status.get("ok")) and bool(fal_status.get("ok"))
    errors = [e for e in [el_status.get("error"), fal_status.get("error")] if e]

    if strict and not ok:
        if not el_status.get("ok"):
            raise CreativeBalanceError("elevenlabs", el_status.get("error", "Balans yoxdur"), el_status.get("topup_url", ""))
        raise CreativeBalanceError("fal", fal_status.get("error", "FAL_KEY yoxdur"), fal_status.get("topup_url", ""))

    return {
        "ok": ok,
        "errors": errors,
        "elevenlabs": el_status,
        "fal": fal_status,
        "estimate": estimate,
        "all_tiers": all_tiers,
    }


def _file_to_data_uri_or_fal_url(file_path: str, headers: Optional[Dict[str, str]] = None) -> str:
    """
    Uploads local media to Fal.ai temporary storage if possible, falling back to a compact
    data URI (works natively on Fal Queue APIs for <3MB hook clips/images).
    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"Media faylı tapılmadı: {file_path}")
    mime_type, _ = mimetypes.guess_type(str(p))
    if not mime_type:
        mime_type = "audio/mpeg" if p.suffix.lower() == ".mp3" else "image/jpeg"
    raw_bytes = p.read_bytes()

    if headers:
        try:
            init_resp = requests.post(
                "https://rest.alpha.fal.ai/storage/upload/initiate",
                headers=headers,
                json={"file_name": p.name, "content_type": mime_type},
                timeout=12,
            )
            if init_resp.status_code == 200:
                init_json = init_resp.json()
                upload_url = init_json.get("upload_url")
                file_url = init_json.get("file_url")
                if upload_url and file_url:
                    put_resp = requests.put(
                        upload_url,
                        data=raw_bytes,
                        headers={"Content-Type": mime_type},
                        timeout=30,
                    )
                    if put_resp.status_code in (200, 201, 204):
                        return str(file_url)
        except Exception as up_err:
            logger.debug(f"Fal storage upload note (using data URI): {up_err}")

    b64 = base64.b64encode(raw_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{b64}"


def _extract_fal_video_url(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    for key in ("video_url", "url"):
        val = data.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    for nested in ("video", "output", "result"):
        sub = data.get(nested)
        if isinstance(sub, dict):
            u = _extract_fal_video_url(sub)
            if u:
                return u
        elif isinstance(sub, str) and sub.startswith("http"):
            return sub
    return None


def submit_fal_queue(
    model_id: str,
    payload: Dict[str, Any],
    timeout_sec: int = 180,
    existing_request_id: Optional[str] = None,
    existing_status_url: Optional[str] = None,
    existing_response_url: Optional[str] = None,
    on_request_submitted: Optional[Callable[[str, str, str], None]] = None,
) -> Dict[str, Any]:
    """
    Calls Fal.ai Async Queue API (`https://queue.fal.run/{model_id}`), polls status_url
    until COMPLETED, and strictly raises CreativeBalanceError on 401/402/403 balance errors.
    Supports resuming an already-submitted `existing_request_id` without double-paying.
    """
    keys = get_fal_api_keys()
    if not keys:
        raise CreativeBalanceError("fal", "FAL_KEY təyin edilməyib.", "https://fal.ai/dashboard/billing")

    active_headers: Dict[str, str] = {"Authorization": f"Key {keys[0]}", "Content-Type": "application/json"}
    request_id = existing_request_id
    status_url = existing_status_url
    response_url = existing_response_url

    if not request_id:
        submit_url = f"https://queue.fal.run/{model_id.lstrip('/')}"
        resp = None
        for k in keys:
            active_headers = {"Authorization": f"Key {k}", "Content-Type": "application/json"}
            resp = requests.post(submit_url, headers=active_headers, json=payload, timeout=30)
            if resp.status_code != 401:
                break

        if resp is None:
            raise CreativeBalanceError("fal", "Fal.ai sorğusu göndərilə bilmədi.", "https://fal.ai/dashboard/billing")
        if resp.status_code == 401:
            raise CreativeBalanceError("fal", "Fal.ai API açarı etibarsızdır (401).", "https://fal.ai/dashboard/billing")
        if resp.status_code in (402, 403):
            raise CreativeBalanceError(
                "fal",
                f"Fal.ai hesab balansı çatmır və ya bağlıdır (HTTP {resp.status_code}): {resp.text[:180]}",
                "https://fal.ai/dashboard/billing",
            )
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError(f"Fal.ai '{model_id}' xətası (HTTP {resp.status_code}): {resp.text[:250]}")

        submit_data = resp.json()
        # Direct synchronous response (some models still return inline if fast)
        direct_media = _extract_fal_video_url(submit_data) or _extract_fal_image_url(submit_data)
        if direct_media:
            return submit_data

        status_url = submit_data.get("status_url")
        response_url = submit_data.get("response_url")
        request_id = submit_data.get("request_id")

    if not status_url and request_id:
        status_url = f"https://queue.fal.run/{model_id.lstrip('/')}/requests/{request_id}/status"
    if not response_url and request_id:
        response_url = f"https://queue.fal.run/{model_id.lstrip('/')}/requests/{request_id}"

    if request_id and status_url and response_url and on_request_submitted:
        try:
            on_request_submitted(str(request_id), str(status_url), str(response_url))
        except Exception:
            pass

    deadline = time.monotonic() + timeout_sec
    transient_poll_errors = 0
    while time.monotonic() < deadline:
        try:
            st_resp = requests.get(status_url, headers=active_headers, timeout=20)
        except Exception as poll_exc:
            transient_poll_errors += 1
            if transient_poll_errors >= 5:
                raise FalQueueTimeoutError(
                    f"Fal.ai Queue '{model_id}' polling şəbəkə xətası: {poll_exc}",
                    model_id=model_id,
                    request_id=request_id,
                    status_url=status_url,
                    response_url=response_url,
                )
            time.sleep(3.0)
            continue

        if st_resp.status_code in (401, 402, 403):
            raise CreativeBalanceError(
                "fal",
                f"Fal.ai balans/icazə xətası (HTTP {st_resp.status_code}): {st_resp.text[:160]}",
                "https://fal.ai/dashboard/billing",
            )
        if st_resp.status_code in (500, 502, 503, 504):
            transient_poll_errors += 1
            time.sleep(3.0)
            continue
        if st_resp.status_code in (200, 202):
            transient_poll_errors = 0
            st_json = st_resp.json()
            st = str(st_json.get("status") or "").upper()
            if st == "COMPLETED":
                res_resp = requests.get(response_url, headers=active_headers, timeout=30)
                if res_resp.status_code in (401, 402, 403):
                    raise CreativeBalanceError(
                        "fal",
                        f"Fal.ai balans xətası (HTTP {res_resp.status_code}): {res_resp.text[:160]}",
                        "https://fal.ai/dashboard/billing",
                    )
                res_resp.raise_for_status()
                data = res_resp.json()
                if isinstance(data, dict) and request_id:
                    data.setdefault("_fal_request_id", request_id)
                return data
            if st in ("FAILED", "ERROR", "CANCELLED"):
                err_txt = str(st_json.get("error") or st_json.get("logs") or st_json)[:240]
                if "balance" in err_txt.lower() or "credit" in err_txt.lower() or "top_up" in err_txt.lower():
                    raise CreativeBalanceError("fal", f"Fal.ai balans xətası: {err_txt}", "https://fal.ai/dashboard/billing")
                raise RuntimeError(f"Fal.ai '{model_id}' task uğursuz oldu: {err_txt}")
        time.sleep(2.5)

    raise FalQueueTimeoutError(
        f"Fal.ai Queue '{model_id}' {timeout_sec}s ərzində tamamlanmadı.",
        model_id=model_id,
        request_id=request_id,
        status_url=status_url,
        response_url=response_url,
    )


def _download_mp4_binary(video_url: str, dest_path: Path) -> Path:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(video_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 512):
                if chunk:
                    f.write(chunk)
    if not dest_path.exists() or dest_path.stat().st_size < 10000:
        raise RuntimeError(f"Fal.ai endirilmiş video faylı boşdur: {dest_path}")
    return dest_path


def _resolve_avatar_url(avatar_image_url: str, auth_headers: Dict[str, str]) -> str:
    if avatar_image_url and avatar_image_url.startswith("http"):
        return avatar_image_url
    if avatar_image_url and Path(avatar_image_url).exists():
        return _file_to_data_uri_or_fal_url(avatar_image_url, headers=auth_headers)
    raise ValueError("Etibarlı UGC Avatar şəkli tapılmadı.")


def _probe_media_duration(file_path: Path) -> float:
    """Measure exact duration in seconds of a local MP4/MP3 via ffmpeg stderr."""
    try:
        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
        res = subprocess.run(
            [ffmpeg_bin, "-i", str(file_path)],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=15,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", res.stderr or "")
        if m:
            hrs, mins, secs = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return round(hrs * 3600 + mins * 60 + secs, 3)
    except Exception:
        pass
    return 0.0


def _extract_fal_image_url(data: Any) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    for key in ("images", "output", "outputs"):
        arr = data.get(key)
        if isinstance(arr, list) and arr:
            first = arr[0]
            if isinstance(first, dict) and isinstance(first.get("url"), str) and first["url"].startswith("http"):
                return first["url"]
            if isinstance(first, str) and first.startswith("http"):
                return first
    img = data.get("image")
    if isinstance(img, dict) and isinstance(img.get("url"), str) and img["url"].startswith("http"):
        return img["url"]
    if isinstance(data.get("url"), str) and data["url"].startswith("http"):
        return data["url"]
    return None


def _load_image_from_source(image_source: str) -> Image.Image:
    src = str(image_source or "").strip()
    if src.startswith("http://") or src.startswith("https://"):
        resp = requests.get(
            src,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"},
            timeout=20,
        )
        resp.raise_for_status()
        return Image.open(BytesIO(resp.content)).convert("RGB")
    if src.startswith("/"):
        rel = Path(__file__).resolve().parent.parent / src.lstrip("/")
        if rel.exists():
            return Image.open(rel).convert("RGB")
    p = Path(src)
    if p.exists():
        return Image.open(p).convert("RGB")
    raise FileNotFoundError(f"Şəkil mənbəyi açılmadı: {image_source}")


def prepare_vertical_9x16_studio_frame(
    image_source: str,
    dest_path: Path,
    width: int = 720,
    height: int = 1280,
) -> Path:
    """
    Formats any product photo (landscape, square, or portrait) into a crisp 9:16 vertical
    studio frame WITHOUT cropping product edges. Ensures Kling/Hailuo/Seedance generate true 9:16.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    im = _load_image_from_source(image_source)

    # Blurred darkened atmospheric backdrop from the product photo itself
    bg = im.resize((width, height), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(radius=34))
    dark_tint = Image.new("RGB", (width, height), (14, 18, 30))
    bg = Image.blend(bg, dark_tint, alpha=0.45)

    # Fit sharp foreground product cleanly inside central 9:16 safe area (90% w x 78% h)
    max_w, max_h = int(width * 0.90), int(height * 0.78)
    fg = im.copy()
    fg.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    px = (width - fg.width) // 2
    py = (height - fg.height) // 2
    bg.paste(fg, (px, py))

    bg.save(dest_path, format="JPEG", quality=92)
    return dest_path


def _build_broll_payload(
    model_id: str,
    resolution: str,
    duration_sec: int,
    img_ref: str,
    prompt: str,
) -> Dict[str, Any]:
    """Build exact model-compliant payload enforcing 9:16 and locked resolution so billing never spikes."""
    mid = (model_id or "").lower()
    if "seedance" in mid:
        res_clean = resolution if resolution in ("480p", "720p", "1080p") else ("720p" if ("1.5" in mid or "2.0" in mid or "2.5" in mid) else "480p")
        dur_str = str(max(4 if ("1.5" in mid or "2.0" in mid or "2.5" in mid) else 2, min(int(duration_sec or 5), 12)))
        payload: Dict[str, Any] = {
            "image_url": img_ref,
            "prompt": prompt,
            "aspect_ratio": "9:16",
            "resolution": res_clean,
            "duration": dur_str,
        }
        # Seedance v1.5 / 2.0 / 2.5 support native audio; disable it because we use clean ElevenLabs voiceover
        if any(ver in mid for ver in ("1.5", "2.0", "2.5")):
            payload["generate_audio"] = False
        return payload
    if "hailuo" in mid:
        res_clean = "512P" if ("480" in resolution or "512" in resolution) else "768P"
        return {
            "image_url": img_ref,
            "prompt": prompt,
            "resolution": res_clean,
            "duration": "6",
        }
    if "wan" in mid:
        res_clean = resolution if resolution in ("480p", "720p", "1080p") else "720p"
        return {
            "image_url": img_ref,
            "prompt": prompt,
            "aspect_ratio": "9:16",
            "resolution": res_clean,
            "duration": "5",
        }
    # Kling v1.6 Std & Kling v2.1 Pro infer aspect ratio cleanly from the pre-framed 9:16 image
    return {
        "image_url": img_ref,
        "prompt": prompt,
        "duration": "5" if int(duration_sec or 5) <= 5 else "10",
    }


def generate_product_scene_images(
    existing_image_urls: List[str],
    product_name: str,
    output_dir: Path,
    run_uid: str,
    target_count: int = 3,
    frame_size: Tuple[int, int] = (720, 1280),
) -> Dict[str, Any]:
    """
    Ensures at least `target_count` (3) distinct 9:16 studio-framed images exist for multi-clip AI B-Roll:
      - Uses all verified Stage 1 / user-uploaded images first ($0 extra)
      - If fewer than 3 unique images exist, generates missing lifestyle angles via fal-ai/nano-banana/edit ($0.039/img)
    """
    keys = get_fal_api_keys()
    if not keys:
        raise CreativeBalanceError("fal", "FAL_KEY təyin edilməyib.", "https://fal.ai/dashboard/billing")
    auth_headers = {"Authorization": f"Key {keys[0]}"}

    unique_sources: List[str] = []
    for u in (existing_image_urls or []):
        s = str(u or "").strip()
        if s and s not in unique_sources:
            unique_sources.append(s)
    if not unique_sources:
        raise ValueError("B-Roll səhnələri üçün heç bir məhsul şəkli tapılmadı.")

    w, h = frame_size
    prepared_paths: List[str] = []
    for idx, src in enumerate(unique_sources[:target_count]):
        dest = output_dir / f"broll_src_{idx+1}_{run_uid}.jpg"
        try:
            prepare_vertical_9x16_studio_frame(src, dest, width=w, height=h)
            prepared_paths.append(str(dest))
        except Exception as exc:
            logger.warning(f"B-Roll source image #{idx+1} skip note ({exc})")

    if not prepared_paths:
        raise ValueError("Mövcud məhsul şəkillərindən heç biri endirilə bilmədi.")

    generated_count = 0
    gen_cost = 0.0
    models_used: List[str] = []

    if len(prepared_paths) < target_count:
        hero_ref = _file_to_data_uri_or_fal_url(prepared_paths[0], headers=auth_headers)
        angle_prompts = [
            (
                f"Photorealistic vertical 9:16 lifestyle UGC product photo of this exact {product_name} "
                "held naturally in real user hands in a bright modern sunlit room, shallow depth of field, "
                "keep exact product design and colors intact, no text, no logos"
            ),
            (
                f"Photorealistic 45-degree macro studio close-up of this exact {product_name} "
                "resting on a sleek minimalist stone surface with soft dramatic rim lighting showcasing texture and details, "
                "vertical 9:16 composition, no text"
            ),
        ]
        need_n = target_count - len(prepared_paths)
        for gi in range(need_n):
            prompt = angle_prompts[gi % len(angle_prompts)]
            edit_models = [
                ("fal-ai/nano-banana/edit", {"prompt": prompt, "image_urls": [hero_ref], "aspect_ratio": "9:16", "num_images": 1}),
                ("fal-ai/flux-kontext/dev", {"prompt": prompt, "image_url": hero_ref, "aspect_ratio": "9:16", "num_images": 1}),
            ]
            img_url: Optional[str] = None
            used_m = ""
            for em_id, em_payload in edit_models:
                try:
                    logger.info(f"Generating missing B-Roll angle #{len(prepared_paths)+1}/{target_count} via {em_id}...")
                    res = submit_fal_queue(em_id, em_payload, timeout_sec=120)
                    img_url = _extract_fal_image_url(res)
                    if img_url:
                        used_m = em_id
                        break
                except CreativeBalanceError:
                    raise
                except Exception as exc:
                    logger.warning(f"Scene image generator '{em_id}' note: {exc}")
                    continue

            if img_url:
                raw_gen = output_dir / f"broll_gen_{gi+1}_{run_uid}.jpg"
                prepared_gen = output_dir / f"broll_src_gen_{gi+1}_{run_uid}.jpg"
                with requests.get(img_url, timeout=45) as r:
                    r.raise_for_status()
                    raw_gen.write_bytes(r.content)
                prepare_vertical_9x16_studio_frame(str(raw_gen), prepared_gen, width=w, height=h)
                raw_gen.unlink(missing_ok=True)
                prepared_paths.append(str(prepared_gen))
                generated_count += 1
                gen_cost = round(gen_cost + SCENE_GEN_COST_PER_IMAGE_USD, 3)
                models_used.append(used_m)

    return {
        "image_paths": prepared_paths,
        "generated_count": generated_count,
        "cost_usd": round(gen_cost, 3),
        "models_used": models_used,
    }


def _cache_meta_path(cache_dir: Path, cache_key: str) -> Path:
    return cache_dir / f"{cache_key}.meta.json"


def _pending_req_path(cache_dir: Path, cache_key: str) -> Path:
    return cache_dir / f"{cache_key}.pending.json"


def _load_cached_media(cache_dir: Optional[Path], cache_key: Optional[str]) -> Optional[Dict[str, Any]]:
    if not cache_dir or not cache_key:
        return None
    meta_p = _cache_meta_path(cache_dir, cache_key)
    if not meta_p.exists():
        return None
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        fp = Path(meta.get("file_path") or "")
        if fp.exists() and fp.stat().st_size >= 10000:
            meta["from_cache"] = True
            return meta
    except Exception:
        pass
    return None


def _save_cached_media(cache_dir: Optional[Path], cache_key: Optional[str], meta: Dict[str, Any]) -> None:
    if not cache_dir or not cache_key:
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _cache_meta_path(cache_dir, cache_key).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        _pending_req_path(cache_dir, cache_key).unlink(missing_ok=True)
    except Exception:
        pass


def _load_pending_request(cache_dir: Optional[Path], cache_key: Optional[str]) -> Optional[Dict[str, str]]:
    if not cache_dir or not cache_key:
        return None
    p = _pending_req_path(cache_dir, cache_key)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("request_id") and data.get("model_id"):
            return data
    except Exception:
        pass
    return None


def _save_pending_request(
    cache_dir: Optional[Path],
    cache_key: Optional[str],
    model_id: str,
    request_id: str,
    status_url: str,
    response_url: str,
) -> None:
    if not cache_dir or not cache_key:
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _pending_req_path(cache_dir, cache_key).write_text(
            json.dumps(
                {
                    "model_id": model_id,
                    "request_id": request_id,
                    "status_url": status_url,
                    "response_url": response_url,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _ensure_min_audio_duration(audio_path: str, min_sec: float = 4.15) -> Tuple[str, Optional[Path]]:
    """
    OmniHuman v2 strictly requires audio >= 4.0s. If the hook audio is shorter,
    pad trailing silence via FFmpeg `apad` into a temporary MP3 so API calls never fail with 422.
    Returns (path_to_use, temp_file_to_cleanup_or_None).
    """
    src = Path(audio_path)
    dur = _probe_media_duration(src)
    if dur <= 0 or dur >= min_sec:
        return str(src), None
    padded = src.with_name(f"padded_{uuid.uuid4().hex[:6]}_{src.name}")
    try:
        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
        res = subprocess.run(
            [
                ffmpeg_bin, "-y", "-i", str(src),
                "-af", f"apad=whole_dur={min_sec:.2f}",
                "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                str(padded),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=25,
        )
        if res.returncode == 0 and padded.exists() and padded.stat().st_size > 1500:
            logger.info(f"🔧 OmniHuman v2 audio pad: {dur:.2f}s → {min_sec:.2f}s ({padded.name})")
            return str(padded), padded
    except Exception as exc:
        logger.warning(f"Audio pad note (using original): {exc}")
    padded.unlink(missing_ok=True)
    return str(src), None


def generate_hook_lipsync_video(
    avatar_image_url: str,
    hook_audio_path: str,
    dest_video_path: Path,
    quality_tier: str = "balanced",
    return_details: bool = False,
    timeout_sec: int = 300,
    cache_dir: Optional[Path] = None,
    cache_key: Optional[str] = None,
    max_fal_budget_usd: Optional[float] = None,
    spent_fal_usd: float = 0.0,
    progress_cb: Optional[Callable[[str], None]] = None,
    avatar_direction_prompt: Optional[str] = None,
) -> Any:
    """
    Generates a real lip-synced talking-to-camera UGC Hook video (~3.5-4.5s ONLY)
    via Fal.ai Async Queue API with OmniHuman v2 4.15s min-audio guard, 300s timeout,
    request_id resume, run-level cache, and +25% retry Fal budget guard.
    """
    tier_id = normalize_quality_tier(quality_tier)
    cfg = QUALITY_TIERS[tier_id]

    cached = _load_cached_media(cache_dir, cache_key)
    if cached:
        logger.info(f"♻️ Lip-Sync cache tapıldı ({cache_key}): {cached.get('file_path')}")
        return cached if return_details else str(cached["file_path"])

    keys = get_fal_api_keys()
    if not keys:
        raise CreativeBalanceError("fal", "FAL_KEY təyin edilməyib.", "https://fal.ai/dashboard/billing")

    auth_headers = {"Authorization": f"Key {keys[0]}"}
    orig_audio_ref = _file_to_data_uri_or_fal_url(hook_audio_path, headers=auth_headers)
    avatar_ref = _resolve_avatar_url(avatar_image_url, auth_headers)

    models_to_try = [cfg["lipsync_model"]]
    if cfg.get("lipsync_fallback_model"):
        models_to_try.append(cfg["lipsync_fallback_model"])

    lipsync_res = str(cfg.get("lipsync_resolution") or "720p")
    est_hook_sec = float(cfg.get("lipsync_target_sec") or 4.0)
    pending = _load_pending_request(cache_dir, cache_key)
    last_schema_err: Optional[Exception] = None
    padded_audio_ref: Optional[str] = None
    padded_temp_file: Optional[Path] = None

    kling_direction = (avatar_direction_prompt or "").strip() or (
        "Natural energetic UGC creator speaking excitedly directly to smartphone camera, "
        "expressive hand gestures, warm natural indoor daylight, authentic TikTok selfie look"
    )

    try:
        for idx, model_id in enumerate(models_to_try):
            mid_low = model_id.lower()
            est_model_cost = round(est_hook_sec * get_fal_rate_per_sec(model_id, lipsync_res), 3)
            if idx > 0 and max_fal_budget_usd is not None and (spent_fal_usd + est_model_cost) > max_fal_budget_usd:
                logger.warning(
                    f"🛑 Lip-Sync fallback '{model_id}' (~${est_model_cost:.2f}) retry büdcə limitini aşır "
                    f"(xərclənmiş ${spent_fal_usd:.2f}, limit ${max_fal_budget_usd:.2f})."
                )
                break

            active_audio_ref = orig_audio_ref
            if "omnihuman/v2" in mid_low:
                if padded_audio_ref is None:
                    pad_path, padded_temp_file = _ensure_min_audio_duration(hook_audio_path, min_sec=4.15)
                    padded_audio_ref = (
                        _file_to_data_uri_or_fal_url(pad_path, headers=auth_headers)
                        if padded_temp_file
                        else orig_audio_ref
                    )
                active_audio_ref = padded_audio_ref
                # OmniHuman v2 streamlined schema: image_url + audio_url ONLY (no manual resolution switch)
                payload: Dict[str, Any] = {
                    "image_url": avatar_ref,
                    "audio_url": active_audio_ref,
                }
            elif "omnihuman" in mid_low:
                payload = {
                    "audio_url": active_audio_ref,
                    "aspect_ratio": "9:16",
                    "image_url": avatar_ref,
                    "resolution": lipsync_res,
                }
            else:
                payload = {
                    "audio_url": active_audio_ref,
                    "aspect_ratio": "9:16",
                    "image_url": avatar_ref,
                    "prompt": kling_direction,
                }

            use_pending = pending if (pending and pending.get("model_id") == model_id) else None

            def _on_sub(req_id: str, st_url: str, res_url: str, _mid: str = model_id) -> None:
                _save_pending_request(cache_dir, cache_key, _mid, req_id, st_url, res_url)

            try:
                if progress_cb:
                    progress_cb(f"🎭 Lip-Sync növbədə ({model_id.split('/')[-1]}, ~2 dəq)...")
                logger.info(
                    f"{'Resuming' if use_pending else 'Submitting'} Fal.ai Queue Lip-Sync Hook [{tier_id} | {model_id}]..."
                )
                try:
                    res = submit_fal_queue(
                        model_id,
                        payload,
                        timeout_sec=timeout_sec,
                        existing_request_id=use_pending["request_id"] if use_pending else None,
                        existing_status_url=use_pending.get("status_url") if use_pending else None,
                        existing_response_url=use_pending.get("response_url") if use_pending else None,
                        on_request_submitted=_on_sub,
                    )
                except FalQueueTimeoutError as te:
                    if te.request_id:
                        logger.warning(
                            f"⏳ Lip-Sync '{model_id}' timeout — mövcud request_id ({te.request_id}) ilə +60s bərpa polling..."
                        )
                        res = submit_fal_queue(
                            model_id,
                            payload,
                            timeout_sec=60,
                            existing_request_id=te.request_id,
                            existing_status_url=te.status_url,
                            existing_response_url=te.response_url,
                        )
                    else:
                        raise

                video_url = _extract_fal_video_url(res)
                if not video_url:
                    raise RuntimeError(f"Fal.ai '{model_id}' cavabında video URL yoxdur: {str(res)[:200]}")
                _download_mp4_binary(video_url, dest_video_path)
                actual_dur = _probe_media_duration(dest_video_path) or est_hook_sec
                rate = get_fal_rate_per_sec(model_id, lipsync_res)
                actual_cost = round(actual_dur * rate, 3)
                logger.info(f"✅ Fal.ai Lip-Sync Hook hazır oldu: {dest_video_path.name} ({model_id} | {actual_dur:.2f}s ≈ ${actual_cost:.3f})")
                details = {
                    "file_path": str(dest_video_path),
                    "model": model_id,
                    "resolution": lipsync_res,
                    "duration_sec": actual_dur,
                    "rate_per_sec": rate,
                    "cost_usd": actual_cost,
                    "from_cache": False,
                }
                _save_cached_media(cache_dir, cache_key, details)
                if return_details:
                    return details
                return str(dest_video_path)
            except CreativeBalanceError:
                raise
            except Exception as exc:
                last_schema_err = exc
                if idx + 1 < len(models_to_try):
                    logger.warning(f"Fal Lip-Sync model '{model_id}' note ({exc}); retrying fallback model...")
                    continue
                raise

        raise RuntimeError(f"Fal.ai Lip-Sync Hook generasiyası uğursuz oldu: {last_schema_err}")
    finally:
        if padded_temp_file:
            padded_temp_file.unlink(missing_ok=True)


def generate_shared_ai_broll_clips(
    product_image_urls: List[str],
    product_name: str,
    output_dir: Path,
    run_uid: str,
    quality_tier: str = "balanced",
    return_details: bool = False,
    target_scene2_dur: Optional[float] = None,
    cache_dir: Optional[Path] = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    custom_broll_prompts: Optional[List[str]] = None,
) -> Any:
    """
    Generates 3 shared vertical 9:16 moving commercial product B-Roll clips IN PARALLEL
    from 3 distinct studio-framed images using Fal.ai Async Queue Image-to-Video with
    AI Creative Director shot-specific prompts, per-clip caching, and request_id timeout recovery.
    """
    tier_id = normalize_quality_tier(quality_tier)
    cfg = QUALITY_TIERS[tier_id]
    keys = get_fal_api_keys()
    if not keys:
        raise CreativeBalanceError("fal", "FAL_KEY təyin edilməyib.", "https://fal.ai/dashboard/billing")

    valid_imgs = [str(u).strip() for u in (product_image_urls or []) if isinstance(u, str) and u.strip()]
    if not valid_imgs:
        raise ValueError("AI B-Roll generasiyası üçün təsdiqlənmiş məhsul şəkli yoxdur.")

    target_count = int(cfg["broll_clip_count"])
    broll_res = str(cfg.get("broll_resolution") or "720p")
    broll_dur = int(cfg.get("broll_clip_dur_sec") or 5)
    # Match clip length to Scene 2 cut duration for Seedance so stream_loop doesn't visibly jump
    if target_scene2_dur and target_scene2_dur > 0 and "seedance" in str(cfg.get("broll_model", "")).lower():
        needed_cut = (float(target_scene2_dur) / max(target_count, 1)) + 0.25
        broll_dur = max(broll_dur, min(6, int(round(needed_cut + 0.49))))

    frame_wh = (1080, 1920) if tier_id == "cinema" else (720, 1280)
    auth_headers = {"Authorization": f"Key {keys[0]}"}

    default_prompts = [
        (
            f"Cinematic commercial macro close-up of {product_name}, smooth subtle 25-degree orbital camera sweep, "
            "crisp key studio light revealing premium surface texture and build quality, shallow depth of field, "
            "preserve exact product geometry and colors, no text, no watermark"
        ),
        (
            f"Authentic handheld UGC POV demonstration of {product_name} in natural everyday sunlit home setting, "
            "gentle forward push-in motion showcasing smooth effortless usability, realistic lighting and shadows, "
            "preserve exact product shape, no text overlay, no distorted hands"
        ),
        (
            f"Hero commercial detail reveal of {product_name}, slow dramatic dolly-out from key ergonomic feature "
            "to full aesthetic lifestyle framing, soft rim lighting, clean high-converting DTC commercial look, "
            "no text, no logos, no watermark"
        ),
    ]
    cleaned_custom = [str(p).strip() for p in (custom_broll_prompts or []) if isinstance(p, str) and len(str(p).strip()) >= 15]
    prompts = [
        cleaned_custom[i] if i < len(cleaned_custom) else default_prompts[i % len(default_prompts)]
        for i in range(target_count)
    ]

    broll_models: List[str] = [cfg["broll_model"]]
    for fb_key in ("broll_fallback_model", "broll_secondary_fallback_model"):
        fb_m = cfg.get(fb_key)
        if fb_m and fb_m not in broll_models:
            broll_models.append(fb_m)

    completed_counter = {"n": 0}

    def _gen_single_clip(idx: int) -> Dict[str, Any]:
        ckey = f"broll_{idx+1}"
        cached = _load_cached_media(cache_dir, ckey)
        if cached:
            completed_counter["n"] += 1
            if progress_cb:
                progress_cb(f"♻️ B-Roll {completed_counter['n']}/{target_count} cache-dən yükləndi")
            return cached

        raw_img = valid_imgs[idx % len(valid_imgs)]
        framed_path = output_dir / f"broll_framed_{idx+1}_{run_uid}.jpg"
        try:
            try:
                prepare_vertical_9x16_studio_frame(raw_img, framed_path, width=frame_wh[0], height=frame_wh[1])
                img_ref = _file_to_data_uri_or_fal_url(str(framed_path), headers=auth_headers)
            except Exception:
                img_ref = raw_img if raw_img.startswith("http") else _file_to_data_uri_or_fal_url(raw_img, headers=auth_headers)
        finally:
            framed_path.unlink(missing_ok=True)

        pending = _load_pending_request(cache_dir, ckey)
        res: Optional[Dict[str, Any]] = None
        used_model = broll_models[0]
        last_err: Optional[Exception] = None

        for m_idx, mid in enumerate(broll_models):
            eff_dur = 6 if "hailuo" in mid.lower() else broll_dur
            payload = _build_broll_payload(
                model_id=mid,
                resolution=broll_res,
                duration_sec=eff_dur,
                img_ref=img_ref,
                prompt=prompts[idx % len(prompts)],
            )
            use_pending = pending if (pending and pending.get("model_id") == mid) else None

            def _on_sub(req_id: str, st_url: str, res_url: str, _mid: str = mid) -> None:
                _save_pending_request(cache_dir, ckey, _mid, req_id, st_url, res_url)

            try:
                logger.info(
                    f"Submitting Fal.ai Queue Shared B-Roll #{idx+1}/{target_count} "
                    f"[{tier_id} | {mid} | {broll_res} | {payload.get('duration')}s]..."
                )
                try:
                    res = submit_fal_queue(
                        mid,
                        payload,
                        timeout_sec=210,
                        existing_request_id=use_pending["request_id"] if use_pending else None,
                        existing_status_url=use_pending.get("status_url") if use_pending else None,
                        existing_response_url=use_pending.get("response_url") if use_pending else None,
                        on_request_submitted=_on_sub,
                    )
                except FalQueueTimeoutError as te:
                    if te.request_id:
                        logger.warning(f"⏳ B-Roll #{idx+1} timeout — mövcud request_id ({te.request_id}) bərpa olunur (+60s)...")
                        res = submit_fal_queue(
                            mid,
                            payload,
                            timeout_sec=60,
                            existing_request_id=te.request_id,
                            existing_status_url=te.status_url,
                            existing_response_url=te.response_url,
                        )
                    else:
                        raise
                used_model = mid
                break
            except CreativeBalanceError:
                raise
            except Exception as exc:
                last_err = exc
                if m_idx + 1 < len(broll_models):
                    logger.warning(f"Fal B-Roll #{idx+1} model '{mid}' xətası ({exc}); fallback modelə keçilir...")
                    continue
                raise

        if not res:
            raise RuntimeError(f"Fal.ai B-Roll #{idx+1} generasiyası uğursuz oldu: {last_err}")

        video_url = _extract_fal_video_url(res)
        if not video_url:
            raise RuntimeError(f"Fal.ai B-Roll #{idx+1} cavabında video URL yoxdur.")
        dest = output_dir / f"shared_broll_{idx+1}_{run_uid}.mp4"
        _download_mp4_binary(video_url, dest)
        actual_dur = _probe_media_duration(dest) or float(6 if "hailuo" in used_model.lower() else broll_dur)
        eff_res = "512p" if ("hailuo" in used_model and broll_res == "480p") else broll_res
        rate = get_fal_rate_per_sec(used_model, eff_res)
        clip_cost = round(actual_dur * rate, 3)

        meta = {
            "index": idx,
            "file_path": str(dest),
            "model": used_model,
            "resolution": eff_res,
            "duration_sec": actual_dur,
            "rate_per_sec": rate,
            "cost_usd": clip_cost,
            "prompt_used": prompts[idx % len(prompts)],
            "from_cache": False,
        }
        _save_cached_media(cache_dir, ckey, meta)
        completed_counter["n"] += 1
        logger.info(
            f"✅ Shared AI B-Roll #{idx+1}/{target_count} hazır oldu: {dest.name} ({used_model} | {actual_dur:.2f}s ≈ ${clip_cost:.3f})"
        )
        if progress_cb:
            progress_cb(f"🎬 B-Roll {completed_counter['n']}/{target_count} hazırdır ({used_model.split('/')[-1]})")
        return meta

    results_by_idx: Dict[int, Dict[str, Any]] = {}
    errors: List[Exception] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, target_count)) as pool:
        fut_map = {pool.submit(_gen_single_clip, idx): idx for idx in range(target_count)}
        for fut in concurrent.futures.as_completed(fut_map):
            idx = fut_map[fut]
            try:
                results_by_idx[idx] = fut.result()
            except CreativeBalanceError:
                raise
            except Exception as exc:
                logger.warning(f"B-Roll klip #{idx+1} uğursuz oldu ({exc}); 1 dəfə təkrar cəhd olunur...")
                try:
                    results_by_idx[idx] = _gen_single_clip(idx)
                except Exception as retry_exc:
                    errors.append(retry_exc)

    if not results_by_idx:
        raise RuntimeError(f"Bütün AI B-Roll klipləri uğursuz oldu: {errors[0] if errors else 'naməlum xəta'}")

    ordered_meta = [results_by_idx[i] for i in sorted(results_by_idx.keys())]
    output_paths = [m["file_path"] for m in ordered_meta]
    total_broll_cost = round(sum(float(m.get("cost_usd") or 0.0) for m in ordered_meta), 3)

    if return_details:
        return {
            "clip_paths": output_paths,
            "clips_meta": ordered_meta,
            "total_cost_usd": total_broll_cost,
        }
    return output_paths


def generate_realistic_ai_avatar(
    output_dir: Path,
    gender: str = "female",
    niche: str = "lifestyle",
    age_group: str = "25-32",
    style_prompt: str = "",
) -> Dict[str, Any]:
    """
    Generate a hyper-realistic 9:16 human UGC creator selfie portrait via Fal.ai Flux
    (~$0.03-$0.04) so users can create bespoke realistic talking-head avatars in 1 click.
    """
    keys = get_fal_api_keys()
    if not keys:
        raise CreativeBalanceError("fal", "FAL_KEY təyin edilməyib.", "https://fal.ai/dashboard/billing")

    g_txt = "young man" if str(gender).lower() == "male" else "young woman"
    niche_env = {
        "tech": "modern minimalist home desk with subtle monitor glow and indoor daylight",
        "kitchen": "bright sunlit modern home kitchen counter background",
        "fitness": "bright airy home gym or sunny outdoor park wearing casual athleisure",
        "beauty": "warm natural daylight bedroom mirror setting with clean minimal aesthetic",
        "luxury": "chic modern sunlit apartment living room wearing smart-casual outfit",
        "auto": "clean bright modern car interior or sunlit driveway",
    }.get(str(niche).lower(), "bright sunlit modern living room with soft natural window light")

    extra = f", {style_prompt.strip()}" if style_prompt and style_prompt.strip() else ""
    prompt = (
        f"Candid hyper-realistic vertical 9:16 smartphone selfie portrait photo of a friendly authentic {g_txt}, "
        f"age {age_group}, looking directly into the camera lens with a warm confident natural smile, "
        f"head and shoulders clearly framed centered for talking-head video, natural skin texture with pores, "
        f"subtle natural lighting in a {niche_env}{extra}, shot on iPhone 15 Pro, raw authentic UGC creator photo, "
        "no text, no watermark, no studio AI plastic skin, no extra hands"
    )

    models = [
        ("fal-ai/flux-pro/v1.1", {"prompt": prompt, "image_size": "portrait_16_9", "num_images": 1, "safety_tolerance": "2"}),
        ("fal-ai/flux/schnell", {"prompt": prompt, "image_size": "portrait_16_9", "num_images": 1}),
    ]
    last_err: Optional[Exception] = None
    for mid, payload in models:
        try:
            res = submit_fal_queue(mid, payload, timeout_sec=90)
            img_url = _extract_fal_image_url(res)
            if not img_url:
                raise RuntimeError(f"Fal.ai '{mid}' cavabında şəkil URL tapılmadı.")
            output_dir.mkdir(parents=True, exist_ok=True)
            fname = f"ai_avatar_{uuid.uuid4().hex[:8]}.jpg"
            dest = output_dir / fname
            with requests.get(img_url, timeout=45) as r:
                r.raise_for_status()
                dest.write_bytes(r.content)
            logger.info(f"✅ Realistik AI UGC Avatar yaradıldı: {fname} ({mid})")
            return {
                "file_path": str(dest),
                "filename": fname,
                "web_url": f"/static/media/avatars/{fname}",
                "model": mid,
                "cost_usd": AI_AVATAR_GEN_COST_USD,
            }
        except CreativeBalanceError:
            raise
        except Exception as exc:
            last_err = exc
            logger.warning(f"AI Avatar generator '{mid}' note: {exc}")
            continue

    raise RuntimeError(f"Realistik AI Avatar generasiyası uğursuz oldu: {last_err}")


ELEVENLABS_RATE_PER_1K_CHARS: Dict[str, float] = {
    "eleven_v3": 0.18,
    "eleven_v4_turbo": 0.16,
    "eleven_v4": 0.22,
    "eleven_flash_v2_5": 0.06,
    "eleven_multilingual_v2": 0.18,
}


def compute_actual_creative_cost(
    quality_tier: str,
    lipsync_records: List[Dict[str, Any]],
    broll_records: List[Dict[str, Any]],
    scene_gen_cost_usd: float = 0.0,
    elevenlabs_char_count: int = 0,
) -> Dict[str, Any]:
    """Summarize exact post-run costs based on actual measured clip durations and real ElevenLabs char count."""
    cfg = QUALITY_TIERS[normalize_quality_tier(quality_tier)]
    lipsync_usd = round(sum(float(r.get("cost_usd") or 0.0) for r in lipsync_records), 3)
    broll_usd = round(sum(float(r.get("cost_usd") or 0.0) for r in broll_records), 3)
    scene_usd = round(float(scene_gen_cost_usd or 0.0), 3)
    fal_total = round(lipsync_usd + broll_usd + scene_usd, 2)

    el_per_1k = ELEVENLABS_RATE_PER_1K_CHARS.get(cfg["elevenlabs_model"], 0.18)
    el_usd = round(max((elevenlabs_char_count / 1000.0) * el_per_1k, 0.01), 3) if elevenlabs_char_count > 0 else 0.0
    shared_cost_usd = round(broll_usd + scene_usd, 2)
    grand_total = round(fal_total + el_usd, 2)

    return {
        "actual_cost_usd": grand_total,
        "actual_fal_usd": fal_total,
        "actual_elevenlabs_usd": round(el_usd, 2),
        "shared_cost_usd": shared_cost_usd,
        "elevenlabs_char_count": int(elevenlabs_char_count),
        "breakdown": {
            "fal_lipsync_hooks_usd": round(lipsync_usd, 2),
            "fal_shared_ai_broll_usd": round(broll_usd, 2),
            "fal_scene_gen_usd": round(scene_usd, 2),
            "elevenlabs_voice_timestamps_usd": round(el_usd, 2),
        },
    }

