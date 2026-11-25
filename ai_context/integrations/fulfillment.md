# Fulfillment, Orders, and Supplier Resilience

`tools/binding_store.py` creates the durable product-primary/backup supplier relationship and a binding SKU. `supplier_monitor.py` periodically tests price/stock, creates alerts, selects backups, and optionally switches failover. `order_automation.py` creates and syncs local orders across CJ/AliExpress paths.

`fulfillment_bridge.py` verifies Shopify webhook HMAC, translates paid orders into fulfillment work, pushes tracking back to Shopify, and compares bulk versus dropship costs. These functions affect actual customer fulfillment: preserve idempotency and validate signatures before acting.

Related: `workflow/stage-one.md`, `web/operations-api.md`, `governance/security.md`.
