"""
E-Commerce Multi-Agent System — Web Dashboard (FastAPI)
========================================================

Serves a web-based control panel for managing the AI e-commerce pipeline.

Usage:
    python webapp.py

Then open http://localhost:8000 in your browser.
"""

import sys
import uuid
import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── ensure project root is on sys.path ──
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, BackgroundTasks, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from dotenv import load_dotenv
from langgraph.types import Command

from config.settings import get_settings, Settings
from workflows.pipeline import build_pipeline
from main import create_gemini_client, build_tools_map, make_initial_state

load_dotenv()

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("webapp")


# ══════════════════════════════ FastAPI App ════════════════════════════════════

app = FastAPI(
    title="AI E-Ticarət Sistemi",
    description="Süni İntellekt ilə Avtomatlaşdırılmış E-Ticarət İdarəetmə Paneli",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.staticfiles import StaticFiles
static_dir = PROJECT_ROOT / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

ad_video_dir = PROJECT_ROOT / "ad_video"
ad_video_dir.mkdir(parents=True, exist_ok=True)
app.mount("/ad_video", StaticFiles(directory=str(ad_video_dir)), name="ad_video")

# ══════════════════════════════ Global State ═══════════════════════════════════

pipeline_state: Dict[str, Any] = {
    "is_running": False,
    "current_phase": "",
    "current_stage": "",       # for pipeline progress visualization
    "status": "idle",          # idle | running | pending_approval | completed | error
    "pending_approval": None,  # {step, message} when waiting for human decision
    "message": "",             # approval message text
    "results": {},             # final state snapshot
    "logs": [],                # list of log strings
    "error": None,
    "started_at": None,
    "completed_at": None,
}

# ── Synchronization primitives ──
approval_event = asyncio.Event()
approval_decision: Optional[Any] = None
abort_pipeline: bool = False
current_pipeline_task: Optional[asyncio.Task] = None
log_queues: List[asyncio.Queue] = []

# ── Phase → stage name mapping for the frontend progress bar ──
PHASE_TO_STAGE = {
    "reset_product_cycle": "research",
    "product_research": "research",
    "human_approval_product": "research",
    "market_research": "research",
    "sourcing": "sourcing",
    "human_approval_supplier": "sourcing",
    "human_approval_store_config": "store",
    "store_build": "store",
    "human_approval_store": "store",
    "human_approval_stage_one": "store",
    "human_approval_video_sources": "creative",
    "parallel_fanout_creative_social": "creative",
    "creative_production": "creative",
    "social_media_setup": "creative",
    "fan_in_creative_social": "creative",
    "human_approval_creatives": "creative",
    "media_buying": "ads",
    "monitoring": "monitoring",
}


def _add_log(msg: str, level: str = "info"):
    """Append a log entry and broadcast to all SSE listeners."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_entry = {
        "timestamp": timestamp,
        "message": msg,
        "level": level,
    }
    pipeline_state["logs"].append(log_entry)
    logger.info(f"[{timestamp}] {msg}")
    # Push to all SSE queues
    for q in log_queues:
        try:
            q.put_nowait(json.dumps(log_entry, ensure_ascii=False))
        except asyncio.QueueFull:
            pass


# ══════════════════════════════ Request Models ════════════════════════════════

class StartRequest(BaseModel):
    model: str = "gemini-3.5-flash-lite"
    category: str = "all"
    auto_approve: bool = False
    thread_id: str = "web_run_1"
    user_id: Optional[str] = "default_user"


class ApproveRequest(BaseModel):
    decision: str  # "approved", "rejected", "add_another_product", etc.
    manual_link: Optional[str] = None
    backup_link: Optional[str] = None
    auto_failover: Optional[bool] = True
    video_url_1: Optional[str] = None
    video_url_2: Optional[str] = None
    avatar_id: Optional[str] = None
    custom_avatar_url: Optional[str] = None
    broll_mode: Optional[str] = None
    custom_script: Optional[str] = None
    ad_language: Optional[str] = "en"
    quality_tier: Optional[str] = "balanced"
    variant_count: Optional[int] = 3
    broll_image_urls: Optional[List[str]] = None
    store_config: Optional[Dict[str, Any]] = None
    selected_catalog_id: Optional[str] = None
    feedback: Optional[str] = None
    thread_id: str = "web_run_1"


class SocialGenerateRequest(BaseModel):
    product_name: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    model: Optional[str] = "gemini-3.5-flash-lite"


class RadarEmailRequest(BaseModel):
    email: str


class RadarSubscribeRequest(BaseModel):
    email: str
    category: Optional[str] = "all"


class RadarScanRequest(BaseModel):
    source: Optional[str] = "all"  # "shopify" | "amazon" | "all"
    top_n: Optional[int] = 20


# ══════════════════════════════ Pipeline Runner ═══════════════════════════════

async def _run_pipeline_task(
    model: str,
    thread_id: str,
    auto_approve: bool,
    category: str = "all",
    user_id: str = "default_user",
):
    """
    Run the full LangGraph pipeline as a background task.
    Handles human-in-the-loop interrupts via asyncio.Event.
    """
    global approval_decision, abort_pipeline

    try:
        abort_pipeline = False
        # Reset state
        pipeline_state.update({
            "is_running": True,
            "status": "running",
            "current_phase": "product_research",
            "current_stage": "research",
            "pending_approval": None,
            "message": "",
            "results": {},
            "logs": [],
            "error": None,
            "started_at": datetime.now().isoformat(),
            "completed_at": None,
        })

        _add_log(f"🚀 Pipeline başladılır — Model: {model}, Kateqoriya: {category}, User: {user_id}", "info")

        # ── Initialize Gemini client ──
        settings = get_settings()
        try:
            client = create_gemini_client(settings)
            _add_log("✅ Gemini client yaradıldı", "success")
        except Exception as e:
            _add_log(f"❌ Gemini client xətası: {e}", "error")
            pipeline_state["error"] = str(e)
            pipeline_state["status"] = "error"
            return

        # ── Build tools and pipeline ──
        tools_map = build_tools_map()
        _add_log(f"✅ {sum(len(v) for v in tools_map.values())} alət yükləndi", "success")

        compiled_app = build_pipeline(
            client=client,
            model=model,
            tools_map=tools_map,
            settings=settings,
        )
        _add_log("✅ Pipeline qrafı kompilyasiya edildi", "success")

        # ── Run pipeline with interrupt handling ──
        config = {"configurable": {"thread_id": thread_id}}
        initial_state = make_initial_state()
        initial_state["category"] = category
        initial_state["user_id"] = (user_id or "default_user").strip() or "default_user"
        current_input: Any = initial_state

        while True:
            interrupted = False

            try:
                async for event in compiled_app.astream(
                    current_input, config, stream_mode="updates"
                ):
                    if abort_pipeline:
                        _add_log("🛑 Proses səhifə yenilənməsinə görə dayandırıldı", "warn")
                        raise Exception("Pipeline aborted by page reload.")
                        
                    for node_name, update in event.items():
                        if node_name == "__interrupt__":
                            interrupted = True

                            # Correctly unpack tuple or list of Interrupt objects
                            raw_items = list(update) if isinstance(update, (list, tuple)) else [update]
                            for item in raw_items:
                                val = getattr(item, "value", item)
                                if isinstance(val, dict):
                                    msg = val.get("message", "")
                                    step = val.get("step", "product_approval")
                                    product = val.get("product")
                                    products = val.get("products")  # 3-product list
                                    supplier = val.get("supplier")
                                    suppliers = val.get("suppliers")  # Top-3 supplier list
                                    unit_economics = val.get("unit_economics")
                                    store = val.get("store")
                                    creatives = val.get("creatives")
                                    brand = val.get("brand")
                                    store_config = val.get("store_config")
                                    recommended_design = val.get("recommended_design")
                                    design_presets = val.get("design_presets")
                                    font_pairs = val.get("font_pairs")
                                    suggested_images = val.get("suggested_images")
                                    winning_catalog = val.get("winning_catalog")
                                    active_catalog_id = val.get("active_catalog_id")
                                    latest_item = val.get("latest_item")
                                    supply_summary = val.get("supply_summary")
                                    social_media = val.get("social_media")
                                    content_plan = val.get("content_plan")
                                    preflight_status = val.get("preflight_status")
                                    creative_assets = val.get("creative_assets")
                                    avatar_presets = val.get("avatar_presets")
                                    gallery_images = val.get("gallery_images") or []
                                else:
                                    msg = str(val)
                                    step = "unknown"
                                    product = None
                                    products = None
                                    supplier = None
                                    suppliers = None
                                    unit_economics = None
                                    store = None
                                    creatives = None
                                    brand = None
                                    store_config = None
                                    recommended_design = None
                                    design_presets = None
                                    font_pairs = None
                                    suggested_images = None
                                    winning_catalog = None
                                    active_catalog_id = None
                                    latest_item = None
                                    supply_summary = None
                                    social_media = None
                                    content_plan = None
                                    preflight_status = None
                                    creative_assets = None
                                    avatar_presets = None
                                    gallery_images = None

                                # Safety net: only for product_approval step, grab products list from state
                                if step == "product_approval" and not products:
                                    try:
                                        snap = await compiled_app.aget_state(config)
                                        if snap and hasattr(snap, "values"):
                                            products = snap.values.get("products")
                                            if not product:
                                                product = snap.values.get("selected_product")
                                    except Exception:
                                        pass

                                # Safety net for supplier_approval: grab Top-6 suppliers (AliExpress DS + CJ) from state
                                if step == "supplier_approval" and not suppliers:
                                    try:
                                        snap = await compiled_app.aget_state(config)
                                        if snap and hasattr(snap, "values"):
                                            suppliers = (snap.values.get("suppliers") or [])[:6]
                                            if not supplier:
                                                supplier = snap.values.get("selected_supplier")
                                    except Exception:
                                        pass
                                
                                # Safety net for store_config_approval
                                if step == "store_config_approval":
                                    try:
                                        from tools.design_intel import (
                                            get_store_design_presets,
                                            get_store_font_pairs,
                                            resolve_store_design_system,
                                        )
                                        if not design_presets:
                                            design_presets = get_store_design_presets()
                                        if not font_pairs:
                                            font_pairs = get_store_font_pairs()
                                        snap = await compiled_app.aget_state(config)
                                        if snap and hasattr(snap, "values"):
                                            if not product:
                                                product = snap.values.get("selected_product")
                                            if not supplier:
                                                supplier = snap.values.get("selected_supplier")
                                            if not unit_economics:
                                                unit_economics = snap.values.get("unit_economics")
                                            if not brand:
                                                brand = snap.values.get("brand_identity") or snap.values.get("brand")
                                            if not store_config:
                                                store_config = snap.values.get("store_config")
                                        if not recommended_design:
                                            recommended_design = resolve_store_design_system(
                                                product=product or {},
                                                brand=brand or {},
                                                store_config=store_config if (store_config or {}).get("mode") == "manual" else None,
                                            )
                                    except Exception:
                                        pass

                                # Safety net for store_approval
                                if step == "store_approval" and not store:
                                    try:
                                        snap = await compiled_app.aget_state(config)
                                        if snap and hasattr(snap, "values"):
                                            store = snap.values.get("store")
                                    except Exception:
                                        pass

                                # Safety net for stage_one_decision
                                if step == "stage_one_decision":
                                    try:
                                        snap = await compiled_app.aget_state(config)
                                        if snap and hasattr(snap, "values"):
                                            if not winning_catalog:
                                                winning_catalog = snap.values.get("winning_catalog") or []
                                            if not active_catalog_id:
                                                active_catalog_id = snap.values.get("active_catalog_id")
                                            if not product:
                                                product = snap.values.get("selected_product")
                                    except Exception:
                                        pass

                                # Safety net for video_source_approval
                                if step == "video_source_approval" and not product:
                                    try:
                                        snap = await compiled_app.aget_state(config)
                                        if snap and hasattr(snap, "values"):
                                            product = snap.values.get("selected_product")
                                            if not supplier:
                                                supplier = snap.values.get("selected_supplier")
                                    except Exception:
                                        pass

                                # Safety net for creatives_approval (Agent 4 + Agent 5 parallel)
                                if step == "creatives_approval":
                                    try:
                                        snap = await compiled_app.aget_state(config)
                                        if snap and hasattr(snap, "values"):
                                            if not creatives:
                                                creatives = snap.values.get("creatives")
                                            if not creative_assets:
                                                creative_assets = snap.values.get("creative_assets")
                                            if not brand:
                                                brand = snap.values.get("brand")
                                            if not product:
                                                product = snap.values.get("selected_product")
                                            if not content_plan:
                                                content_plan = snap.values.get("content_plan") or []
                                            if not social_media:
                                                social_media = snap.values.get("social_media") or {
                                                    "brand": brand,
                                                    "content_plan": content_plan,
                                                    "postiz": snap.values.get("postiz", {}),
                                                }
                                    except Exception:
                                        pass

                                _add_log(f"⏸️ Təsdiq gözlənilir: {step}", "warn")

                                pipeline_state.update({
                                    "status": "pending_approval",
                                    "pending_approval": {
                                        "step": step,
                                        "message": msg,
                                        "product": product,
                                        "products": products if step == "product_approval" else None,
                                        "supplier": supplier,
                                        "suppliers": suppliers if step == "supplier_approval" else None,
                                        "unit_economics": unit_economics,
                                        "store": store,
                                        "creatives": creatives,
                                        "creative_assets": creative_assets,
                                        "preflight_status": preflight_status,
                                        "avatar_presets": avatar_presets,
                                        "gallery_images": gallery_images,
                                        "brand": brand,
                                        "store_config": store_config,
                                        "recommended_design": recommended_design,
                                        "design_presets": design_presets,
                                        "font_pairs": font_pairs,
                                        "suggested_images": suggested_images,
                                        "winning_catalog": winning_catalog,
                                        "active_catalog_id": active_catalog_id,
                                        "latest_item": latest_item,
                                        "supply_summary": supply_summary,
                                        "social_media": social_media,
                                        "content_plan": content_plan,
                                    },
                                    "message": msg,
                                })

                            if auto_approve:
                                _add_log("🤖 Avtomatik təsdiq edildi", "success")
                                current_input = Command(resume="approved")
                            else:
                                # Wait for human decision via /api/pipeline/approve
                                approval_event.clear()
                                await approval_event.wait()

                                decision = approval_decision or "approved"
                                if decision == "abort":
                                    _add_log("🛑 Proses səhifə yenilənməsinə görə dayandırıldı", "warn")
                                    raise Exception("Pipeline aborted by page reload.")
                                
                                _add_log(
                                    f"👤 İnsan qərarı: {decision}", "info"
                                )
                                pipeline_state.update({
                                    "status": "running",
                                    "pending_approval": None,
                                    "message": "",
                                })
                                current_input = Command(resume=decision)

                            break  # restart the astream loop
                        else:
                            # Normal node completion
                            stage = PHASE_TO_STAGE.get(node_name, "")
                            pipeline_state.update({
                                "current_phase": node_name,
                                "current_stage": stage,
                            })

                            if node_name == "reset_product_cycle":
                                preserved_catalog = pipeline_state.get("results", {}).get("winning_catalog", [])
                                pipeline_state["results"] = {"winning_catalog": preserved_catalog}

                            if isinstance(update, dict):
                                if "winning_catalog" in update:
                                    pipeline_state["results"]["winning_catalog"] = update["winning_catalog"]
                                if "market_research_report" in update and update["market_research_report"] is not None:
                                    pipeline_state["results"]["market_research_report"] = update["market_research_report"]
                                if "selected_product" in update and update["selected_product"] is not None:
                                    pipeline_state["results"]["product"] = update["selected_product"]
                                if "selected_supplier" in update and update["selected_supplier"] is not None:
                                    ss = update["selected_supplier"]
                                    ue = update.get("unit_economics") or pipeline_state["results"].get("supplier", {}).get("unit_economics")
                                    pipeline_state["results"]["supplier"] = {**(ss or {}), "unit_economics": ue}
                                if "suppliers" in update and update["suppliers"]:
                                    pipeline_state["results"]["suppliers"] = update["suppliers"][:6]
                                if "bound_sourcing" in update and update["bound_sourcing"] is not None:
                                    pipeline_state["results"]["bound_sourcing"] = update["bound_sourcing"]
                                if "binding_sku" in update and update["binding_sku"]:
                                    pipeline_state["results"]["binding_sku"] = update["binding_sku"]
                                if "store" in update and update["store"] is not None:
                                    pipeline_state["results"]["store"] = update["store"]
                                if "creatives" in update and update["creatives"]:
                                    pipeline_state["results"]["creative"] = update["creatives"]
                                if "creative_assets" in update and update["creative_assets"]:
                                    pipeline_state["results"]["creative_assets"] = update["creative_assets"]
                                if "ad_campaigns" in update and update["ad_campaigns"]:
                                    pipeline_state["results"]["ads"] = update["ad_campaigns"]
                                if "brand" in update and update["brand"] is not None:
                                    pipeline_state["results"]["brand"] = update["brand"]
                                if "social_media" in update and update["social_media"] is not None:
                                    pipeline_state["results"]["social_media"] = update["social_media"]
                                if "content_plan" in update and update["content_plan"]:
                                    pipeline_state["results"]["content_plan"] = update["content_plan"]
                                if "unit_economics" in update and update["unit_economics"] is not None and "supplier" in pipeline_state["results"]:
                                    pipeline_state["results"]["supplier"]["unit_economics"] = update["unit_economics"]

                            node_labels = {
                                "reset_product_cycle": "Yeni Məhsul Dövrü Üçün State Təmizləndi",
                                "product_research": "Məhsul Araşdırması",
                                "market_research": "Bazar Araşdırması",
                                "sourcing": "Avtomatlaşdırılmış Tədarük Konfiqurasiyası",
                                "store_build": "3-cü Agent: WordPress Mağaza Qurulması",
                                "human_approval_stage_one": "1-ci Mərhələ Kataloq Seçimi",
                                "parallel_fanout_creative_social": "4-cü Agent (Kreativ) və 5-ci Agent (Sosial Media) paralel işə salındı",
                                "creative_production": "4-cü Agent: Reklam Kreativləri və Video Montaj",
                                "social_media_setup": "5-ci Agent: Sosial Media və Paylaşım Planı",
                                "fan_in_creative_social": "4-cü və 5-ci Agentlər sinxronlaşdırıldı",
                                "media_buying": "6-cı Agent: Meta Reklam Kampaniyası",
                                "monitoring": "Monitorinq",
                            }
                            friendly_name = node_labels.get(node_name, node_name)
                            _add_log(
                                f"✅ {friendly_name} tamamlandı", "success"
                            )

                    if interrupted:
                        break

            except Exception as stream_err:
                import traceback
                tb = traceback.format_exc()
                logger.error(f"Stream error traceback:\n{tb}")
                _add_log(f"❌ Stream xətası: {stream_err}", "error")
                pipeline_state["error"] = str(stream_err)
                pipeline_state["status"] = "error"
                break

            if not interrupted:
                break

        # ── Collect final results ──
        try:
            snapshot = await compiled_app.aget_state(config)
            final_state = snapshot.values if hasattr(snapshot, "values") else {}

            # Serialize results for JSON
            results = {}
            wc = final_state.get("winning_catalog")
            if isinstance(wc, list) and wc:
                results["winning_catalog"] = wc

            sp = final_state.get("selected_product")
            if isinstance(sp, dict):
                results["product"] = sp

            ss = final_state.get("selected_supplier")
            ue = final_state.get("unit_economics")
            if isinstance(ss, dict):
                results["supplier"] = {**(ss or {}), "unit_economics": ue}
            all_sups = final_state.get("suppliers") or []
            if all_sups:
                results["suppliers"] = all_sups[:6]
            bs = final_state.get("bound_sourcing")
            if isinstance(bs, dict):
                results["bound_sourcing"] = bs
            bsku = final_state.get("binding_sku")
            if bsku:
                results["binding_sku"] = bsku

            st = final_state.get("store")
            if isinstance(st, dict):
                results["store"] = st

            cr = final_state.get("creatives", [])
            if cr:
                results["creative"] = cr
            ca = final_state.get("creative_assets")
            if isinstance(ca, dict) and ca:
                results["creative_assets"] = ca

            ac = final_state.get("ad_campaigns", [])
            if ac:
                results["ads"] = ac

            brand = final_state.get("brand")
            if isinstance(brand, dict):
                results["brand"] = brand

            mr = final_state.get("market_research_report")
            if mr:
                results["market_research_report"] = mr

            sm = final_state.get("social_media") or {}
            if sm or final_state.get("content_plan"):
                results["social_media"] = sm or {
                    "brand": brand,
                    "content_plan": final_state.get("content_plan", []),
                    "postiz": final_state.get("postiz", {}),
                }

            pipeline_state["results"] = results

        except Exception as state_err:
            _add_log(f"⚠️ Final state oxunarkən xəta: {state_err}", "warn")

        if not pipeline_state.get("error"):
            pipeline_state["status"] = "completed"
            pipeline_state["current_stage"] = "monitoring"
            _add_log("🏁 Pipeline uğurla tamamlandı!", "success")
        else:
            pipeline_state["status"] = "error"
            _add_log("❌ Pipeline xəta ilə dayandı", "error")

    except asyncio.CancelledError:
        logger.info("Pipeline task cancelled by user reset.")
        _add_log("🛑 Pipeline prosesi tam dayandırıldı", "warn")
    except Exception as e:
        logger.error(f"Pipeline xətası: {e}", exc_info=True)
        pipeline_state["error"] = str(e)
        pipeline_state["status"] = "error"
        _add_log(f"💥 Gözlənilməz xəta: {e}", "error")

    finally:
        pipeline_state["is_running"] = False
        pipeline_state["completed_at"] = datetime.now().isoformat()


# ══════════════════════════════ API Endpoints ═════════════════════════════════


@app.get("/", response_class=HTMLResponse)
async def serve_landing():
    """Serve the public 3D SaaS landing storefront."""
    landing_path = PROJECT_ROOT / "templates" / "landing.html"
    if landing_path.exists():
        return HTMLResponse(content=landing_path.read_text(encoding="utf-8"))
    return await serve_dashboard(reset=False)


@app.get("/robots.txt")
async def serve_robots():
    """Serve robots.txt for production domain indexing."""
    return Response(
        content="User-agent: *\nAllow: /\nDisallow: /api/\n",
        media_type="text/plain",
    )


@app.get("/app", response_class=HTMLResponse)
async def serve_dashboard(reset: bool = False):
    """Serve the HTML dashboard. Preserves running state unless explicit reset is requested."""
    global approval_decision, abort_pipeline
    
    if reset:
        if pipeline_state["is_running"]:
            abort_pipeline = True
            if not approval_event.is_set():
                approval_decision = "abort"
                approval_event.set()
                
        pipeline_state.clear()
        pipeline_state.update({
            "is_running": False,
            "status": "idle",
            "current_phase": "",
            "current_stage": "",
            "pending_approval": None,
            "message": "",
            "results": {},
            "logs": [],
            "error": None,
            "started_at": None,
            "completed_at": None,
        })
    
    html_path = PROJECT_ROOT / "templates" / "dashboard.html"
    if not html_path.exists():
        return HTMLResponse(
            content="<h1>dashboard.html tapılmadı!</h1>"
            "<p>templates/dashboard.html faylını yaradın.</p>",
            status_code=404,
        )
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.post("/api/pipeline/start")
async def start_pipeline(req: StartRequest):
    """Start a new pipeline run in the background."""
    global current_pipeline_task
    if pipeline_state["is_running"]:
        raise HTTPException(
            status_code=400,
            detail="Pipeline artıq işləyir. Tamamlanmasını gözləyin.",
        )

    effective_user_id = (req.user_id or req.thread_id or "default_user").strip() or "default_user"
    current_pipeline_task = asyncio.create_task(
        _run_pipeline_task(req.model, req.thread_id, req.auto_approve, req.category, effective_user_id)
    )
    return {"message": "Pipeline başladıldı", "thread_id": req.thread_id, "user_id": effective_user_id}


@app.post("/api/pipeline/reset")
async def reset_pipeline():
    """Abort running pipeline and reset all state to idle."""
    global approval_decision, abort_pipeline, current_pipeline_task
    abort_pipeline = True
    if not approval_event.is_set():
        approval_decision = "abort"
        approval_event.set()

    if current_pipeline_task and not current_pipeline_task.done():
        current_pipeline_task.cancel()
        current_pipeline_task = None

    pipeline_state.clear()
    pipeline_state.update({
        "is_running": False,
        "status": "idle",
        "current_phase": "",
        "current_stage": "",
        "pending_approval": None,
        "message": "",
        "results": {},
        "logs": [],
        "error": None,
        "started_at": None,
        "completed_at": None,
    })
    _add_log("🔄 Pipeline sıfırlandı və dayandırıldı", "warn")
    return {"message": "Pipeline sıfırlandı"}


@app.get("/api/social/postiz/status")
async def get_postiz_status_endpoint():
    """Returns self-hosted Postiz server connection status, channels, and queued posts."""
    from tools.postiz_tool import get_postiz_status
    return get_postiz_status()


class PostizConfigRequest(BaseModel):
    api_url: str = "http://localhost:5000"
    api_key: Optional[str] = ""


class PostizChannelConnectRequest(BaseModel):
    provider: str = "tiktok"
    account_handle: str = "@magazam_az"
    access_token: Optional[str] = ""


class PostizChannelDisconnectRequest(BaseModel):
    channel_id: str


@app.post("/api/social/postiz/config")
async def update_postiz_config_endpoint(req: PostizConfigRequest):
    """Updates the self-hosted Postiz server URL and API key."""
    from tools.postiz_tool import save_postiz_config
    res = save_postiz_config(req.api_url, req.api_key or "")
    _add_log(f"🔗 Postiz konfiqurasiyası yeniləndi: {res.get('api_url')}", "info")
    return {"success": True, **res}


@app.post("/api/social/postiz/auto-key")
async def auto_provision_postiz_key_endpoint(req: Optional[PostizConfigRequest] = None):
    """1-Click automatic Postiz API key generator & binder."""
    from tools.postiz_tool import auto_provision_postiz_api_key, get_postiz_status
    api_url = req.api_url if req and req.api_url else None
    res = auto_provision_postiz_api_key(api_url)
    _add_log(f"🔑 {res.get('message')}", "success")
    status = get_postiz_status()
    return {**res, "status": status}


@app.post("/api/social/postiz/connect-channel")
async def connect_postiz_channel_endpoint(req: PostizChannelConnectRequest):
    """Directly connects a TikTok, Instagram, Facebook, or YouTube account inside the 5th Agent window."""
    from tools.postiz_tool import connect_social_channel_direct, get_postiz_status
    res = connect_social_channel_direct(
        provider=req.provider,
        account_handle=req.account_handle,
        access_token=req.access_token,
    )
    _add_log(f"📱 {res.get('message')}", "success")
    status = get_postiz_status()
    return {**res, "status": status}


@app.post("/api/social/postiz/disconnect-channel")
async def disconnect_postiz_channel_endpoint(req: PostizChannelDisconnectRequest):
    """Disconnects a social channel by ID."""
    from tools.postiz_tool import disconnect_social_channel, get_postiz_status
    res = disconnect_social_channel(req.channel_id)
    _add_log(f"🔌 {res.get('message')}", "info")
    status = get_postiz_status()
    return {**res, "status": status}


@app.post("/api/social/postiz/schedule")
async def schedule_postiz_posts():
    """Pushes the current 7-day social media plan (and rendered UGC video) into Postiz queue."""
    from tools.postiz_tool import schedule_content_plan_to_postiz
    source = pipeline_state.get("results") or {}
    sm = source.get("social_media") or {}
    content_plan = sm.get("content_plan") or source.get("content_plan") or []
    if not content_plan:
        raise HTTPException(status_code=400, detail="Hələ heç bir sosial media planı generasiya edilməyib.")

    prod = source.get("product") or {}
    creatives = source.get("creatives") or []
    video_url = None
    if creatives and isinstance(creatives, list):
        for c in creatives:
            if isinstance(c, dict) and c.get("video_url"):
                video_url = c["video_url"]
                break

    image_url = prod.get("image_url")
    res = schedule_content_plan_to_postiz(
        content_plan=content_plan,
        image_url=image_url,
        video_url=video_url,
        product_name=prod.get("name", "Seçilmiş Məhsul"),
    )
    if res.get("success"):
        _add_log(f"🚀 Postiz: {res.get('message')}", "success")
    else:
        _add_log(f"⚠️ Postiz: {res.get('message')}", "warn")
    return res


@app.post("/api/social/generate")
async def generate_social_media(req: Optional[SocialGenerateRequest] = None):
    """Run 5th Agent (Social Media) independently as an assistant agent on demand."""
    try:
        from main import create_gemini_client
        from agents.social_media import SocialMediaAgent

        settings = get_settings()
        client = create_gemini_client(settings)
        model_name = (req.model if req and req.model else "gemini-3.8-flash")
        agent = SocialMediaAgent(client=client, model=model_name)

        # Determine product to use
        results = pipeline_state.get("results") or {}
        prod = dict(results.get("product") or {})

        if req and req.product_name and req.product_name.strip():
            prod["name"] = req.product_name.strip()
            if req.description:
                prod["description"] = req.description.strip()
            if req.image_url:
                prod["image_url"] = req.image_url.strip()

        if not prod.get("name"):
            # Check pending approval for product
            pa = pipeline_state.get("pending_approval") or {}
            pa_prod = pa.get("product") or {}
            if isinstance(pa_prod, dict) and pa_prod.get("name"):
                prod = dict(pa_prod)

        if not prod.get("name"):
            raise HTTPException(
                status_code=400,
                detail="Məhsul tapılmadı. Əvvəlcə pipeline-da məhsul seçin və ya köməkçi pəncərədə məhsul adı daxil edin."
            )

        simulated_state = {
            "selected_product": prod,
            "selected_supplier": results.get("supplier") or {},
            "creatives": results.get("creative") or [],
            "store": results.get("store") or {},
            "logs": []
        }

        _add_log(f"📱 5-ci Köməkçi Agent (Sosial Media): '{prod.get('name')}' üçün brend və 7 günlük cədvəl tərtib edilir...", "info")
        res = await agent.run(simulated_state)

        # Save to pipeline_state results
        if not pipeline_state.get("results"):
            pipeline_state["results"] = {}
        if "social_media" in res:
            pipeline_state["results"]["social_media"] = res["social_media"]
        if "brand" in res:
            pipeline_state["results"]["brand"] = res["brand"]
        if "content_plan" in res:
            pipeline_state["results"]["content_plan"] = res["content_plan"]

        _add_log("✅ 5-ci Köməkçi Agent: Sosial media və brend planı uğurla hazırlandı!", "success")
        return {"success": True, "data": res}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in social generate: {e}")
        _add_log(f"❌ Sosial media köməkçi agentində xəta: {e}", "error")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/export/shopify")
async def export_shopify_csv():
    """Generates a Shopify-compatible CSV file for the selected product."""
    source = pipeline_state.get("results") or pipeline_state.get("pending_approval")
    if not source:
        raise HTTPException(status_code=400, detail="No pipeline results available for export.")
        
    store = source.get("store") or {}
    copy = store.get("copy") or {}
    prod = source.get("product") or {}
    sup = source.get("supplier") or {}
    ue = source.get("unit_economics") or sup.get("unit_economics") or {}
    
    title = copy.get("product_title") or prod.get("name") or "Yeni Məhsul"
    
    # Translate title to English using Gemini
    try:
        from main import create_gemini_client
        settings = get_settings()
        client = create_gemini_client(settings)
        resp = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=f"Translate this product name to English. ONLY output the English product name, nothing else: '{title}'"
        )
        if resp.text:
            title = resp.text.strip().replace('"', '').replace("'", "")
    except Exception as e:
        logger.error(f"Error translating product title: {e}")

    # Clean the handle properly for Shopify (only lowercase alphanumeric and single dashes)
    import re
    raw_handle = title.lower()
    clean_handle = re.sub(r'[^a-z0-9]+', '-', raw_handle).strip('-')
    handle = clean_handle if clean_handle else "product"

    # Clean HTML body (Remove raw AIDA labels if they exist)
    raw_html = copy.get('description_aida', '')
    for label in ["Attention:", "Interest:", "Desire:", "Action:", "Diqqət:", "Maraq:", "İstək:", "Hərəkət:"]:
        raw_html = raw_html.replace(label, "").replace(f"**{label}**", "")
    
    body_html = f"<p>{raw_html}</p>".replace("\n", "<br>")
    if copy.get("faqs"):
        body_html += "<h3>Tez-Tez Verilən Suallar</h3><ul>"
        for faq in copy["faqs"]:
            body_html += f"<li><strong>{faq.get('question')}</strong><br>{faq.get('answer')}</li>"
        body_html += "</ul>"
        
    vendor = "My Store"
    prod_type = "Viral Product"
    tags = "viral, trending"
    sku = sup.get("product_sku", "SKU-001")
    price = str(ue.get("selling_price", 0))
    compare_price = str(round(float(price) * 1.5, 2)) if ue.get("selling_price") else ""
    cost = str(ue.get("product_cost", 0))
    img_url = sup.get("product_image_url") or prod.get("image_url") or ""
    
    # CSV Header
    header = "Handle,Title,Body (HTML),Vendor,Type,Tags,Published,Option1 Name,Option1 Value,Variant SKU,Variant Inventory Tracker,Variant Inventory Qty,Variant Inventory Policy,Variant Fulfillment Service,Variant Price,Variant Compare At Price,Variant Requires Shipping,Variant Taxable,Image Src,Image Position,Cost per item,Status"
    
    # Simple CSV escape helper
    def csv_esc(val):
        s = str(val).replace('"', '""')
        return f'"{s}"' if ',' in s or '"' in s or '\n' in s else s

    row = [
        csv_esc(handle),
        csv_esc(title),
        csv_esc(body_html),
        csv_esc(vendor),
        csv_esc(prod_type),
        csv_esc(tags),
        "TRUE",
        "Title",
        "Default Title",
        csv_esc(sku),
        "shopify",  # Variant Inventory Tracker MUST be 'shopify' if qty is set
        "100", # Variant Inventory Qty
        "deny",
        "manual",
        price,
        compare_price,
        "TRUE",
        "TRUE",
        img_url,
        "1",
        cost,
        "active"
    ]
    
    csv_content = header + "\n" + ",".join(row)
    
    from fastapi.responses import PlainTextResponse
    
    # Clean handle for safe filename (remove non-ascii)
    safe_handle = "".join(c for c in handle if c.isalnum() or c == "-").strip("-")
    if not safe_handle:
        safe_handle = "product"

    return PlainTextResponse(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="shopify_{safe_handle}.csv"'}
    )


def _generate_latest_wordpress_html(source: Dict[str, Any]) -> tuple[str, str]:
    """Builds or retrieves the latest professional WordPress E-Commerce HTML template and safe slug."""
    store = source.get("store") or {}
    copy_data = store.get("copy") or source.get("store_copy") or {}
    prod = source.get("product") or source.get("selected_product") or {}
    sup = source.get("supplier") or source.get("selected_supplier") or {}
    if sup and not prod.get("image_url") and sup.get("product_image_url"):
        prod = dict(prod)
        prod["image_url"] = sup.get("product_image_url")
    ue = source.get("unit_economics") or sup.get("unit_economics") or {}
    brand = source.get("brand") or source.get("brand_identity") or {}

    ds = store.get("design_system") or copy_data.get("design_system")
    cfg = store.get("store_config") or copy_data.get("store_config") or source.get("store_config")
    from agents.store_builder import build_wordpress_landing_page
    html_content = build_wordpress_landing_page(copy_data, prod, ue, brand=brand, design_system=ds, store_config=cfg)

    raw_title = copy_data.get("product_title") or prod.get("name") or "ecommerce_store"
    safe_handle = "".join(c if c.isalnum() else "_" for c in str(raw_title).lower()).strip("_")[:40] or "ecommerce_store"
    return html_content, safe_handle


@app.get("/api/preview/wordpress")
async def preview_wordpress_landing_page():
    """Renders the generated WordPress / WooCommerce HTML e-commerce site in browser."""
    source = pipeline_state.get("results") or pipeline_state.get("pending_approval")
    if not source:
        raise HTTPException(status_code=400, detail="Heç bir aktiv mağaza nəticəsi tapılmadı.")

    html_content, _ = _generate_latest_wordpress_html(source)
    return HTMLResponse(content=html_content)


@app.get("/api/export/wordpress")
async def export_wordpress_html():
    """Downloads the generated WordPress e-commerce landing page as a ready-to-use .html file."""
    source = pipeline_state.get("results") or pipeline_state.get("pending_approval")
    if not source:
        raise HTTPException(status_code=400, detail="Heç bir aktiv mağaza nəticəsi tapılmadı.")

    html_content, safe_handle = _generate_latest_wordpress_html(source)

    from fastapi.responses import Response
    return Response(
        content=html_content.encode("utf-8"),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="wordpress_{safe_handle}.html"'}
    )


@app.get("/api/pipeline/status")
async def get_status():
    """Get the current pipeline status for UI polling."""
    return {
        "is_running": pipeline_state["is_running"],
        "status": pipeline_state["status"],
        "current_phase": pipeline_state["current_phase"],
        "current_stage": pipeline_state["current_stage"],
        "pending_approval": pipeline_state["pending_approval"],
        "message": pipeline_state.get("message", ""),
        "error": pipeline_state["error"],
        "started_at": pipeline_state["started_at"],
        "completed_at": pipeline_state["completed_at"],
        "results": pipeline_state.get("results", {}),
        "logs": pipeline_state.get("logs", []),
    }


@app.post("/api/store/upload-asset")
async def upload_store_asset(file: UploadFile = File(...)):
    """Upload a product image or logo file locally for Agent 03 Store Builder."""
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}
    orig_name = Path(file.filename or "asset.png").name
    ext = Path(orig_name).suffix.lower()
    if ext not in allowed_exts:
        content_type = (file.content_type or "").lower()
        ct_map = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/svg+xml": ".svg",
        }
        ext = ct_map.get(content_type, "")
    if ext not in allowed_exts:
        raise HTTPException(
            status_code=400,
            detail="Yalnız şəkil faylları (.jpg, .png, .webp, .gif, .svg) yüklənə bilər.",
        )

    raw_bytes = await file.read()
    max_bytes = 5 * 1024 * 1024
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Fayl boşdur.")
    if len(raw_bytes) > max_bytes:
        raise HTTPException(status_code=400, detail="Şəkil faylının ölçüsü 5 MB-dan çox ola bilməz.")

    assets_dir = PROJECT_ROOT / "static" / "media" / "store_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    unique_name = f"asset_{uuid.uuid4().hex[:12]}{ext}"
    dest = assets_dir / unique_name
    dest.write_bytes(raw_bytes)

    return {
        "url": f"/static/media/store_assets/{unique_name}",
        "filename": unique_name,
        "size": len(raw_bytes),
    }


@app.get("/api/catalog")
async def list_winning_catalog():
    """Return all saved 'uğurlu məhsul' entries from catalog storage."""
    from tools.catalog_store import load_catalog
    items = load_catalog()
    compact = []
    for cur in items:
        prod = cur.get("product") or {}
        ue = cur.get("unit_economics") or {}
        st = cur.get("store") or {}
        copy_d = st.get("copy") or {}
        compact.append({
            "id": cur.get("id"),
            "created_at": cur.get("created_at"),
            "status": cur.get("status", "winning_product"),
            "brand_name": cur.get("brand_name") or prod.get("name", ""),
            "product": {
                "name": prod.get("name", ""),
                "image_url": prod.get("image_url", ""),
                "what_it_does": prod.get("what_it_does", ""),
            },
            "unit_economics": {
                "product_cost": ue.get("product_cost"),
                "selling_price": (cur.get("store_config") or {}).get("selling_price") or ue.get("selling_price"),
                "net_profit": ue.get("net_profit"),
            },
            "store_title": copy_d.get("product_title") or prod.get("name", ""),
            "ux_audit": st.get("ux_audit"),
            "supply_summary": cur.get("supply_summary") or {
                "configured": True,
                "label": "Avtomatlaşdırılmış tədarük konfiqurasiya edildi",
            },
        })
    return {"items": compact, "count": len(compact)}


@app.get("/api/catalog/{item_id}/preview")
async def preview_catalog_store(item_id: str):
    """Render the saved WordPress storefront HTML for a winning catalog item."""
    from tools.catalog_store import get_catalog_item
    item = get_catalog_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Kataloq məhsulu tapılmadı.")
    html_content, _ = _generate_latest_wordpress_html(item)
    return HTMLResponse(content=html_content)


@app.delete("/api/catalog/{item_id}")
async def remove_catalog_item(item_id: str):
    """Delete an entry from the saved winning products catalog."""
    from tools.catalog_store import delete_catalog_item
    deleted = delete_catalog_item(item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Kataloq məhsulu tapılmadı.")
    return {"deleted": True, "id": item_id}


@app.post("/api/pipeline/approve")
async def approve_pipeline(req: ApproveRequest):
    """Submit a human approval decision."""
    global approval_decision

    pending = pipeline_state.get("pending_approval")
    if not pending:
        raise HTTPException(
            status_code=400,
            detail="Hal-hazırda təsdiq gözlənilmir.",
        )

    valid_decision = (
        req.decision.startswith("approved")
        or req.decision.startswith("research")
        or req.decision.startswith("search_1688")
        or req.decision in ("rejected", "add_another_product")
    )
    if not valid_decision:
        raise HTTPException(
            status_code=400,
            detail="Qərar 'approved', 'research', 'search_1688', 'add_another_product' və ya 'rejected' olmalıdır.",
        )

    if req.decision.startswith("approved") and pending.get("step") == "store_config_approval":
        cfg = req.store_config if isinstance(req.store_config, dict) else {"mode": "auto"}
        mode = str(cfg.get("mode") or "auto").strip().lower()
        if mode == "manual":
            clean_images = [
                str(u).strip() for u in (cfg.get("product_images") or []) if isinstance(u, str) and str(u).strip()
            ]
            if len(clean_images) < 4:
                raise HTTPException(
                    status_code=400,
                    detail="Manual seçimdə minimum 4 ədəd məhsul şəkli daxil edilməlidir.",
                )
            if cfg.get("selling_price") not in (None, ""):
                try:
                    sp = float(cfg["selling_price"])
                    if sp <= 0:
                        raise HTTPException(status_code=400, detail="Məhsulun satış qiyməti 0-dan böyük olmalıdır.")
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail="Məhsulun satış qiyməti düzgün rəqəm olmalıdır.")
        req.store_config = cfg

    if req.decision == "add_another_product":
        approval_decision = {"decision": "add_another_product"}
    elif req.decision.startswith("approved") or req.decision.startswith("research") or req.decision.startswith("search_1688"):
        decision_dict: Dict[str, Any] = {"decision": req.decision}
        if req.manual_link:
            decision_dict["manual_link"] = req.manual_link
        if req.backup_link:
            decision_dict["backup_link"] = req.backup_link
        if req.auto_failover is not None:
            decision_dict["auto_failover"] = bool(req.auto_failover)
        if req.video_url_1:
            decision_dict["video_url_1"] = req.video_url_1
        if req.video_url_2:
            decision_dict["video_url_2"] = req.video_url_2
        if req.avatar_id:
            decision_dict["avatar_id"] = req.avatar_id
        if req.custom_avatar_url:
            decision_dict["custom_avatar_url"] = req.custom_avatar_url
        if req.broll_mode:
            decision_dict["broll_mode"] = req.broll_mode
        if req.custom_script:
            decision_dict["custom_script"] = req.custom_script
        if req.ad_language:
            decision_dict["ad_language"] = req.ad_language
        if req.quality_tier:
            decision_dict["quality_tier"] = req.quality_tier
        if req.variant_count is not None:
            decision_dict["variant_count"] = int(req.variant_count)
        if req.broll_image_urls:
            decision_dict["broll_image_urls"] = [
                str(u).strip() for u in req.broll_image_urls if isinstance(u, str) and str(u).strip()
            ]
        if req.store_config is not None:
            decision_dict["store_config"] = req.store_config
        if req.selected_catalog_id:
            decision_dict["selected_catalog_id"] = req.selected_catalog_id
        if req.feedback and req.feedback.strip():
            decision_dict["feedback"] = req.feedback.strip()

        approval_decision = decision_dict if len(decision_dict) > 1 else req.decision
    else:
        if req.feedback and req.feedback.strip():
            approval_decision = {"decision": req.decision, "feedback": req.feedback.strip()}
        else:
            approval_decision = req.decision

    approval_event.set()
    return {"message": f"Qərar qəbul edildi: {req.decision}"}


@app.get("/api/creative/preflight")
async def api_creative_preflight(
    quality_tier: str = "balanced",
    variant_count: int = 3,
    has_supplier_video: bool = False,
    broll_mode: str = "auto",
    language: str = "en",
    ad_language: Optional[str] = None,
    image_count: int = 3,
):
    """Returns real-time ElevenLabs quota, FAL_KEY status, localized avatar presets, and exact $/sec unit-cost estimates."""
    from tools.creative_tools import get_ugc_avatar_presets
    from tools.fal_video_engine import check_creative_preflight

    eff_lang = (ad_language or language or "en").strip().lower()
    data = await asyncio.to_thread(
        check_creative_preflight,
        quality_tier,
        variant_count,
        has_supplier_video,
        broll_mode,
        False,
        image_count,
    )
    data["avatar_presets"] = get_ugc_avatar_presets(language=eff_lang)
    return data


class RetryVariantRequest(BaseModel):
    run_uid: str
    variant_index: int


@app.post("/api/creative/retry-variant")
async def api_creative_retry_variant(req: RetryVariantRequest):
    """Retry ONLY a single failed creative variant using the run's cached shared assets ($0 shared B-Roll/audio)."""
    from agents.creative import retry_cached_creative_variant

    try:
        rebuilt = await retry_cached_creative_variant(req.run_uid.strip(), int(req.variant_index))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Variant yenidən cəhd uğursuz oldu: {exc}")

    # Update live pending_approval & results state so UI immediately reflects the recovered variant
    pa = pipeline_state.get("pending_approval")
    if isinstance(pa, dict) and isinstance(pa.get("creatives"), list):
        creatives_list = pa["creatives"]
        if 0 <= req.variant_index < len(creatives_list):
            creatives_list[req.variant_index] = rebuilt
        ca = pa.get("creative_assets")
        if isinstance(ca, dict) and isinstance(ca.get("ad_scripts"), list) and 0 <= req.variant_index < len(ca["ad_scripts"]):
            ca["ad_scripts"][req.variant_index] = rebuilt
    res_creatives = pipeline_state.get("results", {}).get("creatives")
    if isinstance(res_creatives, list) and 0 <= req.variant_index < len(res_creatives):
        res_creatives[req.variant_index] = rebuilt

    return {"status": "success", "variant": rebuilt}


@app.post("/api/creative/upload-avatar")
async def api_creative_upload_avatar(file: UploadFile = File(...)):
    """Upload a custom realistic human portrait image for UGC talking-head lip-sync."""
    raw = await file.read()
    if not raw or len(raw) < 1000:
        raise HTTPException(status_code=400, detail="Şəkil faylı boşdur və ya çox kiçikdir.")
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Şəkil ölçüsü 12 MB-dan böyük ola bilməz.")
    ext = Path(file.filename or "avatar.jpg").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    avatars_dir = Path("static") / "media" / "avatars"
    avatars_dir.mkdir(parents=True, exist_ok=True)
    fname = f"upload_avatar_{uuid.uuid4().hex[:8]}{ext}"
    dest = avatars_dir / fname
    dest.write_bytes(raw)
    return {
        "status": "success",
        "filename": fname,
        "url": f"/static/media/avatars/{fname}",
    }


class GenerateAvatarRequest(BaseModel):
    gender: str = "female"
    niche: str = "lifestyle"
    age_group: str = "25-32"
    style_prompt: str = ""


@app.post("/api/creative/generate-avatar")
async def api_creative_generate_avatar(req: GenerateAvatarRequest):
    """Generate a bespoke hyper-realistic 9:16 human UGC creator selfie portrait via Fal.ai Flux (~$0.04)."""
    from tools.fal_video_engine import CreativeBalanceError, generate_realistic_ai_avatar

    avatars_dir = Path("static") / "media" / "avatars"
    try:
        res = await asyncio.to_thread(
            generate_realistic_ai_avatar,
            avatars_dir,
            req.gender,
            req.niche,
            req.age_group,
            req.style_prompt,
        )
        return {
            "status": "success",
            "url": res["web_url"],
            "filename": res["filename"],
            "model": res["model"],
            "cost_usd": res["cost_usd"],
        }
    except CreativeBalanceError as exc:
        raise HTTPException(status_code=402, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"AI Avatar yaradıla bilmədi: {exc}")


@app.get("/api/pipeline/results")
async def get_results():
    """Return the pipeline results (products, supplier, store, creatives, ads)."""
    return pipeline_state["results"]


@app.get("/api/pipeline/logs")
async def stream_logs(request: Request):
    """SSE endpoint — streams real-time log events to the browser."""
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    log_queues.append(q)

    # Send existing logs first
    for entry in pipeline_state["logs"]:
        try:
            q.put_nowait(json.dumps(entry, ensure_ascii=False))
        except asyncio.QueueFull:
            break

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield {"data": msg}
                except asyncio.TimeoutError:
                    # Send keepalive comment to prevent connection drop
                    yield {"comment": "keepalive"}
        finally:
            if q in log_queues:
                log_queues.remove(q)

    return EventSourceResponse(event_generator())


@app.get("/api/proxy-image")
async def proxy_image(url: str):
    """Fetches an image from an external URL, converts it to PNG, and returns it (CORS-friendly for clipboard)."""
    import urllib.request
    from io import BytesIO
    from fastapi.responses import Response
    from PIL import Image

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        )
        with urllib.request.urlopen(req, timeout=15) as res:
            img_data = res.read()
        
        # Convert to PNG using PIL so browser ClipboardItem('image/png') can always read it
        img = Image.open(BytesIO(img_data)).convert("RGB")
        out_buf = BytesIO()
        img.save(out_buf, format="PNG")
        png_bytes = out_buf.getvalue()

        return Response(content=png_bytes, media_type="image/png", headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=86400"
        })
    except Exception as e:
        logger.error(f"Error proxying image {url}: {e}")
        return Response(content=b"", status_code=400)


