# Sourcing Agent

`agents/sourcing.py` searches suppliers and computes unit economics. The supplier approval node in `workflows/pipeline.py` performs the final selection/binding because it must honor human-provided source links and backup/failover policy.

Inputs: selected product including clean sourcing terms and product image. Outputs: normalized `suppliers`, `selected_supplier`, and `unit_economics`. Supplier records must preserve source channel, IDs/SKUs, order link, price/shipping, delivery expectations, image, and verification status.

Related: `integrations/sourcing.md`, `integrations/fulfillment.md`, `workflow/stage-one.md`.
