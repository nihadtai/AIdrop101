from typing import List, Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field, HttpUrl


class ProductCandidate(BaseModel):
    """Represents a potential product for the e-commerce store."""
    name: str = Field(..., description="The name of the product")
    description: str = Field(..., description="A detailed description of the product")
    source_url: str = Field(..., description="URL where the product was found")
    category: str = Field(..., description="Product category")
    estimated_selling_price: float = Field(..., description="Estimated retail price")
    trend_score: int = Field(..., ge=1, le=10, description="Trendiness score from 1 to 10")
    problem_solving_score: int = Field(..., ge=1, le=10, description="Problem-solving capability score from 1 to 10")
    competition_level: str = Field(..., description="Level of competition: 'low', 'medium', or 'high'")
    viral_potential: int = Field(..., ge=1, le=10, description="Potential for virality from 1 to 10")
    image_urls: List[str] = Field(default_factory=list, description="List of product image URLs")
    pros: List[str] = Field(default_factory=list, description="List of product pros")
    cons: List[str] = Field(default_factory=list, description="List of product cons")


class SupplierInfo(BaseModel):
    """Information about a product supplier."""
    supplier_name: str = Field(..., description="Name of the supplier")
    platform: str = Field(..., description="Sourcing platform, e.g., 'alibaba', '1688', 'cj'")
    product_url: str = Field(..., description="URL to the product on the supplier's platform")
    unit_cost: float = Field(..., description="Cost per unit")
    moq: int = Field(..., description="Minimum Order Quantity")
    shipping_cost: float = Field(..., description="Estimated shipping cost")
    shipping_days: int = Field(..., description="Estimated shipping time in days")
    supplier_rating: float = Field(..., description="Supplier's rating on the platform")
    response_rate: float = Field(..., description="Supplier's response rate percentage")
    years_in_business: int = Field(..., description="Number of years the supplier has been in business")


class UnitEconomics(BaseModel):
    """Calculated economics for a product."""
    selling_price: float = Field(..., description="Final retail selling price")
    product_cost: float = Field(..., description="Cost to source the product")
    shipping_cost: float = Field(..., description="Cost to ship the product")
    estimated_cpa: float = Field(..., description="Estimated Cost Per Acquisition for marketing")
    payment_gateway_fee: float = Field(..., description="Estimated payment processing fee")
    net_profit: float = Field(..., description="Calculated net profit per unit")
    profit_margin_pct: float = Field(..., description="Calculated profit margin percentage")
    breakeven_roas: float = Field(..., description="Breakeven Return On Ad Spend")


class BrandIdentity(BaseModel):
    """Brand identity attributes for the store."""
    brand_name: str = Field(..., description="The name of the brand")
    slogan: str = Field(..., description="Brand slogan or tagline")
    color_palette: List[str] = Field(..., description="List of hex color codes for the brand")
    font_pair: str = Field(..., description="Selected font pairing")
    bio_text: str = Field(..., description="Brand biography or about text")
    target_audience: str = Field(..., description="Description of the target audience")
    tone_of_voice: str = Field(..., description="Brand's tone of voice")


class SocialMediaAccount(BaseModel):
    """Information about a created social media account."""
    platform: str = Field(..., description="Platform name, e.g., 'instagram', 'tiktok'")
    username: str = Field(..., description="Account username or handle")
    profile_url: str = Field(..., description="URL to the social media profile")
    follower_count: int = Field(default=0, description="Current follower count")
    status: str = Field(default="created", description="Account status: 'created', 'active', 'suspended'")


class ContentPiece(BaseModel):
    """Represents a piece of content for social media."""
    platform: str = Field(..., description="Target platform for the content")
    content_type: str = Field(..., description="Type of content, e.g., 'post', 'reel', 'tiktok', 'story'")
    caption: str = Field(..., description="Caption for the post")
    media_urls: List[str] = Field(default_factory=list, description="List of media URLs for the content")
    hashtags: List[str] = Field(default_factory=list, description="List of hashtags to use")
    scheduled_at: Optional[datetime] = Field(default=None, description="When the content is scheduled for publication")
    published_at: Optional[datetime] = Field(default=None, description="When the content was actually published")
    engagement_metrics: Dict[str, Any] = Field(default_factory=dict, description="Metrics such as likes, comments, shares")


