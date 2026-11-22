import operator
from typing import TypedDict, Annotated, List, Dict, Optional, Any
from langgraph.graph.message import add_messages


def _merge_parallel_nodes(left: List[str], right: List[str]) -> List[str]:
    """
    Custom reducer for completed_parallel_nodes.
    - If right is an empty list → reset (fan-out started fresh)
    - Otherwise → deduplicate-append (fan-in accumulates)
    """
    if right == []:
        return []
    seen = set(left)
    result = list(left)
    for item in right:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


class ECommerceState(TypedDict):
    """
    Shared state for the e-commerce automation multi-agent system.
    This state flows through the LangGraph graph, accumulating context.
    """
    # Current execution phase (e.g., 'product_research', 'store_creation')
    current_phase: str
    
    # User-selected product category & tenant/user identifier
    category: str
    user_id: Optional[str]
    
    # Products and Sourcing
    products: List[Dict[str, Any]]
    selected_product: Optional[Dict[str, Any]]
    rejected_products: Annotated[List[str], operator.add]
    market_research_report: Optional[str]
    suppliers: List[Dict[str, Any]]
    selected_supplier: Optional[Dict[str, Any]]
    unit_economics: Optional[Dict[str, Any]]
    bound_sourcing: Optional[Dict[str, Any]]
    binding_sku: Optional[str]
    
    # Branding and Identity
    brand: Optional[Dict[str, Any]]
    brand_identity: Optional[Dict[str, Any]]
    
    # Marketing and Storefront
    social_accounts: List[Dict[str, Any]]
    content_plan: List[Dict[str, Any]]
    social_media: Optional[Dict[str, Any]]
    postiz: Optional[Dict[str, Any]]
    store: Optional[Dict[str, Any]]
    store_config: Optional[Dict[str, Any]]
    store_copy: Optional[Dict[str, Any]]
    wordpress_landing_page_html: Optional[str]
    creatives: List[Dict[str, Any]]
    creative_assets: Optional[Dict[str, Any]]
    manual_video_links: Optional[List[str]]
    creative_config: Optional[Dict[str, Any]]

    # Stage 1 Winning Catalog (persisted across "Return to Agent 1" loops)
    winning_catalog: List[Dict[str, Any]]
    active_catalog_id: Optional[str]
    
    # Advertising
    ad_campaigns: List[Dict[str, Any]]
    campaign_plan: Optional[Dict[str, Any]]
    ad_metrics: List[Dict[str, Any]]
    
    # Human-in-the-loop and Execution context
    human_approvals: Annotated[List[Dict[str, Any]], operator.add]
    pending_approval: Optional[Dict[str, Any]]
    
    # Messages use the add_messages reducer for conversational memory
    messages: Annotated[list, add_messages]
    
    # Error tracking and Logs (using operator.add for safe parallel graph updates)
    errors: Annotated[List[str], operator.add]
    logs: Annotated[List[str], operator.add]
    completed_parallel_nodes: Annotated[List[str], _merge_parallel_nodes]
