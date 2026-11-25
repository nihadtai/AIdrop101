# AI Context: Workflow and Shared State

## Orchestrator

`workflows/pipeline.py::build_pipeline` creates a `StateGraph(ECommerceState)` and compiles it with `MemorySaver`. It is the sole source of truth for graph topology. `main.py::make_initial_state` creates the initial payload; keep it synchronized with fields added to `state/graph_state.py`.

## Current flow

```text
START
  -> product_research -> human_approval_product
       -> market_research (optional, then back to product approval)
       -> reset_product_cycle (reject/loop)
       -> sourcing -> human_approval_supplier -> human_approval_store_config
       -> store_build -> human_approval_store -> human_approval_stage_one
       -> reset_product_cycle OR human_approval_video_sources
       -> parallel_fanout_creative_social
          -> creative_production ─┐
          -> social_media_setup ──┼-> fan_in -> human_approval_creatives
                                  -> media_buying -> monitoring -> END/loop
```

## Human approval contracts

The workflow pauses using LangGraph `interrupt()` and receives `Command(resume=...)`. The web implementation holds this continuation in process memory. Approval payloads must retain the `step` identifier because `webapp.py` uses it to render the correct UI.

Critical gates:

- `product_approval`: choose/reject/research one of discovered products.
- `supplier_approval`: choose supplier, manual source link, optional backup, or image search.
- `store_config_approval`: choose auto/manual storefront configuration.
- `store_approval` and `stage_one_decision`: review a built store and either loop to product research or advance.
- `video_source_approval` and `creatives_approval`: configure/review production before media buying.

When changing a decision string, inspect both routing functions in `pipeline.py` and the request/approval handling in `webapp.py`.

## State contract

`state/graph_state.py::ECommerceState` defines fields flowing between nodes. Major groups are:

- Product/supply: `products`, `selected_product`, `suppliers`, `selected_supplier`, `unit_economics`, `bound_sourcing`, `binding_sku`.
- Brand/store: `brand`, `brand_identity`, `store_config`, `store`, `store_copy`, `wordpress_landing_page_html`.
- Marketing: `creative_assets`, `creatives`, `social_media`, `content_plan`, `ad_campaigns`, `campaign_plan`, `ad_metrics`.
- Control: `current_phase`, `pending_approval`, `human_approvals`, `errors`, `logs`, `completed_parallel_nodes`.

`messages` uses the LangGraph message reducer. `errors`, `logs`, and approvals append with `operator.add`. `completed_parallel_nodes` has a custom merge reducer to safely join the creative/social fan-out. Never replace a reducer-backed field without understanding parallel behavior.

## Safe workflow modifications

1. Add/alter state field and initialize it in `make_initial_state`.
2. Make each node return a partial update, not a full copied state.
3. Add node and edges in `build_pipeline`; explicitly handle every router output.
4. If UI-visible, map the phase in `webapp.py::PHASE_TO_STAGE` and ensure its interrupt data is serializable.
5. Run `python main.py --dry-run` after installing dependencies and test each altered approval route.
