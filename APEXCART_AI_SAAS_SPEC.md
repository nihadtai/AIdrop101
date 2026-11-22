# ApexCart AI — Layihənin Tam Texniki və Biznes Spesifikasiyası (SaaS Quruluşu Üçün)

> **Sənədin Məqsədi:** Bu sənəd **ApexCart AI** (Multi-Agent E-Commerce & Dropshipping Automation Platform) layihəsinin nə işə yaradığını, bütün mövcud 6+1 AI agentini, real API/alət inteqrasiyalarını, backend endpoint-lərini, verilənlər modellərini və bu layihəni **tam istehsala hazır (production-ready) Multi-Tenant SaaS vebsayta** çevirmək üçün tələb olunan bütün memarlıq, təhlükəsizlik, ödəniş və miqyaslanma addımlarını əhatə edir.

---

## 1. Layihənin Ümumi Məqsədi və Biznes Dəyəri

**ApexCart AI** — dropshipping və D2C (Direct-to-Consumer) e-ticarət bizneslərini **uçdan-uca (end-to-end) avtomatlaşdıran süni intellekt platformasıdır**.

### Ənənəvi Dropshipping Problemi vs ApexCart AI Həlli

| Mərhələ | Ənənəvi Yanaşma | ApexCart AI İlə | Qənaət / Üstünlük |
| :--- | :--- | :--- | :--- |
| **1. Məhsul Araşdırması** | Saatlarla əl ilə trend axtarışı, doymuş məhsullar | **Agent 01** + **Trend Radar** (TikTok, Amazon, Shopify, Google Trends) | ~6–10 saat/gün |
| **2. Tədarük & 3PL** | Əl ilə sifariş, vasitəçilər, yüksək maya dəyəri | **Agent 02** (CJ API 2.0 + 1688 birbaşa + dHash vizual müqayisə + Avto-Failover) | 15–30% maya dəyəri qənaəti |
| **3. Vebsayt / Mağaza** | 3–7 gün quraşdırma + $300–$1,000+ dizayner xərci | **Agent 03** (AIDA kopiraytinq + 1 kliklə WordPress HTML & Shopify ixracı) | 3–7 gün + $300–$1,000 |
| **4. Reklam Videoları** | $150–$500+ influencer/UGC xərci, günlərlə gözləmə | **Agent 04** (3-Səhnəli 15–20s AI UGC + ElevenLabs səs + Fal.ai/B-Roll montaj) | $150–$500 + 3–5 gün |
| **5. SMM İdarəetməsi** | $300–$800/ay SMM menecer xərci | **Agent 05** (7 günlük kontent planı + Self-Hosted Postiz avtomatik paylaşım) | $300–$800/ay • 24/7 |
| **6. Meta Reklamlar** | Səhv hədəfləmə, boşa gedən reklam büdcəsi | **Agent 06** (CBO/ABO A/B test strukturu + CPA/ROAS Kill/Scale qaydaları) | 25–40% az büdcə israfı |
| **1 Məhsul Buraxılışı (Yekun)** | **7–14 gün • $500–$1,500+ ilkin xərc** | **~15–20 dəqiqə • $29–$79/ay abunəlik** | **~95%+ zaman & $450–$1,400+ qənaət** |

---

## 2. Sistemin Çoxagentli Arxitekturası (`LangGraph` + `Google Gemini`)

Bütün iş axını `workflows/pipeline.py` daxilində **LangGraph (`StateGraph(ECommerceState)`)** qrafı ilə orkestrasiya olunur. Bütün agentlər `agents/base.py` (`BaseAgent`) sinfindən törəyir və Google Gemini API (`google-genai`) üzərindən struktur JSON çıxışı (`response_schema`), Google Search Grounding, `503` overload retry (4s, 8s, 15s backoff) və avtomatik model fallback zənciri ilə işləyir.

### İş Axını (Pipeline Qrafı)

