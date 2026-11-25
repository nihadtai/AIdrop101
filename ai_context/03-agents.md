# AI Context: Agents

All specialist agents inherit `agents/base.py::BaseAgent`. It centralizes Gemini calls, model fallbacks, retries, optional Google Search grounding, and AI Context telemetry. New agents should reuse `_call_gemini` or `_call_gemini_with_tools`; do not instantiate provider clients in individual agent code.

| Agent | File | Main responsibility | Typical state produced |
| --- | --- | --- | --- |
| Product Research | `agents/product_research.py` | Finds/scoring candidate products using trend and competitor signals. | `products`, `selected_product`, research logs. |
| Market Research | `agents/market_research.py` | Produces deeper demand/market validation when requested at product review. | `market_research_report`. |
| Sourcing | `agents/sourcing.py` | Finds source options and calculates unit economics. | `suppliers`, `selected_supplier`, `unit_economics`. |
| Store Builder | `agents/store_builder.py` | Builds branded product/store artifacts and exportable storefront material. | `brand`, `store_config`, `store`, `store_copy`, landing-page HTML. |
| Creative | `agents/creative.py` | Generates ad strategy, scripts, voice/video/subtitle assets. | `creatives`, `creative_assets`. |
| Social Media | `agents/social_media.py` | Creates social presence/content and invokes publishing integrations. | `social_accounts`, `content_plan`, `social_media`, `postiz`. |
| Media Buyer | `agents/media_buyer.py` | Creates campaigns/ad sets/ads and interprets metrics. | `ad_campaigns`, `campaign_plan`, `ad_metrics`. |

## Agent implementation pattern

1. Read only fields required from `state`.
2. Build a specific system instruction and prompt, requesting machine-readable output where possible.
3. Use tools for external data/action; retain graceful fallback behavior where APIs are unavailable.
4. Validate/normalize response into a partial state update.
5. Return concise `logs` and `errors` entries rather than throwing away useful recovery information.

## Context and token discipline

- Put stable role rules in the system instruction; send only relevant state slices in the prompt.
- Do not serialize the complete graph state, raw tool responses, historic logs, or unused catalog entries into each call.
- Preserve identifiers and URLs needed by the next node; summarize verbose research before passing it downstream.
- Tool-capable calls are intentionally not cached because tools may perform reads with freshness requirements or write side effects.
- Search-grounded calls are not reused from cache, because freshness is part of their contract.

## Failure behavior

`BaseAgent` tries candidate Gemini models and applies bounded retries. A provider failure should become a useful state error/log where the domain can safely continue. Do not silently invent marketplace, campaign, or fulfillment results when an external integration fails.
