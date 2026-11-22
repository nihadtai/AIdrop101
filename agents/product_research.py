import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional
from google import genai
from pydantic import BaseModel, Field

from state.graph_state import ECommerceState
from agents.base import BaseAgent

logger = logging.getLogger(__name__)


import urllib.parse

class WinningProduct(BaseModel):
    name: str = Field(description="Məhsulun cəlbedici brend və kommersiya adı")
    english_name: str = Field(description="Məhsulun dəqiq, tam İngiliscə adı (Brend adı OLMADAN, sırf məhsulun ümumi adı, məs: 'Magnetic Levitation Moon Lamp')", default="")
    sourcing_search_query: str = Field(
        description="CJ Dropshipping və AliExpress kataloq axtarışı üçün 2-3 sözlük DƏQİQ İNGİLİSCƏ ƏSAS MƏHSUL İSMİ (adjective/marketing sözlərini çıxar, əsas məhsul növünü saxla: məs: 'wireless sports earbuds', 'portable espresso maker', 'ultrasonic jewelry cleaner', 'neck posture massager')",
        default="",
    )
    chinese_keywords: str = Field(
        description="Məhsulun 1688.com fabriklərində 100% DƏQİQ TAPILMASI üçün Çin fabriklərinin istifadə etdiyi YALNIZ 1-2 sözlük rəsmi sənaye/kateqoriya adı (品类核心词). QƏTİYYƏN vergül, cümlə, brend adı və ya sifət yazma! (məs: 'RGB拾音灯', '火焰香薰机', '磁悬浮月球灯', '三合一折叠无线充', '自动搅拌杯')",
        default=""
    )
    what_it_does: str = Field(
        description="Məhsulun nə işə yaradığı ilə bağlı QISA və SADƏ məlumat (maksimum 1-2 anlaşıqlı cümlə, uzun yazma)"
    )
    wow_factor: str = Field(
        description="Məhsulun 'Wow' faktoru — insanları ilk 3 saniyədə heyrətləndirən və dərhal almağa vadar edən cəhəti (1 cümlə)"
    )
    description: str = Field(description="Məhsul haqqında 1 cümləlik qısa xülasə")
    image_url: str = Field(
        description="Boş burax — sistem özü təsdiqlənmiş şəkil tapıb yerləşdirir",
        default="",
    )
    video_ad_url: str = Field(
        description="Boş burax — sistem özü TikTok axtarış linkini yaradır",
        default="",
    )
    meta_ads_url: str = Field(
        description="Boş burax — sistem özü Meta Ad Library linkini yaradır",
        default="",
    )
    platform_source: str = Field(
        description="Reklam videosunun aid olduğu platforma ('TikTok', 'Instagram', 'Meta Ads')",
        default="TikTok",
    )
    trend_score: int = Field(description="1-10 arası qlobal trend dərəcəsi")
    problem_solving_score: int = Field(description="1-10 arası problem həll etmə balı")
    viral_potential: int = Field(description="1-10 arası viral marketinq potensialı balı")
    estimated_margin_multiplier: float = Field(
        description="Təxmini satış qiymətinin maya dəyərinə nisbəti (məsələn: 3.5x və ya 4.0x)"
    )
    target_audience: str = Field(description="Məhsulun əsas alıcı profili və hədəf kütləsi")


from tools.product_image_search import search_product_image
from tools.trend_tools import find_radar_image_for_product
from tools.discovery_pool import (
    fetch_unseen_from_pool,
    get_user_excluded_items,
    is_fuzzy_duplicate,
    mark_products_for_user,
    save_discovered_to_pool,
)

VIRAL_CATEGORY_TERMS = {
    "earbud": "蓝牙耳机",
    "headphone": "头戴式蓝牙耳机",
    "wireless charger": "三合一无线充",
    "magsafe": "磁吸无线充",
    "air duster": "电动除尘枪",
    "dust blower": "电动除尘枪",
    "laser": "便携激光雕刻机",
    "stirring mug": "自动搅拌杯",
    "blender": "便携榨汁杯",
    "car mount": "车载磁吸支架",
    "lint remover": "毛球修剪器",
    "espresso": "便携咖啡机",
    "thermal printer": "迷你热敏打印机",
    "camera": "迷你高清相机",
}


