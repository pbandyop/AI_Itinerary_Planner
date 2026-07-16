# Implementation Plan — Voice-First AI Travel Planner

**Project:** Graduation Capstone (Applied Generative AI Bootcamp)  
**Deadline:** Jul 25, 2026  
**Scope:** **Jaipur only** (demo lock), **2–4 day** itineraries (expand `ALLOWED_CITIES` later; day max stays 4)  
**Architecture:** **Multi-agent workflow** on **LangGraph** (deterministic waves — not an open-ended autonomous swarm). LLM/heuristic **Orchestrator** confirms constraints, then runs an explicit **ExecutionPlan**; **Itinerary Agent** owns structure + scoped voice edits; **Synthesis** presents; **Reviewer** gates quality. Voice STT → confirm → plan / edit / explain. Standalone **weather** and **knowledge (RAG tip)** questions can answer without starting a trip.

This plan is derived from `docs/problemStatement.md` and the design decisions agreed during brainstorming. Build in order; do not skip ahead to UI/voice until the itinerary contract and MCP loop are solid.

**Last synced with code:** Jul 15, 2026 (Phases 0–7 implemented; Phases 8–9 remaining for submission).

---

## Architecture snapshot

```
Voice (STT: MediaRecorder → /stt Gemini/Whisper)  or  typed text
    ↓
┌──────────────────────────────────────────────────────────────┐
│  LangGraph multi-agent WORKFLOW (deterministic by default)   │
│                                                              │
│  Orchestrator Agent                                          │
│    1. Safety / policy gate                                   │
│    2. Intent: plan | confirm | edit | explain                │
│    3. Slot fill (Jaipur, 2–4 days, pace, interests, profile)│
│    4. CONFIRM constraints before plan generation (required)  │
│    5. Fast paths (no trip start): Weather MCP Q&A; RAG tip Q&A│
│    6. Emit ExecutionPlan {waves, success_criteria}           │
│         ↕                                                    │
│  plan:   Wave1 POI ∥ Weather ∥ Knowledge                     │
│          Wave2 TravelTime                                    │
│          Wave3 Itinerary (build + optimize)                  │
│  edit:   [POI?] → Itinerary.apply_edit_patch (scoped day)    │
│  explain: Knowledge (+ Weather if rain) / Synthesis grounded │
│         ↓ (success_criteria met)                             │
│  Synthesis Agent (presentation + grounded explanations)      │
│  Reviewer Agent (approve / revise with target + constraints) │
└──────────────────────────────────────────────────────────────┘
    ↓
Companion UI (transcript · pipeline log · day blocks · sources)
```

**Workflow vs autonomous:** Default `AGENT_WORKFLOW_MODE=true` uses fixed specialist waves for stable voice demos. Edit/explain always use workflow routing. Optional LLM orchestration for *plan* only when workflow mode is off and `ORCHESTRATOR_LLM=true`.

**Ownership split**
- **Itinerary Agent** — plan optimization *and* **scoped voice edits** via `apply_edit_patch` (only target day/block changes; other days copied). Pace budgets: **relaxed ≤4**, **moderate ≤6**, **packed ≤11** stops/day when POIs allow; ideal block splits (relaxed **2-1-1**, moderate **2-2-2**, packed up to **4+4+3** with dinner last). Meal rules when food + other interests: breakfast-first, dinner-last; evening soft stops (market/shopping/park) only when the user chose matching interests. **`reassert_meal_pace_layout`** runs after optimize/LLM reshuffles; **`ensure_interest_coverage`** post-pack guard restores any missing stated interest (e.g. park) from live POI pool. Travel legs get distance + walk/car mode.
- **Synthesis Agent** — presentation; for explain, grounds answers in place-matched RAG tips (or honest “no citation”), day load / pace, weather adjustments; attaches place-matched stop citations (never random round-robin Wikivoyage cites).
- **Orchestrator** — clarify (max 6), **confirm before generate**, dispatch waves; **also** answers standalone Weather MCP and Knowledge RAG tip questions (safety / etiquette / areas / POI tips / **opening hours**) with **`(Source: Title - URL)`** citations or explicit missing-data refusals.
- **Reviewer** — structured feedback (`target_agent` + `constraints`), not free-form inter-agent chat

### Current product behaviour (voice / clarify)

