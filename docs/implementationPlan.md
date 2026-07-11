# Implementation Plan — Voice-First AI Travel Planner

**Project:** Graduation Capstone (Applied Generative AI Bootcamp)  
**Deadline:** Jul 25, 2026  
**Scope:** One city (Jaipur), 2–4 day itineraries  
**Architecture:** Multi-agent pipeline on **LangGraph** — Orchestrator (with safety gate) → specialist agents (MCP/RAG via LangChain tools) → Merger → Reviewer — plus voice UX and n8n PDF/email

This plan is derived from `docs/problemStatement.md` and the design decisions agreed during brainstorming. Build in order; do not skip ahead to UI/voice until the itinerary contract and MCP loop are solid.

---

## Architecture snapshot

```
Voice (STT)
    ↓
┌─────────────────────────────────────────────────────────┐
│  LangGraph multi-agent graph                            │
│                                                         │
│  Orchestrator Agent                                     │
│    1. Safety / policy gate (block unsafe → refuse)      │
│    2. Parse intent (plan | edit | explain | confirm)    │
│    3. Clarify / confirm constraints (max 6 questions)   │
│    4. Dispatch specialist nodes (parallel when possible)│
│         ↓                                               │
│  Specialist Agents (LangGraph nodes + LangChain tools)  │
│    ├── POI Agent       → POI Search MCP                 │
│    ├── Itinerary Agent → Itinerary Builder MCP          │
│    ├── Travel-Time Agent → Travel Time Estimator MCP    │
│    ├── Weather Agent   → Weather Adjustment MCP         │
│    ├── Knowledge Agent → RAG (Wikivoyage / Wikipedia)   │
│    └── (Knowledge is Phase 3; other MCPs are Phase 2)   │
│         ↓                                               │
│  Merger Agent                                           │
│    • Fuse specialist outputs → one itinerary JSON       │
│    • Citations / sources; flag missing data             │
│    • Edits = targeted patch (not full rewrite)          │
│         ↓                                               │
│  Reviewer Agent (final gate)                            │
│    • Feasibility, grounding, edit-scope                 │
│    • Approve → END (UI + TTS)                           │
│    • Revise → back to Orchestrator (max 1–2 loops)      │
└─────────────────────────────────────────────────────────┘
    ↓
Companion UI  →  n8n (PDF + email)
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
| **Orchestrator** | `orchestrator` | Yes | No (dispatches only) | Safety gate, intent, slots, confirmations, dispatch plan |
| **POI Agent** | `poi_agent` | No | POI Search MCP (LangChain tool) | Ranked POIs with OSM ids |
| **Itinerary Agent** | `itinerary_agent` | No | Itinerary Builder MCP | Day/block packing from candidate POIs |
| **Travel-Time Agent** | `travel_time_agent` | No | Travel Time Estimator MCP | Heuristic leg times between stops |
| **Weather Agent** | `weather_agent` | No | Weather Adjustment MCP (Open-Meteo) | Rain risk + indoor/outdoor adjustments |
| **Knowledge Agent** | `knowledge_agent` | No | LangChain RAG retriever | Tips + citations for explanations |
| **Merger** | `merger` | No | No | Fuse specialist results → schema-valid JSON |
| **Reviewer** | `reviewer` | No | No | Approve or return structured issues |

### LangGraph graph shape

```
START → orchestrator
          ├─(unsafe / need_clarify)→ END (reply to user)
          └─(ready)→ [poi ∥ itinerary ∥ knowledge ∥ travel_time ∥ weather]
                          → merger → reviewer
                                       ├─(approve)→ END
                                       └─(revise, revision_count < 2)→ orchestrator
