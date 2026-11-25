# Shared State Contract

`state/graph_state.py::ECommerceState` is the graph-wide TypedDict. `main.py::make_initial_state` must initialize every newly added optional/collection field. `state/models.py` supplies domain Pydantic models used for validation and structured concepts.

State groups: discovery (`products`, `selected_product`), sourcing (`suppliers`, `unit_economics`, `binding_sku`), storefront (`brand`, `store_config`, `store`), growth (`creatives`, `social_media`, `ad_campaigns`), and control (`pending_approval`, `human_approvals`, `logs`, `errors`).

`logs`, `errors`, and approvals append via reducers. `completed_parallel_nodes` uses a custom reducer for the creative/social fan-in. Return partial updates only; replacing reducer-managed lists can break concurrent nodes.
