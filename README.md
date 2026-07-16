# AI Itinerary Planner

Voice-first AI travel planning assistant for **India** (one city per trip, 2–4 day itineraries). Capstone project: multi-agent GenAI system with MCP tools, RAG grounding, voice UX, evals, and n8n PDF/email.

**Repo:** [pbandyop/AI_Itinerary_Planner](https://github.com/pbandyop/AI_Itinerary_Planner)

## Architecture (locked)

```
Voice (STT) → LangGraph
                Orchestrator (ExecutionPlan: waves + success_criteria)
                  Wave1: POI ∥ Weather ∥ Knowledge
                  Wave2: TravelTime
                  Wave3: Itinerary (optimizer)
                  → Synthesis (presentation) → Reviewer
              → Companion UI → n8n (PDF + email)
```

| Layer | Choice |
|-------|--------|
| Agent graph | **LangGraph** (Python) |
| Tools / RAG | **LangChain** |
| LLM | OpenAI API |
| UI | Next.js + Browser Web Speech API (STT) |
| Scope | **India** (`data/india_cities.json`) · one city per trip · 2–4 days · heuristic travel times |

See [`docs/implementationPlan.md`](docs/implementationPlan.md) for the full phase plan and [`data/README.md`](data/README.md) for the India data model.

### LangGraph nodes

| Node | Role |
|------|------|
| `orchestrator` | Safety, intent, **ExecutionPlan**, artifact completion check |
| `poi_agent` | POI Search MCP (OpenStreetMap) |
| `weather_agent` / `knowledge_agent` | Weather MCP / RAG citations |
| `travel_time_agent` | Travel legs among POI candidates |
| `itinerary_agent` | **Owns** optimization (move/skip/reorder) |
| `synthesis_agent` | Presentation only (citations, schema, narrative) |
| `reviewer` | Autonomous `{status, reason, target_agent, constraints}` |
## Monorepo layout

```
apps/web/           Next.js companion UI
services/agent/     Python LangGraph + FastAPI agent service
evals/              Golden fixtures + Phase 7 eval runners (feasibility, edit, grounding)
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
# Add GOOGLE_API_KEY for Gemini (default LLM). Optional: LLM_PROVIDER=openai + OPENAI_API_KEY
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
#   body: { "user_message": "...", "previous_itinerary": { ... } }  # optional for edit/explain
# GET  http://localhost:8000/mcp/tools
# POST http://localhost:8000/mcp/poi_search
# POST http://localhost:8000/mcp/itinerary_builder
# POST http://localhost:8000/mcp/knowledge
```

MCP / RAG smoke tests:

```bash
python -m agent.smoke_mcp --interests food culture --days 3 --pace relaxed
python -m agent.smoke_mcp --no-overpass   # seed-only / offline
python -m agent.smoke_rag --city Jaipur
python -m agent.smoke_rag --missing-city
python -m agent.smoke_graph --city Jaipur   # Phase 4 E2E: plan + safety + edit
```

### 3. Web app

```bash
cd apps/web
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). Mic + live transcript talk to the agent at `NEXT_PUBLIC_AGENT_BASE_URL` (default `http://localhost:8000`). Chrome/Edge recommended for speech.

## MCP tools & RAG

| Tool | Status | Call |
|------|--------|------|
| POI Search (OpenStreetMap / Overpass) | Phase 2 ✅ | `poi_search_mcp` / `POST /mcp/poi_search` |
| Itinerary Builder | Phase 2 ✅ | `itinerary_builder_mcp` / `POST /mcp/itinerary_builder` |
| Travel Time Estimator | Phase 2 ✅ | `travel_time_estimator_mcp` / `POST /mcp/travel_time` |
| Weather Adjustment (Open-Meteo) | Phase 2 ✅ | `weather_adjustment_mcp` / `POST /mcp/weather` |
| Knowledge RAG (Wikivoyage) | Phase 3 ✅ | `knowledge_rag` / `POST /mcp/knowledge` |

## Datasets

- `data/india_cities.json` — Indian city catalog (coords + Overpass bbox) — **config, not POI content**
- OpenStreetMap (**live Overpass API**) — **primary** POI source
- `data/pois/*.json` — **Overpass fallback** only (OSM-id curated seeds when Overpass fails/sparse)
- Open-Meteo — live weather forecasts / rain-risk adjustments
- `data/rag/corpus/` — Wikivoyage extracts for **RAG** tips + citations (Phase 3)

See [`data/README.md`](data/README.md) and [`data/rag/README.md`](data/rag/README.md).

```bash
python -m agent.smoke_mcp --city Delhi --interests heritage culture --days 2
python -m agent.smoke_mcp --city Mumbai --no-overpass
python -m agent.rag.ingest --force-chunks   # rebuild chunks; Chroma if embeddings configured
# List cities: GET http://localhost:8000/mcp/cities
```

## Evaluations (Phase 7)

Three runnable suites from the repo root (agent `src` on `PYTHONPATH`):

| Suite | Checks |
|-------|--------|
| **feasibility** | Daily duration ≤ time window; stops ≤ pace cap (relaxed 4 / moderate 6 / packed 11); travel legs ≤ 120 min |
| **edit_correctness** | Before/after edit fixtures under `evals/fixtures/edits/`; only target day(s) change |
| **grounding** | Every stop has OSM id + citations **or** explicit uncertainty; tip fixtures cite sources or refuse invention |

```bash
# From repo root
set PYTHONPATH=services/agent/src
python -m evals --suite all
python -m evals --suite feasibility
python -m evals --suite edit
python -m evals --suite grounding
python -m evals --suite fixtures
```

Fixtures: `evals/fixtures/*.json` (golden plans), `evals/fixtures/edits/*.json`, `evals/fixtures/tips/*.json`.
## Sample test transcripts (placeholder)

```
Plan a 3-day trip to Jaipur next weekend. I like food and culture, relaxed pace.
Plan a 2-day trip to Delhi focused on heritage.
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

**Phase 8 (app wiring done):** After a plan appears, use **Email this plan** to POST itinerary + email via `/api/email-itinerary` to n8n. Finish PDF + Gmail in n8n Cloud ([`docs/n8n.md`](docs/n8n.md)), then **Phase 9** — deploy + demo.

Sample voice transcripts: [`evals/fixtures/sample_transcripts.md`](evals/fixtures/sample_transcripts.md).