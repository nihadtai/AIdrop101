# Supplier Discovery and Economics

`tools/sourcing_tools.py` handles FX lookup, 1688 browser/image search, cookies, direct scraping, CJ wrapper access, and economics helpers. `cj_dropshipping_tool.py` implements CJ tokens, product/freight/stock/order operations. `aliexpress_ds_tool.py` implements OAuth, signed IOP calls, product/freight/status/order operations. `cargo_3pl.py` models 1688-to-YunExpress logistics and landed cost.

`supplier_scorer.py` ranks product/supplier matches using text and optional visual similarity. Normalized supplier data must retain platform/channel identifiers and source link so fulfillment, monitoring, and failover remain possible.

Related: `agents/sourcing.md`, `integrations/fulfillment.md`, `web/operations-api.md`.
