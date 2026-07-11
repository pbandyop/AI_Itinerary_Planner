# AI Itinerary Planner — Agent Service

Phase 0 LangGraph stub: `START → orchestrator → END`.

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
