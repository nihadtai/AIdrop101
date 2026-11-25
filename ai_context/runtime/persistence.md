# Runtime Persistence

Runtime data is split between ignored SQLite/JSON operational stores and committed radar examples/reports under `data/`. `tools/db_utils.py` standardizes SQLite connections. Product discovery, bindings, supplier watches, and orders use their own tool-owned stores.

`workflows/pipeline.py` currently compiles with LangGraph `MemorySaver`; a restart loses graph checkpoints and `webapp.py` global pipeline state. Do not claim workflow recovery or multi-worker safety without replacing these process-local mechanisms.

Related: `integrations/discovery.md`, `integrations/sourcing.md`, `integrations/fulfillment.md`.