| Behaviour | Current rule |
|-----------|----------------|
| City scope | Demo plans **Jaipur only**; out-of-scope cities/landmarks refused for tips/weather |
| Slot fill | Ask days → pace → interests; never invent missing slots |
| Day answers | Accepts `3`, `three`, `Three.`, `3 days`, etc. |
| Off-topic briefs | Europe / multi-country paste during clarify is rejected; re-ask Jaipur interests |
| Preference tweaks | Before confirm, “remove couple friendly” (etc.) updates trip slots — **not** itinerary edit |
| Confirm | Required (“yes” / “confirm”) before generating |
| RAG tips | Practical guidance + justifications; **every tip has `(Source: Title - URL)`** when a URL exists (official website preferred over Maps link); UI References panel; TTS stays short |
| Opening hours | Place-matched RAG (OSM / Google Places cards); refuse to invent when corpus has no hours |
| Empty RAG | Explicit “won’t invent” / corpus missing — no hallucinated facts |
| Edits | Scoped to named day(s); compound “and” edits supported (e.g. trim food + add outdoor); **`balance_block`** (“make day N more balanced”) densifies one day toward moderate pacing |
| Interest coverage | Post-pack guard: every stated interest (heritage, park, shopping, …) must appear on the final plan when a live POI exists; shopping shows as **market** category from OSM |
| Weather Q&A | Open-Meteo only; Jaipur-scoped; never invents forecast |

Example execution plan object:

```json
{
  "waves": [
    ["poi_agent", "weather_agent", "knowledge_agent"],
    ["travel_time_agent"],
    ["itinerary_agent"]
  ],
  "success_criteria": [
    "poi_candidates",
    "travel_times_available",
    "citations_present",
    "weather_adjustments",
    "itinerary_complete"
  ]
}
```

### Runtime split: LangGraph vs LangChain

| Layer | Technology | Role |
|-------|------------|------|
| **Agent graph** | **LangGraph** | Nodes, edges, shared state, parallel specialists, Reviewer→Orchestrator loop |
| **Tools / RAG** | **LangChain** | MCP wrappers as tools, retrievers, prompt templates, LLM clients |
| **App / voice UI** | Next.js (or similar) | STT, companion UI, API that invokes the LangGraph graph |
| **Automation** | n8n | PDF generation + email |

**Recommendation:** Prefer **Python + LangGraph** for the agent service (most mature docs/examples), with Next.js calling it over HTTP. Alternative: JS `@langchain/langgraph` inside the Next.js backend if you want a single TypeScript repo.

### Agent roster

| Agent | LangGraph node | Talks to user? | Calls MCP/RAG? | Responsibility |
|-------|----------------|----------------|----------------|----------------|
| **Orchestrator** | `orchestrator` | Yes | Weather MCP + Knowledge RAG (fast Q&A only); no POI invent | Safety, slots, confirm, **ExecutionPlan**, tip/weather Q&A, artifact completion check |
| **POI Agent** | `poi_agent` | No | POI Search MCP (LangChain tool) | Ranked POIs with OSM ids |
| **Itinerary Agent** | `itinerary_agent` | No | Itinerary Builder MCP + optimizer | **Owns** move/skip/reorder; best draft + `optimization_reasoning` |
| **Travel-Time Agent** | `travel_time_agent` | No | Travel Time Estimator MCP | Legs among POI candidates (before itinerary) |
| **Weather Agent** | `weather_agent` | No | Weather Adjustment MCP (Open-Meteo) | Rain risk + indoor/outdoor context |
| **Knowledge Agent** | `knowledge_agent` | No | LangChain RAG retriever | Tips + citations for plan/explain waves |
| **Synthesis** | `synthesis_agent` | No* | No MCP | **Presentation only**: citations, schema, narrative, `user_reply` |
| **Reviewer** | `reviewer` | No | No MCP | **Autonomous gate**: `{status, reason, target_agent, constraints}` |
\*Synthesis may set `user_reply` but does not run clarification dialogue — that stays with Orchestrator.

### LangGraph graph shape

```
START → orchestrator
          ├─(unsafe / need_clarify)→ END (reply to user)
          └─(ready)→ Send(wave₁) [agent ∥ agent ∥ …]
                          → join orchestrator → Send(wave₂) → …
                          → merger → reviewer
                                       ├─(approve)→ END
                                       └─(revise + target_agent)→ orchestrator
                                            → waves_for_revision(target) → … → merger → reviewer
```

**Default plan waves (workflow):** `[poi ∥ knowledge ∥ weather] → [travel_time] → [itinerary]`.  
**Explain:** `[knowledge]` or `[knowledge ∥ weather]` (rain).  
**Edit:** `[itinerary]` or `[poi] → [itinerary]` (add food / indoor); weather first when indoor/rain.  
**Revise (Reviewer-directed):** e.g. target `itinerary_agent` → `[itinerary]`.

Shared **LangGraph state** holds: user message, intent, trip slots, `dispatch_plan` (`agent_waves` + flattened `agent_sequence`), control-loop fields (`orchestration_started`, `next_agents`, `pending_waves`, `ready_for_merger`, `orchestrator_steps`, `agent_trace`), specialist outputs, merged itinerary, `reviewer_verdict` + **`revision_feedback`** (`reason` / `target_agent` / `constraints`), `revision_count`, sources, and the user-facing reply.

### Data (India) — for graders

**Primary POI source = live Overpass (MCP).** Local POI JSON files are **not** the main dataset; they are an **Overpass fallback** only.