# ══════════════════════════════ TREND RADAR ENDPOINTS ═════════════════════════

@app.get("/api/radar/latest")
async def get_radar_latest():
    """Returns the latest reports for both Shopify and Amazon trackers, plus unified dual-market convergence."""
    rep_dir = PROJECT_ROOT / "data" / "radar" / "reports"
    shopify_path = rep_dir / "shopify_latest.json"
    amazon_path = rep_dir / "amazon_latest.json"
    unified_path = rep_dir / "unified_latest.json"

    shopify_data = {}
    amazon_data = {}
    unified_data = {}

    if shopify_path.exists():
        try:
            with open(shopify_path, "r", encoding="utf-8") as f:
                shopify_data = json.load(f)
        except Exception as e:
            logger.warning(f"Error loading shopify_latest.json: {e}")

    if amazon_path.exists():
        try:
            with open(amazon_path, "r", encoding="utf-8") as f:
                amazon_data = json.load(f)
        except Exception as e:
            logger.warning(f"Error loading amazon_latest.json: {e}")

    if unified_path.exists():
        try:
            with open(unified_path, "r", encoding="utf-8") as f:
                unified_data = json.load(f)
        except Exception as e:
            logger.warning(f"Error loading unified_latest.json: {e}")

    return {
        "success": True,
        "shopify": shopify_data,
        "amazon": amazon_data,
        "unified": unified_data,
        "timestamp": datetime.now().isoformat()
    }


