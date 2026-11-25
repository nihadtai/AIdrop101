# Settings and Secrets

`config/settings.py` contains nested provider settings and cached `get_settings()`. `.env.example` is the complete non-secret configuration template. Main secrets cover Gemini, Meta, Shopify, TikTok/RapidAPI, Postiz, ElevenLabs, Fal/Kling, and notifications.

Add any new runtime behavior as a typed `Settings` field and document its env variable in `.env.example`. Never expose values through FastAPI response models, dashboard scripts, Markdown, or error strings.

Related: `integrations/` documents and `governance/security.md`.