| Asset | Path | Role |
|-------|------|------|
| City catalog | `data/india_cities.json` | Config: ~130 Indian cities with lat/lon/bbox/aliases (needed to aim Overpass / Open-Meteo) |
| **Overpass fallback** POI seeds | `data/pois/<city_slug>.json` | Curated OSM-id stops used **only when Overpass fails or returns too few POIs** (demo/offline reliability) |
| Legacy Overpass fallback | `data/jaipur_pois_seed.json` | Same as `data/pois/jaipur.json` (compat); prefer `pois/jaipur.json` |
| Docs | `data/README.md` | Data layout + fallback policy |

**RAG corpus:** `data/rag/corpus/` — **Jaipur-only**, multi-source (see `data/rag/README.md`). Generated chunks/index live under `data/rag/` (gitignored binaries).

| RAG source | Path | Use |
|------------|------|-----|
| Wikivoyage | `corpus/jaipur.json` | City guide narrative |
| Wikipedia | `corpus/wikipedia/*.json` | Landmark pages |
| OSM fact cards | `corpus/osm/jaipur_osm_facts.json` | Hours, phone, website |
| Google Places | `corpus/google/jaipur_places.json` | Hours, address (API fetch when key set) |
| Rajasthan Tourism | `corpus/tourism/jaipur_tourism.json` | Official extracts |
| Curated stubs | `corpus/curated/jaipur_places.json` | Aliases + thin long-tail POIs |

Build: `python -m agent.rag.build_corpus` → `ingest --force-chunks`. Retrieval boosts OSM/Google cards for **opening-hours** queries; place aliases expand matching.

**POI resolution order (POI Search MCP):**
1. Resolve city via `india_cities.json` (India only; one city per trip).
2. **Live Overpass** query inside the city bbox ← primary public dataset.
3. If Overpass errors / is empty / is sparse → merge **Overpass fallback** seeds from `data/pois/<slug>.json` (still OSM-grounded via `osm_type`/`osm_id`).
4. If still empty → `missing_data=true` (no invented places).

**Weather:** live Open-Meteo at the city’s coordinates (no local weather files).  
**Travel times:** heuristic from coordinates (no local transit dump).

**Constraint:** Demo city is always **Jaipur**; country is always India; one city per itinerary.

**Rubric focus (effort allocation):**

| Area | Weight | Plan emphasis |
|------|--------|---------------|
| Voice UX & intent | 25% | Phases 5–6 |
| MCP & system design | 20% | Phases 1–2, 4 |
| Grounding & RAG | 15% | Phase 3 |
| AI evals & iteration | 20% | Phase 7 (start early stubs in Phase 1) |
| Workflow automation | 10% | Phase 8 |
| Deployment & code quality | 10% | Phase 9 |

---

## Phase 0 — Project setup & decisions (0.5–1 day)

**Goal:** Repo ready, stack locked, scope frozen.

### Tasks
- [x] Initialize git repo and project structure (`apps/web` + `services/agent` monorepo)
- [x] Lock stack:
  - **LangGraph** — multi-agent orchestration runtime (**Python**)
  - **LangChain** — tools, RAG retriever, LLM wrappers
  - **OpenAI API** (or compatible) for LLM calls
  - **Next.js** companion UI + STT (Browser Web Speech API; Whisper optional)
  - Agent service: **Python + langgraph** (locked)
  - Local/simple vector store for RAG (LangChain-compatible) — Phase 3
- [x] Freeze scope: **Jaipur-only demo** (`SCOPED_CITY` / `ALLOWED_CITIES`), **one city per trip**, **2–4 days**, heuristic travel times OK (`data/india_cities.json` still used for geo / out-of-scope detection)
- [x] Create `.env.example` (LLM keys, n8n webhook, Overpass if needed)
- [x] Draft README skeleton (architecture diagram, LangGraph nodes, MCP list, datasets, evals)

### Exit criteria
- [x] Empty web app + empty LangGraph stub graph runs locally (`START → orchestrator → END`)
- [x] Stack choice (Python LangGraph) documented

---

## Phase 1 — Itinerary schema & LangGraph state (1–1.5 days)

**Goal:** One shared contract for itinerary JSON **and** LangGraph graph state.

### Tasks
- [x] Define itinerary types / JSON Schema:
  - Trip metadata: city (India catalog), country=`India`, dates/window, interests, pace, constraints, confirmed flags
  - Day → Morning / Afternoon / Evening blocks
  - Stop: name, OSM id, lat/lon, category, duration_min, travel_to_next_min, reason, citations[], uncertainty?
  - Sources list (dataset + URL/title)
  - Specialist result envelopes (POI list, itinerary draft, knowledge snippets)
  - Edit patch format: `{ target: { day, block }, operation, payload }`
  - Reviewer verdict: `{ status, reason, target_agent, constraints[], issues[], affected_sections[] }`
