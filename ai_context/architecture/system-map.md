# System Map

The product is a Python e-commerce automation system. `main.py` is the CLI bootstrap; `webapp.py` is the FastAPI control plane; `workflows/pipeline.py` owns LangGraph ordering; `agents/` decides domain output; `tools/` owns external I/O and deterministic operations; `state/` defines shared contracts.

Related references: `runtime/bootstrap.md`, `workflow/graph.md`, `web/application.md`. Start here before changing a cross-cutting concern.

Dependency direction is `web/CLI -> workflow -> agents -> tools`; `state` and `config` are shared foundations. Do not make tools import web UI globals or let agents own graph routing.
