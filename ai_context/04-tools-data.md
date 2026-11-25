# AI Context: Tools and Data

`tools/` is the integration and deterministic-logic layer. Functions passed to Gemini must have clear typed parameters and docstrings because the SDK exposes them as function-calling tools.

## Tool groups

| Group | Key files | Responsibility |
| --- | --- | --- |
| Discovery/Radar | `trend_tools.py`, `rapidapi_tiktok.py`, `amazon_tracker.py`, `cross_radar.py`, `discovery_pool.py` | Trend collection, historical discovery data, ranking signals. |
| Sourcing | `sourcing_tools.py`, `aliexpress_ds_tool.py`, `cj_dropshipping_tool.py`, `cargo_3pl.py`, `supplier_scorer.py` | Supplier discovery, supplier economics, 1688/AliExpress/CJ routing. |
| Supply resiliency | `binding_store.py`, `supplier_monitor.py`, `fulfillment_bridge.py` | Product-to-supplier bindings, backup sources, price/stock monitoring and failover. |
| Store/design | `shopify_tools.py`, `shopify_tracker.py`, `design_intel.py`, `product_image_search.py` | Shopify operations, store audit/design defaults, imagery. |
| Creative | `creative_tools.py`, `fal_video_engine.py`, `creative_cards.py` | Script, voice, video, subtitle, asset workflow. |
| Social/ads | `social_tools.py`, `postiz_tool.py`, `meta_ads_tools.py` | Social account/post scheduling and Meta campaign management. |
| Operations | `order_automation.py`, `notification_tools.py`, `db_utils.py` | Orders, fulfillment state, alerts, database helpers. |

## Data contracts and persistence

- Do not guess a provider response shape. Normalize external payloads into project fields before returning them to an agent.
- Supplier choice becomes durable operational context through `binding_store.py`; it connects selected product, primary supplier, backup supplier, unit economics, and logistics.
- Radar reports are written below `data/radar/`; latest report APIs in `webapp.py` read them for UI use.
- Webhook/order flows should be idempotent: incoming delivery events can be retried by providers.

## External action safety

Creating a product, scheduling a post, creating an ad, or sending a notification has side effects. Keep any confirmation/human-approval boundary in the graph or API layer. Add dry-run/test capability before broadening an integration. Do not put irreversible behavior behind an unauthenticated GET endpoint.

## Adding a tool

1. Place provider-specific I/O in a focused `tools/<domain>.py` module.
2. Return a stable, JSON-serializable result and useful failure reason.
3. Add it only to the relevant list in `main.py::build_tools_map`.
4. Update the owning agent's prompt to explain when it may call the tool.
5. Document new secret settings in `.env.example` and `01-runtime-config.md`.