@app.get("/api/radar/unified")
async def get_radar_unified():
    """Returns the unified Top 10-20 daily ranking with cross-platform confirmed winning products."""
    unified_path = PROJECT_ROOT / "data" / "radar" / "reports" / "unified_latest.json"
    if not unified_path.exists():
        from tools.cross_radar import run_cross_radar_scan
        return run_cross_radar_scan(top_n=20)
    try:
        with open(unified_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error reading unified_latest.json: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/radar/scan")
async def trigger_radar_scan(req: Optional[RadarScanRequest] = None):
    """Triggers an asynchronous scan of Shopify and/or Amazon trend trackers with cross-market convergence."""
    source = (req.source if req and req.source else "all").lower()
    top_n = req.top_n if req and req.top_n else 20

    _add_log(f"🛰️ Trend Radar asinxron skanı başladılır (Mənbə: {source}, Limit: {top_n})...", "info")

    results = {}
    try:
        if source in ("all", "cross", "unified"):
            from tools.cross_radar import run_cross_radar_scan_async
            _add_log("🚀 Shopify və Amazon radarları paralel (asinxron) işə salınır...", "info")
            u_rep = await run_cross_radar_scan_async(top_n=top_n)
            results["unified"] = u_rep
            results["cross_matches"] = u_rep.get("cross_matches", [])
            _add_log(
                f"✅ Asinxron radar tamamlandı: {len(u_rep.get('cross_matches', []))} dual-market qalib məhsul, "
                f"{len(u_rep.get('signals', []))} top siqnal aşkar edildi ({u_rep.get('elapsed_seconds', 0)} san)",
                "success",
            )
        elif source == "shopify":
            from tools.shopify_tracker import run_scan as run_shopify_scan
            _add_log("🛍️ Shopify rəqib mağazaları skan edilir...", "info")
            s_rep = await asyncio.to_thread(run_shopify_scan, top_n=top_n)
            results["shopify"] = s_rep
            _add_log(f"✅ Shopify skanı tamamlandı: {len(s_rep.get('signals', []))} siqnal tapıldı", "success")
        elif source == "amazon":
            from tools.amazon_tracker import run_scan as run_amazon_scan
            _add_log("📦 Amazon Movers & Hot New Releases skan edilir...", "info")
            a_rep = await asyncio.to_thread(run_amazon_scan, top_n=top_n)
            results["amazon"] = a_rep
            _add_log(f"✅ Amazon skanı tamamlandı: {len(a_rep.get('signals', []))} siqnal tapıldı", "success")

        return {"success": True, "results": results}
    except Exception as e:
        logger.error(f"Radar scan error: {e}", exc_info=True)
        _add_log(f"❌ Radar skanında xəta: {e}", "error")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/radar/send-email")
async def send_radar_email(req: RadarEmailRequest):
    """Builds and dispatches the latest trend analysis report to the user's email."""
    if not req.email or "@" not in req.email:
        raise HTTPException(status_code=400, detail="Düzgün e-poçt ünvanı daxil edin.")

    try:
        from tools.notification_tools import send_radar_email_report
        res = await asyncio.to_thread(send_radar_email_report, req.email)
        if res.get("success"):
            _add_log(f"📧 Trend Radar hesabatı '{req.email}' ünvanına hazırlandı ({res.get('mode')})", "success")
        else:
            _add_log(f"⚠️ Trend Radar hesabat göndərişi: {res.get('message')}", "warn")
        return res
    except Exception as e:
        logger.error(f"Error sending radar email: {e}", exc_info=True)
        _add_log(f"❌ E-poçt göndərilməsində xəta: {e}", "error")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/radar/preview-email")
async def preview_radar_email():
    """Renders the latest generated Trend Radar email report in the browser."""
    email_path = PROJECT_ROOT / "data" / "radar" / "emails" / "latest_radar_report.html"
    if not email_path.exists():
        from tools.notification_tools import send_radar_email_report
        send_radar_email_report("preview@localhost")

    return HTMLResponse(content=email_path.read_text(encoding="utf-8"))


@app.get("/api/radar/subscribers")
async def get_radar_subscribers():
    """Returns active daily email subscribers and schedule details."""
    from tools.notification_tools import load_subscribers
    subs = load_subscribers()
    active_subs = [s for s in subs if s.get("active", True)]
    return {
        "success": True,
        "frequency": "daily_1x",
        "frequency_label": "Gündə 1 dəfə (Hər səhər 09:00)",
        "delivery_time": "09:00",
        "catalog_stores_total": 172,
        "subscribers_count": len(active_subs),
        "subscribers": active_subs,
    }


@app.post("/api/radar/subscribe")
async def subscribe_radar(req: RadarSubscribeRequest):
    """Subscribes a user email to receive the 1x/day automated Trend Radar digest."""
    if not req.email or "@" not in req.email:
        raise HTTPException(status_code=400, detail="Düzgün e-poçt ünvanı daxil edin.")

    try:
        from tools.notification_tools import subscribe_radar_email
        res = await asyncio.to_thread(subscribe_radar_email, req.email, req.category or "all")
        if res.get("success"):
            _add_log(
                f"🔔 Yeni Radar Abunəliyi: '{req.email}' (Gündə 1 dəfə avtomatik hesabat aktivləşdirildi)",
                "success",
            )
        else:
            _add_log(f"⚠️ Abunəlik xətası: {res.get('message')}", "warn")
        return res
    except Exception as e:
        logger.error(f"Error subscribing radar email: {e}", exc_info=True)
        _add_log(f"❌ Abunəlik zamanı xəta: {e}", "error")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/radar/unsubscribe")
async def unsubscribe_radar(req: RadarEmailRequest):
    """Unsubscribes an email from the daily Trend Radar digest."""
    if not req.email:
        raise HTTPException(status_code=400, detail="E-poçt ünvanı qeyd edilməyib.")
    try:
        from tools.notification_tools import unsubscribe_radar_email
        res = await asyncio.to_thread(unsubscribe_radar_email, req.email)
        _add_log(f"🔕 Radar abunəliyi ləğv edildi: '{req.email}'", "info")
        return res
    except Exception as e:
        logger.error(f"Error unsubscribing radar email: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════ FAZA 3: AUTOMATED ORDER FULFILLMENT (CJ API + 1688/AGENT PACK) ══════════════════════════════

class CreateOrderRequest(BaseModel):
    order_type: str = "sample"  # "sample" | "customer"
    quantity: int = 1
    pay_from_balance: bool = False
    customer_name: str
    address: str
    city: str
    province: str = ""
    zip_code: str
    country_code: str = "US"
    phone: str = ""
    email: str = ""
    supplier: Optional[Dict[str, Any]] = None


@app.post("/api/orders/create")
async def api_create_order(req: CreateOrderRequest):
    """Creates an automated CJ Dropshipping order or a 1688/HyperSKU/SourcinBox agent order pack."""
    from tools.order_automation import place_automated_order

    sup = req.supplier or pipeline_state.get("results", {}).get("supplier")
    if not sup and pipeline_state.get("pending_approval"):
        sup = pipeline_state["pending_approval"].get("supplier")
    if not sup:
        raise HTTPException(
            status_code=400,
            detail="Aktiv və ya seçilmiş tədarükçü tapılmadı. Əvvəlcə 2-ci Agentdən tədarükçünü seçin.",
        )

    shipping = {
        "customer_name": req.customer_name,
        "address": req.address,
        "city": req.city,
        "province": req.province,
        "zip_code": req.zip_code,
        "country_code": req.country_code,
        "phone": req.phone,
        "email": req.email,
    }
    res = await asyncio.to_thread(
        place_automated_order,
        sup,
        shipping,
        req.quantity,
        req.order_type,
        req.pay_from_balance,
    )
    _add_log(
        f"📦 Faza 3 Sifariş [{res['order_number']}]: {res['status']} — {res['api_message']}",
        "success" if "ERROR" not in res["status"] else "warn",
    )
    return res


@app.get("/api/orders")
async def api_list_orders(limit: int = 20):
    """Returns recent sample & customer orders stored in SQLite."""
    from tools.order_automation import list_orders

    orders = await asyncio.to_thread(list_orders, limit)
    return {"orders": orders}


@app.post("/api/orders/{order_number}/sync")
async def api_sync_order(order_number: str):
    """Syncs live order status and tracking number from CJ Dropshipping API 2.0."""
    from tools.order_automation import sync_cj_order_status

    res = await asyncio.to_thread(sync_cj_order_status, order_number)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Sinxronizasiya xətası"))
    return res


@app.get("/api/orders/{order_number}/export-csv")
async def api_export_order_csv(order_number: str):
    """Downloads a Shopify / HyperSKU / SourcinBox compatible CSV row for the order."""
    from tools.order_automation import export_order_csv

    csv_data = await asyncio.to_thread(export_order_csv, order_number)
    if not csv_data:
        raise HTTPException(status_code=404, detail="Sifariş tapılmadı.")
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="order_{order_number}.csv"'},
    )


# ══════════════════════════════ FAZA 4: SUPPLIER MONITORING, BACKUP FAILOVER & TELEGRAM ══════════════════════════════

class SwitchBackupRequest(BaseModel):
    watch_id: int
    backup_index: int = 0


@app.get("/api/monitoring/status")
async def api_monitoring_status():
    """Returns watched suppliers and recent price/stock/tracking alerts from SQLite."""
    from tools.supplier_monitor import list_watches_and_alerts

    return await asyncio.to_thread(list_watches_and_alerts, 15)


@app.post("/api/monitoring/register")
async def api_monitoring_register():
    """Registers the current active/approved supplier + backups into Phase 4 monitoring."""
    from tools.supplier_monitor import register_supplier_watch

    results = pipeline_state.get("results") or {}
    sup = results.get("supplier")
    all_sups = results.get("suppliers") or []
    prod = results.get("product") or {}
    if not sup and pipeline_state.get("pending_approval"):
        sup = pipeline_state["pending_approval"].get("supplier")
        all_sups = pipeline_state["pending_approval"].get("suppliers") or []
        prod = pipeline_state["pending_approval"].get("product") or {}
    if not sup:
        raise HTTPException(status_code=400, detail="Monitorinqə əlavə ediləcək aktiv tədarükçü yoxdur.")

    watch_id = await asyncio.to_thread(
        register_supplier_watch,
        sup,
        all_sups,
        prod.get("name", ""),
    )
    _add_log(f"👁️ Tədarükçü monitorinqə alındı (Watch #{watch_id}): {sup.get('supplier_name', '')}", "info")
    return {"success": True, "watch_id": watch_id}


@app.post("/api/monitoring/check-now")
async def api_monitoring_check_now():
    """Runs an immediate Phase 4 check: CJ price spikes (>10%), stock levels, and order tracking."""
    from tools.supplier_monitor import run_supply_monitor_cycle

    res = await asyncio.to_thread(run_supply_monitor_cycle)
    for a in res.get("new_alerts", []):
        _add_log(f"🚨 {a.get('message')}", "warn")
    for u in res.get("order_updates", []):
        _add_log(f"🚚 Sifariş [{u.get('order_number')}] yeniləndi: {u.get('status')}", "success")
    if not res.get("new_alerts") and not res.get("order_updates"):
        _add_log(f"✅ Monitorinq yoxlaması tamamlandı ({res.get('checked_watches', 0)} tədarükçü stabil).", "info")
    return res


@app.post("/api/monitoring/switch-backup")
async def api_monitoring_switch_backup(req: SwitchBackupRequest):
    """Promotes Backup #2 supplier to Primary and updates pipeline_state.results."""
    from tools.supplier_monitor import switch_to_backup_supplier

    res = await asyncio.to_thread(switch_to_backup_supplier, req.watch_id, req.backup_index)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Ehtiyat tədarükçüyə keçid uğursuz oldu."))

    new_sup = res.get("new_supplier")
    if isinstance(pipeline_state.get("results"), dict) and new_sup:
        pipeline_state["results"]["supplier"] = new_sup
    _add_log(
        f"🔄 Ehtiyat tədarükçüyə (#2) keçid edildi: {new_sup.get('supplier_name', 'N/A') if new_sup else 'N/A'}",
        "success",
    )
    return res


@app.post("/api/monitoring/test-telegram")
async def api_monitoring_test_telegram():
    """Tests Telegram bot configuration cleanly without crashing if token is unset."""
    from tools.notification_tools import send_telegram_notification

    raw = await asyncio.to_thread(
        send_telegram_notification,
        "✅ *E-Commerce AI Agent (Faza 4)*: Telegram bildiriş xətti aktivdir! Qiymət/stok xəbərdarlıqları və tracking yenilikləri buraya gələcək.",
    )
    return json.loads(raw)


# ══════════════════════════════ FAZA 5: SHOPIFY WEBHOOK AUTO-FULFILLMENT, TRACKING PUSH & 3PL SCALING ══════════════════════════════

class ToggleFailoverRequest(BaseModel):
    watch_id: int
    enabled: bool


class BulkCompareRequest(BaseModel):
    unit_cost_usd: float
    single_shipping_usd: float = 5.20
    selling_price_usd: float = 24.99
    batch_qty: int = 100
    estimated_cpa_usd: float = 15.0


class PushTrackingRequest(BaseModel):
    shopify_order_id: str


@app.post("/api/monitoring/toggle-auto-failover")
async def api_toggle_auto_failover(req: ToggleFailoverRequest):
    """Enables or disables automatic backup #2 failover on out-of-stock."""
    from tools.supplier_monitor import toggle_auto_failover

    res = await asyncio.to_thread(toggle_auto_failover, req.watch_id, req.enabled)
    if not res.get("success"):
        raise HTTPException(status_code=404, detail=res.get("error", "Watch tapılmadı"))
    _add_log(
        f"🛡️ Watch #{req.watch_id} Avto-Keçid rejimi: {'AKTİV' if req.enabled else 'DEAKTİV'}",
        "info",
    )
    return res


@app.post("/api/webhooks/shopify/orders-paid")
async def api_shopify_orders_paid_webhook(request: Request):
    """
    Receives Shopify 'orders/paid' webhooks, verifies HMAC if SHOPIFY_WEBHOOK_SECRET is configured,
    matches line items to watched suppliers, and triggers automated CJ API 2.0 / 1688 Agent fulfillment.
    """
    from tools.fulfillment_bridge import verify_shopify_webhook_hmac, process_shopify_paid_order

    raw_body = await request.body()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    sig = verify_shopify_webhook_hmac(raw_body, hmac_header)
    if not sig.get("ok"):
        raise HTTPException(status_code=401, detail=sig.get("error", "Unauthorized webhook"))

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Etibarsız JSON payload.")

    fallback_sup = (pipeline_state.get("results") or {}).get("supplier")
    res = await asyncio.to_thread(process_shopify_paid_order, payload, fallback_sup, False)
    res["webhook_auth"] = sig["mode"]
    _add_log(
        f"🛒 Faza 5 Shopify Webhook [#{res['shopify_order_number']}] ({sig['mode']}): {len(res['orders'])} sifariş işləndi.",
        "success",
    )
    return res


class PublicStoreOrderRequest(BaseModel):
    binding_sku: Optional[str] = ""
    sourcing_channel: Optional[str] = ""
    product_name: Optional[str] = "Storefront Product"
    quantity: int = 1
    total_price_usd: Optional[float] = None
    customer: Dict[str, Any] = {}


@app.post("/api/store/public-order")
async def api_public_store_order(req: PublicStoreOrderRequest):
    """
    Receives public customer orders directly from the Agent 3 Global DTC storefront modal (#quick-order-form),
    resolves the bound supplier via binding_sku (or active pipeline supplier), and triggers place_automated_order().
    """
    from tools.binding_store import resolve_binding_by_sku
    from tools.order_automation import place_automated_order

    results = pipeline_state.get("results") or {}
    fallback_sup = dict(results.get("supplier") or {})
    b_sku = (req.binding_sku or "").strip()
    if b_sku:
        binding = await asyncio.to_thread(resolve_binding_by_sku, b_sku)
        if binding:
            bound_sup = binding.get("primary_supplier") or binding.get("supplier")
            if isinstance(bound_sup, dict):
                fallback_sup = dict(bound_sup)
                fallback_sup["binding_sku"] = b_sku
    if req.sourcing_channel and not fallback_sup.get("channel"):
        fallback_sup["channel"] = req.sourcing_channel
    if req.product_name and not fallback_sup.get("product_name"):
        fallback_sup["product_name"] = req.product_name

    order_res = await asyncio.to_thread(
        place_automated_order,
        fallback_sup,
        req.customer or {},
        max(1, int(req.quantity or 1)),
        "customer",
        False,
    )
    _add_log(
        f"🛍️ Mağaza Sifarişi [{order_res.get('order_number')}]: {req.product_name} (x{req.quantity}) | "
        f"SKU: {req.binding_sku or 'N/A'} | Kanal: {order_res.get('platform')}",
        "success" if order_res.get("success") else "warn",
    )
    return {
        "success": bool(order_res.get("success")),
        "order_id": order_res.get("order_number"),
        "supplier_order_id": order_res.get("supplier_order_id"),
        "channel": order_res.get("platform"),
        "status": order_res.get("status"),
        "details": order_res,
    }


@app.post("/api/orders/{order_number}/push-shopify-tracking")
async def api_push_shopify_tracking(order_number: str, req: PushTrackingRequest):
    """Pushes tracking number from SQLite order back to Shopify Admin API."""
    from tools.order_automation import get_order
    from tools.fulfillment_bridge import push_tracking_to_shopify

    order = await asyncio.to_thread(get_order, order_number)
    if not order:
        raise HTTPException(status_code=404, detail="Sifariş tapılmadı.")
    track_num = order.get("tracking_number") or ""
    if not track_num:
        raise HTTPException(status_code=400, detail="Bu sifarişdə hələ tracking nömrəsi yoxdur (əvvəlcə 🔄 Sync edin).")

    return await asyncio.to_thread(
        push_tracking_to_shopify,
        req.shopify_order_id,
        track_num,
        order.get("logistic_line") or "CJ Dropshipping",
    )


@app.post("/api/sourcing/bulk-compare")
async def api_sourcing_bulk_compare(req: BulkCompareRequest):
    """Compares 1-by-1 Dropshipping vs Bulk 1688 MOQ + US/EU 3PL Fulfillment economics."""
    from tools.fulfillment_bridge import calculate_bulk_vs_dropship

    return await asyncio.to_thread(
        calculate_bulk_vs_dropship,
        req.unit_cost_usd,
        req.single_shipping_usd,
        req.selling_price_usd,
        req.batch_qty,
        req.estimated_cpa_usd,
    )


class VisualSearch1688Request(BaseModel):
    image_url: Optional[str] = None
    keyword: Optional[str] = None
    target_country: str = "US"


class Save1688CookiesRequest(BaseModel):
    cookies_json: str


@app.post("/api/sourcing/1688-visual-search")
async def api_sourcing_1688_visual_search(req: VisualSearch1688Request):
    """
    Registration-free 1688 visual search via shared server cookie Playwright session.
    Enriches each matched 1688 supplier with 1688 -> HyperSKU/SourcinBox -> YunExpress landed cost.
    """
    from tools.cargo_3pl import calculate_1688_yunexpress_landed_cost
    from tools.sourcing_tools import get_live_fx_rates, search_1688_by_image_playwright

    img = (req.image_url or "").strip()
    kw = (req.keyword or "").strip()
    res = await asyncio.to_thread(search_1688_by_image_playwright, img, None, 8)
    fx = await asyncio.to_thread(get_live_fx_rates)
    cny_rate = float(res.get("fx_cny_per_usd") or fx.get("CNY") or 7.15)

    enriched = []
    for s in res.get("suppliers") or []:
        landed = calculate_1688_yunexpress_landed_cost(
            s.get("price_cny"),
            fx_cny_per_usd=cny_rate,
            target_country=req.target_country or "US",
        )
        enriched.append({
            **s,
            "yunexpress_shipping_usd": landed["shipping_cost"],
            "landed_product_cost_usd": landed["product_cost"],
            "total_landed_usd": landed["cost_breakdown"].get("total_landed_cost_usd"),
            "logistic_line": landed["logistic_line"],
            "delivery_days": landed["delivery_days"],
        })

    import urllib.parse as _up
    return {
        "success": bool(res.get("success")),
        "session_required": bool(res.get("session_required")),
        "reason": res.get("reason") or "",
        "fx_cny_per_usd": cny_rate,
        "fallback_search_url": f"https://s.1688.com/selloffer/offer_search.htm?keywords={_up.quote(kw)}" if kw else "https://www.1688.com",
        "suppliers": enriched,
    }


@app.get("/api/sourcing/1688-session/status")
async def api_1688_session_status():
    """Check whether shared server-side 1688 cookie session is active."""
    from tools.sourcing_tools import get_1688_session_status
    return await asyncio.to_thread(get_1688_session_status)


@app.post("/api/sourcing/1688-session/cookies")
async def api_save_1688_cookies(req: Save1688CookiesRequest):
    """Save admin 1688 session cookies so all end-users search 1688 by image without registration."""
    from tools.sourcing_tools import save_1688_session_cookies
    try:
        status = await asyncio.to_thread(save_1688_session_cookies, req.cookies_json)
        return {"ok": True, **status}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/sourcing/aliexpress/status")
async def api_aliexpress_status():
    """Return AliExpress Dropshipping (DS) API & OAuth connection status."""
    from tools.aliexpress_ds_tool import get_aliexpress_config_status
    return await asyncio.to_thread(get_aliexpress_config_status)


@app.get("/api/sourcing/aliexpress/auth-url")
async def api_aliexpress_auth_url(request: Request):
    """Generate AliExpress OAuth authorization URL pointing to our callback endpoint."""
    from tools.aliexpress_ds_tool import get_aliexpress_oauth_url
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/sourcing/aliexpress/callback"
    res = get_aliexpress_oauth_url(redirect_uri=redirect_uri)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "OAuth URL yaradıla bilmədi."))
    return res


