# AI Context: Change Playbook

## Before editing

1. Read `00-overview.md`, then the focused component document.
2. Check `git status --short`; preserve unrelated work.
3. Locate existing contracts with `rg` before adding a parallel implementation.
4. Decide whether the request changes a public API, workflow/state contract, provider side effect, or only local behavior.

## Change paths

| Requested change | Primary files to inspect | Required follow-through |
| --- | --- | --- |
| New workflow stage | `workflows/pipeline.py`, `state/graph_state.py`, `main.py` | Node + route + initialization + UI stage mapping + docs. |
| New AI behavior | relevant `agents/*.py`, `agents/base.py` | Narrow state inputs, structured output handling, tool scope, error/log path. |
| New integration | `tools/`, `config/settings.py`, `.env.example` | Secret config, typed/serializable result, dry-run/error behavior, docs. |
| Dashboard/API change | `webapp.py`, `templates/` | Request validation, route contract, loading/error state, no secret exposure. |
| Data/schema change | `state/graph_state.py`, `tools/db_utils.py`, consumers | Backward compatibility/migration and all graph producers/consumers. |

## Verification baseline

- Run `python -m compileall` on changed Python modules.
- Run `git diff --check`.
- For workflow changes, run `python main.py --dry-run` with dependencies installed.
- For FastAPI changes, start `python webapp.py` and exercise the relevant route or `TestClient` case.
- For provider actions, use sandbox/test credentials or a dry-run path; never validate by accidentally creating production ads, posts, products, or orders.

## Common pitfalls

- Adding a state field without initializing it in `make_initial_state`.
- Returning a replacement list for a reducer-managed parallel field.
- Changing an interrupt `step` or decision string without changing web approval handling/routing.
- Passing the full accumulated state to every LLM call, increasing cost and latency.
- Caching a tool or search-grounded response that needs fresh external data.
- Treating `webapp.py` globals as durable or multi-tenant infrastructure.

## Definition of done

A change is complete when implementation, targeted verification, configuration/docs, and the matching `ai_context` reference all agree. Keep this library concise enough to load selectively: an agent should need one overview file plus one focused file, not the entire repository.
