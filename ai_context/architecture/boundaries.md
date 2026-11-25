# Architectural Boundaries

`config/settings.py` owns typed configuration. `agents/base.py` owns provider retry/fallback/context behavior. `workflows/pipeline.py` owns decisions between stages and human interrupts. `tools/*.py` own integration calls and local persistence. `webapp.py` owns HTTP/UI lifecycle.

Keep business side effects behind explicit tool calls and graph approval gates. Keep secrets in `.env`. Do not put full state, raw provider payloads, or credentials into logs, telemetry, HTML, or context Markdown.

Related: `governance/security.md`, `integrations/fulfillment.md`, `web/application.md`.