@app.get("/api/sourcing/aliexpress/callback")
async def api_aliexpress_oauth_callback(code: str = "", state: str = ""):
    """OAuth 2.0 callback handler exchanging AliExpress authorization code for Access Token."""
    from fastapi.responses import RedirectResponse
    from tools.aliexpress_ds_tool import exchange_aliexpress_code

    if not code:
        return RedirectResponse(url="/app?ae_oauth=missing_code")
    res = await asyncio.to_thread(exchange_aliexpress_code, code)
    if res.get("ok"):
        _add_log("🔗 AliExpress DS OAuth Access Token uğurla əldə edildi və aktivləşdirildi.", "success")
        return RedirectResponse(url="/app?ae_oauth=success")
    err = urllib.parse.quote(str(res.get("error") or "oauth_error"))
    return RedirectResponse(url=f"/app?ae_oauth=error&msg={err}")


@app.get("/api/sourcing/bindings")
async def api_list_product_bindings():
    """List encoded Product-Supplier binding_sku records linking Product, Supplier, Fulfillment, and Backup #2."""
    from tools.binding_store import list_bindings
    return {"bindings": await asyncio.to_thread(list_bindings, 25)}


# ══════════════════════════════ SAAS SETTINGS, INTEGRATIONS & HEALTH API ══════════════════════════════
import os