```
START
  └─▶ Agent 01: Product Research (3 yeni viral məhsul tapır)
        └─▶ [HITL 1] human_approval_product
              ├─ "research_X" ──▶ Market Research Subagent ──▶ [HITL 1]
              ├─ "rejected"   ──▶ Agent 01 (rədd edilənləri təkrarlamadan yeni axtarış)
              └─ "approved_X" ──▶ Agent 02: Sourcing & Unit Economics (CJ API 2.0 + 1688 + FX)
                                    └─▶ [HITL 2] human_approval_supplier (Top-3 tədarükçü və ya manual 1688 linki)
                                          └─▶ PARALEL FAN-OUT:
                                                ├─ Agent 03: Store Builder (AIDA + WordPress HTML + Shopify)
                                                └─ Agent 05: Social Media Manager (Brend + 7-günlük plan + Postiz)
                                                └─▶ FAN-IN ──▶ [HITL 3] human_approval_store
                                                      └─▶ [HITL 4] human_approval_video_sources (Avatar, B-roll, ssenari)
                                                            └─▶ Agent 04: Creative AI UGC Video Studio (3-səhnəli montaj)
                                                                  └─▶ [HITL 5] human_approval_creatives
                                                                        └─▶ Agent 06: Meta Ads Media Buyer
                                                                              └─▶ Monitoring Node (Qiymət/Stok + Sifariş İzləmə)
                                                                                    └─▶ END
```

---

## 3. Agentlər və Funksional Modulların Təfərrüatı

### 3.1. Agent 01 — Məhsul Araşdırması (`agents/product_research.py`, `tools/trend_tools.py`)
- **Məqsəd:** Son **7–14 gündə** viral olmuş, bazarda hələ doymamış (köhnə 2020–2023 məhsulları filtrdən keçirilir) **3 tam fərqli qalib məhsul** tapmaq.
- **Mənbələr:** Canlı Google Search Grounding, TikTok (`#tiktokmademebuyit`), Amazon Hot Releases/Bestsellers, Google Trends, Meta Ad Library.
- **Çıxış Məlumatları (`WinningProduct`):** Məhsul adı, nə işə yaradığı (`what_it_does`), ilk 3 saniyəlik "Wow" faktoru (`wow_factor`), minimum `3x` marja potensialı (`estimated_margin_multiplier >= 3.0`), şəkil/video reklam linki və 1688 fabriklərində dəqiq axtarış üçün **1–2 sözlük rəsmi Çin kateqoriya açar sözü (`chinese_keywords`)**.
- **Anti-Təkrar Mexanizmi:** İstifadəçinin rədd etdiyi məhsullar (`rejected_products`) state-də saxlanılır və növbəti axtarışda təkrarlanmır.

### 3.2. Subagent 1B — Dərin Bazar Araşdırması (`agents/market_research.py`)
- **Məqsəd:** İstifadəçi [HITL 1] mərhələsində konkret məhsul üçün dərin yoxlama istədikdə Google Search Grounding + canlı trend alətləri ilə 5 başlıq üzrə texniki-iqtisadi hesabat hazırlayır:
  1. Həll etdiyi problem
  2. Rəqabət və bazar doyması
  3. Hədəf kütlə (ICP)
  4. Tədarük zənciri riskləri, geri qaytarma səbəbləri və sertifikat tələbləri
  5. Fərqləndirici satış üstünlüyü (USP) + mənbə linkləri.

### 3.3. Canlı Cross-Platform Trend Radar & Gündəlik Digest (`tools/cross_radar.py`, `shopify_tracker.py`, `amazon_tracker.py`, `notification_tools.py`)
- **172 Shopify Mağazası + Amazon Sinxronizasiyası:** 10 kateqoriya (`electronics`, `home`, `beauty`, `fashion`, `kitchen`, `fitness`, `kids`, `pet`, `automotive`, `outdoor`) üzrə hər saat 1 kateqoriya drip-scan edilir (429/rate-limit bloklarının qarşısını almaq üçün), Amazon Movers & Shakers isə paralel skan olunur.
- **Dual-Market Convergence:** Həm Shopify mağazalarında, həm də Amazon-da eyni anda yüksələn məhsullar Jaccard token oxşarlığı ilə üst sıralara ("Qalib Məhsul") çıxarılır və Top 20 `unified_latest.json` hesabatı yaradılır.
- **Avtomatik E-poçt Abunəliyi:** Hər gün saat `09:00`-da `subscribers.json`-dakı aktiv abunəçilərə gündə 1 dəfə HTML Trend Radar hesabatı göndərilir.

