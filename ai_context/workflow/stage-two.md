# Stage Two: Creative to Monitoring

After video/source approval, `parallel_fanout_creative_social` starts Creative and Social Media nodes concurrently. Both must append to `completed_parallel_nodes`; `fan_in_creative_social` proceeds only when both are present. Creative approval can repeat the fan-out or advance to media buying.

Media buying creates the campaign plan and campaign artifacts. Monitoring invokes supplier/order monitoring and may route to another monitor cycle based on metrics. Preserve reducer semantics whenever changing this parallel segment.

Related files: `agents/creative.py`, `agents/social_media.py`, `agents/media_buyer.py`, `tools/supplier_monitor.py`.