- [x] Define **LangGraph state schema** (TypedDict / Pydantic / Zod) including:
  - `messages`, `intent`, `safety_status`
  - `trip_constraints`, `dispatch_plan` (incl. `agent_waves` / `agent_sequence`)
  - Orchestrator loop: `next_agents`, `pending_waves`, `ready_for_merger`, `agent_trace`
  - `poi_results`, `itinerary_draft`, `knowledge_results`
  - `merged_itinerary`, `previous_itinerary`
  - `reviewer_verdict`, `revision_count`
  - `user_reply`, `sources`
- [x] Write 1–2 **golden sample itineraries** (hand-authored JSON) for Jaipur (India)
- [x] Add schema validation used by Merger, Reviewer, API, and evals
- [x] Stub empty eval runners that load golden JSON

### Exit criteria
- [x] Itinerary + graph state schemas documented (`docs/schema.md`)
- [x] Sample itinerary validates (`python -m evals --suite fixtures`)
- [x] Rule: **no POI without OSM id; no tip without citation or explicit “data missing”**

---

## Phase 2 — MCP tools (4 tools) (2–3 days)

**Goal:** Required + bonus MCP integration; wrap as **LangChain tools** for specialist nodes.

### 2a — POI Search MCP
- [x] Implement Overpass (OpenStreetMap) queries for **Indian cities** (bbox from `data/india_cities.json`)
- [x] Inputs: city (India catalog), interests, constraints
- [x] Outputs: ranked POIs with metadata + **stable OSM ids**
- [x] Handle missing/empty results honestly; **Overpass fallback** seeds in `data/pois/` (OSM ids; not a substitute for live Overpass)

### 2b — Itinerary Builder MCP
- [x] Inputs: candidate POIs, daily time windows, pace, interests
- [x] Outputs: day-wise structure matching Phase 1 schema
- [x] Heuristic travel times; respect pace
- [x] **Legacy diversify** (`ITINERARY_STRATEGY=legacy`, default): ≥1 stop per stated interest, then score fill
- [x] **Hybrid mode** (optional): interest quotas + geographic clusters via POI shortlist
- [x] Meal/pace block packing: breakfast-first, dinner-last; adaptive AM/PM/evening targets by pace
- [x] **`ensure_interest_coverage`** post-pack guard (after build + after optimize): swap or add missing interest when live POI exists; trim never drops sole interest cover
- [x] **`reassert_meal_pace_layout`**: rebuild day blocks after optimizer/LLM so caps and meal order stick

### 2c — Travel Time Estimator MCP
- [x] Inputs: ordered stops or explicit from/to legs + mode (`walk` | `city`)
- [x] Outputs: per-leg distance_km + duration_min (haversine heuristic)
- [x] Honest notes that estimates are not live transit
- [x] LangChain tool: `travel_time_estimator_mcp` · HTTP: `POST /mcp/travel_time`

### 2d — Weather Adjustment MCP (Open-Meteo)
- [x] Inputs: city (India catalog), start_date, num_days
- [x] Outputs: daily forecast, rain_risk, indoor/outdoor `adjustments[]`
- [x] Supports “What if it rains?” grounded in Open-Meteo (state missing data if API fails)
- [x] Standalone weather Q&A via Orchestrator (Jaipur-scoped forecast window; refuse OOS cities)
- [x] LangChain tool: `weather_adjustment_mcp` · HTTP: `POST /mcp/weather`

### 2e — LangChain tool wrappers + smoke test
- [x] Expose all four MCPs as LangChain `StructuredTool`s
- [x] Call tools directly (no full graph yet) → validated partial JSON
- [x] Log tool/MCP traces for demo (`python -m agent.smoke_mcp`)

### Exit criteria
- [x] All four MCPs work and are callable as LangChain tools
- [x] OSM ids present on every POI
- [x] Travel-time and weather results include honest uncertainty / method notes
- [x] Tool traces visible in logs

### Specialist agents (wired in Phase 4)
| Agent node | MCP tool |
|------------|----------|
| POI Agent | `poi_search_mcp` |
| Itinerary Agent | `itinerary_builder_mcp` |
| Travel-Time Agent | `travel_time_estimator_mcp` |
| Weather Agent | `weather_adjustment_mcp` |
| Knowledge Agent | RAG (Phase 3) |

---

## Phase 3 — RAG grounding with LangChain (1.5–2 days)

**Goal:** Cited city guidance via a LangChain retriever; used by Knowledge Agent **and** Orchestrator tip Q&A.