### 3.4. Agent 02 — Tədarük, Marja, Avtomatik Sifariş & 3PL Monitorinq
Modullar: `agents/sourcing.py`, `tools/cj_dropshipping_tool.py`, `tools/sourcing_tools.py`, `tools/supplier_scorer.py`, `tools/order_automation.py`, `tools/fulfillment_bridge.py`, `tools/supplier_monitor.py`.
- **Paralel Tədarük Axtarışı:** Eyni anda **CJ Dropshipping Official API 2.0**, **pulsuz Playwright 1688 axtarışı** (açar söz və şəkil üzrə) və **canlı valyuta məzənnələri (USD/CNY/EUR)** icra olunur.
- **Vizual & Mətn Uyğunluğu (`supplier_scorer.py`):** 64-bitlik **perseptual image dHash** alqoritmi ilə Agent 01-in məhsul şəklini tədarükçü şəkli ilə müqayisə edir (0–100 bal) və mətn token oxşarlığı ilə birləşdirib Top-3 tədarükçünü sıralayır.
- **Unit Economics (`UnitEconomics`):** Satış qiyməti, məhsul maya dəyəri, US/EU çatdırılma (`CJPacket / YunExpress`), təxmini CPA, ödəniş komissiyası, vahid başına xalis mənfəət, marja `%` və `breakeven_roas` hesablanır.
- **Avtomatik Sifariş & Shopify Webhook Bridge:**
  - `POST /api/webhooks/shopify/orders-paid` üzərindən ödənilmiş Shopify sifarişlərini qəbul edir (`X-Shopify-Hmac-Sha256` HMAC yoxlaması ilə).
  - CJ API 2.0 üzərindən birbaşa sifariş yaradır və ya 1688/Private Agent (HyperSKU, SourcinBox və s.) üçün hazır sifariş paketi + CSV ixracı (`data/orders.db`) hazırlayır, izləmə kodunu Shopify-a geri ötürür.
- **Saatlıq Tədarük Monitorinqi & Avto-Failover (`supplier_monitor.py`):**
  - Aktiv tədarükçünü və 3-ə qədər ehtiyat (backup) tədarükçünü izləyir.
  - Qiymət `>10%` qalxdıqda və ya stok bitdikdə Telegram xəbərdarlığı göndərir və `auto_failover` aktivdirsə avtomatik ehtiyat tədarükçüyə keçid edir.

### 3.5. Agent 03 — Mağaza Qurucu & AIDA Kopirayter (`agents/store_builder.py`, `tools/shopify_tools.py`)
- **AIDA Satış Səhifəsi Generasiyası:** Gemini vasitəsilə viral başlıq, alt başlıq, AIDA hekayəsi (Attention-Interest-Desire-Action), 3–4 əsas fayda, FAQ, təsdiqlənmiş stil rəylər, tələskənlik (`urgency_text`) və güvən nişanları yaradır.
- **Dual Platform Dəstəyi:**
  - **WordPress / WooCommerce:** Hazır responsiv Tailwind HTML Landing Page (`GET /api/preview/wordpress`, `GET /api/export/wordpress`).
  - **Shopify:** Məhsulun Shopify Admin API / CSV formatında ixracı (`GET /api/export/shopify`), tracking pixel quraşdırılması və endirim kodlarının yaradılması.

### 3.6. Agent 04 — AI UGC & 3-Səhnəli Reklam Video Studiyası (`agents/creative.py`, `tools/creative_tools.py`)
- **3-Səhnəli 15–20 saniyəlik Storyboard Arxitekturası:**
  - **0–4 san (UGC Avatar Hook):** Kameraya həyəcanla danışan real hərəkətli UGC personajı. 4 hazır persona (`aysel_ugc`, `resad_ugc`, `leyla_ugc`, `emin_ugc`) mövcuddur (məhsul kateqoriyasına görə avtomatik və ya əl ilə seçilir; Fal.ai Hedra / keşlənmiş kliplər).
  - **4–15 san (Məhsul B-Roll Nümayişi):** Seçilmiş rejimdən asılı olaraq **Fal.ai Minimax 4K AI B-Roll**, 1688/TikTok su nişansız video yüklənməsi, istifadəçinin verdiyi URL-lər və ya məhsul şəkillərindən **Ken-Burns effektli HD slaydşou**.
  - **15–20 san (Təklif & CTA Banneri):** Məhsul şəkli, endirim və sifariş çağırışı banneri.
- **Səsləndirmə & Montaj:** **ElevenLabs Multilingual v2** insan səsi (fallback: Edge-TTS `az-AZ-BanuNeural` / `az-AZ-BabekNeural` və ehtiyat audio), `ffmpeg` vasitəsilə `1080x1920` (9:16 TikTok/Reels) H.264 render (`ad_video/` və `static/media/rendered_ads/`).

