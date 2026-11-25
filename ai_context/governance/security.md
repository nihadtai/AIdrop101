# Security and Side-Effect Rules

Secrets belong in `.env` and must never reach committed docs, logs, telemetry, browser payloads, screenshots, or error responses. Validate external webhook signatures, sanitize/validate file and URL inputs, and use allow-listed paths when exposing local documents.

Classify calls before changing them: reads may be retried/cached cautiously; writes such as publish, campaign creation, order creation, notifications, provider generation, and supplier failover require explicit handling and should not be replayed by a generic cache.

Related: `architecture/boundaries.md`, `integrations/fulfillment.md`, `integrations/distribution.md`.
