# AI Context: Web UI and API

## Web app architecture

`webapp.py` owns the FastAPI app and static mounts. It starts the compiled graph in a background task and keeps one process-local `pipeline_state` dictionary containing status, current phase/stage, results, logs, approval data, timestamps, and errors. `approval_event`, `approval_decision`, and `current_pipeline_task` coordinate the interactive continuation.

This design is appropriate for a single local process. It is not safe for multiple Uvicorn workers, multiple concurrent users, or restarts requiring job recovery without replacing the globals with durable storage/queueing.

## Important pages

| URL | Template/use |
| --- | --- |
| `/` | Landing page. |
| `/app` | Main pipeline control dashboard (`templates/dashboard.html`). |
| `/ai-context` | Context-library and runtime observability dashboard (`templates/ai_context.html`). |
| `/api/health` | Basic application health status. |

## Pipeline API surface

- `POST /api/pipeline/start`: starts a run with model, category, approval mode, thread ID, and user ID.
- `POST /api/pipeline/approve`: supplies a decision and optional supplier/store/creative fields to the interrupted graph.
- `POST /api/pipeline/reset`: aborts/resets the active in-process run.
- `GET /api/pipeline/status`, `/results`, `/logs`: read current state and logs.
- The SSE event stream is used by the dashboard for live pipeline logs; preserve event payload compatibility when changing `_add_log`.

## Operational API groups

`webapp.py` also exposes endpoints for Postiz/social generation, Shopify/WordPress exports, catalog previews, radar reports and subscription, orders/webhooks, supplier monitoring, sourcing session/OAuth/bindings, and integration settings/testing. Before changing a route, use its request Pydantic model and trace the tool it calls.

## AI Context API

- `GET /api/ai-context/documents`: context-library manifest with titles and descriptions.
- `GET /api/ai-context/documents/{id}`: one registered Markdown document as text data; IDs are allow-listed in `ai_context/catalog.py`.
- `GET /api/ai-context/telemetry`: process-local token/latency/cache metrics.
- `DELETE /api/ai-context/telemetry`: clears in-memory metrics and response cache.

The HTML view must not expose `.env`, raw prompts, model responses, credentials, or full integration responses. Render Markdown as text/preformatted content unless a vetted server-side renderer and sanitization policy are added.
