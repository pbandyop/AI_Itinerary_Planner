# AI Itinerary Planner — Agent Service

LangGraph multi-agent service (Phases 0–5): Orchestrator wave planning → specialists → Synthesis → Reviewer. Voice clients call `POST /invoke` with spoken/typed text (and optional prior itinerary for edit/explain).

See [`docs/schema.md`](../../docs/schema.md) and [`evals/fixtures/sample_transcripts.md`](../../evals/fixtures/sample_transcripts.md).

## Deploy (Railway)

See [`docs/deploy-railway.md`](../../docs/deploy-railway.md). Dockerfile: `services/agent/Dockerfile` (build from repo root). Honors Railway `PORT`; default RAG on Railway is `bm25`.

## Setup

```bash
cd services/agent
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -e .
```

Load secrets from the repo-root `.env` (`GOOGLE_API_KEY`, `LLM_PROVIDER=gemini`, …).

## Run graph (CLI)

```bash
python -m agent.main "Plan a 3-day trip to Jaipur"
```

## HTTP API

```bash
python -m agent.main --serve
```

- `GET /health` — phase, graph summary, MCP tool names
- `GET /mcp/cities`, `GET /mcp/tools`
- `POST /mcp/poi_search`, `/mcp/itinerary_builder`, `/mcp/travel_time`, `/mcp/weather`, `/mcp/knowledge`
- `POST /invoke` — body:

```json
{
  "user_message": "Plan a 3-day trip to Jaipur…",
  "previous_itinerary": null
}
```

For edit/explain turns, send the last `merged_itinerary` as `previous_itinerary`. CORS defaults allow `http://localhost:3000` (override with `CORS_ORIGINS`).

## Smoke tests

```bash
python -m agent.smoke_mcp --interests food culture --days 3
python -m agent.smoke_mcp --no-overpass
python -m agent.smoke_rag --city Jaipur
python -m agent.smoke_graph --city Jaipur
```