### Tasks
- [x] Collect Wikivoyage / Wikipedia content for major Indian cities (start with trip city; expand corpus over time)
- [x] Chunk + embed; store in a LangChain-compatible vector store
- [x] Build retriever used by Knowledge Agent for planning context and “why / doable / rain” answers
- [x] Citation objects: title, URL/source id, snippet
- [x] Empty retrieval → explicit “data missing” (no hallucinated tips)
- [x] Topic-aware retrieve + lexical boost (safety / etiquette / areas / timing / highlights)
- [x] Orchestrator **knowledge Q&A** path (no trip required): safety, scams, etiquette, areas to visit, POI tips, **opening hours**
- [x] Out-of-scope tip places refused (e.g. Paris) — never substitute foreign tips with Jaipur invents
- [x] Place-matched stop citations in Synthesis (no unrelated Wikivoyage round-robin)
- [x] Multi-source Jaipur corpus: Wikivoyage + Wikipedia + OSM facts + Google Places + tourism + curated stubs
- [x] **Source URLs in replies:** `ensure_source_link` appends `(Source: Title - URL)`; prefers official website from card text over Maps link

### Exit criteria
- [x] Tips include citations (`(Source: Title - URL)` in reply when URL exists + `sources[]` for UI)
- [x] `sources[]` can be filled from retrieval
- [x] Spot-check: every tip traces to a chunk
- [x] Voice replies can stay short; full citations appear in the References UI
- [x] Opening-hours Q&A refuses to invent when corpus lacks hours

