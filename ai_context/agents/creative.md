# Creative Agent

`agents/creative.py` plans ad hooks/scripts and coordinates creative assets. It relies on typed models for hook variants, script plans, ad scripts, and creative assets. It runs in parallel with the Social Media agent and must mark its parallel completion exactly once.

Inputs: approved product, store/brand material, supplied videos or avatar/b-roll choices, language, quality tier, and requested variants. Outputs: `creatives`, `creative_assets`, and production logs/errors.

Related: `integrations/creative.md`, `workflow/stage-two.md`, `web/pipeline-control.md`.