SAAS_INTEGRATION_KEYS = [
    {"key": "GOOGLE_API_KEY", "group": "ai", "label": "Google Gemini API Key", "secret": True},
    {"key": "ALIEXPRESS_APP_KEY", "group": "sourcing", "label": "AliExpress Dropshipping App Key", "secret": False},
    {"key": "ALIEXPRESS_APP_SECRET", "group": "sourcing", "label": "AliExpress Dropshipping App Secret", "secret": True},
    {"key": "ALIEXPRESS_ACCESS_TOKEN", "group": "sourcing", "label": "AliExpress OAuth Access Token", "secret": True},
    {"key": "CJ_API_KEY", "group": "sourcing", "label": "CJ Dropshipping API 2.0 Key", "secret": True},
    {"key": "HYPERSKU_API_KEY", "group": "sourcing", "label": "HyperSKU / 3PL Agent API Key (1688 -> YunExpress)", "secret": True},
    {"key": "SOURCINBOX_WEBHOOK_URL", "group": "sourcing", "label": "3PL Agent Webhook URL (SourcinBox / Özəl 3PL)", "secret": False},
    {"key": "SHOPIFY_STORE_URL", "group": "shopify", "label": "Shopify Store Domain", "secret": False},
    {"key": "SHOPIFY_ACCESS_TOKEN", "group": "shopify", "label": "Shopify Admin Access Token (shpat_...)", "secret": True},
    {"key": "SHOPIFY_WEBHOOK_SECRET", "group": "shopify", "label": "Shopify Webhook HMAC Secret", "secret": True},
    {"key": "TELEGRAM_BOT_TOKEN", "group": "notifications", "label": "Telegram Bot Token", "secret": True},
    {"key": "TELEGRAM_CHAT_ID", "group": "notifications", "label": "Telegram Chat ID", "secret": False},
    {"key": "SLACK_WEBHOOK_URL", "group": "notifications", "label": "Slack Webhook URL", "secret": True},
    {"key": "META_ACCESS_TOKEN", "group": "ads", "label": "Meta Ads Access Token", "secret": True},
    {"key": "META_AD_ACCOUNT_ID", "group": "ads", "label": "Meta Ad Account ID (act_...)", "secret": False},
    {"key": "POSTIZ_API_URL", "group": "social", "label": "Postiz Server URL", "secret": False},
    {"key": "POSTIZ_API_KEY", "group": "social", "label": "Postiz API Key", "secret": True},
    {"key": "ELEVENLABS_API_KEY", "group": "creative", "label": "ElevenLabs Voice API Key", "secret": True},
    {"key": "FAL_KEY", "group": "creative", "label": "Fal.ai UGC & Video Key", "secret": True},
]


