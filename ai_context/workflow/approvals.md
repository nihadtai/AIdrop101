# Human Approval Gates

Pipeline pauses use LangGraph `interrupt()` and continue using `Command(resume=...)`. `webapp.py::_run_pipeline_task` inspects each payload’s `step` and holds the awaiting decision in process memory. Gate steps include `product_approval`, `supplier_approval`, `store_config_approval`, `store_approval`, `stage_one_decision`, `video_source_approval`, and `creatives_approval`.

When changing a gate, update its payload in `pipeline.py`, route function, request model/approval handler in `webapp.py`, and dashboard renderer. Decisions are contracts: changing a string without every consumer creates dead-end runs.
