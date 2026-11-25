# Media Buyer Agent

`agents/media_buyer.py` transforms approved creative/store context into audience targeting and campaign structure. It uses Meta tool wrappers to create campaign, ad set, ad, read insights, alter budgets, pause ads, and apply rules.

Inputs: product, economics, creatives, landing/store data, targeting constraints. Outputs: `campaign_plan`, `ad_campaigns`, and `ad_metrics`. Advertising calls can spend money or change delivery; preserve the human approval boundary before adding automatic action.

Related: `integrations/distribution.md`, `workflow/stage-two.md`, `governance/security.md`.