def _is_key_configured(val: Optional[str]) -> bool:
    if not val:
        return False
    s = str(val).strip()
    if not s or s.startswith("your_") or "your/webhook/url" in s or s == "act_your_ad_account_id":
        return False
    return True


def _mask_secret(val: Optional[str], is_secret: bool) -> str:
    if not _is_key_configured(val):
        return ""
    s = str(val).strip()
    if not is_secret:
        return s
    if len(s) <= 8:
        return "••••••••"
    return f"{s[:4]}••••••••{s[-4:]}"


class IntegrationSaveRequest(BaseModel):
    updates: Dict[str, str]


class IntegrationTestRequest(BaseModel):
    provider: str


@app.get("/api/health")
async def api_health():
    """Production domain & reverse-proxy health endpoint."""
    from dotenv import dotenv_values
    env_path = PROJECT_ROOT / ".env"
    file_vals = dotenv_values(env_path) if env_path.exists() else {}
    configured_count = 0
    for item in SAAS_INTEGRATION_KEYS:
        k = item["key"]
        v = os.getenv(k) or file_vals.get(k)
        if _is_key_configured(v):
            configured_count += 1
    return {
        "status": "ok",
        "service": "ApexCart AI SaaS OS",
        "version": "2.5.0",
        "pipeline_running": pipeline_state.get("is_running", False),
        "pipeline_status": pipeline_state.get("status", "idle"),
        "configured_integrations": configured_count,
        "total_integrations": len(SAAS_INTEGRATION_KEYS),
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/settings/integrations")
async def api_get_integrations():
    """Returns masked status of all SaaS API keys & integrations."""
    from dotenv import dotenv_values
    env_path = PROJECT_ROOT / ".env"
    file_vals = dotenv_values(env_path) if env_path.exists() else {}

    items = []
    configured_count = 0
    for spec in SAAS_INTEGRATION_KEYS:
        k = spec["key"]
        raw_val = os.getenv(k) or file_vals.get(k) or ""
        ok = _is_key_configured(raw_val)
        if ok:
            configured_count += 1
        items.append({
            "key": k,
            "group": spec["group"],
            "label": spec["label"],
            "secret": spec["secret"],
            "configured": ok,
            "masked_value": _mask_secret(raw_val, spec["secret"]),
            "current_non_secret": str(raw_val).strip() if (ok and not spec["secret"]) else "",
        })

    return {
        "items": items,
        "configured_count": configured_count,
        "total_count": len(SAAS_INTEGRATION_KEYS),
        "orders_db_exists": (PROJECT_ROOT / "data" / "orders.db").exists(),
        "webhook_path": "/api/webhooks/shopify/orders-paid",
    }


@app.post("/api/settings/integrations/save")
async def api_save_integrations(req: IntegrationSaveRequest):
    """Persists updated API keys to .env and applies them to runtime os.environ without restart."""
    allowed_keys = {item["key"] for item in SAAS_INTEGRATION_KEYS}
    env_path = PROJECT_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

    clean_updates: Dict[str, str] = {}
    for k, v in (req.updates or {}).items():
        if k in allowed_keys and v is not None:
            val_str = str(v).strip()
            # Ignore masked placeholders if accidentally submitted
            if val_str and "••••" not in val_str:
                clean_updates[k] = val_str

    if not clean_updates:
        return {"success": True, "updated_keys": [], "message": "Dəyişiklik göndərilmədi."}

    updated_in_file = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k_part = stripped.split("=", 1)[0].strip()
            if k_part in clean_updates:
                new_lines.append(f"{k_part}={clean_updates[k_part]}")
                updated_in_file.add(k_part)
                continue
        new_lines.append(line)

    for k, v in clean_updates.items():
        if k not in updated_in_file:
            new_lines.append(f"{k}={v}")
        os.environ[k] = v

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # Clear cached settings & CJ token cache if CJ key updated
    try:
        get_settings.cache_clear()
    except Exception:
        pass
    if "CJ_API_KEY" in clean_updates:
        try:
            from tools.cj_dropshipping_tool import _CJ_TOKEN_CACHE
            _CJ_TOKEN_CACHE["token"] = None
            _CJ_TOKEN_CACHE["expires_at"] = 0
        except Exception:
            pass

    _add_log(f"⚙️ SaaS İnteqrasiya ayarları yeniləndi: {', '.join(clean_updates.keys())}", "success")
    return {
        "success": True,
        "updated_keys": list(clean_updates.keys()),
        "message": f"{len(clean_updates)} açar uğurla .env faylına və canlı serverə tətbiq edildi.",
    }


@app.post("/api/settings/integrations/test")
async def api_test_integration(req: IntegrationTestRequest):
    """Tests live connection for a specific SaaS integration provider."""
    prov = (req.provider or "").strip().lower()
    if prov == "cj":
        from tools.cj_dropshipping_tool import get_cj_access_token
        tok = await asyncio.to_thread(get_cj_access_token)
        if tok:
            return {"ok": True, "provider": "cj", "message": "CJ Dropshipping API 2.0 Access Token aktivdir."}
        return {"ok": False, "provider": "cj", "message": "CJ_API_KEY təyin edilməyib və ya token alına bilmədi."}

    if prov == "telegram":
        from tools.notification_tools import send_telegram_notification
        raw = await asyncio.to_thread(
            send_telegram_notification,
            "✅ *ApexCart AI SaaS*: Telegram bildiriş inteqrasiyası aktivdir!",
        )
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw}
        ok = parsed.get("status") == "success"
        return {
            "ok": ok,
            "provider": "telegram",
            "message": "Telegram mesajı uğurla göndərildi!" if ok else f"Simulyasiya / Xəta: {parsed.get('message') or parsed.get('error') or 'Token əskikdir'}",
        }

    if prov == "shopify":
        store = (os.getenv("SHOPIFY_STORE_URL") or "").strip()
        tok = (os.getenv("SHOPIFY_ACCESS_TOKEN") or "").strip()
        wh = (os.getenv("SHOPIFY_WEBHOOK_SECRET") or "").strip()
        if _is_key_configured(store) and _is_key_configured(tok):
            return {
                "ok": True,
                "provider": "shopify",
                "message": f"Shopify mağazası ({store}) və Admin API Token hazırdır. Webhook HMAC: {'Aktiv' if _is_key_configured(wh) else 'Test rejimi'}.",
            }
        return {
            "ok": False,
            "provider": "shopify",
            "message": f"Mağaza: {store or 'Yoxdur'} | Admin Token: {'Aktiv' if _is_key_configured(tok) else 'Əskikdir (shpat_...)'}",
        }

    if prov == "gemini":
        gkey = os.getenv("GOOGLE_API_KEY") or ""
        if _is_key_configured(gkey):
            return {"ok": True, "provider": "gemini", "message": "Google Gemini AI açarı aktivdir və hazırdır."}
        return {"ok": False, "provider": "gemini", "message": "GOOGLE_API_KEY əskikdir."}

    return {"ok": False, "provider": prov, "message": "Naməlum inteqrasiya provayderi."}


