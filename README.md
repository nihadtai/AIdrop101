# 🛒 AI-Powered Multi-Agent E-Commerce Automation System

> Süni intellekt və çoxagentli (multi-agent) arxitektura ilə e-ticarəti uçdan-uca avtomatlaşdıran sistem.

## 🏗️ Arxitektura

```
                          ┌──────────────────────────┐
                          │   👑 Orchestrator         │
                          │   (LangGraph Pipeline)    │
                          └────────────┬─────────────┘
                                       │
         ┌─────────────────────────────┼─────────────────────────────┐
         │                             │                             │
  ┌──────┴──────┐              ┌──────┴──────┐              ┌──────┴──────┐
  │ 🔍 Product  │              │ 🚢 Sourcing │              │ 📱 Social   │
  │  Research   │              │    Agent     │              │ Media Agent │
  └──────┬──────┘              └──────┬──────┘              └──────┬──────┘
         │                             │                             │
  ┌──────┴──────┐              ┌──────┴──────┐              ┌──────┴──────┐
  │ 💻 Store    │              │ 🎬 Creative │              │ 📈 Media    │
  │  Builder    │              │   Agent     │              │ Buyer Agent │
  └─────────────┘              └─────────────┘              └─────────────┘
```

## ⚡ Quick Start

```bash
# 1. Virtual environment yaradın
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

# 2. Asılılıqları quraşdırın
pip install -r requirements.txt

# 3. Google Gemini API açarını quraşdırın
copy .env.example .env
# .env faylına GOOGLE_API_KEY əlavə edin (https://aistudio.google.com/apikey)

# 4. Sistemi işə salın (Gemini 3.8 Flash)
python main.py

# Arxitektura qrafını və axını yoxlamaq (Dry-Run rejimi)
python main.py --dry-run

# Fərqli Gemini modeli ilə icra (məsələn, ən sürətli gemini-3.5-flash-lite)
python main.py --model gemini-3.5-flash-lite
```

## 🧠 AI Context Brain

`ai_context/` is the project brain: a selective, versioned Markdown tree. Start with `ai_context/START_HERE.md`, then load only the relevant reference from `architecture/`, `runtime/`, `workflow/`, `agents/`, `integrations/`, `web/`, or `governance/`. Each focused document links its implementation files, contracts, dependencies, and safety constraints. Update that reference whenever a component contract changes.

Start the web app with `python webapp.py`, then open `http://localhost:8000/ai-context` to browse the full context library and live token/latency telemetry. The runtime companion normalizes oversized prompts, caches exact non-search requests for five minutes, and never places prompt/response contents in telemetry. Tune it with `AI_CONTEXT_INPUT_CHAR_BUDGET` and `AI_CONTEXT_CACHE_TTL_SECONDS` in `.env`.

For the standalone graph workspace, run `npm run brain`. It starts the lightweight Brain server on port `8100` and opens the graph in your browser. Set `BRAIN_PORT` to override the port.

## 📁 Layihə Strukturu

```
ecommerce-agent/
├── config/
│   ├── __init__.py
│   └── settings.py              # Pydantic Settings — bütün konfiqurasiya
│
├── state/
│   ├── __init__.py
│   ├── models.py                # Pydantic v2 data modelləri (17 model)
│   └── graph_state.py           # LangGraph paylaşılan state (TypedDict)
│
├── agents/
│   ├── __init__.py
│   ├── base.py                  # BaseAgent (abstrakt əsas sinif)
│   ├── product_research.py      # 🔍 Məhsul araşdırması agenti
│   ├── sourcing.py              # 🚢 Tədarük agenti
│   ├── social_media.py          # 📱 Sosial media agenti
│   ├── store_builder.py         # 💻 Mağaza qurma agenti
│   ├── creative.py              # 🎬 Kreativ istehsal agenti
│   └── media_buyer.py           # 📈 Meta reklam agenti
│
├── tools/
│   ├── __init__.py
│   ├── trend_tools.py           # TikTok, Google Trends, Amazon, Meta Ad Library
│   ├── sourcing_tools.py        # 1688, Alibaba, CJ Dropshipping
│   ├── social_tools.py          # Instagram & TikTok posting
│   ├── shopify_tools.py         # Shopify Admin API
│   ├── creative_tools.py        # Video/Voiceover/Subtitle generation
│   ├── meta_ads_tools.py        # Meta Marketing API
│   └── notification_tools.py    # Telegram & Slack notifications
│
├── workflows/
│   ├── __init__.py
│   ├── pipeline.py              # 🔧 LangGraph ana pipeline (orchestrator)
│   └── human_approval.py        # ⏸️ Human-in-the-Loop manager
│
├── .env.example                 # API açarları şablonu
├── requirements.txt             # Python asılılıqları
├── main.py                      # 🚀 Giriş nöqtəsi (CLI)
└── README.md                    # Bu fayl
```

## 🔄 Pipeline Axını

```
START
  │
  ▼
┌─────────────────┐
│ Product Research │  ← TikTok trends, Google Trends, Amazon BSR
└────────┬────────┘
         ▼
┌─────────────────┐
│ 👤 HUMAN REVIEW │  → Məhsulları təsdiqləyin
└────────┬────────┘
         ▼
┌─────────────────┐
│    Sourcing     │  ← 1688, Alibaba, CJ Dropshipping
└────────┬────────┘
         ▼
┌─────────────────┐
│ 👤 HUMAN REVIEW │  → Tədarükçü və marjanı təsdiqləyin
└────────┬────────┘
         ▼
  ┌──────┴───────┬────────────────┐
  │              │                │
  ▼              ▼                ▼
┌──────┐   ┌──────────┐   ┌───────────┐
│Social│   │  Store    │   │ Creative  │   ← PARALEL İCRA
│Media │   │  Builder  │   │Production │
└──┬───┘   └────┬─────┘   └─────┬─────┘
   └─────────────┼───────────────┘
                 ▼
┌─────────────────┐
│ 👤 HUMAN REVIEW │  → Kreativləri və mağazanı təsdiqləyin
└────────┬────────┘
         ▼
┌─────────────────┐
│  Media Buying   │  ← Meta Ads kampaniyaları yaradır
└────────┬────────┘
         ▼
┌─────────────────┐
│   Monitoring    │  ← ROAS izləmə, büdcə optimallaşdırma
└────────┬────────┘
         ▼
        END
```

## 🔑 Tələb Olunan API Açarları

| Xidmət | Env Dəyişəni | Məqsəd |
|:---|:---|:---|
| OpenAI / Anthropic / Google | `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` | LLM beyin |
| Meta (Facebook) | `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID` | Reklam idarəetməsi |
| Shopify | `SHOPIFY_STORE_URL`, `SHOPIFY_ACCESS_TOKEN` | Mağaza yaratma |
| TikTok | `TIKTOK_ACCESS_TOKEN` | Kontent paylaşma |
| ElevenLabs | `ELEVENLABS_API_KEY` | Voiceover |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Bildirişlər |

## 🛡️ Human-in-the-Loop

Sistem 3 kritik nöqtədə dayanır və sizin təsdiğinizi gözləyir:

1. **Məhsul Təsdiqi** — AI-ın tapdığı məhsulları nəzərdən keçirin
2. **Tədarükçü Təsdiqi** — Maya dəyəri və mənfəət marjasını yoxlayın  
3. **Kreativ Təsdiqi** — Reklam videoları və mağazanı yoxlayıb reklam büdcəsini buraxın

Bu, maliyyə risklərini minimuma endirir.

## 📝 License

MIT
