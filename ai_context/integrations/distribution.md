# Social Distribution and Advertising

`tools/social_tools.py` exposes Instagram/TikTok profile and publishing functions. `postiz_tool.py` stores Postiz configuration, connects channels, uploads media, and schedules individual/content-plan posts. `meta_ads_tools.py` wraps campaign, ad set, ad, insights, budget, pause, and automated-rule actions.

Posts and ads have public/financial effects. Do not cache side-effecting tool calls, and retain a confirmation boundary for new campaign/post behavior. Store platform identifiers returned by providers for later status/update operations.

Related: `agents/social-media.md`, `agents/media-buyer.md`, `web/operations-api.md`.
