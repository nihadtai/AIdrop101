# Bootstrap and Execution

`main.py` loads dotenv, obtains `Settings`, creates the Google GenAI client, builds the per-agent tool map, initializes graph state, and streams LangGraph updates. `python main.py --dry-run` builds and prints the graph without performing a live workflow. `python webapp.py` runs the dashboard server.

Dependencies are defined in `requirements.txt`; install them before import/testing. The runtime expects a Gemini key for live work. Use `--model` only when the selected Gemini model supports the agent’s requested capability.

Related: `runtime/settings.md`, `workflow/graph.md`, `web/application.md`.
