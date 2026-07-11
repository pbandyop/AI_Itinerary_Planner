# AI Itinerary Planner

Voice-first AI travel planning assistant for **India** (one city per trip, 2–4 day itineraries). Capstone project: multi-agent GenAI system with MCP tools, RAG grounding, voice UX, evals, and n8n PDF/email.

**Repo:** [pbandyop/AI_Itinerary_Planner](https://github.com/pbandyop/AI_Itinerary_Planner)

## Architecture (locked)

```
Voice (STT) → LangGraph
                Orchestrator (safety gate + intent)
                  → POI / Itinerary / Travel-Time / Weather / Knowledge agents
                  → Merger → Reviewer
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
# GET  http://localhost:8000/mcp/tools
# POST http://localhost:8000/mcp/poi_search
# POST http://localhost:8000/mcp/itinerary_builder
```

MCP smoke test:

```bash
python -m agent.smoke_mcp --interests food culture --days 3 --pace relaxed
python -m agent.smoke_mcp --no-overpass   # seed-only / offline
```

### 3. Web app

```bash
cd apps/web
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

## MCP tools

| MCP | Status | Call |
|-----|--------|------|
| POI Search (OpenStreetMap / Overpass) | Phase 2 ✅ | `poi_search_mcp` / `POST /mcp/poi_search` |
| Itinerary Builder | Phase 2 ✅ | `itinerary_builder_mcp` / `POST /mcp/itinerary_builder` |
| Travel Time Estimator | Phase 2 ✅ | `travel_time_estimator_mcp` / `POST /mcp/travel_time` |
| Weather Adjustment (Open-Meteo) | Phase 2 ✅ | `weather_adjustment_mcp` / `POST /mcp/weather` |

## Datasets

- `data/india_cities.json` — Indian city catalog (coords + Overpass bbox)
- OpenStreetMap (Overpass API) — POIs per city bbox
- `data/pois/*.json` — curated OSM-id seeds (fallback)
- Open-Meteo — weather forecasts / rain-risk adjustments
- Wikivoyage / Wikipedia — city tips (RAG) — Phase 3

```bash
python -m agent.smoke_mcp --city Delhi --interests heritage culture --days 2
python -m agent.smoke_mcp --city Mumbai --no-overpass
# List cities: GET http://localhost:8000/mcp/cities
```

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

**Phase 2 complete:** four MCP tools (POI Search, Itinerary Builder, Travel Time Estimator, Weather Adjustment), LangChain wrappers, smoke test.

Next: **Phase 3** — RAG grounding (Wikivoyage/Wikipedia).
