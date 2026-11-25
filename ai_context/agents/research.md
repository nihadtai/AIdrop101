# Product and Market Research Agents

`agents/product_research.py` creates scored winning-product candidates, cleans sourcing queries/Chinese keywords, sanitizes URLs, and persists discovery-pool knowledge. `agents/market_research.py` produces a deeper report when the product approval decision requests research.

Inputs: category, rejected products, user identity, existing discovery context. Outputs: `products`, `selected_product`, and optionally `market_research_report`. Do not send every historic radar report to the model; pass ranked candidates and only the evidence needed for the current choice.

Related tools: `trend_tools.py`, `rapidapi_tiktok.py`, `amazon_tracker.py`, `shopify_tracker.py`, `cross_radar.py`, `discovery_pool.py`.
