"""
E-Commerce Multi-Agent System — Main Entry Point
=================================================

Usage:
    python main.py                          # Run full pipeline
    python main.py --phase sourcing         # Resume from a specific phase
    python main.py --dry-run                # Print the graph diagram without running
    python main.py --model gemini-3.5-flash-lite  # Use a different model
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dotenv import load_dotenv
from google import genai
from langgraph.types import Command
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ── ensure project root is on sys.path ──
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import get_settings, Settings
from workflows.pipeline import build_pipeline

# Tools — plain Python functions (Gemini auto-calls them via function calling)
from tools.trend_tools import (
    search_tiktok_trends,
    search_google_trends,
    search_amazon_bestsellers,
    search_meta_ad_library,
)
from tools.sourcing_tools import (
    search_1688_products,
    search_cj_dropshipping,
    calculate_unit_economics,
)
from tools.social_tools import (
    create_instagram_profile,
    create_tiktok_profile,
    publish_instagram_post,
    publish_tiktok_video,
)
from tools.shopify_tools import (
    create_shopify_product,
    setup_shopify_pixel,
    update_product_page,
    create_discount_code,
)
from tools.creative_tools import (
    generate_ad_script,
    generate_ai_video,
    generate_voiceover,
    add_subtitles_to_video,
    combine_video_assets,
)
from tools.meta_ads_tools import (
    create_campaign,
    create_adset,
    create_ad,
    get_ad_insights,
    update_campaign_budget,
    pause_ad,
    apply_automated_rules,
)

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ecommerce-agent")

console = Console(safe_box=True)
load_dotenv()


# ──────────────────────────── GEMINI CLIENT ───────────────────────────────────

def create_gemini_client(settings: Settings, dry_run: bool = False) -> genai.Client:
    """
    Create a Google Gemini API client.

    Get your API key from https://aistudio.google.com/apikey
    """
    api_key = settings.google_api_key
    if not api_key:
        if dry_run:
            api_key = "dry-run-placeholder-key"
        else:
            raise ValueError(
                "GOOGLE_API_KEY is not set. "
                "Get your API key from https://aistudio.google.com/apikey "
                "and add it to your .env file."
            )
    return genai.Client(api_key=api_key)


# ──────────────────────────── TOOLS MAP ───────────────────────────────────────

def build_tools_map() -> dict[str, list]:
    """
    Return a mapping of agent name → its available Python functions.

    Gemini SDK auto-calls these functions when passed as tools.
    Each function needs: type-annotated params + docstring + returns str.
    """
    return {
        "product_research": [
            search_tiktok_trends,
            search_google_trends,
            search_amazon_bestsellers,
            search_meta_ad_library,
        ],
        "market_research": [
            search_google_trends,
            search_amazon_bestsellers,
        ],
        "sourcing": [
            search_1688_products,
            search_cj_dropshipping,
            calculate_unit_economics,
        ],
        "social_media": [
            create_instagram_profile,
            create_tiktok_profile,
            publish_instagram_post,
            publish_tiktok_video,
        ],
        "store_builder": [
            create_shopify_product,
            setup_shopify_pixel,
            update_product_page,
            create_discount_code,
        ],
        "creative": [
            generate_ad_script,
            generate_ai_video,
            generate_voiceover,
            add_subtitles_to_video,
            combine_video_assets,
        ],
        "media_buyer": [
            create_campaign,
            create_adset,
            create_ad,
            get_ad_insights,
            update_campaign_budget,
            pause_ad,
            apply_automated_rules,
        ],
    }


# ──────────────────────────── INITIAL STATE ───────────────────────────────────

def make_initial_state(start_phase: str = "product_research") -> dict:
    return {
        "current_phase": start_phase,
        "category": "",
        "products": [],
        "selected_product": None,
        "rejected_products": [],
        "market_research_report": None,
        "suppliers": [],
        "selected_supplier": None,
        "unit_economics": None,
        "brand": None,
        "brand_identity": None,
        "social_accounts": [],
        "content_plan": [],
        "social_media": None,
        "postiz": None,
        "store": None,
        "store_config": None,
        "store_copy": None,
        "wordpress_landing_page_html": None,
        "creatives": [],
        "creative_assets": None,
        "manual_video_links": [],
        "creative_config": {},
        "winning_catalog": [],
        "active_catalog_id": None,
        "ad_campaigns": [],
        "campaign_plan": None,
        "ad_metrics": [],
        "human_approvals": [],
        "pending_approval": None,
        "messages": [],
        "errors": [],
        "logs": [],
        "completed_parallel_nodes": [],
    }


# ──────────────────────────── RUN PIPELINE ────────────────────────────────────

async def run_pipeline(
    app,
    initial_state: dict,
    thread_id: str = "run_1",
    auto_approve: bool = False,
):
    """Stream the pipeline and handle human-in-the-loop interrupts cleanly."""
    config = {"configurable": {"thread_id": thread_id}}

    console.print(Panel("🚀 [bold green]Pipeline Başlayır (Google Gemini 3.8 Flash)[/bold green]", expand=False))

    current_input: Any = initial_state
    while True:
        interrupted = False
        async for event in app.astream(current_input, config, stream_mode="updates"):
            for node_name, update in event.items():
                if node_name == "__interrupt__":
                    interrupted = True
                    console.print()
                    for item in (update if isinstance(update, list) else [update]):
                        val = getattr(item, "value", item)
                        msg = val.get("message", str(val)) if isinstance(val, dict) else str(val)
                        console.print(Panel(
                            msg,
                            title="⏸️  [bold yellow]Human-in-the-Loop Təsdiq Tələb Olunur[/bold yellow]",
                            border_style="yellow",
                        ))
                    if auto_approve:
                        decision = "approved"
                        console.print("  🤖 [bold green][Auto-Approved][/bold green] Təsdiq edildi (Canlı test rejimi)")
                    else:
                        decision = console.input(
                            "[bold cyan]Qərarınız (approved / rejected): [/bold cyan]"
                        ).strip().lower()

                    current_input = Command(resume=decision)
                    break
                else:
                    console.print(f"  ✅ [cyan]{node_name}[/cyan] tamamlandı.")
            if interrupted:
                break
        if not interrupted:
            break

    console.print(Panel("🏁 [bold green]Bütün İş Axını Uğurla Tamamlandı[/bold green]", expand=False))

    # ── Show summary ──
    snapshot = await app.aget_state(config)
    state = snapshot.values if hasattr(snapshot, "values") else {}
    _print_summary(state)


def _print_summary(state: dict):
    """Print a rich summary table of the final state."""
    table = Table(title="📊 Final Pipeline State", show_lines=True)
    table.add_column("Component", style="bold")
    table.add_column("Details")

    table.add_row("Phase", state.get("current_phase", "—"))
    sp = state.get("selected_product")
    if isinstance(sp, dict):
        table.add_row("Winning Product", sp.get("name", "—"))
        table.add_row("What It Does", sp.get("what_it_does", "—")[:80] + "..." if len(sp.get("what_it_does", "")) > 80 else sp.get("what_it_does", "—"))
        table.add_row("Wow Factor", sp.get("wow_factor", "—")[:80] + "..." if len(sp.get("wow_factor", "")) > 80 else sp.get("wow_factor", "—"))
        table.add_row("Video Ad Link", sp.get("video_ad_url", "—"))
        table.add_row("Image URL", sp.get("image_url", "—"))
    else:
        table.add_row("Winning Product", "—")

    ss = state.get("selected_supplier")
    table.add_row("Supplier", ss.get("supplier_name", "—") if isinstance(ss, dict) else "—")
    st = state.get("store")
    table.add_row("Store URL", st.get("store_url", "—") if isinstance(st, dict) else "—")
    table.add_row("Creatives", str(len(state.get("creatives", []))))
    table.add_row("Ad Campaigns", str(len(state.get("ad_campaigns", []))))
    table.add_row("Errors", str(len(state.get("errors", []))))

    console.print(table)

    errors = state.get("errors", [])
    if errors:
        console.print("[bold red]Errors encountered:[/bold red]")
        for e in errors:
            console.print(f"  ❌ {e}")


# ──────────────────────────── CLI ─────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="🛒 AI-Powered Multi-Agent E-Commerce Automation System (Gemini)"
    )
    parser.add_argument(
        "--phase",
        type=str,
        default="product_research",
        help="Phase to start from (product_research, sourcing, social_media, store, creative, ads)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override the Gemini model (default: gemini-3.8-flash)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the graph visualization without running the pipeline",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically approve human-in-the-loop review gates (useful for testing)",
    )
    parser.add_argument(
        "--thread-id",
        type=str,
        default="ecommerce_run_1",
        help="Thread ID for checkpointing (allows resuming later)",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    settings = get_settings()
    model = args.model or settings.llm.model

    console.print(Panel.fit(
        "[bold]🛒 AI E-Commerce Multi-Agent System[/bold]\n"
        f"Engine: [cyan]Google Gemini[/cyan]  "
        f"Model: [cyan]{model}[/cyan]  "
        f"Phase: [cyan]{args.phase}[/cyan]",
        border_style="blue",
    ))

    # Initialize Gemini client
    try:
        client = create_gemini_client(settings, dry_run=args.dry_run)
        if args.dry_run and not settings.google_api_key:
            console.print("  ⚠️  Running in dry-run mode without API key (graph inspection only)")
        else:
            console.print(f"  ✅ Gemini client initialized — model: [green]{model}[/green]")
    except Exception as e:
        console.print(f"  ❌ [bold red]Failed to initialize Gemini client:[/bold red] {e}")
        console.print("  💡 Get your API key: [link]https://aistudio.google.com/apikey[/link]")
        return

    # Build tools
    tools_map = build_tools_map()
    console.print(
        f"  ✅ Tools loaded: {sum(len(v) for v in tools_map.values())} tools "
        f"across {len(tools_map)} agents"
    )

    # Build pipeline
    app = build_pipeline(client=client, model=model, tools_map=tools_map, settings=settings)
    console.print("  ✅ Pipeline graph compiled")

    if args.dry_run:
        console.print("\n[bold yellow]Dry-run mode — Graph Structure:[/bold yellow]")
        nodes = list(app.get_graph().nodes.keys())
        console.print(f"  • Registered Nodes ({len(nodes)}): [cyan]{', '.join(nodes)}[/cyan]")
        try:
            mermaid_str = app.get_graph().draw_mermaid()
            console.print("\n[bold green]Mermaid Architecture Flow:[/bold green]")
            console.print(Panel(mermaid_str, title="Mermaid Flow", border_style="green"))
        except Exception as e:
            console.print(f"  (Mermaid drawing: {e})")
        return

    # Run
    initial_state = make_initial_state(args.phase)
    await run_pipeline(
        app,
        initial_state,
        thread_id=args.thread_id,
        auto_approve=args.auto_approve,
    )


if __name__ == "__main__":
    asyncio.run(main())
