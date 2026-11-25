# Store Builder Agent

`agents/store_builder.py` converts approved product/supplier economics into brand identity, store configuration, structured product copy, Shopify-oriented artifacts, and WordPress landing-page HTML. It contains the rendering/copy helpers and is intentionally much larger than other agents.

Inputs: selected product, supplier, economics, brand/store configuration, design presets, font pairs, and product images. Outputs: `brand`, `brand_identity`, `store_config`, `store_copy`, `store`, and `wordpress_landing_page_html`.

Related: `integrations/storefront.md`, `tools/design_intel.py`, `tools/product_image_search.py`, `workflow/stage-one.md`.
