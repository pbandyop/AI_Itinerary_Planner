# AI Itinerary Planner — Agent Service

Phase 1: shared itinerary + `GraphState` schemas; LangGraph stub still `START → orchestrator → END`.

See [`docs/schema.md`](../../docs/schema.md).

## Setup

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

## Run stub graph (CLI)

```bash
python -m agent.main "Plan a 3-day trip to Jaipur"
```

## Run HTTP API

```bash
python -m agent.main --serve
# GET  http://localhost:8000/health
# POST http://localhost:8000/invoke  {"user_message":"..."}
```