### 3.7. Agent 05 — Sosial Media Menecer / SMM (`agents/social_media.py`, `tools/postiz_tool.py`, `tools/social_tools.py`)
- **Brend İdentikliyi & 7 Günlük Plan:** Məhsul və Agent 04 kreativləri əsasında brend adı, rəng palitrası, sloqan, bio və TikTok / Instagram Reels üçün 7 günlük paylaşım təqvimi (başlıqlar + heşteqlər).
- **Self-Hosted Postiz İnteqrasiyası (`docker-compose.postiz.yml`):** TikTok Business, Instagram Reels, Facebook Page və YouTube Shorts kanallarının qoşulması və videoların avtomatik planlaşdırılması (`/api/social/postiz/*`).

### 3.8. Agent 06 — Media Buyer & Meta Ads ROAS (`agents/media_buyer.py`, `tools/meta_ads_tools.py`)
- **Kampaniya Strukturu (`CampaignStructure`):** `CONVERSIONS` hədəfli CBO/ABO test planı, gündəlik büdcə, auditoriya seqmentləri (`AudienceTargeting`) və avtomatlaşdırılmış ROAS/CPA Kill/Scale qaydaları (məs: `Spend > $15 və ROAS < 1.0 olarsa reklamı dayandır`).
- **Meta Marketing API Alətləri:** Kampaniya, AdSet, Ad yaratma, Insights oxuma, büdcə yeniləmə və reklam dayandırma.

---

## 4. Mövcud Veb İnterfeys və FastAPI Backend (`webapp.py`, `templates/`)

### 4.1. İki Hissəli Frontend Strukturu
1. **İctimai SaaS Vitrini (`GET /` → `templates/landing.html`):**
   - Əsas dil **İngilis dili (`en`)** + tam **Azərbaycan dili (`az`)** keçidi (`154+` `data-i18n` açarı).
   - `Dark / Light` rəng sxemi dəstəyi (`color-scheme`).
   - Three.js izometrik 3D Hero (Fabrik → 3PL → Mağaza + TikTok AI UGC kartı + Canlı Profit kartı).
   - Birləşdirilmiş **6-Agent Bento Grid & 1-Click Pipeline Flow**, **Canlı Trend Radar Ön-Baxışı** (`/api/radar/unified`), yığcam **6-Agent Müqayisə Cədvəli**, **Cədvəllə sinxron ROI Kalkulyatoru**, **4 Abunəlik Planı** (`Free Trial $0`, `Starter $29`, `Growth Pro $79`, `Agency $199`; Aylıq / İllik `-25%`), FAQ və doğma `<dialog id="authDialog">` Giriş/Qeydiyyat modalı.
2. **6-Agent Əməliyyat Paneli (`GET /app` → `templates/dashboard.html`):**
   - Canlı SSE log axını, kateqoriya seçimi, 5 HITL təsdiq kartı, Trend Radar idarəetməsi, Sifariş/3PL & Avto-Failover paneli, AI UGC Video Studiyası ön-baxışı, WordPress/Shopify ixracı, Postiz SMM qrafiki və **Settings & Integrations** (14 API açarının maskalanmış idarəsi və canlı test düymələri).

### 4.2. Mövcud API Endpoint-ləri (`webapp.py`)
- **Səhifələr & Sağlamlıq:** `GET /`, `GET /app`, `GET /robots.txt`, `GET /api/health`
- **Pipeline İdarəsi:** `POST /api/pipeline/start`, `POST /api/pipeline/reset`, `GET /api/pipeline/status`, `POST /api/pipeline/approve`, `GET /api/pipeline/results`, `GET /api/pipeline/logs` (SSE), `GET /api/proxy-image`
- **Trend Radar & Digest:** `GET /api/radar/latest`, `GET /api/radar/unified`, `POST /api/radar/scan`, `POST /api/radar/send-email`, `GET /api/radar/preview-email`, `GET /api/radar/subscribers`, `POST /api/radar/subscribe`, `POST /api/radar/unsubscribe`
- **Sifarişlər, Shopify Webhook & 3PL Monitorinq:** `POST /api/orders/create`, `GET /api/orders`, `POST /api/orders/{order_number}/sync`, `GET /api/orders/{order_number}/export-csv`, `POST /api/webhooks/shopify/orders-paid`, `POST /api/orders/{order_number}/push-shopify-tracking`, `POST /api/sourcing/bulk-compare`, `GET /api/monitoring/status`, `POST /api/monitoring/register`, `POST /api/monitoring/check-now`, `POST /api/monitoring/switch-backup`, `POST /api/monitoring/test-telegram`, `POST /api/monitoring/toggle-auto-failover`
- **Mağaza & SMM (Postiz):** `GET /api/export/shopify`, `GET /api/preview/wordpress`, `GET /api/export/wordpress`, `GET /api/social/postiz/status`, `POST /api/social/postiz/config`, `POST /api/social/postiz/auto-key`, `POST /api/social/postiz/connect-channel`, `POST /api/social/postiz/disconnect-channel`, `POST /api/social/postiz/schedule`, `POST /api/social/generate`
- **SaaS Ayarlar & Açar İdarəsi:** `GET /api/settings/integrations`, `POST /api/settings/integrations/save`, `POST /api/settings/integrations/test`

