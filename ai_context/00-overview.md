# AI Context: Project Overview

## Purpose

ApexCart is a Python/FastAPI e-commerce automation system. A LangGraph workflow coordinates specialist AI agents to select a product, source it, construct a storefront, create marketing assets, publish social content, create ads, and monitor supply/order health. This directory is the canonical orientation pack for an AI agent changing the repository.

## Read order for a new task

1. Read `06-change-playbook.md` first for safety and verification rules.
2. Read the component document named by the task: pipeline/state, agents, tools/data, or web/API.
3. Open only the referenced implementation files needed for the requested change.
4. Update the relevant context document whenever a contract, route, state field, integration, or workflow path changes.

## Repository map

| Area | Main location | Responsibility |
| --- | --- | --- |
| CLI bootstrap | `main.py` | Loads settings, creates Gemini client, maps tools to agents, builds initial state, runs the graph. |
| Web app | `webapp.py` | FastAPI control plane, pipeline lifecycle, approvals, SSE logs, API integrations, and HTML pages. |
| Workflow | `workflows/pipeline.py` | LangGraph nodes, human interrupts, routing, parallel execution, and checkpointing. |
| Agent behavior | `agents/` | Gemini prompts, structured parsing, and state updates for each business role. |
| Shared contract | `state/graph_state.py` | `ECommerceState` TypedDict and reducers used by every graph node. |
| Configuration | `config/settings.py` / `.env` | Pydantic settings and secrets. |
| Integrations | `tools/` | Marketplace, Shopify, social, creative, ads, monitoring, persistence, and notifications. |
| Web templates | `templates/` | Main dashboard, landing page, and this context viewer. |
| Persistent artifacts | `data/` | Radar snapshots/reports and runtime-generated operational data. |

## Architectural boundaries

- `agents/` should decide and format domain output; `tools/` should perform I/O or deterministic calculations.
- `workflows/pipeline.py` owns sequencing and human approval gates. Do not add orchestration decisions inside a tool.
- `state/graph_state.py` is the compatibility contract. A state field change requires reviewing every producing and consuming node.
- `webapp.py` owns in-process UI state. It is not a multi-worker durable job queue.
- Secrets remain in `.env`; never put credentials into state, logs, Markdown context, or HTML.

## Context library maintenance rule

This library deliberately summarizes stable contracts, not line-by-line code. Keep links and paths accurate. When a change invalidates a statement here, update the matching Markdown file in the same change.
