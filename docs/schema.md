# Itinerary & LangGraph State Schema (Phase 1)

Shared contracts used by Orchestrator, specialists, Merger, Reviewer, UI, PDF, and evals.

**Rule:** every POI must have an OSM id (`osm_type` + `osm_id`). Every stop must include **citations** and/or an explicit **`uncertainty`** note — no silent hallucination.

## Itinerary JSON (`schema_version: "1.0"`)

| Field | Description |
|-------|-------------|
| `trip` | Indian city (catalog), `country: India`, `num_days` (2–4), interests, pace, constraints, `confirmed`, daily time window |
| `days[]` | `day_index` 1..N, optional `calendar_date`, with `morning` / `afternoon` / `evening` blocks |
| `days[].*.stops[]` | Place visits with OSM id, duration, travel-to-next, reason, citations, uncertainty |
| `sources[]` | Trip-level references for the UI Sources panel |
| `summary` / `uncertainty_notes` | Optional narrative + global caveats |
| `reasoning[]` | Itinerary Agent optimization decisions (copied through by Synthesis) |

### Stop (required grounding fields)

```json
{
  "name": "Hawa Mahal",
  "osm_type": "way",
  "osm_id": 246901234,
  "duration_min": 60,
  "travel_to_next_min": 10,
  "arrive_time": "09:00",
  "depart_time": "10:00",
  "reason": "Why this stop was chosen",
  "citations": [{ "title": "...", "url": "...", "dataset": "wikivoyage" }],
  "uncertainty": null
}
```

`arrive_time` / `depart_time` are optional `HH:MM` (24h) stamps from the Itinerary Builder (day start by pace + duration + travel).

If citations are empty, `uncertainty` **must** explain the missing data.

## Specialist envelopes

- `POISearchResult` — ranked POIs with OSM ids (POI Agent)
- `ItineraryDraftResult` — day packing from Itinerary Builder
- `TravelTimeResult` — heuristic legs between stops (Travel-Time Agent)
- `WeatherResult` — Open-Meteo forecast + rain adjustments (Weather Agent)
- `KnowledgeResult` — RAG snippets + citations (Knowledge Agent; Phase 3 `knowledge_rag`)
- `DispatchPlan` — which specialists the Orchestrator should run

**Phase 4 RAG + multi-agent:** Orchestrator emits `ExecutionPlan` (`waves` + `success_criteria`); Itinerary optimizes; Synthesis presents; Reviewer returns autonomous revise packets. `agent_trace` records the plan and decisions.

## Edit patch

```json
{
  "target": { "day": 2, "block": "evening" },
  "operation": "relax_block",
  "payload": {},
  "user_utterance": "Make Day 2 more relaxed."
}
```

## Reviewer verdict

Autonomous feedback packet. On revise, the Orchestrator routes **directly** to
`target_agent` and forwards `constraints` — no inference.

```json
{
  "status": "revise",
  "reason": "Travel time exceeds relaxed pace",
  "target_agent": "itinerary_agent",
  "constraints": ["Reduce travel", "Keep museum", "Preserve Day 1"],
  "issues": [],
  "affected_sections": ["day2"]
}
```

or `status: "approve"` with `reason` and empty `target_agent` / `constraints`.

## LangGraph `GraphState`

| Key | Purpose |
|-----|---------|
| `messages`, `user_message`, `user_reply` | Conversation I/O |
| `intent`, `safety_status` | Routing / safety gate |
| `trip_constraints`, `dispatch_plan`, `edit_patch` | Orchestrator outputs |
| `poi_results`, `itinerary_draft`, `knowledge_results` | Specialist outputs |
| `merged_itinerary`, `previous_itinerary`, `sources` | Synthesis Agent outputs (composed itinerary) |
| `reviewer_verdict`, `revision_feedback`, `revision_count` | Review loop + targeted revise |
| `ready_for_synthesis` | Set when Orchestrator `success_criteria` are satisfied |

Python source of truth: `services/agent/src/agent/schemas/`.

JSON Schema endpoint (when agent is serving): `GET /schema/itinerary`

Golden fixtures: `evals/fixtures/jaipur_3day_relaxed.json`, `evals/fixtures/jaipur_2day_culture.json`

## Validate fixtures

```bash
cd services/agent
.\.venv\Scripts\activate   # or source .venv/bin/activate
pip install -e .

cd ../..
python -m evals --suite fixtures
python -m evals                 # all Phase 1 stubs
```
