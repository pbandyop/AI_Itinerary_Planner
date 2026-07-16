# India travel data

Country scope: **India**. Each trip plans **one Indian city** (2–4 days).

## For graders (read this first)

| Question | Answer |
|----------|--------|
| Where do POIs come from? | **Live OpenStreetMap via Overpass** (POI Search MCP) |
| What is `data/pois/`? | **Overpass fallback only** — used when Overpass fails, times out, or returns too few named POIs |
| Is this RAG? | **No for POIs.** Tips/guidance use **RAG** under `data/rag/` (Phase 3) |
| Are fallback POIs hallucinated? | **No.** Each seed stop includes `osm_type` + `osm_id` mapping back to OSM |

Primary path is always live MCP. Fallback seeds exist so demos stay reliable if Overpass is flaky.

## Files

| Path | Label | Purpose |
|------|--------|---------|
| `india_cities.json` | City catalog (config) | Supported Indian cities: coords + Overpass bbox + aliases |
| `pois/<city_slug>.json` | **Overpass fallback** | Curated OSM-backed POIs when live Overpass is unavailable/sparse |
| `jaipur_pois_seed.json` | **Overpass fallback** (legacy) | Same role as `pois/jaipur.json` — prefer the `pois/` path |
| `rag/corpus/*.json` | **RAG corpus** | Wikivoyage extracts (CC BY-SA) for Knowledge Agent citations |
| `rag/chunks.jsonl` | **RAG chunks** (generated) | Chunked corpus for BM25 / Chroma ingest |
| `rag/chroma/` | **Chroma store** (generated) | Vector DB when embeddings are configured |

## How POIs are resolved

1. Resolve city name against `india_cities.json` (must be in India catalog).
2. Query **OpenStreetMap Overpass** inside that city’s bbox ← **primary**.
3. If Overpass fails or returns too few named POIs, merge **Overpass fallback** seeds from `pois/<slug>.json` when present.
4. If still empty → `missing_data=true` (no invented places).

Live Overpass works for **any** catalog city, even without a fallback file. Fallback files improve offline/demo reliability for popular destinations (Jaipur, Delhi, Mumbai, Bengaluru, Agra, …).

Force fallback-only for local demos:

```bash
python -m agent.smoke_mcp --city Delhi --no-overpass
```

## RAG corpus (Phase 3)

Tips and “why / doable / rain” answers come from LangChain retrieval over Wikivoyage text — **not** from inventing facts.

```bash
# Re-fetch Wikivoyage extracts (rate-limited)
python -m agent.rag.fetch_corpus

# Chunk + optional Chroma (BGE / OpenAI embeddings)
python -m agent.rag.ingest --force-chunks

# Smoke test citations
python -m agent.smoke_rag --city Jaipur
python -m agent.smoke_rag --missing-city
```

Empty retrieval returns `missing_data=true` (no hallucinated tips).

## Adding a city

1. Add an entry to `india_cities.json` (`name`, `state`, `lat`, `lon`, `bbox`, optional `aliases`).
2. Optionally add an **Overpass fallback** file `pois/<slug>.json` with stops that include real `osm_type` / `osm_id`.
3. Optionally add a Wikivoyage extract under `rag/corpus/<city>.json` (or run `fetch_corpus`).