class StoreInfo(BaseModel):
    """Information about the e-commerce store."""
    platform: str = Field(..., description="Store platform, e.g., 'shopify', 'woocommerce'")
    store_url: str = Field(..., description="URL of the store")
    product_page_url: Optional[str] = Field(default=None, description="URL of the primary product page")
    pixel_installed: bool = Field(default=False, description="Whether the tracking pixel is installed")
    tracking_events: List[str] = Field(default_factory=list, description="List of active tracking events")
    status: str = Field(default="setup", description="Store status")


class CreativeAsset(BaseModel):
    """A creative asset generated for marketing."""
    asset_type: str = Field(..., description="Type of asset, e.g., 'video', 'image', 'carousel'")
    file_path: str = Field(..., description="Local path or URL to the asset file")
    hook_text: str = Field(..., description="The hook text used in the creative")
    cta_text: str = Field(..., description="The Call to Action text used")
    duration_seconds: Optional[float] = Field(default=None, description="Duration in seconds if it's a video")
    has_voiceover: bool = Field(default=False, description="Whether the video has a voiceover")
    has_subtitles: bool = Field(default=False, description="Whether the video has subtitles")


class AdCampaign(BaseModel):
    """An advertising campaign."""
    campaign_id: str = Field(..., description="Platform's ID for the campaign")
    campaign_name: str = Field(..., description="Name of the campaign")
    objective: str = Field(..., description="Campaign objective, e.g., 'CONVERSIONS'")
    daily_budget: float = Field(..., description="Daily budget for the campaign")
    status: str = Field(default="PAUSED", description="Campaign status")
    ad_sets: List[Dict[str, Any]] = Field(default_factory=list, description="List of ad sets in this campaign")


class AdSet(BaseModel):
    """An ad set within a campaign."""
    adset_id: str = Field(..., description="Platform's ID for the ad set")
    name: str = Field(..., description="Name of the ad set")
    targeting: Dict[str, Any] = Field(default_factory=dict, description="Targeting criteria")
    bid_strategy: str = Field(..., description="Bidding strategy used")
    budget: float = Field(..., description="Budget allocated to this ad set")


class Ad(BaseModel):
    """An individual advertisement."""
    ad_id: str = Field(..., description="Platform's ID for the ad")
    creative_asset_id: str = Field(..., description="ID or reference to the creative asset used")
    primary_text: str = Field(..., description="Primary text or caption of the ad")
    headline: str = Field(..., description="Headline of the ad")
    call_to_action: str = Field(..., description="Call to Action used, e.g., 'SHOP_NOW'")
    status: str = Field(default="PAUSED", description="Ad status")


class AdMetrics(BaseModel):
    """Performance metrics for an ad."""
    ad_id: str = Field(..., description="Platform's ID for the ad")
    impressions: int = Field(default=0, description="Number of impressions")
    clicks: int = Field(default=0, description="Number of clicks")
    ctr: float = Field(default=0.0, description="Click-through rate")
    cpc: float = Field(default=0.0, description="Cost per click")
    cpm: float = Field(default=0.0, description="Cost per mille (1000 impressions)")
    conversions: int = Field(default=0, description="Number of conversions/purchases")
    cost_per_conversion: float = Field(default=0.0, description="Cost per conversion")
    roas: float = Field(default=0.0, description="Return On Ad Spend")
    spend: float = Field(default=0.0, description="Total amount spent")
    revenue: float = Field(default=0.0, description="Total revenue generated")


class HumanApproval(BaseModel):
    """A record of a human-in-the-loop approval step."""
    step_name: str = Field(..., description="Name of the step requiring approval")
    description: str = Field(..., description="Description of what needs to be approved")
    data: Dict[str, Any] = Field(default_factory=dict, description="Contextual data for the approval")
    approved: Optional[bool] = Field(default=None, description="Whether it was approved, rejected, or pending")
    feedback: Optional[str] = Field(default=None, description="Feedback provided by the human approver")
    requested_at: datetime = Field(default_factory=datetime.utcnow, description="When the approval was requested")
    responded_at: Optional[datetime] = Field(default=None, description="When the human responded")
