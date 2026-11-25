# Social Media Agent

`agents/social_media.py` derives a brand identity and content strategy, then creates profile/content-plan data and can invoke account/publication tools. It runs in parallel with creative production; do not use it to modify creative-only state.

Inputs: product, brand/store output, creative references, platform constraints. Outputs: `social_accounts`, `content_plan`, `social_media`, and optionally Postiz-related data.

Related: `integrations/distribution.md`, `workflow/stage-two.md`, `web/operations-api.md`.
