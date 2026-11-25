# Context Library HTML and API

`templates/ai_context.html` is an observability/browser UI for this knowledge tree. `ai_context/catalog.py` recursively lists Markdown documents and reads only a path resolved inside `ai_context/`; `webapp.py` exposes its manifest and document endpoints.

The UI should help agents and operators inspect documentation without embedding arbitrary HTML. It renders content as text in a dialog. When documents move or are added, the recursive catalog automatically includes them; preserve its traversal protection.

Related: `runtime/observability.md`, `governance/updating-context.md`.
