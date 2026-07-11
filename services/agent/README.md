# AI Itinerary Planner — Agent Service

Phase 2: POI Search + Itinerary Builder MCPs (LangChain tools). LangGraph stub remains `START → orchestrator → END` until Phase 4.

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

## MCP smoke test (Phase 2)

```bash
python -m agent.smoke_mcp --interests food culture --days 3
python -m agent.smoke_mcp --no-overpass
python -m agent.main --smoke-mcp
```

## HTTP API

```bash
python -m agent.main --serve
```

- `GET /health`
- `GET /mcp/tools`
- `POST /mcp/poi_search`
- `POST /mcp/itinerary_builder`
- `POST /mcp/travel_time`
- `POST /mcp/weather`
- `POST /invoke`