**Delivered:** `data/rag/corpus/` (Jaipur multi-source — see `data/rag/README.md`), `agent.rag` (chunk/ingest/retrieve + topic rerank + place aliases), LangChain tool `knowledge_rag`, HTTP `POST /mcp/knowledge`, smoke `python -m agent.smoke_rag`. Default local embeddings: **BGE** (`BAAI/bge-small-en-v1.5`) + **Chroma**; BM25 fallback when embeddings are off. Orchestrator tip/safety/etiquette/**hours** Q&A + Synthesis place-matched citations. Optional live fetch: `python -m agent.rag.fetch_google_places` when `GOOGLE_PLACES_API_KEY` set.


---

## Phase 4 — LangGraph multi-agent pipeline (2.5–3 days)

**Goal:** Implement the full graph with Orchestrator **planning autonomy** (choose agents + exploit parallelism) → specialists → Merger → Reviewer (text-first).

### 4a — Graph wiring (LangGraph)
- [x] Create `StateGraph` with shared state from Phase 1
- [x] Add nodes: `orchestrator`, `poi_agent`, `itinerary_agent`, `knowledge_agent`, `weather_agent`, `travel_time_agent`, `merger`, `reviewer`
- [x] **Multi-agent control loop:** Orchestrator ⇄ specialists as **parallel waves** (`langgraph.types.Send` fan-out + join); finalize → Merger
- [x] Conditional edges (`route_after_orchestrator`):
  - Orchestrator → END if unsafe or needs clarification
  - Orchestrator → one specialist **or** `[Send(a, state) for a in next_agents]` for a parallel wave
  - Orchestrator → Merger when `ready_for_merger`
  - Each specialist → Orchestrator (fan-in / join before next wave)
  - Reviewer → END if `approve` or `revision_count >= 2`
  - Reviewer → Orchestrator if `revise` and retries remain
- [x] Cap loops with `revision_count` + `orchestrator_steps`
- [x] `agent_trace` records `dispatch_wave:…` / `wave_returned` for demos; LangSmith optional via env

### 4b — Orchestrator node (planning autonomy)
- [x] **Safety / policy gate** before any specialist dispatch
- [x] Unsafe → short refusal in `user_reply`, skip specialists
- [x] Preference collection (max 6 clarifying questions)
- [x] Confirm constraints before generating
- [x] Intent: `plan` | `edit` | `explain` | `confirm`
- [x] Slot answers: bare day words (`Three` / `2`), pace words (`balanced` → moderate), interests
- [x] Reject off-scope Europe/multi-country briefs during clarify (do not absorb as Jaipur prefs)
- [x] Preference corrections before confirm (e.g. “remove couple friendly”) stay in **plan** intent — never itinerary edit
- [x] Standalone **Weather MCP** Q&A (Open-Meteo; Jaipur-only; no invention)
- [x] Standalone **Knowledge RAG** tip Q&A (cited; missing-data honest)
- [x] **`agent_planner`:** choose *which* agents + pack into **`agent_waves`** (LLM if `GOOGLE_API_KEY` / provider key + `ORCHESTRATOR_LLM`, else heuristic; default provider **Gemini**)
- [x] **Dependency enforcement:** never run `itinerary_agent` before POI results; never run `travel_time_agent` before itinerary draft; pack independent agents into the same wave
- [x] Dispatch current wave via `next_agents` / `pending_waves`; after join, dispatch next wave or set `ready_for_merger`
- [x] On revise: **route Reviewer `target_agent` + `constraints`** via `waves_for_revision` (no inference)
- [x] Edits → targeted patch + minimal wave set (e.g. `[travel_time]` or `[weather ∥ travel_time]`); compound “and” edits; day-scoped only

### 4c — Specialist nodes
- [x] **POI Agent** — POI Search MCP
- [x] **Itinerary Agent** — Itinerary Builder MCP
- [x] **Knowledge Agent** — RAG (Chroma / BGE)
- [x] **Weather Agent** — Weather Adjustment MCP
- [x] **Travel-Time Agent** — Travel Time Estimator MCP
- [x] Specialists write only their state slice (reducers for concurrent wave writes); **no user chat**; return to Orchestrator

### 4d — Synthesis Agent (presentation / response composer)
- [x] Compose optimized itinerary draft into schema-valid JSON (no structural changes)
- [x] Attach **place-matched** Knowledge citations + aggregate `sources[]`
- [x] User-friendly summary / `user_reply` (optional `SYNTHESIS_LLM`)
- [x] Deduplicate sources; ensure uncertainty notes where citations missing
- [x] Explain / “why this stop”: place-matched RAG tip + Source, or honest no-citation fallback (no invented justification)
- [x] **Does not** move/skip/reorder stops (Itinerary Agent owns optimization)
- [x] Record `agent_trace` `{agent: synthesis, action: compose}`

### 4c — Specialist nodes (addendum)
- [x] **Itinerary Agent** runs optimizer (`itinerary_optimize.py`) after builder MCP
- [x] Travel Agent estimates among POI candidates before itinerary when no draft yet
- [x] Orchestrator emits `ExecutionPlan` with `success_criteria`; `artifacts_complete()` gates Synthesis
- [x] Optimizer + edits call **`reassert_meal_pace_layout`** so breakfast/dinner order and pace caps survive LLM moves
- [x] **`ensure_interest_coverage`** after optimize (itinerary agent) when POI pool available
- [x] Voice edit ops include **`balance_block`** (densify one day toward moderate; fetches unused POIs when pool thin; shopping/market only when shopping interest set)

### 4e — Reviewer Agent (fully autonomous)
- [x] No new POIs, no MCP calls, no user clarification chat
- [x] **Heuristic hard checks** always run: feasibility, grounding, edit scope
- [x] Emit structured feedback: `{ status, reason, target_agent, constraints[] }` (+ issues/sections)
- [x] **LLM decision layer** (`REVIEWER_LLM`) may enrich reason/target/constraints
- [x] Hard heuristic failures **cannot** be overridden by LLM approve
- [x] Orchestrator stores `revision_feedback` and dispatches `waves_for_revision(target_agent)`
- [x] Specialists (esp. Itinerary) honor `constraints` (reduce travel, keep X, preserve Day N)
- [x] Heuristic fallback when no API key / `REVIEWER_LLM=false`
- [x] Drive conditional edge; record `agent_trace` with target + constraints

### Exit criteria
- [x] Text E2E: invoke LangGraph → orchestrator wave loop → agentic merge → autonomous review → approve
- [x] Safety refusal path works without calling specialists
- [x] Edit “Make Day 2 more relaxed” changes Day 2 only
- [x] `agent_trace` shows waves + merger/reviewer modes; revise path shows `target_agent` when triggered
- [x] Offline smoke works with `ORCHESTRATOR_LLM=MERGER_LLM=REVIEWER_LLM=false`
- [x] Graph diagram + smoke ready for demo/README

**Delivered:** Orchestrator wave planning + `Send` parallelism; autonomous Reviewer feedback (`reason` / `target_agent` / `constraints`) routed by Orchestrator; agentic Merger; smoke `python -m agent.smoke_graph`.

---

## Phase 5 — Voice input (STT) & intent UX (1–1.5 days)

**Goal:** Speech → Orchestrator for plan / confirm / edit / explain / tip & weather Q&A.

### Tasks
- [x] Microphone + live transcript in UI
- [x] Server STT: MediaRecorder → `POST /stt` (Gemini/Whisper) + Web Speech fallback
- [x] Auto-send after speech (toggle) + typed fallback
- [x] Confirm-before-plan gate + “Confirm & plan” control
- [x] Session memory (`session_id`) for unconfirmed trip slots + finished itinerary follow-ups
- [x] Scoped voice edits (`nodes/edit_apply.py`) — only target day/block changes; compound edits; ops: relax/pack/**balance_block**/balance_categories/add/remove/swap/trim/indoor/reduce_travel
- [x] Grounded explain (place-matched RAG, doable load, rain + sources)
- [x] Short TTS (`speakableReply`); citations remain on-screen in References
- [x] Sample utterances for required capabilities

### Exit criteria
- [x] Spoken plan asks for confirm, then generates after “yes”
- [x] Voice edits change only the affected day/block
- [x] “Why / doable / rain / safe / etiquette?” answers are itinerary- or citation-grounded

**Delivered:** `/stt` + MediaRecorder; confirm gate; edit applicator; grounded synthesis/orchestrator explain; pipeline log; session trip + itinerary memory; TTS shortened while Sources stay visible.

---

## Phase 6 — Companion UI (1.5–2 days)

**Goal:** Minimal UI per problem statement.

