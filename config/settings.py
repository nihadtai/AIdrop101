from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, Field


class LLMSettings(BaseModel):
    """Settings for Language Model Providers."""
    provider: str = "google"
    model: str = "gemini-3.5-flash-lite"
    temperature: float = 0.7
    max_output_tokens: int = 8192


class MetaAdsSettings(BaseModel):
    """Settings for Meta Ads API integration."""
    access_token: Optional[str] = None
    ad_account_id: Optional[str] = None
    app_id: Optional[str] = None
    app_secret: Optional[str] = None
    pixel_id: Optional[str] = None


class ShopifySettings(BaseModel):
    """Settings for Shopify store integration."""
    store_url: Optional[str] = None
    access_token: Optional[str] = None


class SocialMediaSettings(BaseModel):
    """Settings for Social Media accounts & Self-Hosted Postiz integration."""
    tiktok_access_token: Optional[str] = None
    instagram_access_token: Optional[str] = None
    instagram_account_id: Optional[str] = None
    postiz_api_url: str = "http://localhost:5000"
    postiz_api_key: Optional[str] = None


class NotificationSettings(BaseModel):
    """Settings for notifications via Telegram and Slack."""
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    slack_webhook_url: Optional[str] = None


class SourcingSettings(BaseModel):
    """Settings for product sourcing platforms."""
    cj_api_key: Optional[str] = None


class CreativeSettings(BaseModel):
    """Settings for creative generation APIs (ElevenLabs voice, Fal.ai UGC/B-Roll, Cloud Render)."""
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: str = "cgSgspJ2msm6clMCkdW9"
    fal_api_key: Optional[str] = None
    kling_api_key: Optional[str] = None
    shotstack_api_key: Optional[str] = None
    creatomate_api_key: Optional[str] = None


class Settings(BaseSettings):
    """Main settings configuration loading from .env file."""
    
    # Provider API keys
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = Field(default=None, validation_alias="GOOGLE_API_KEY")
    
    # Creative & AI UGC API Keys (loaded exclusively from .env)
    elevenlabs_api_key: Optional[str] = Field(
        default=None,
        validation_alias="ELEVENLABS_API_KEY",
    )
    elevenlabs_voice_id: Optional[str] = Field(default="cgSgspJ2msm6clMCkdW9", validation_alias="ELEVENLABS_VOICE_ID")
    fal_api_key: Optional[str] = Field(
        default=None,
        validation_alias="FAL_KEY",
    )
    kling_api_key: Optional[str] = Field(default=None, validation_alias="KLING_API_KEY")
    shotstack_api_key: Optional[str] = Field(default=None, validation_alias="SHOTSTACK_API_KEY")
    creatomate_api_key: Optional[str] = Field(default=None, validation_alias="CREATOMATE_API_KEY")
    
    # RapidAPI TikTok & Self-Hosted Postiz
    rapidapi_key: Optional[str] = None  # set RAPIDAPI_KEY in .env
    rapidapi_tiktok_host: Optional[str] = "tiktok239.p.rapidapi.com"
    postiz_api_url: str = Field(default="http://localhost:5000", validation_alias="POSTIZ_API_URL")
    postiz_api_key: Optional[str] = Field(default=None, validation_alias="POSTIZ_API_KEY")
    
    redis_url: Optional[str] = "redis://localhost:6379/0"
    database_url: Optional[str] = "sqlite:///ecommerce.db"
    
    llm: LLMSettings = LLMSettings()
    meta_ads: MetaAdsSettings = MetaAdsSettings()
    shopify: ShopifySettings = ShopifySettings()
    social_media: SocialMediaSettings = SocialMediaSettings()
    notifications: NotificationSettings = NotificationSettings()
    sourcing: SourcingSettings = SourcingSettings()
    creative: CreativeSettings = CreativeSettings()

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore"
    )


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