# ══════════════════════════════ BACKGROUND DRIP-SCAN & DAILY DIGEST (1x/DAY) ══════════════════════════════

RADAR_CATEGORIES = [
    "electronics", "home", "beauty", "fashion", "kitchen",
    "fitness", "kids", "pet", "automotive", "outdoor"
]
_radar_scheduler_task: Optional[asyncio.Task] = None


async def _radar_daily_scheduler_loop():
    """
    Autonomous background daemon that:
    1) Drip-scans 1 Shopify category per hour (so all 172 stores across 10 categories
       are scanned smoothly every day with 0 rate-limit / 429 issues), plus Amazon daily.
    2) Dispatches the daily digest email strictly ONCE PER DAY (at or after 09:00)
       to all active subscribers in data/radar/subscribers.json.
    """
    cat_idx = 0
    last_amazon_scan_date = ""
    await asyncio.sleep(15)  # wait briefly after server boot

    while True:
        try:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            # 1) Hourly background scan: if Amazon hasn't been scanned today, run Shopify + Amazon
            #    ASYNCHRONOUSLY IN PARALLEL via cross_radar; otherwise drip-scan next Shopify category
            #    and refresh the Unified Top 20 report + Cross-Platform Winning Matches.
            cat = RADAR_CATEGORIES[cat_idx % len(RADAR_CATEGORIES)]
            cat_idx += 1
            from tools.cross_radar import run_cross_radar_scan_async, build_unified_report
            from tools.shopify_tracker import run_scan as run_shopify_scan

            if last_amazon_scan_date != today_str:
                await run_cross_radar_scan_async(category=cat, top_n=20)
                last_amazon_scan_date = today_str
            else:
                s_rep = await asyncio.to_thread(run_shopify_scan, category=cat, top_n=20)
                amazon_file = PROJECT_ROOT / "data" / "radar" / "reports" / "amazon_latest.json"
                a_rep = json.loads(amazon_file.read_text(encoding="utf-8")) if amazon_file.exists() else {}
                await asyncio.to_thread(build_unified_report, s_rep, a_rep, 20)

            # 2) Daily digest dispatch check (strictly 1x per day at/after 09:00)
            if now.hour >= 9:
                from tools.notification_tools import dispatch_daily_digest, load_subscribers
                subs = load_subscribers()
                pending = [
                    s for s in subs
                    if s.get("active", True) and s.get("last_sent_date") != today_str
                ]
                if pending:
                    _add_log(
                        f"📧 Gündəlik Trend Radar hesabatı {len(pending)} abunəçiyə göndərilir (Gündə 1 dəfə)...",
                        "info",
                    )
                    digest_res = await asyncio.to_thread(dispatch_daily_digest)
                    if digest_res.get("sent_count", 0) > 0:
                        _add_log(
                            f"✅ Gündəlik hesabat {digest_res['sent_count']} abunəçiyə çatdırıldı!",
                            "success",
                        )

            # 3) Phase 4 Supply Monitor hourly cycle (price spikes >10%, stock, order tracking)
            from tools.supplier_monitor import run_supply_monitor_cycle
            mon_res = await asyncio.to_thread(run_supply_monitor_cycle)
            for a in mon_res.get("new_alerts", []):
                _add_log(f"🚨 Faza 4 Xəbərdarlıq: {a.get('message')}", "warn")
            for u in mon_res.get("order_updates", []):
                _add_log(f"🚚 Sifariş [{u.get('order_number')}] yeniləndi: {u.get('status')}", "info")

        except Exception as e:
            logger.warning(f"Radar/Monitor background scheduler error: {e}")

        # Sleep 1 hour between background cycles
        await asyncio.sleep(3600)


@app.on_event("startup")
async def start_radar_background_scheduler():
    global _radar_scheduler_task
    if _radar_scheduler_task is None or _radar_scheduler_task.done():
        _radar_scheduler_task = asyncio.create_task(_radar_daily_scheduler_loop())
        logger.info("Started autonomous Trend Radar background drip-scan & 1x/day email digest scheduler.")



# ══════════════════════════════ Entry Point ════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    print("\n" + "=" * 60)
    print("  AI E-Ticaret Idareetme Paneli")
    print("  http://localhost:8000")
    print("=" * 60 + "\n")

    uvicorn.run("webapp:app", host="0.0.0.0", port=8000, reload=True)
