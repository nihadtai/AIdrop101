# Storefront and Design Integrations

`tools/shopify_tools.py` wraps product creation, pixel setup, product page updates, and discounts. `tools/design_intel.py` provides design presets, font pairs, contrast-safe design resolution, and storefront HTML audit. `product_image_search.py` finds reachable product imagery. `catalog_store.py` persists winning catalog records.

Shopify calls can change a live store. The Store Builder agent should prepare artifacts; approval/workflow code decides when to enact side effects. Keep generated HTML and image URLs serializable for web preview/export routes.

Related: `agents/store-builder.md`, `workflow/stage-one.md`, `web/operations-api.md`.
