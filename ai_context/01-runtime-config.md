# AI Context: Runtime and Configuration

## Supported entry points

- `python main.py`: terminal execution of the full LangGraph workflow.
- `python main.py --dry-run`: builds the graph and prints its structure without requiring a live API key.
- `python main.py --model <model>`: chooses a Gemini model for the run.
- `python webapp.py`: FastAPI dashboard, normally served on `http://localhost:8000`.

`requirements.txt` defines runtime packages. Key dependencies are Google GenAI, LangGraph, FastAPI/Uvicorn, Pydantic Settings, SSE Starlette, and provider/integration libraries. Install before importing the web app: `pip install -r requirements.txt`.

## Settings and secrets

`config/settings.py` defines Pydantic `Settings`. `.env.example` is the non-secret reference. The main provider key is `GOOGLE_API_KEY`; integrations include Shopify, Meta Ads, TikTok/RapidAPI, Postiz, ElevenLabs, Fal, and notifications.

Important rules:

- Read settings through `get_settings()` rather than parsing environment variables in feature code, except the context brain's lightweight boot-time configuration.
- Never return raw API keys from routes or place them in the frontend.
- `Settings` allows extra environment values so legacy variables may exist; add a typed setting before making a new value part of application behavior.

## AI Context Brain settings

The context library lives in `ai_context/*.md`. The runtime companion in `ai_context/brain.py` supplies telemetry and conservative exact-call reuse. Its defaults can be changed in `.env`:

| Variable | Default | Effect |
| --- | ---: | --- |
| `AI_CONTEXT_ENABLED` | `true` | Enables prompt budget enforcement and exact non-search response reuse. |
| `AI_CONTEXT_INPUT_CHAR_BUDGET` | `24000` | Maximum prompt plus system-instruction characters retained before compaction. |
| `AI_CONTEXT_CACHE_TTL_SECONDS` | `300` | In-memory TTL for identical non-search requests. |
| `AI_CONTEXT_MAX_EVENTS` | `500` | Number of in-memory telemetry events shown in the HTML page. |

The cache is process-local and ephemeral. It must not be treated as business persistence or cross-user memory.

## Runtime persistence and generated files

- `data/radar/` contains snapshots and reports used by the Trend Radar. These are operational artifacts, not application source.
- SQLite files and volatile runtime data are ignored by Git according to `.gitignore`.
- Generated video and media belong in `ad_video/` or `static/media/`, both ignored.
- LangGraph currently uses `MemorySaver` in `build_pipeline`; workflow checkpoints are lost on process restart.

## Logging and diagnosis

`main.py`, `webapp.py`, and agents use Python logging. Web runs also accumulate `pipeline_state["logs"]` and broadcast entries over SSE. AI Context telemetry records only metadata—estimated tokens, latency, cache status, error summary—not prompt or response text.
