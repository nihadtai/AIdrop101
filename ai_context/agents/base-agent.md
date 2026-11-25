# Base Agent and LLM Gateway

`agents/base.py::BaseAgent` centralizes Gemini client calls, candidate-model fallback, retries, Google Search grounding metadata, and context telemetry. All specialist classes inherit it and should call its methods rather than create ad-hoc provider clients.

`_call_gemini` may reuse exact recent non-search requests; `_call_gemini_with_tools` is never cached because tools can require fresh reads or create side effects. Context compaction preserves system instruction plus the beginning/end of oversized prompts, so agents should still send deliberate, narrow state slices.

Related: `runtime/observability.md`, `architecture/boundaries.md`.