### 4.3. Sistemdə İstifadə Olunan 14 İnteqrasiya Açarı (`SAAS_INTEGRATION_KEYS`)
`GOOGLE_API_KEY`, `CJ_API_KEY`, `SHOPIFY_STORE_URL`, `SHOPIFY_ACCESS_TOKEN`, `SHOPIFY_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SLACK_WEBHOOK_URL`, `META_ACCESS_TOKEN`, `META_AD_ACCOUNT_ID`, `POSTIZ_API_URL`, `POSTIZ_API_KEY`, `ELEVENLABS_API_KEY`, `FAL_KEY` (+ əlavə SMTP, RapidAPI TikTok, Redis, Database URL parametrləri).

---

## 5. SaaS Vebsayt Quracaq AI Agenti Üçün Məcburi Production Tələbləri

Hazırkı kod bazası **tək istifadəçili lokal prototipdir**. Bunu kommersiya SaaS vebsaytına çevirmək üçün SaaS qurucu AI agenti aşağıdakı kritik boşluqları mütləq həll etməlidir:

### 5.1. Multi-Tenancy və State İzolyasiyası *(Kritik)*
- **Mövcud Problem:** `webapp.py` qlobal tək `pipeline_state = {}`, tək `approval_event = asyncio.Event()` və RAM-da işləyən `MemorySaver()` istifadə edir. İki istifadəçi eyni anda işlədikdə məlumatlar qarışır və server yenidən başladıqda state itir.
- **Tələb:**
  - Hər işəsalma üçün `tenant_id` (və ya `workspace_id`) + `run_id = uuid.uuid4()`.
  - **PostgreSQL (`PostgresSaver` + SQLAlchemy Async + Alembic):** `users`, `workspaces`, `team_members`, `subscriptions`, `usage_quotas`, `workspace_integrations` (Fernet/KMS ilə şifrələnmiş müştəri açarları — `.env` faylına yazmaq qadağandır!), `pipeline_runs`, `agent_logs`, `radar_signals`, `orders`, `webhook_events`.
  - **Redis:** Task növbəsi (`arq` / `Celery`), Pub/Sub SSE axını (`/api/stream/{run_id}`), API rate-limit və Trend Radar 12 saatlıq keş.

### 5.2. Worker Ayrılması və Video/Scraping Miqyaslanması
- **FFmpeg CPU/RAM İzolyasiyası:** Video montajı FastAPI əsas prosesindən ayrı `worker.py` prosesinə (və ya serverless GPU/CPU konteynerlərə) çıxarılmalı, eyni anda işləyən FFmpeg sayı `Semaphore` ilə məhdudlaşdırılmalıdır.
- **Object Storage & CDN:** Lokal `ad_video/` və `static/media/` əvəzinə **Cloudflare R2 / AWS S3** + CDN + `presigned URL` + plan üzrə saxlama TTL-i.
- **Anti-Scraping & Fallback Dayanıqlığı:** 1688/TikTok scraper-lər üçün fırlanan rezident proksilər + rəsmi API fallback-ları (`CJ API 2.0`); TTS üçün çoxpilləli fallback (`ElevenLabs` ↔ `Edge-TTS` ↔ `Kokoro/gTTS`); Video üçün TikTok uğursuz olduqda avtomatik məhsul şəkilli AI Ken-Burns / Fal.ai renderinə keçid.