```

Shared **LangGraph state** holds: user message, intent, trip slots, specialist outputs (`poi_results`, `itinerary_draft`, `knowledge_results`, `travel_time_results`, `weather_results`), merged itinerary, previous itinerary (for edits), reviewer verdict, `revision_count`, sources, and the user-facing reply.

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
- [x] Freeze scope: **Jaipur only**, **2–4 days**, heuristic travel times OK
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
  - Trip metadata: city, dates/window, interests, pace, constraints, confirmed flags
  - Day → Morning / Afternoon / Evening blocks
  - Stop: name, OSM id, lat/lon, category, duration_min, travel_to_next_min, reason, citations[], uncertainty?
  - Sources list (dataset + URL/title)
  - Specialist result envelopes (POI list, itinerary draft, knowledge snippets)
  - Edit patch format: `{ target: { day, block }, operation, payload }`
  - Reviewer verdict: `{ status: "approve" | "revise", issues[], affected_sections[] }`
- [x] Define **LangGraph state schema** (TypedDict / Pydantic / Zod) including:
  - `messages`, `intent`, `safety_status`
  - `trip_constraints`, `dispatch_plan`
  - `poi_results`, `itinerary_draft`, `knowledge_results`
  - `merged_itinerary`, `previous_itinerary`
  - `reviewer_verdict`, `revision_count`
  - `user_reply`, `sources`
- [x] Write 1–2 **golden sample itineraries** (hand-authored JSON) for Jaipur
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
- [x] Implement Overpass (OpenStreetMap) queries for Jaipur POIs
- [x] Inputs: city, interests, constraints
- [x] Outputs: ranked POIs with metadata + **stable OSM ids**
- [x] Handle missing/empty results honestly

### 2b — Itinerary Builder MCP
- [x] Inputs: candidate POIs, daily time windows, pace
- [x] Outputs: day-wise structure matching Phase 1 schema
- [x] Heuristic travel times; respect pace

### 2c — Travel Time Estimator MCP
- [x] Inputs: ordered stops or explicit from/to legs + mode (`walk` | `city`)
- [x] Outputs: per-leg distance_km + duration_min (haversine heuristic)
- [x] Honest notes that estimates are not live transit
- [x] LangChain tool: `travel_time_estimator_mcp` · HTTP: `POST /mcp/travel_time`

### 2d — Weather Adjustment MCP (Open-Meteo)
- [x] Inputs: city (Jaipur), start_date, num_days
- [x] Outputs: daily forecast, rain_risk, indoor/outdoor `adjustments[]`
- [x] Supports “What if it rains?” grounded in Open-Meteo (state missing data if API fails)
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

**Goal:** Cited city guidance via a LangChain retriever; owned later by the Knowledge Agent node.

### Tasks
- [ ] Collect Wikivoyage / Wikipedia content for Jaipur
- [ ] Chunk + embed; store in a LangChain-compatible vector store
- [ ] Build retriever used by Knowledge Agent for planning context and “why / doable / rain” answers
- [ ] Citation objects: title, URL/source id, snippet
- [ ] Empty retrieval → explicit “data missing” (no hallucinated tips)

### Exit criteria
- Tips include citations
- `sources[]` can be filled from retrieval
- Spot-check: every tip traces to a chunk

---

## Phase 4 — LangGraph multi-agent pipeline (2.5–3 days)

**Goal:** Implement the full graph: Orchestrator → specialists → Merger → Reviewer (text-first).

### 4a — Graph wiring (LangGraph)
- [ ] Create `StateGraph` with shared state from Phase 1
- [ ] Add nodes: `orchestrator`, `poi_agent`, `itinerary_agent`, `knowledge_agent`, `merger`, `reviewer`
- [ ] Fan-out specialists in parallel when dispatch allows (LangGraph Send / parallel edges)
- [ ] Conditional edges:
  - Orchestrator → END if unsafe or needs clarification
  - Orchestrator → specialists when ready
  - Reviewer → END if `approve` or `revision_count >= 2`
  - Reviewer → Orchestrator if `revise` and retries remain
- [ ] Cap loops with `revision_count`
- [ ] Persist/trace node execution for demo (LangSmith optional)

### 4b — Orchestrator node
- [ ] **Safety / policy gate** before any specialist dispatch (abuse, jailbreak, off-scope, harmful intent)
- [ ] Unsafe → short refusal in `user_reply`, skip MCP/RAG
- [ ] Preference collection (max 6 clarifying questions)
- [ ] Confirm constraints before generating
- [ ] Intent: `plan` | `edit` | `explain` | `confirm`
- [ ] Write `dispatch_plan` (which specialists + inputs)
- [ ] On revise: re-dispatch **only affected** specialists
- [ ] Edits → targeted patch instructions for specialists/Merger

### 4c — Specialist nodes
- [ ] **POI Agent** — LangChain tool → POI Search MCP
- [ ] **Itinerary Agent** — LangChain tool → Itinerary Builder MCP
- [ ] **Knowledge Agent** — LangChain retriever / RAG chain
- [ ] **Weather Agent** — wraps Weather Adjustment MCP (Open-Meteo)
- [ ] **Travel-Time Agent** — wraps Travel Time Estimator MCP
- [ ] Specialists write only their state slice; **no user chat**

### 4d — Merger node
- [ ] Merge specialist outputs into one schema-valid itinerary
- [ ] Attach Knowledge citations onto stops / sources
- [ ] Edits: patch previous itinerary; untouched sections stay identical
- [ ] Flag uncertainty when a specialist returned empty data
- [ ] Do not invent facts not present in specialist outputs

### 4e — Reviewer node
- [ ] No new POIs, no MCP calls, no user chat
- [ ] Check feasibility, grounding, edit scope
- [ ] Emit structured verdict into state
- [ ] Drive conditional edge (approve vs revise)

### Exit criteria
- Text E2E: invoke LangGraph → plan → merge → review → approve
- Safety refusal path works without calling specialists
- Edit “Make Day 2 more relaxed” changes Day 2 only
- Graph diagram + node traces ready for demo/README

---

## Phase 5 — Voice input (STT) & intent UX (1–1.5 days)

**Goal:** Speech-to-text into the LangGraph Orchestrator entrypoint.

### Tasks
- [ ] Microphone + live transcript in UI
- [ ] STT → API → `graph.invoke` / `ainvoke` with user message in state
- [ ] Optional short TTS for confirmations / explanations
- [ ] Ambiguous transcripts → Orchestrator clarification path
- [ ] Sample test transcripts under `docs/` or `evals/fixtures/`

### Exit criteria
- Spoken plan, edit, and “why this plan?” work end-to-end
- Live transcript visible in UI

---

## Phase 6 — Companion UI (1.5–2 days)

**Goal:** Minimal UI per problem statement.

### Required UI
- [ ] Day-wise itinerary (Day 1 / 2 / 3…)
- [ ] Morning / Afternoon / Evening blocks
- [ ] Duration + estimated travel time between stops
- [ ] Mic + live transcript
- [ ] Sources / References section
- [ ] Optional: LangGraph node / MCP trace panel for demo

### Exit criteria
- UI renders approved `merged_itinerary` + `sources` from graph state
- Mobile-usable enough for demo recording

---

## Phase 7 — AI evaluations (2 days, iterate)

**Goal:** Three runnable evals; iterate on graph nodes/prompts/tools.

### 7a — Feasibility Eval
- [ ] Daily duration ≤ available time
- [ ] Reasonable travel times
- [ ] Pace consistency

### 7b — Edit Correctness Eval
- [ ] Before/after + edit command fixtures
- [ ] Only intended day/block changed

### 7c — Grounding & Hallucination Eval
- [ ] POIs map to OSM records
- [ ] Tips cite RAG sources
- [ ] Missing data → explicit uncertainty

### Tasks
- [ ] CLI entrypoint (e.g. `npm run eval` or `python -m evals`)
- [ ] Optionally invoke LangGraph on fixtures for integration-style evals
- [ ] Document how to run evals in README
- [ ] Fix Orchestrator / Merger / Reviewer / tools; re-run

### Exit criteria
- All three evals runnable from README
- At least one eval shown in the demo video

---

## Phase 8 — n8n workflow: PDF + email (1 day)

**Goal:** Workflow automation (10% rubric).

### Tasks
- [ ] n8n webhook with approved itinerary JSON + user email
- [ ] Generate PDF (day-wise blocks, sources summary)
- [ ] Email PDF to user
- [ ] UI handles success/failure gracefully

### Exit criteria
- Plan in app → PDF received by email
- Workflow documented in repo/docs

---

## Phase 9 — Deploy, README, demo (1.5–2 days)

**Goal:** Public URL + submission package.

### Deploy
- [ ] Deploy UI + LangGraph agent service (public URL)
- [ ] Env vars configured; Overpass/RAG work in production
- [ ] Smoke test voice + plan + edit + sources on public URL

### Git / README deliverables
- [ ] Architecture + LangGraph graph diagram (nodes/edges)
- [ ] Note: LangGraph for orchestration, LangChain for tools/RAG
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
| Days 12–13 | Phase 5–6 (Voice + UI) |
| Days 14–15 | Phase 7 (Evals + harden) |
| Day 16 | Phase 8 (n8n) |
| Days 17–18 | Phase 9 (deploy + demo + buffer) |

Never cut Phase 1, 2, 3, or 7. Phase 4 is the longest build block because of LangGraph wiring + all agent nodes.

---

## Definition of done (submission checklist)

- [ ] Deployed public URL
- [ ] Voice plan + voice edit + grounded explanation
- [ ] Companion UI with day blocks, travel times, mic/transcript, sources
- [ ] ≥ 2 MCP tools used via specialist agents (demo-visible)
- [ ] RAG citations for tips; OSM-backed POIs; missing data stated
- [ ] **LangGraph** multi-agent graph: Orchestrator (incl. safety) → specialists → Merger → Reviewer
- [ ] **LangChain** used for tools and/or RAG
- [ ] 3 runnable evals documented
- [ ] n8n PDF + email works
- [ ] Git repo + README with architecture, LangGraph diagram, MCPs, datasets, evals, sample transcripts
- [ ] 5-minute demo video recorded

---

## Explicit non-goals (protect scope)

- Multi-city support
- Perfect real-time transit routing
- Highly polished marketing UI
- More than 4 days per trip
- Unbounded clarifying questions
- Separate Safety agent (safety lives in Orchestrator pre-dispatch gate)
- Specialists / Merger / Reviewer chatting with the user
- Reviewer inventing POIs or calling MCPs
- Merger inventing facts not present in specialist outputs
- Unbounded Reviewer→Orchestrator loops (hard cap at 1–2)

---

## Next immediate action

Phase 0–2 are done. Next: **Phase 3** — Wikivoyage/Wikipedia RAG with LangChain retriever + citations.
