# Discovery and Radar Integrations

`tools/trend_tools.py` is the agent-facing trend wrapper. It combines Google/TikTok/Amazon/Meta signals with local radar data. `amazon_tracker.py` and `shopify_tracker.py` scan source catalogs; `cross_radar.py` merges cross-platform evidence; `radar_common.py` supplies polite HTTP, snapshots, signals, and report writing; `rapidapi_tiktok.py` accesses TikTok provider data.

`tools/discovery_pool.py` persists discovered products and user-specific seen/rejected status, preventing repeated candidate recommendations. Outputs are evidence, not ground truth: preserve source URLs/timestamps and handle provider failure honestly.

Related: `agents/research.md`, `web/operations-api.md`, `runtime/persistence.md`.
