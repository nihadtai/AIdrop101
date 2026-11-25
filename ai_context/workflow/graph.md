# LangGraph Pipeline

`workflows/pipeline.py::build_pipeline` is the graph source of truth. Stage 1 is product research -> product approval -> sourcing -> supplier approval -> store configuration -> store build -> store/stage-one review. Stage 2 is video-source approval -> parallel creative and social generation -> creative approval -> media buying -> monitoring.

The product loop returns via `reset_product_cycle`; the creative loop returns to its parallel fan-out. Node additions require a `StateGraph` registration, incoming/outgoing edges, explicit router mapping, and UI phase mapping in `webapp.py::PHASE_TO_STAGE`.

Related: `workflow/approvals.md`, `workflow/stage-one.md`, `workflow/stage-two.md`.
