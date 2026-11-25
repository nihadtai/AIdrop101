# FastAPI Application Lifecycle

`webapp.py` constructs FastAPI, static mounts, CORS, and global in-process pipeline control state. It imports graph/bootstrap utilities from `main.py`, starts a compiled pipeline in a background task, and makes interrupts interactive through async events.

The global `pipeline_state`, queues, and approval variables mean one process owns one active workflow context. Adding multiple users/workers requires durable job, state, and event infrastructure—not only a route change.

Related: `web/pipeline-control.md`, `runtime/persistence.md`, `architecture/boundaries.md`.
