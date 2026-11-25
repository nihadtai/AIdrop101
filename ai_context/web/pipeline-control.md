# Pipeline Control API and Dashboard

The dashboard template is `templates/dashboard.html`. Core routes in `webapp.py` are `POST /api/pipeline/start`, `POST /api/pipeline/approve`, `POST /api/pipeline/reset`, and GET status/results/log routes. `StartRequest` and `ApproveRequest` define their accepted input fields.

`_run_pipeline_task` updates pipeline status, maps graph phases to UI stages, invokes the compiled graph, detects `__interrupt__`, and builds UI approval payloads. Preserve JSON-serializability and backwards compatibility when adding result fields or approval options.

Related: `workflow/approvals.md`, `agents/creative.md`, `integrations/creative.md`.