### Required UI
- [x] Day-wise itinerary (Day 1 / 2 / 3…) — `ItineraryView`
- [x] Morning / Afternoon / Evening blocks
- [x] Duration + estimated travel time between stops (distance + walk/car mode when available)
- [x] Mic + live transcript — `VoicePlanner`
- [x] Sources / References section — `SourcesPanel` (API `sources` + stop citations)
- [x] LangGraph / MCP pipeline trace panel for demo — `PipelineTrace`

### Exit criteria
- [x] UI renders approved `merged_itinerary` + `sources` from graph state
- [x] Mobile-usable enough for demo recording

**Delivered:** Next.js companion at `apps/web` calling `POST /invoke` on the agent service; day blocks + travel legs; References panel; pipeline stage log; pending-trip confirm UX.

---

## Phase 7 — AI evaluations (2 days, iterate)

**Goal:** Three runnable evals; iterate on graph nodes/prompts/tools.

### 7a — Feasibility Eval
- [x] Daily duration ≤ available time
- [x] Reasonable travel times (leg ≤ 120 min)
- [x] Pace consistency (stops ≤ STOPS_PER_DAY for pace)

### 7b — Edit Correctness Eval
- [x] Before/after + edit command fixtures (`evals/fixtures/edits/`)
- [x] Only intended day/block changed (`apply_edit_patches`)

### 7c — Grounding & Hallucination Eval
- [x] POIs map to OSM records (`osm_type` + `osm_id > 0`)
- [x] Tips cite RAG sources (`evals/fixtures/tips/hours_cited.json`)
- [x] Missing data → explicit uncertainty / won’t invent (`hours_missing.json`)

### Tasks
- [x] CLI entrypoint: `python -m evals --suite {all|fixtures|feasibility|edit|grounding}`
- [x] Stub runners + fixtures (`evals/runners/`: feasibility, edit_correctness, grounding, validate_fixtures)
- [x] Expand fixtures for edit scope + tip cite-or-refuse; align golden plans with pace caps
- [x] Document how to run evals in README
- [x] Re-run until all suites PASS

### Exit criteria
- [x] All three evals runnable from README
- [ ] At least one eval shown in the demo video (Phase 9)

**Delivered:** `python -m evals --suite all` → fixtures + feasibility + edit_correctness + grounding all PASS.
---

## Phase 8 — n8n workflow: PDF + email (1 day)

**Goal:** Workflow automation (10% rubric).

### Tasks
- [x] n8n webhook with approved itinerary JSON + user email (`POST /api/email-itinerary` → `N8N_WEBHOOK_URL`)
- [ ] Generate PDF (day-wise blocks, sources summary) — **in n8n** (see `docs/n8n.md`)
- [ ] Email PDF to user — **in n8n** (Gmail/SMTP node)
- [x] UI handles success/failure gracefully (`EmailItineraryForm` on itinerary view)

### Exit criteria
- [x] App can POST itinerary + email to webhook (proxy + UI)
- [ ] Plan in app → PDF received by email (complete once n8n PDF/email nodes are Active)
- [x] Workflow documented in repo (`docs/n8n.md`, `n8n/itinerary-pdf-email.json`)

**Delivered (app side):** Next.js `apps/web/src/app/api/email-itinerary/route.ts`; email form under itinerary; env `N8N_WEBHOOK_URL` in `apps/web/.env.local` / `.env.example`. Finish PDF + Gmail in n8n Cloud, then switch to production `/webhook/<id>` URL.

---

## Phase 9 — Deploy, README, demo (1.5–2 days)

**Goal:** Public URL + submission package.

### Deploy
- [ ] Deploy UI + LangGraph agent service (public URL)
- [ ] Env vars configured; Overpass/RAG work in production
- [ ] Smoke test voice + plan + edit + sources on public URL

### Git / README deliverables
- [ ] Architecture + LangGraph graph diagram (nodes/edges, **parallel waves** / `Send`)
- [ ] Note: LangGraph for orchestration (wave planning + fan-out), LangChain for tools/RAG
- [ ] List of MCP tools used
- [ ] Datasets referenced
- [ ] How to run evals
- [ ] Sample test transcripts

### Demo video (≤ 5 min)
- [ ] Voice-based planning
- [ ] Voice-based edit
- [ ] Explanation (“why this plan?”)
- [ ] Sources view
- [ ] At least one eval running
- [ ] (Recommended) Brief view of LangGraph node/tool traces

### Exit criteria
- Deployed link works without local setup
- README complete
- Demo covers all required beats

---

## Suggested calendar (≈ 2 weeks to Jul 25)

| Window | Focus |
|--------|--------|
| Days 1–2 | Phase 0–1 (setup, LangGraph stub, schema/state) |
| Days 3–5 | Phase 2 (MCP + LangChain tools) |
| Days 6–7 | Phase 3 (LangChain RAG) |
| Days 8–11 | Phase 4 (full LangGraph multi-agent graph) |
| Days 12–13 | Phase 5–6 (Voice + UI) — **done** |
| Days 14–15 | Phase 7 (Evals + harden) — **done** |
| Day 16 | Phase 8 (n8n) — **next** |
| Days 17–18 | Phase 9 (deploy + demo + buffer) |