### 5.3. Autentifikasiya, Rollar (RBAC) və İnteqrasiya Təhlükəsizliyi
- **Auth:** `Argon2`/`bcrypt` + `HttpOnly; Secure; SameSite=Lax` JWT Access/Refresh token axını + Google OAuth2 və Shopify OAuth.
- **BYOK vs Managed Credits:** İstifadəçi ya öz API açarlarını (Shopify, Meta, CJ, ElevenLabs — şifrələnmiş tenant bazasında) bağlaya bilər, ya da platformanın idarə etdiyi kredit/kvota sistemindən istifadə edə bilər.
- **Tenant Mühafizəsi:** Bütün `/api/pipeline/*`, `/api/orders/*`, `/api/settings/*`, `/api/webhooks/shopify/{workspace_slug}` endpoint-lərində ciddi `tenant_id` yoxlaması.

### 5.4. Ödəniş Qapısı (Billing), Kvotalar və Chargeback Qoruması
- **Planlar və Limitlər:**
  - **Free Trial ($0 / 7 gün):** 1 mağaza, gündə 3 AI trend analizi, 10 sandbox pipeline, CJ/Fabrik ön-baxış.
  - **Starter ($29/ay, illik $22/ay):** 1 Shopify mağazası, 25 tam pipeline/ay, 15 AI UGC video, avtonom SMM + Telegram.
  - **Growth Pro ($79/ay, illik $59/ay):** 5 Shopify mağazası, limitsiz Radar & 150 pipeline/ay, 100 AI video + 3PL Avto-Failover, tam SMM + Meta Ads ROAS bot.
  - **Agency ($199/ay, illik $149/ay):** 25 müştəri mağazası, komanda yerləri (Team Seats), limitsiz pipeline & video, White-Label hesabatlar + CDN, ayrılmış worker & SLA.
- **Ödəniş Provayderi Seçimi:** Dropshipping SaaS kateqoriyası Stripe/PayPal-da yüksək riskli sayıldığı üçün **Merchant of Record (Paddle və ya LemonSqueezy)** tövsiyə olunur (qlobal KDV/VAT və chargeback qoruması üçün).
- **Quota Gatekeeper & Webhook-lar:** `POST /api/billing/checkout`, `POST /api/billing/portal`, `POST /api/webhooks/billing` (HMAC imzalı). Hər əməliyyatdan əvvəl aylıq kvota yoxlaması.

### 5.5. Hüquqi və Müştəri Saxlama (Retention) Xüsusiyyətləri
- **DMCA / Müəllif Hüququ Qoruması:** Orijinal TikTok videolarını 1-ə 1 köçürmək əvəzinə dərin transformasiya və ya məhsul şəkillərindən AI slaydşou/B-roll generasiyasına üstünlük verilməsi + qeydiyyatda Terms of Service / Copyright imtina bəyanatı.
- **Retention Mexanizmləri:** Gündəlik `09:00` Trend Radar digest, 24/7 qiymət/stok Telegram xəbərdarlığı, Shopify sifarişlərinin avtomatik 3PL icrası müştərinin platformada daimi qalmasını (LTV artımını) təmin edir.

---

## 6. SaaS Qurucu AI Agentinə Birbaşa Tapşırıq (Prompt Çeklisti)

1. Mövcud `webapp.py`, `workflows/pipeline.py`, `agents/*.py` və `tools/*.py` nüvəsini qoruyaraq backend-i **Multi-Tenant FastAPI Gateway + Redis/ARQ Worker + PostgreSQL (`PostgresSaver`)** arxitekturasına keçir.
2. Hazır **Concept B (Emerald & Gold)** `templates/landing.html` (`GET /`) və `templates/dashboard.html` (`GET /app`) dizayn dilini saxlayaraq real Auth (`Login/Register/OAuth`), Billing (`Paddle/LemonSqueezy` Checkout + Portal), Workspace Seçici, Kredit/Kvota Göstəricisi və Multi-Tenant Settings səhifələrini tam birləşdir.
3. `ad_video/` və `static/media/` fayllarını **Cloudflare R2 / S3** obyekt saxlancına yönləndir.
4. `GET /api/health`, `/api/webhooks/billing`, `/api/webhooks/shopify/{tenant_id}` və bütün tenant-izolyasiyalı CRUD/SSE endpoint-lərini tam testlər və `Docker Compose` + Reverse Proxy (`Nginx/Caddy`) deploy konfiqurasiyası ilə təhvil ver.