def _extract_sourcing_query(english_name: str, raw_query: str = "") -> str:
    """Builds a concise 2-3 word core product noun query for CJ/AliExpress catalog APIs."""
    candidate = (raw_query or "").strip()
    fluff = {"pro", "max", "ultra", "premium", "luxury", "viral", "tiktok", "new", "original", "2025", "2026", "for", "with", "and", "the", "display", "power"}
    if candidate:
        c_words = [w for w in candidate.replace("-", " ").split() if w.isalnum() and w.lower() not in fluff]
        if 1 <= len(c_words) <= 4:
            return " ".join(c_words[-3:])
    words = [w for w in (english_name or "").replace("-", " ").split() if w.isalnum() and w.lower() not in fluff]
    if len(words) > 3:
        return " ".join(words[-3:])
    return " ".join(words) if words else (english_name or "").strip()


def _clean_chinese_keywords(chinese_kw: str, english_name: str = "", prod_name: str = "") -> str:
    """
    Cleans Chinese keywords to ensure 100% exact factory matches on 1688.
    Removes commas, punctuation, redundant adjectives, and keeps only core category terms.
    """
    cleaned = chinese_kw or ""
    for ch in [",", "，", "、", ";", "；", ".", "。", "!", "?", '"', "'", "：", ":", "\n", "\t"]:
        cleaned = cleaned.replace(ch, " ")

    tokens = [t.strip() for t in cleaned.split() if t.strip()]

    valid_tokens = []
    for t in tokens:
        has_chinese = any('\u4e00' <= char <= '\u9fff' for char in t)
        is_tech = t.upper() in ["RGB", "LED", "USB", "TYPE-C", "3-IN-1", "3合1"]
        if has_chinese or is_tech:
            valid_tokens.append(t)

    if valid_tokens:
        return " ".join(valid_tokens[:2])

    # Fallback to category dictionary if no valid Chinese tokens
    combined_text = f"{english_name} {prod_name}".lower()
    for pattern, zh_term in VIRAL_CATEGORY_TERMS.items():
        if pattern in combined_text:
            return zh_term

    if tokens:
        return " ".join(tokens[:2])

    en_words = [w for w in (english_name or prod_name or "").split() if w.isalnum()]
    return " ".join(en_words[-2:]) if en_words else cleaned.strip()

