# Creative Production Integrations

`tools/creative_tools.py` handles scripts, voiceover, timestamps, subtitles, media validation, and local FFmpeg composition. `fal_video_engine.py` estimates cost, checks provider balance/configuration, creates visual/video jobs, and tracks actual creative cost. `creative_cards.py` renders branded cards through Playwright or Pillow fallback.

Generated media is written under ignored `ad_video/` or `static/media/`. Keep provider-cost preflight visible in the API/UI before creating paid generation jobs. Media rendering must gracefully report missing FFmpeg, browser, provider key, or source asset.

Related: `agents/creative.md`, `workflow/stage-two.md`, `web/pipeline-control.md`.
