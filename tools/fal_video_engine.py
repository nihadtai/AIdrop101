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
from typing import Any, Dict, List, Optional, Tuple

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


# Official Fal.ai per-second video billing rates (verified 2025-2026) & per-image scene edit cost
FAL_MODEL_RATES_PER_SEC: Dict[str, float] = {
    "fal-ai/kling-video/ai-avatar/v2/standard": 0.0562,
    "fal-ai/kling-video/v1/pro/ai-avatar": 0.115,
    "fal-ai/bytedance/omnihuman": 0.140,
    "fal-ai/bytedance/omnihuman/v1.5:720p": 0.145,
    "fal-ai/bytedance/omnihuman/v1.5:1080p": 0.210,
    "fal-ai/bytedance/omnihuman/v1.5": 0.145,
    "fal-ai/bytedance/seedance/v1/lite/image-to-video:480p": 0.014,
    "fal-ai/bytedance/seedance/v1/lite/image-to-video:720p": 0.030,
    "fal-ai/bytedance/seedance/v1/lite/image-to-video:1080p": 0.068,
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


def get_fal_rate_per_sec(model_id: str, resolution: Optional[str] = None) -> float:
    mid = (model_id or "").strip().lower()
    res_tag = (resolution or "").strip().lower()
    if res_tag and f"{mid}:{res_tag}" in FAL_MODEL_RATES_PER_SEC:
        return FAL_MODEL_RATES_PER_SEC[f"{mid}:{res_tag}"]
    if mid in FAL_MODEL_RATES_PER_SEC:
        return FAL_MODEL_RATES_PER_SEC[mid]
    return 0.05


QUALITY_TIERS: Dict[str, Dict[str, Any]] = {
    "economy": {
        "id": "economy",
        "label": "Economy (Sürətli & Minimum Xərc)",
        "elevenlabs_model": "eleven_flash_v2_5",
        "lipsync_model": "fal-ai/kling-video/ai-avatar/v2/standard",
        "lipsync_fallback_model": "fal-ai/bytedance/omnihuman",
        "lipsync_resolution": "720p",
        "lipsync_target_sec": 3.5,
        "broll_model": "fal-ai/bytedance/seedance/v1/lite/image-to-video",
        "broll_fallback_model": "fal-ai/minimax/hailuo-02/standard/image-to-video",
        "broll_resolution": "480p",
        "broll_clip_dur_sec": 4,
        "broll_clip_count": 3,
        "elevenlabs_base_usd": 0.012,
        "elevenlabs_per_hook_usd": 0.008,
    },
    "balanced": {
        "id": "balanced",
        "label": "Balanced Hybrid (Tövsiyə Edilən)",
        "elevenlabs_model": "eleven_multilingual_v2",
        "lipsync_model": "fal-ai/kling-video/ai-avatar/v2/standard",
        "lipsync_fallback_model": "fal-ai/kling-video/v1/pro/ai-avatar",
        "lipsync_resolution": "720p",
        "lipsync_target_sec": 3.8,
        "broll_model": "fal-ai/kling-video/v1.6/standard/image-to-video",
        "broll_fallback_model": "fal-ai/bytedance/seedance/v1/lite/image-to-video",
        "broll_resolution": "720p",
        "broll_clip_dur_sec": 5,
        "broll_clip_count": 3,
        "elevenlabs_base_usd": 0.025,
        "elevenlabs_per_hook_usd": 0.015,
    },
    "cinema": {
        "id": "cinema",
        "label": "Cinema Pro (Maksimum Realizm)",
        "elevenlabs_model": "eleven_v3",
        "lipsync_model": "fal-ai/bytedance/omnihuman/v1.5",
        "lipsync_fallback_model": "fal-ai/kling-video/v1/pro/ai-avatar",
        "lipsync_resolution": "720p",
        "lipsync_target_sec": 4.0,
        "broll_model": "fal-ai/kling-video/v2.1/pro/image-to-video",
        "broll_fallback_model": "fal-ai/wan/v2.7/image-to-video",
        "broll_resolution": "720p",
        "broll_clip_dur_sec": 5,
        "broll_clip_count": 3,
        "elevenlabs_base_usd": 0.04,
        "elevenlabs_per_hook_usd": 0.02,
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

    return {
        "quality_tier": tier_id,
        "tier_label": cfg["label"],
        "variant_count": n_vars,
        "broll_clip_count": broll_clips if need_ai_broll else 0,
        "broll_resolution": cfg["broll_resolution"],
        "scene_gen_count": missing_scenes,
        "estimated_fal_usd": fal_total,
        "estimated_cost_usd": total,
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


def check_elevenlabs_balance(required_chars: int = 750) -> Dict[str, Any]:
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
                return {
                    "ok": True,
                    "configured": True,
                    "tier": "scoped_tts_key",
                    "character_limit": None,
                    "character_used": None,
                    "remaining_chars": None,
                    "required_chars": required_chars,
                    "error": "",
                    "note": "Scoped TTS açarı aktivdir (user_read icazəsi olmadan TTS işləyir).",
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
      - Checks ElevenLabs quota & Fal.ai key configuration
      - Calculates exact estimated cost breakdown for all 3 tiers + active selection
      - Raises CreativeBalanceError immediately when strict=True and a required check fails
    """
    n_vars = 3 if int(variant_count or 3) >= 3 else 1
    required_chars = 350 + n_vars * 130
    el_status = check_elevenlabs_balance(required_chars=required_chars)
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
) -> Dict[str, Any]:
    """
    Calls Fal.ai Async Queue API (`https://queue.fal.run/{model_id}`), polls status_url
    until COMPLETED, and strictly raises CreativeBalanceError on 401/402/403 balance errors.
    """
    keys = get_fal_api_keys()
    if not keys:
        raise CreativeBalanceError("fal", "FAL_KEY təyin edilməyib.", "https://fal.ai/dashboard/billing")

    submit_url = f"https://queue.fal.run/{model_id.lstrip('/')}"
    resp = None
    active_headers: Dict[str, str] = {}
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

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        st_resp = requests.get(status_url, headers=active_headers, timeout=20)
        if st_resp.status_code in (401, 402, 403):
            raise CreativeBalanceError(
                "fal",
                f"Fal.ai balans/icazə xətası (HTTP {st_resp.status_code}): {st_resp.text[:160]}",
                "https://fal.ai/dashboard/billing",
            )
        if st_resp.status_code in (200, 202):
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
                return res_resp.json()
            if st in ("FAILED", "ERROR", "CANCELLED"):
                err_txt = str(st_json.get("error") or st_json.get("logs") or st_json)[:240]
                if "balance" in err_txt.lower() or "credit" in err_txt.lower() or "top_up" in err_txt.lower():
                    raise CreativeBalanceError("fal", f"Fal.ai balans xətası: {err_txt}", "https://fal.ai/dashboard/billing")
                raise RuntimeError(f"Fal.ai '{model_id}' task uğursuz oldu: {err_txt}")
        time.sleep(2.5)

    raise TimeoutError(f"Fal.ai Queue '{model_id}' {timeout_sec}s ərzində tamamlanmadı.")


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
        res_clean = resolution if resolution in ("480p", "720p", "1080p") else "480p"
        dur_str = str(max(2, min(int(duration_sec or 4), 12)))
        return {
            "image_url": img_ref,
            "prompt": prompt,
            "aspect_ratio": "9:16",
            "resolution": res_clean,
            "duration": dur_str,
        }
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


def generate_hook_lipsync_video(
    avatar_image_url: str,
    hook_audio_path: str,
    dest_video_path: Path,
    quality_tier: str = "balanced",
    return_details: bool = False,
) -> Any:
    """
    Generates a real lip-synced talking-to-camera UGC Hook video (~3.5-4.0s ONLY)
    via Fal.ai Async Queue API with resolution locked to prevent unexpected 1080p spikes.
    """
    tier_id = normalize_quality_tier(quality_tier)
    cfg = QUALITY_TIERS[tier_id]
    keys = get_fal_api_keys()
    if not keys:
        raise CreativeBalanceError("fal", "FAL_KEY təyin edilməyib.", "https://fal.ai/dashboard/billing")

    auth_headers = {"Authorization": f"Key {keys[0]}"}
    audio_ref = _file_to_data_uri_or_fal_url(hook_audio_path, headers=auth_headers)
    avatar_ref = _resolve_avatar_url(avatar_image_url, auth_headers)

    models_to_try = [cfg["lipsync_model"]]
    if cfg.get("lipsync_fallback_model"):
        models_to_try.append(cfg["lipsync_fallback_model"])

    lipsync_res = str(cfg.get("lipsync_resolution") or "720p")
    last_schema_err: Optional[Exception] = None
    for idx, model_id in enumerate(models_to_try):
        payload: Dict[str, Any] = {
            "audio_url": audio_ref,
            "aspect_ratio": "9:16",
            "image_url": avatar_ref,
        }
        if "omnihuman" in model_id:
            payload["resolution"] = lipsync_res
        elif "kling" in model_id:
            payload["prompt"] = "Natural energetic UGC creator speaking excitedly directly to smartphone camera, expressive hand gestures, indoor daylight"

        try:
            logger.info(f"Submitting Fal.ai Queue Lip-Sync Hook [{tier_id} | {model_id}]...")
            res = submit_fal_queue(model_id, payload, timeout_sec=180)
            video_url = _extract_fal_video_url(res)
            if not video_url:
                raise RuntimeError(f"Fal.ai '{model_id}' cavabında video URL yoxdur: {str(res)[:200]}")
            _download_mp4_binary(video_url, dest_video_path)
            actual_dur = _probe_media_duration(dest_video_path) or float(cfg.get("lipsync_target_sec") or 3.8)
            rate = get_fal_rate_per_sec(model_id, lipsync_res)
            actual_cost = round(actual_dur * rate, 3)
            logger.info(f"✅ Fal.ai Lip-Sync Hook hazır oldu: {dest_video_path.name} ({model_id} | {actual_dur:.2f}s ≈ ${actual_cost:.3f})")
            if return_details:
                return {
                    "file_path": str(dest_video_path),
                    "model": model_id,
                    "resolution": lipsync_res,
                    "duration_sec": actual_dur,
                    "rate_per_sec": rate,
                    "cost_usd": actual_cost,
                }
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


def generate_shared_ai_broll_clips(
    product_image_urls: List[str],
    product_name: str,
    output_dir: Path,
    run_uid: str,
    quality_tier: str = "balanced",
    return_details: bool = False,
) -> Any:
    """
    Generates 3 shared vertical 9:16 moving commercial product B-Roll clips from 3 distinct
    studio-framed images using Fal.ai Async Queue Image-to-Video with locked resolution/duration.
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
    frame_wh = (1080, 1920) if tier_id == "cinema" else (720, 1280)
    auth_headers = {"Authorization": f"Key {keys[0]}"}

    prompts = [
        (
            f"Cinematic commercial close-up showcase of {product_name}, smooth subtle orbit camera motion, "
            "crisp studio lighting highlighting build quality and texture, no text, no watermark"
        ),
        (
            f"Lifestyle UGC demonstration shot of {product_name} in everyday action, smooth push-in zoom, "
            "natural ambient lighting, high-converting social ad aesthetic, no text overlay"
        ),
        (
            f"Clean commercial detail angle of {product_name}, slow dramatic pan revealing ergonomic features, "
            "premium product commercial look, no text, no watermark"
        ),
    ]

    broll_models = [cfg["broll_model"]]
    if cfg.get("broll_fallback_model"):
        broll_models.append(cfg["broll_fallback_model"])

    output_paths: List[str] = []
    clips_meta: List[Dict[str, Any]] = []
    total_broll_cost = 0.0

    for idx in range(target_count):
        raw_img = valid_imgs[idx % len(valid_imgs)]
        # Always enforce vertical 9:16 studio framing before sending to Fal I2V
        framed_path = output_dir / f"broll_framed_{idx+1}_{run_uid}.jpg"
        try:
            prepare_vertical_9x16_studio_frame(raw_img, framed_path, width=frame_wh[0], height=frame_wh[1])
            img_ref = _file_to_data_uri_or_fal_url(str(framed_path), headers=auth_headers)
        except Exception:
            img_ref = raw_img if raw_img.startswith("http") else _file_to_data_uri_or_fal_url(raw_img, headers=auth_headers)

        res: Optional[Dict[str, Any]] = None
        used_model = broll_models[0]
        last_err: Optional[Exception] = None
        for m_idx, mid in enumerate(broll_models):
            payload = _build_broll_payload(
                model_id=mid,
                resolution=broll_res,
                duration_sec=broll_dur,
                img_ref=img_ref,
                prompt=prompts[idx % len(prompts)],
            )
            try:
                logger.info(
                    f"Submitting Fal.ai Queue Shared B-Roll #{idx+1}/{target_count} "
                    f"[{tier_id} | {mid} | {broll_res} | {payload.get('duration')}s]..."
                )
                res = submit_fal_queue(mid, payload, timeout_sec=210)
                used_model = mid
                break
            except CreativeBalanceError:
                raise
            except Exception as exc:
                last_err = exc
                if m_idx + 1 < len(broll_models):
                    logger.warning(f"Fal B-Roll model '{mid}' xətası ({exc}); fallback modelə keçilir...")
                    continue
                raise
        if not res:
            raise RuntimeError(f"Fal.ai B-Roll generasiyası uğursuz oldu: {last_err}")

        video_url = _extract_fal_video_url(res)
        if not video_url:
            raise RuntimeError("Fal.ai B-Roll cavabında video URL yoxdur.")
        dest = output_dir / f"shared_broll_{idx+1}_{run_uid}.mp4"
        _download_mp4_binary(video_url, dest)
        actual_dur = _probe_media_duration(dest) or float(broll_dur)
        eff_res = "512p" if ("hailuo" in used_model and broll_res == "480p") else broll_res
        rate = get_fal_rate_per_sec(used_model, eff_res)
        clip_cost = round(actual_dur * rate, 3)
        total_broll_cost = round(total_broll_cost + clip_cost, 3)

        output_paths.append(str(dest))
        clips_meta.append({
            "file_path": str(dest),
            "model": used_model,
            "resolution": eff_res,
            "duration_sec": actual_dur,
            "rate_per_sec": rate,
            "cost_usd": clip_cost,
        })
        logger.info(f"✅ Shared AI B-Roll #{idx+1}/{target_count} hazır oldu: {dest.name} ({used_model} | {actual_dur:.2f}s ≈ ${clip_cost:.3f})")

    if return_details:
        return {
            "clip_paths": output_paths,
            "clips_meta": clips_meta,
            "total_cost_usd": round(total_broll_cost, 3),
        }
    return output_paths


def compute_actual_creative_cost(
    quality_tier: str,
    lipsync_records: List[Dict[str, Any]],
    broll_records: List[Dict[str, Any]],
    scene_gen_cost_usd: float = 0.0,
    elevenlabs_char_count: int = 0,
) -> Dict[str, Any]:
    """Summarize exact post-run costs based on actual measured clip durations and generated assets."""
    cfg = QUALITY_TIERS[normalize_quality_tier(quality_tier)]
    lipsync_usd = round(sum(float(r.get("cost_usd") or 0.0) for r in lipsync_records), 3)
    broll_usd = round(sum(float(r.get("cost_usd") or 0.0) for r in broll_records), 3)
    scene_usd = round(float(scene_gen_cost_usd or 0.0), 3)
    fal_total = round(lipsync_usd + broll_usd + scene_usd, 2)

    # Approximate ElevenLabs cost (~$0.12-$0.24 / 1K chars depending on Flash vs v2/v3)
    el_per_1k = 0.06 if cfg["elevenlabs_model"] == "eleven_flash_v2_5" else 0.12
    el_usd = round(max((elevenlabs_char_count / 1000.0) * el_per_1k, 0.01), 3)
    grand_total = round(fal_total + el_usd, 2)

    return {
        "actual_cost_usd": grand_total,
        "actual_fal_usd": fal_total,
        "actual_elevenlabs_usd": round(el_usd, 2),
        "breakdown": {
            "fal_lipsync_hooks_usd": round(lipsync_usd, 2),
            "fal_shared_ai_broll_usd": round(broll_usd, 2),
            "fal_scene_gen_usd": round(scene_usd, 2),
            "elevenlabs_voice_timestamps_usd": round(el_usd, 2),
        },
    }

