import abc
import asyncio
import logging
import time
from typing import Any, List, Optional
from tenacity import retry, stop_after_attempt, wait_exponential
from google import genai
from google.genai import types
from ai_context import get_context_brain
from ai_context.brain import ContextEvent

logger = logging.getLogger(__name__)


class BaseAgent(abc.ABC):
    """Abstract base class for all agents using Google Gemini API."""

    def __init__(
        self,
        name: str = "BaseAgent",
        role: str = "Specialized E-Commerce Agent",
        goal: str = "Automate e-commerce workflow",
        client: genai.Client | None = None,
        model: str = "gemini-3.8-flash",
        tools: List[Any] | None = None,
        temperature: float = 0.7,
        max_output_tokens: int = 8192,
        use_google_search: bool = False,
    ):
        self.name = name
        self.role = role
        self.goal = goal
        self.client = client
        self.model = model
        self.tools = tools or []
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.use_google_search = use_google_search
        self.last_grounding_info: dict = {"queries": [], "sources": []}

    @abc.abstractmethod
    async def run(self, state: dict) -> dict:
        """Execute the agent's main logic. Returns partial state update."""
        pass

    def _extract_grounding_info(self, response: Any) -> dict:
        """Extract Google Search grounding queries and source URLs from Gemini response."""
        info = {"queries": [], "sources": []}
        try:
            candidates = getattr(response, "candidates", None) or []
            if candidates:
                gm = getattr(candidates[0], "grounding_metadata", None)
                if gm:
                    queries = getattr(gm, "web_search_queries", None) or []
                    info["queries"] = [str(q) for q in queries if q]
                    chunks = getattr(gm, "grounding_chunks", None) or []
                    for ch in chunks:
                        web = getattr(ch, "web", None)
                        if web:
                            uri = getattr(web, "uri", None)
                            title = getattr(web, "title", None)
                            if uri:
                                info["sources"].append({"title": title or uri, "url": uri})
        except Exception as e:
            logger.debug(f"Could not extract grounding metadata: {e}")
        return info

    async def _call_gemini(
        self,
        prompt: str,
        system_instruction: str,
        response_schema: dict | None = None,
        use_google_search: bool | None = None,
    ) -> str:
        """Call Gemini; if every model is temporarily overloaded (503), wait and retry the whole chain."""
        waits = [4, 8, 15]
        for round_no in range(len(waits) + 1):
            try:
                return await self._call_gemini_once(prompt, system_instruction, response_schema, use_google_search)
            except Exception as e:
                err = str(e).lower()
                overloaded = "503" in err or "unavailable" in err or "high demand" in err or "overloaded" in err
                if not overloaded or round_no >= len(waits):
                    raise
                logger.warning(
                    f"Gemini modelləri yüklənib (503). {waits[round_no]} saniyə gözlənilir, "
                    f"cəhd {round_no + 2}/{len(waits) + 1}..."
                )
                await asyncio.sleep(waits[round_no])
        raise RuntimeError("No response received from Gemini API.")

    _SEARCH_QUOTA_EXHAUSTED = False

    async def _call_gemini_once(
        self,
        prompt: str,
        system_instruction: str,
        response_schema: dict | None = None,
        use_google_search: bool | None = None,
    ) -> str:
        """Call Gemini API with optional Google Search Grounding, retry logic, and fallback models."""
        brain = get_context_brain()
        original_prompt, original_system = prompt, system_instruction
        prompt, system_instruction, context_trimmed = brain.prepare(prompt, system_instruction)
        input_before = brain.estimate_tokens(original_prompt + original_system)
        input_sent = brain.estimate_tokens(prompt + system_instruction)
        started_at = time.perf_counter()
        enable_search = self.use_google_search if use_google_search is None else use_google_search
        # Search-grounded calls intentionally skip response caching: their value is freshness.
        cache_key = brain.request_key(self.model, prompt, system_instruction, {"schema": response_schema})
        if not enable_search:
            cached = brain.get_cached(cache_key)
            if cached is not None:
                brain.record(ContextEvent(
                    time.time(), self.name, "generate", self.model, input_before, input_sent,
                    brain.estimate_tokens(cached), int((time.perf_counter() - started_at) * 1000),
                    True, context_trimmed, "ok",
                ))
                return cached
        self.last_grounding_info = {"queries": [], "sources": []}

        candidate_models = [self.model]
        for fallback in [
            "gemini-3.5-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite-preview",
            "gemini-3.5-flash",
            "gemini-3.8-flash",
        ]:
            if fallback not in candidate_models:
                candidate_models.append(fallback)

        base_config_kwargs = {
            "system_instruction": system_instruction,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }
        if response_schema:
            base_config_kwargs["response_mime_type"] = "application/json"
            base_config_kwargs["response_json_schema"] = response_schema

        # Skip redundant search attempts if this API key already hit 429 (0 RPD on Google Search tool)
        if enable_search and not BaseAgent._SEARCH_QUOTA_EXHAUSTED:
            search_modes = [True, False]
        else:
            if enable_search and BaseAgent._SEARCH_QUOTA_EXHAUSTED:
                logger.info("ℹ️ Google Search Grounding kvotası API açarında bağlıdır (0 RPD) — canlı Trend Radar + RSS məlumatları ilə davam edilir.")
            search_modes = [False]
        last_error = None

        for with_search in search_modes:
            config_kwargs = dict(base_config_kwargs)
            config_kwargs["http_options"] = types.HttpOptions(
                timeout=25000,
                retry_options=types.HttpRetryOptions(attempts=1),
            )
            if with_search:
                config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
                logger.info("🔍 [Google Search Grounding] Aktivdir — Gemini canlı Google axtarışı ilə cavab hazırlayır...")

            quota_aborted_search = False
            for current_model in candidate_models:
                max_attempts = 1 if with_search else 2
                for attempt in range(max_attempts):
                    try:
                        response = await self.client.aio.models.generate_content(
                            model=current_model,
                            contents=prompt,
                            config=types.GenerateContentConfig(**config_kwargs),
                        )
                        if with_search:
                            self.last_grounding_info = self._extract_grounding_info(response)
                            if self.last_grounding_info["queries"]:
                                logger.info(
                                    f"✅ [Google Search Grounding] Axtarış sorğuları: {self.last_grounding_info['queries']}"
                                )
                        text = response.text
                        if not with_search:
                            brain.cache_response(cache_key, text)
                        brain.record(ContextEvent(
                            time.time(), self.name, "generate", current_model, input_before, input_sent,
                            brain.estimate_tokens(text), int((time.perf_counter() - started_at) * 1000),
                            False, context_trimmed, "ok",
                        ))
                        return text
                    except Exception as e:
                        last_error = e
                        err_str = str(e).lower()
                        if with_search and ("429" in err_str or "quota" in err_str or "resource_exhausted" in err_str):
                            BaseAgent._SEARCH_QUOTA_EXHAUSTED = True
                            logger.warning(
                                "⚠️ API açarında Google Search Grounding kvotası 0 RPD / tükənib (429 RESOURCE_EXHAUSTED). "
                                "Axtarışsız + canlı RSS/Radar rejiminə keçilir..."
                            )
                            quota_aborted_search = True
                            break
                        if with_search and ("400" in err_str or "invalid_argument" in err_str or "unsupported" in err_str):
                            logger.warning(
                                f"Model {current_model} does not support combined GoogleSearch + JSON schema directly; switching..."
                            )
                            break
                        if "429" in err_str or "quota" in err_str or "rate" in err_str:
                            logger.warning(f"Quota/rate limit on {current_model} (search={with_search}), switching model...")
                            break
                        elif "503" in err_str or "unavailable" in err_str or "high demand" in err_str:
                            logger.warning(
                                f"Model {current_model} busy/unavailable (503): {e}. Attempting fallback..."
                            )
                            break
                        elif attempt < max_attempts - 1:
                            await asyncio.sleep(1.0)
                        else:
                            break
                if quota_aborted_search:
                    break
            if with_search and not quota_aborted_search:
                logger.warning(f"⚠️ Google Search Grounding işləmədi (son xəta: {str(last_error)[:120]}); axtarışsız rejimə keçilir...")

        if last_error:
            brain.record(ContextEvent(
                time.time(), self.name, "generate", self.model, input_before, input_sent, 0,
                int((time.perf_counter() - started_at) * 1000), False, context_trimmed, "error", str(last_error)[:160],
            ))
            raise last_error
        raise RuntimeError("No response received from Gemini API.")

    async def _call_gemini_with_tools(
        self,
        prompt: str,
        system_instruction: str,
        tools: list | None = None,
    ) -> str:
        """Call Gemini with function calling and fallback models."""
        brain = get_context_brain()
        original_prompt, original_system = prompt, system_instruction
        prompt, system_instruction, context_trimmed = brain.prepare(prompt, system_instruction)
        input_before = brain.estimate_tokens(original_prompt + original_system)
        input_sent = brain.estimate_tokens(prompt + system_instruction)
        started_at = time.perf_counter()
        candidate_models = [self.model]
        for fallback in ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.5-flash"]:
            if fallback not in candidate_models:
                candidate_models.append(fallback)

        active_tools = tools or self.tools
        config_kwargs = {
            "system_instruction": system_instruction,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }
        if active_tools:
            config_kwargs["tools"] = active_tools

        last_error = None
        for current_model in candidate_models:
            for attempt in range(3):
                try:
                    # Removed mandatory 1.2s sleep — only sleep on retry
                    response = await self.client.aio.models.generate_content(
                        model=current_model,
                        contents=prompt,
                        config=types.GenerateContentConfig(**config_kwargs),
                    )
                    text = response.text
                    # Tool requests are never response-cached because tools may have side effects.
                    brain.record(ContextEvent(
                        time.time(), self.name, "generate_with_tools", current_model, input_before, input_sent,
                        brain.estimate_tokens(text), int((time.perf_counter() - started_at) * 1000),
                        False, context_trimmed, "ok",
                    ))
                    return text
                except Exception as e:
                    last_error = e
                    err_str = str(e).lower()
                    if "429" in err_str or "quota" in err_str or "rate" in err_str:
                        wait = 2 ** attempt
                        logger.warning(f"Rate limit hit on {current_model}, waiting {wait}s...")
                        await asyncio.sleep(wait)
                    elif "503" in err_str or "unavailable" in err_str or "high demand" in err_str:
                        logger.warning(
                            f"Model {current_model} busy/unavailable (503): {e}. Attempting fallback..."
                        )
                        break
                    elif attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                    else:
                        break

        if last_error:
            brain.record(ContextEvent(
                time.time(), self.name, "generate_with_tools", self.model, input_before, input_sent, 0,
                int((time.perf_counter() - started_at) * 1000), False, context_trimmed, "error", str(last_error)[:160],
            ))
            raise last_error
        raise RuntimeError("No response received from Gemini API.")

    def _log(self, message: str, state: dict) -> dict:
        log_entry = f"[{self.name}] {message}"
        logger.info(log_entry)
        return {"logs": [log_entry]}

    def _add_error(self, error: str, state: dict) -> dict:
        error_entry = f"[{self.name} ERROR] {error}"
        logger.error(error_entry)
        return {"errors": [error_entry]}
