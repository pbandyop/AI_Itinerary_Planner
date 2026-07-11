# AI Itinerary Planner

Voice-first AI travel planning assistant for **Jaipur** (2–4 day itineraries). Capstone project: multi-agent GenAI system with MCP tools, RAG grounding, voice UX, evals, and n8n PDF/email.

**Repo:** [pbandyop/AI_Itinerary_Planner](https://github.com/pbandyop/AI_Itinerary_Planner)

## Architecture (locked)

```
Voice (STT) → LangGraph
                Orchestrator (safety gate + intent)
                  → POI / Itinerary / Knowledge agents (MCP + RAG)
                  → Merger → Reviewer
              → Companion UI → n8n (PDF + email)
```

| Layer | Choice |
|-------|--------|
| Agent graph | **LangGraph** (Python) |
| Tools / RAG | **LangChain** |
| LLM | OpenAI API |
| UI | Next.js + Browser Web Speech API (STT) |
| Scope | Jaipur only · 2–4 days · heuristic travel times |

See [`docs/implementationPlan.md`](docs/implementationPlan.md) for the full phase plan.

### LangGraph nodes (target)

| Node | Role |
|------|------|
| `orchestrator` | Safety, intent, clarify/confirm, dispatch |
| `poi_agent` | POI Search MCP (OpenStreetMap) |
| `itinerary_agent` | Itinerary Builder MCP |
| `knowledge_agent` | Wikivoyage/Wikipedia RAG + citations |
| `merger` | Fuse specialist outputs → itinerary JSON |
| `reviewer` | Feasibility / grounding / edit-scope gate |

**Phase 0 stub:** `START → orchestrator → END` only.

## Monorepo layout

```
apps/web/           Next.js companion UI
services/agent/     Python LangGraph + FastAPI agent service
evals/              Golden fixtures + eval runners (stubs → Phase 7)
docs/               Problem statement, implementation plan, schema
```

## Prerequisites

- Node.js 20+
- Python 3.11+
- OpenAI API key (needed from Phase 2+; stub runs without it)

## Setup

### 1. Environment

```bash
cp .env.example .env
# Add OPENAI_API_KEY when you start LLM-backed phases
```

### 2. Agent service (LangGraph stub)

```bash
cd services/agent
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
pip install -e .
```

Run one-shot stub:

```bash
python -m agent.main "Plan a 3-day trip to Jaipur"
```

Or HTTP API:

```bash
python -m agent.main --serve
# GET  http://localhost:8000/health
# POST http://localhost:8000/invoke
```

### 3. Web app

```bash
cd apps/web
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

## MCP tools (planned)

| MCP | Status |
|-----|--------|
| POI Search (OpenStreetMap / Overpass) | Phase 2 |
| Itinerary Builder | Phase 2 |
| Weather Adjustment (Open-Meteo) | Optional bonus |
| Travel Time Estimator | Optional bonus |

## Datasets (planned)

- OpenStreetMap (Overpass API) — POIs
- Wikivoyage / Wikipedia — city tips (RAG)
- Open-Meteo — weather (optional)

## Evaluations (planned — Phase 7)

1. Feasibility (duration, travel, pace)
2. Edit correctness (targeted patches only)
3. Grounding / hallucination (OSM ids + citations)

Phase 1 stubs already load golden fixtures:

```bash
python -m evals --suite fixtures
python -m evals
```

## Sample test transcripts (placeholder)

```
Plan a 3-day trip to Jaipur next weekend. I like food and culture, relaxed pace.
Make Day 2 more relaxed.
Why did you pick this place?
What if it rains?
```

## Schema (Phase 1)

- Docs: [`docs/schema.md`](docs/schema.md)
- Python: `services/agent/src/agent/schemas/`
- TypeScript: `apps/web/src/types/itinerary.ts`
- Golden fixtures: `evals/fixtures/*.json`

```bash
# From repo root (agent venv activated + pip install -e services/agent)
python -m evals --suite fixtures
python -m evals
```

## Current phase

**Phase 1 complete:** itinerary JSON + LangGraph `GraphState`, golden Jaipur fixtures, validation helpers, eval stubs.

Next: **Phase 2** — POI Search + Itinerary Builder MCP tools (LangChain wrappers).