Never cut Phase 1, 2, 3, or 7. Phase 4 is the longest build block because of LangGraph wiring + all agent nodes.

---

## Definition of done (submission checklist)

- [ ] Deployed public URL
- [x] Voice plan + voice edit + grounded explanation *(local; re-verify on deploy)*
- [x] Companion UI with day blocks, travel times, mic/transcript, sources
- [x] ≥ 2 MCP tools used via specialist agents (demo-visible)
- [x] RAG citations for tips (with URLs when available); OSM-backed POIs; missing data stated
- [x] **LangGraph** multi-agent graph: Orchestrator (safety + wave planning + parallel `Send`) → specialists → **Synthesis/Merger** → **agentic Reviewer**
- [x] **LangChain** used for tools and/or RAG
- [x] 3 runnable evals documented (`python -m evals --suite all`)
- [ ] n8n PDF + email works
- [ ] Git repo + README with architecture, LangGraph diagram, MCPs, datasets, evals, sample transcripts
- [ ] 5-minute demo video recorded

---

## Explicit non-goals (protect scope)

- Multi-city itineraries in a **single** trip (one city per plan)
- Planning cities other than **Jaipur** in the current demo lock (catalog remains for geo / OOS)
- Countries outside India
- Perfect real-time transit routing
- Highly polished marketing UI
- More than 4 days per trip
- Unbounded clarifying questions
- Separate Safety agent (safety lives in Orchestrator pre-dispatch gate)
- Fixed all-specialists pipeline with no Orchestrator choice (Orchestrator must select agents / waves)
- Specialists / Reviewer chatting with the user (clarifications stay on Orchestrator; Synthesis may set plan/edit/explain `user_reply` only)
- Reviewer inventing POIs or calling MCPs
- Merger/Synthesis inventing POIs / facts not present in specialist outputs (synthesis may only rearrange/skip existing OSM stops)
- LLM Reviewer overriding hard heuristic failures (edit scope, missing itinerary, hard feasibility)
- Orchestrator inventing revise targets when Reviewer already provided `target_agent` (must route feedback directly)
- Unbounded Reviewer→Orchestrator loops (hard cap at 1–2)
- Unbounded Orchestrator↔specialist loops (hard cap via `orchestrator_steps`)
- Shipping a full offline dump of every OSM POI in India (live Overpass is primary; `data/pois/` is **Overpass fallback** only)
- Hallucinated tip or weather claims when MCP/RAG returns empty

---

## Next immediate action

Phases **0–7** are implemented; **Phase 8 app wiring** is done (email form → `/api/email-itinerary` → n8n webhook). Finish PDF + email nodes in n8n Cloud, then **Phase 9** — deploy + demo.

## Post–Phase 6 hardening (Jul 15, 2026)

| Area | Change | Key files |
|------|--------|-----------|
| Meal / pace packing | Breakfast-first, dinner-last; block targets by pace; reassert after optimize/edits | `itinerary_builder.py`, `itinerary_optimize.py` |
| Interest coverage | Post-pack guard: ≥1 stop per stated interest when live POI exists; protected during pace trim | `ensure_interest_coverage`, `_trim_day_stops_for_pace` |
| Balance edit | “Make day N more balanced” → `balance_block`; densify + optional POI fetch | `edit_apply.py`, `orchestrator.py`, `specialists.py` |
| Evening soft stops | Market/shopping/park evening slots only when user chose that interest | `_evening_soft_categories`, `_is_evening_soft` |
| RAG citations | Opening-hours + tips include `(Source: Title - URL)`; website preferred | `llm_utils.py`, `orchestrator.py` |
| RAG corpus | OSM facts, Google Places, tourism, curated stubs (Jaipur) | `data/rag/corpus/`, `data/rag/README.md` |

## Deferred follow-ups (remind before polish / evals)

- [ ] **Itinerary balance after Reviewer revise:** Gemini Reviewer can request re-clustering that leaves Day 1 too light (e.g. food-only). Tune Itinerary Agent + Reviewer prompts (and/or post-revise balance checks) so revise rounds keep days reasonably filled under a relaxed pace.
- [ ] **RAG long-tail coverage:** Itinerary OSM POIs not in Wikivoyage/Places corpus still get thin tips — consider batch enrichment (Places API / curated cards keyed by `osm_id`).
- [ ] Expand `ALLOWED_CITIES` beyond Jaipur when demo scope unlocks (schema + RAG corpus already multi-city capable).
- [x] **Post-pack interest coverage** — implemented (`ensure_interest_coverage`).
- [x] **`balance_block` voice edit** — implemented (parse + apply + POI fetch).
- [x] **Source URLs in knowledge Q&A** — implemented (`ensure_source_link`).