def _sanitize_urls(product_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensures image_url is an authentic, reachable product image (radar CDN or verified web image search),
    never an unverified LLM-hallucinated URL, and builds both TikTok and Meta Ad Library links.
    """
    search_term = (product_dict.get("english_name") or "").strip()
    if not search_term or len(search_term) < 2:
        search_term = str(product_dict.get("name", "viral gadget")).strip()

    prod_name_az = str(product_dict.get("name", search_term)).strip()

    # 0. Ensure concise 2-3 word core noun sourcing_search_query for Agent 2
    product_dict["sourcing_search_query"] = _extract_sourcing_query(
        search_term,
        product_dict.get("sourcing_search_query", ""),
    )

    # 1. Clean Chinese keywords for 100% exact 1688 factory matching
    product_dict["chinese_keywords"] = _clean_chinese_keywords(
        product_dict.get("chinese_keywords", ""),
        english_name=search_term,
        prod_name=prod_name_az
    )

    # 2. Discard unverified LLM-generated image URLs before resolving authentic CDN image
    product_dict["image_url"] = ""
    try:
        radar_img = find_radar_image_for_product(search_term)
        if radar_img and radar_img.startswith("http"):
            product_dict["image_url"] = radar_img
            logger.info(f"Matched live radar product image for '{search_term}': {radar_img}")
        else:
            exact_img = search_product_image(search_term, fallback_query=prod_name_az)
            if exact_img and exact_img.startswith("http"):
                product_dict["image_url"] = exact_img
                logger.info(f"Assigned verified product image for '{search_term}': {exact_img}")
    except Exception as img_err:
        logger.warning(f"Error finding matching image for '{search_term}': {img_err}")

    # 3. Build direct TikTok Search & Meta Ad Library links using FULL ENGLISH product name
    encoded_q = urllib.parse.quote_plus(search_term)
    product_dict["video_ad_url"] = f"https://www.tiktok.com/search?q={encoded_q}"
    product_dict["meta_ads_url"] = (
        "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL"
        f"&q={encoded_q}&search_type=keyword_unordered&media_type=all"
    )
    product_dict["platform_source"] = "TikTok"

    return product_dict


class ProductResearchAgent(BaseAgent):
    """
    Agent responsible for researching and identifying winning e-commerce products.
    Finds exactly 3 winning products per run and accommodates human approvals/rejections.
    """

    def __init__(
        self,
        client: genai.Client,
        model: str = "gemini-3.5-flash-lite",
        tools: Optional[List[Callable]] = None,
    ):
        super().__init__(
            name="ProductResearchAgent",
            role="E-Commerce Product Research & Trend Specialist",
            goal="Find 3 winning viral products using live Google Search Grounding and trend tools",
            client=client,
            model=model,
            tools=tools,
            use_google_search=True,
        )
        self.system_instruction = (
            "Sən e-ticarət və dropshipping üzrə dünya səviyyəli Məhsul Araşdırma (Product Research) mütəxəssisisən. "
            "Səndə canlı Google Search Grounding (Google Axtarış) aləti və Trend Radar canlı məlumatları aktivdir.\n\n"
            "ÇOX VACİB TƏLƏB — YALNIZ SON 7–14 GÜNÜN TƏZƏ VƏ DOYMAMIŞ MƏHSULLARI:\n"
            "• Sənin əsas hədəfin YALNIZ son 7–14 gündə TikTok Shop (#tiktokmademebuyit), Amazon Hot New Releases və DTC Shopify brendlərində YENİ partlamış, sürətlə trendə minən və bazarda HƏLƏ DOYMAMIŞ (unsaturated) innovativ məhsulları tapmaqdır.\n"
            "• ⛔ QƏTİYYƏN QADAĞANDIR (2020–2023-cü illərdə viral olmuş köhnə, doymuş məhsullar):\n"
            "   Alov diffuzeri (flame diffuser), Galaxy/Astronaut ulduz proyektoru, LED işıq maskası, Onurğa düzəldici/stretcher, Ay lampası (moon lamp), Duruş korseti, Kristal saç silən, Fırlanan qarışdırıcı stəkan kimi 3-4 il əvvəl hamının satdığı köhnə məhsulları QƏTİYYƏN TƏKLİF ETMƏ! Hər dəfə bu günün ən son, gözlənilməz və innovativ qadjetlərini tap.\n\n"
            "HƏR 3 MƏHSUL bir-birindən TAMAMILƏ FƏRQLI olmalıdır (eyni kateqoriyadan olsa belə, fərqli tipli məhsullar olmalıdır).\n\n"
            "Hər məhsul mütləq bu meyarlara cavab verməlidir:\n"
            "1. Dəqiq və peşəkar adı (name) və tam İngiliscə adı (english_name) olmalıdır.\n"
            "2. CJ Dropshipping və AliExpress kataloq axtarışı üçün 2-3 sözlük DƏQİQ İNGİLİSCƏ ƏSAS MƏHSUL İSMİ (sourcing_search_query) olmalıdır (məs: 'wireless sports earbuds', 'portable espresso maker', 'ultrasonic jewelry cleaner').\n"
            "3. Nə işə yaradığı (what_it_does) aydın, dərindən və inandırıcı şəkildə izah olunmalıdır — gündəlik həyatda hansı dərdi/problemi həll edir.\n"
            "4. 'Wow' faktoru (wow_factor) güclü olmalıdır — videoda ilk 3 saniyədə izləyicini dayandıran (scroll-stopping) və insanları heyrətləndirən xüsusiyyəti.\n"
            "5. Marja potensialı ən azı 3x (estimated_margin_multiplier >= 3.0) olmalıdır.\n"
            "6. Hər məhsul üçün 1688.com topdansatış fabriklərində 100% DƏQİQ TAPILMASI üçün Çin fabriklərinin istifadə etdiyi YALNIZ 1-2 sözlük rəsmi kateqoriya adını (品类核心词) qeyd et. QƏTİYYƏN vergül, cümlə, brend adı və ya sifət yazma!\n"
            "7. İstifadəçinin əvvəllər gördüyü və ya rədd etdiyi heç bir məhsul (və ya onun sinonim/oxşar versiyası) TƏKRAR TƏKLİF EDİLMƏMƏLİDİR. Hər dəfə yeni, innovativ və fərqli məhsullar tapılmalıdır.\n"
            "8. Seçilmiş kateqoriyaya uyğun olan və son 7–14 gündə TikTok-da trend olan məhsulları üstünlük ver.\n"
            "Bütün izahatları zəngin və səlis Azərbaycan dilində tərtib et."
        )

    async def run(self, state: ECommerceState) -> Dict[str, Any]:
        """Execute the product research phase to find 3 winning products."""
        logger.info("Starting Product Research Phase (Finding 3 Winning Products)...")

        try:
            user_id = (state.get("user_id") or "default_user").strip() or "default_user"
            rejected_products: List[str] = state.get("rejected_products", []) or []
            user_category = state.get("category", "") or "all"

            # Record any in-state rejected products into this user's persistent history ledger
            if rejected_products:
                mark_products_for_user(
                    user_id,
                    [{"name": r, "english_name": r} for r in rejected_products if r],
                    status="rejected",
                )

            # Build full per-user exclusion list (everything this user has ever seen/rejected/approved)
            excluded_items: List[Dict[str, str]] = get_user_excluded_items(user_id)
            for r in rejected_products:
                if r and not any(r.strip().lower() == (x.get("name") or "").lower() for x in excluded_items):
                    excluded_items.append({"name": r, "english_name": r, "status": "rejected"})

            # 0. Check Shared Discovery Pool first for unseen fresh (<14d) products for this user
            pool_candidates = fetch_unseen_from_pool(
                user_id=user_id,
                category=user_category,
                count=3,
                ttl_days=14,
            )
            sanitized_products: List[Dict[str, Any]] = []
            for pc in pool_candidates:
                if not is_fuzzy_duplicate(pc, excluded_items) and not is_fuzzy_duplicate(pc, sanitized_products):
                    if not pc.get("sourcing_search_query"):
                        pc["sourcing_search_query"] = _extract_sourcing_query(pc.get("english_name", ""), "")
                    sanitized_products.append(pc)
                    if len(sanitized_products) >= 3:
                        break

            if len(sanitized_products) >= 3:
                mark_products_for_user(user_id, sanitized_products, status="shown")
                logger.info(
                    f"Served 3 unseen winning products for user '{user_id}' from Shared Discovery Pool (0 Gemini calls): "
                    f"{[p['name'] for p in sanitized_products]}"
                )
                return {
                    "products": sanitized_products,
                    "selected_product": None,
                    "current_phase": "product_research",
                    "logs": [
                        f"⚡ 1-ci Agent (Shared Discovery Pool): '{user_id}' üçün son 14 günün ortaq hovuzundan daha əvvəl göstərilməmiş 3 təzə qalib məhsul seçildi (0 API xərci).",
                        f"✅ 1-ci Agent: {len(sanitized_products)} uğurlu məhsul tapıldı: {', '.join(p['name'] for p in sanitized_products)}",
                    ],
                }

            # Map dashboard category value to search terms
            CATEGORY_MAP = {
                "all": "viral trending products",
                "electronics": "electronics gadgets tech",
                "home": "home decor household items",
                "beauty": "beauty skincare personal care",
                "fashion": "fashion clothing accessories",
                "kitchen": "kitchen gadgets cooking tools",
                "fitness": "fitness gym workout equipment",
                "kids": "kids toys baby products",
                "pet": "pet supplies dog cat accessories",
                "automotive": "car accessories automotive gadgets",
                "outdoor": "outdoor camping travel gear",
            }
            primary_cat = CATEGORY_MAP.get(user_category, user_category)

            # 1. Gather trend data using tools — run ALL tools in parallel
            logger.info(f"Gathering trend data across TikTok (#tiktokmademebuyit), Amazon, Google, Meta for '{primary_cat}'...")
            gathered_data = []
            if self.tools:
                async def _run_tool(tool):
                    name = getattr(tool, "__name__", "")
                    try:
                        loop = asyncio.get_running_loop()
                        result = await loop.run_in_executor(None, tool, primary_cat)
                        if "tiktok" in name:
                            return f"TikTok Trends (#tiktokmademebuyit): {result}"
                        elif "amazon" in name:
                            return f"Amazon Bestsellers: {result}"
                        elif "google" in name:
                            return f"Google Trends: {result}"
                        elif "meta" in name:
                            return f"Meta Ads: {result}"
                        return f"{name}: {result}"
                    except Exception as te:
                        logger.warning(f"Tool execution note ({name}): {te}")
                        return None

                tool_results = await asyncio.gather(*[_run_tool(t) for t in self.tools], return_exceptions=True)
                gathered_data = [r for r in tool_results if r and not isinstance(r, Exception)]

            # Direct live 14-day web search guarantees live web grounding even if Gemini's built-in Search Tool quota is empty
            direct_live_news = []
            try:
                from tools.trend_tools import _fetch_live_google_14d_trends
                direct_live_news = await asyncio.to_thread(
                    _fetch_live_google_14d_trends,
                    f"viral products {primary_cat} tiktok made me buy it",
                    6
                )
            except Exception as d_err:
                logger.debug(f"Direct live news fetch note: {d_err}")

            if direct_live_news:
                titles = "\n".join(f"  • {item['title']}" for item in direct_live_news if item.get("title"))
                gathered_data.append(f"📰 Son 14 Günün Real Google Xəbərləri & Viral Məhsul Məqalələri:\n{titles}")

            trend_context = (
                "\n\n".join(gathered_data)
                if gathered_data
                else f"Trending category: {primary_cat} with high viral growth on #tiktokmademebuyit"
            )

            # Build exclusion clause including all products this user has previously seen or rejected
            all_exclusions_for_prompt = list(excluded_items) + [
                {"name": p.get("name", ""), "english_name": p.get("english_name", "")}
                for p in sanitized_products
            ]
            rejected_clause = ""
            if all_exclusions_for_prompt:
                formatted_exclusions = []
                for item in all_exclusions_for_prompt[:30]:
                    az_n = (item.get("name") or "").strip()
                    en_n = (item.get("english_name") or "").strip()
                    if az_n and en_n and az_n.lower() != en_n.lower():
                        formatted_exclusions.append(f"'{az_n}' ({en_n})")
                    elif az_n or en_n:
                        formatted_exclusions.append(f"'{az_n or en_n}'")
                if formatted_exclusions:
                    rejected_list_str = ", ".join(formatted_exclusions)
                    rejected_clause = (
                        f"\n\n⛔ DİQQƏT — İSTİFADƏÇİYƏ ƏVVƏLLƏR GÖSTƏRİLMİŞ VƏ YA RƏDD EDİLMİŞ MƏHSULLAR (TƏKRARLAMAQ QADAĞANDIR):\n"
                        f"{rejected_list_str}\n"
                        f"Bu məhsulları, onların sinonim adlarını və ya eyni funksiyalı analoji modellərini QƏTİYYƏN TƏKLİF ETMƏ! "
                        f"Tamamilə YENİ, FƏRQLİ kateqoriyalı və fərqli funksiyalı qalib məhsullar tap.\n"
                    )

            prompt = (
                f"İstifadəçi '{user_category}' kateqoriyasını seçib.\n"
                f"Aşağıdakı canlı bazar məlumatlarını (son 7-14 günün Google Search, TikTok Shop və Amazon Hot New Releases nəticələrini) təhlil edərək "
                f"bu günlərdə satış rekordları qıran 4 ƏDƏD FƏRQLİ və TƏZƏ UĞURLU QALİB MƏHSUL müəyyən et (ən yaxşı 3-ü seçiləcək):\n\n"
                f"{trend_context}\n"
                f"{rejected_clause}\n"
                f"TƏLƏBLƏR:\n"
                f"1. YALNIZ SON 7–14 GÜNÜN TƏZƏ VƏ DOYMAMIŞ TRENDLƏRİ: 2020–2023-cü illərin köhnə məhsulları (Alov diffuzeri, Ulduz proyektoru, LED maska, Ay lampası, Onurğa dartıcı və s.) QƏTİYYƏN OLMAMALIDIR!\n"
                f"2. Hər məhsul bir-birindən və qadağan olunmuş siyahıdan TAMAMİLƏ FƏRQLİ olmalıdır (eyni və ya oxşar məhsullar olmasın!).\n"
                f"3. Hər məhsulun unikal adı (name), tam ingiliscə adı (english_name) və CJ/AliExpress axtarışı üçün 2-3 sözlük əsas ingiliscə məhsul ismi (sourcing_search_query, məs: 'wireless sports earbuds', 'portable espresso maker', 'ultrasonic jewelry cleaner') olmalıdır.\n"
                f"4. Nə işə yaradığı (what_it_does) — maksimum 1-2 cümlə ilə QISA, SADƏ və aydın.\n"
                f"5. 'Wow' faktoru (wow_factor) — maksimum 1 cümlə ilə ilk 3 saniyədə insanları heyrətləndirən cəhət.\n"
                f"6. Şəkil (image_url) və video (video_ad_url) linklərini boş string ('') saxla — uydurma URL YAZMA, real şəkil və reklam linkləri backend tərəfindən avtomatik tapılacaq.\n"
                f"7. Hər məhsul üçün 1688 fabriklərində 100% DƏQİQ TAPILMASI üçün YALNIZ 1-2 sözlük rəsmi kateqoriya adı (chinese_keywords) mütləq yazılmalıdır (məs: '磁吸无线充', '自动开瓶器', '折叠风扇', '智能测距仪'). Vergül, cümlə və ya brend adı QƏTİYYƏN OLMASIN!\n"
                f"8. Hər məhsul '{user_category}' kateqoriyasına uyğun olmalıdır (all seçilibsə, istənilən kateqoriyadan ola bilər).\n"
                f"9. Məhsulun bütün mətnlərini aydın, səlis Azərbaycan dilində yaz.\n"
                f"\nNəticəni JSON array formatında qaytar: [{{product1}}, {{product2}}, {{product3}}, {{product4}}]"
            )

            # 2. Call Gemini with Google Search Grounding enabled
            logger.info("Generating winning products via Gemini (with Google Search Grounding)...")
            schema = {
                "type": "array",
                "items": WinningProduct.model_json_schema(),
            }

            score_fields = ("trend_score", "problem_solving_score", "viral_potential")
            loop = asyncio.get_running_loop()
            newly_discovered: List[Dict[str, Any]] = []
            last_err: Optional[Exception] = None
            grounding_info: Dict[str, Any] = {"queries": [], "sources": []}

            for attempt in range(2):
                try:
                    if attempt == 0:
                        response_text = await self._call_gemini(
                            prompt=prompt,
                            system_instruction=self.system_instruction,
                            response_schema=schema,
                            use_google_search=True,
                        )
                    else:
                        logger.warning(
                            f"Only {len(sanitized_products)}/3 non-duplicate valid products — retrying once to fill the gap..."
                        )
                        extra_exclude = ", ".join(
                            f"'{p.get('name')}' ({p.get('english_name')})" for p in sanitized_products
                        )
                        response_text = await self._call_gemini(
                            prompt=prompt
                            + f"\n\nDİQQƏT: Əvvəlki cavabda təkrarlanan məhsullar var idi. Bunları da QƏTİYYƏN TƏKLİF ETMƏ: {extra_exclude}. "
                              "Tamamilə FƏRQLİ məhsullar tap və bütün ballar 1-10 arası TAM ƏDƏD olsun.",
                            system_instruction=self.system_instruction,
                            response_schema=schema,
                            use_google_search=True,
                        )
                    if attempt == 0:
                        grounding_info = dict(self.last_grounding_info)

                    products_raw = json.loads(response_text)
                    if isinstance(products_raw, dict):
                        products_raw = [products_raw]

                    valid_p_dicts = []
                    for p_raw in products_raw[:6]:
                        try:
                            for f in score_fields:
                                if f in p_raw and p_raw[f] is not None:
                                    p_raw[f] = max(1, min(10, int(round(float(p_raw[f])))))
                            p_obj = WinningProduct(**p_raw)
                            pd = p_obj.model_dump()
                            if is_fuzzy_duplicate(pd, excluded_items) or is_fuzzy_duplicate(pd, sanitized_products) or is_fuzzy_duplicate(pd, valid_p_dicts):
                                logger.info(f"Filtered out fuzzy duplicate product for user '{user_id}': {pd.get('name')} / {pd.get('english_name')}")
                                continue
                            valid_p_dicts.append(pd)
                        except Exception as parse_err:
                            logger.warning(f"Error parsing product: {parse_err}")

                    sanitized_results = await asyncio.gather(
                        *[loop.run_in_executor(None, _sanitize_urls, pd) for pd in valid_p_dicts],
                        return_exceptions=True,
                    )
                    for p_dict in sanitized_results:
                        if isinstance(p_dict, dict) and p_dict.get("name"):
                            if not is_fuzzy_duplicate(p_dict, excluded_items) and not is_fuzzy_duplicate(p_dict, sanitized_products):
                                newly_discovered.append(p_dict)
                                if len(sanitized_products) < 3:
                                    sanitized_products.append(p_dict)
                except Exception as call_err:
                    last_err = call_err
                    logger.warning(f"Product research attempt {attempt + 1} failed: {call_err}")

                if len(sanitized_products) >= 3:
                    break

            if not sanitized_products:
                raise ValueError(
                    f"Gemini heç bir etibarlı məhsul qaytarmadı. {('Son xəta: ' + str(last_err)) if last_err else ''}".strip()
                )

            # Save newly discovered products to the Shared Discovery Pool & record shown status for this user
            if newly_discovered:
                save_discovered_to_pool(newly_discovered, category=user_category)
            mark_products_for_user(user_id, sanitized_products, status="shown")

            logger.info(f"Discovered {len(sanitized_products)} winning products for user '{user_id}': {[p['name'] for p in sanitized_products]}")

            run_logs = []
            if grounding_info.get("queries"):
                q_str = ", ".join(grounding_info["queries"][:4])
                run_logs.append(f"🌐 1-ci Agent (Google Search Grounding): Canlı axtarış sorğuları icra edildi: {q_str}")
            elif direct_live_news:
                run_logs.append(
                    f"🌐 1-ci Agent (Canlı Web Grounding): Son 14 günün real Google News axtarış xəbərləri ({len(direct_live_news)} məqalə) təhlil edildi."
                )
            else:
                run_logs.append(
                    "⚠️ 1-ci Agent: Google Search Grounding kvotası limitdədir — məhsullar TikTok/Amazon/Shopify radar məlumatları əsasında seçildi."
                )
            if len(sanitized_products) < 3:
                run_logs.append(f"⚠️ 1-ci Agent: 3 əvəzinə yalnız {len(sanitized_products)} etibarlı məhsul tapıldı.")
            run_logs.append(
                f"✅ 1-ci Agent: {len(sanitized_products)} uğurlu məhsul tapıldı: {', '.join(p['name'] for p in sanitized_products)}"
            )

            return {
                "products": sanitized_products,
                "selected_product": None,  # user will pick from the 3
                "current_phase": "product_research",
                "logs": run_logs,
            }

        except Exception as e:
            logger.error(f"Error in Product Research Agent: {str(e)}", exc_info=True)
            raise RuntimeError(f"Məhsul Araşdırması xətası: {str(e)}") from e


def create_product_research_node(
    client: genai.Client,
    model: str = "gemini-3.5-flash-lite",
    tools: Optional[List[Callable]] = None,
) -> Callable[[ECommerceState], Dict[str, Any]]:
    """Factory function to create the product research node."""
    agent = ProductResearchAgent(client=client, model=model, tools=tools)

    async def node(state: ECommerceState) -> Dict[str, Any]:
        return await agent.run(state)

    return node
