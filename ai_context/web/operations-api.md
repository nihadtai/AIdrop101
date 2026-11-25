# Operations API Groups

`webapp.py` groups operational endpoints for Postiz/social generation, Shopify/WordPress preview/export, catalog browsing, radar scans/reports/subscribers, orders/webhooks/tracking, supplier monitoring, 1688 cookies, AliExpress OAuth, bindings, and integration settings/testing.

Before altering an endpoint, trace its Pydantic request model and tool call. Prefer explicit POST for actions, validate webhook authenticity, return stable JSON error shapes, and never pass provider secrets or untrusted URLs directly to sensitive downstream operations.

Related: `integrations/`, `governance/security.md`, `governance/verification.md`.
