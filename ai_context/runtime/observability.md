# Observability

`webapp.py::_add_log` retains pipeline log entries and broadcasts them to SSE listeners. `ai_context/brain.py` records process-local estimated input/output tokens, latency, trimming, cache status, model, agent, and short errors. It does not put prompt/response content in telemetry.

`/ai-context` reads the Markdown manifest from `ai_context/catalog.py` and telemetry endpoints. The cache and events reset when the process restarts or `DELETE /api/ai-context/telemetry` is called.

Related: `web/context-library.md`, `agents/base-agent.md`.
