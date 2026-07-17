# RAG corpus (Knowledge Agent)

**Purpose:** Ground travel tips and explanations with **cited** multi-source text via a LangChain retriever.

**Scope:** Jaipur only.

## Sources

| Source | Path | License / notes |
|--------|------|-----------------|
| Wikivoyage | `corpus/jaipur.json` | CC BY-SA 4.0 |
| Wikipedia place pages | `corpus/wikipedia/*.json` | CC BY-SA 4.0 |
| OpenStreetMap fact cards | `corpus/osm/jaipur_osm_facts.json` | ODbL 1.0 (`description`, `opening_hours`, …); amenities include cafe/restaurant/bar/pub/fast_food (limit 500) |
| Rajasthan Tourism | `corpus/tourism/jaipur_tourism.json` | Official site extracts (cited) |
| Google Places | `corpus/google/jaipur_places.json` | API when `GOOGLE_PLACES_API_KEY` is set (no HTML scrape). Default cap **900**; day-by-day: `python -m agent.rag.fetch_google_places --merge --pack gardens` (packs: named, museums, temples, gardens, heritage, restaurants, cafes, nightlife, markets, hotels, neighborhoods) |
| Curated demo places | `corpus/curated/jaipur_places.json` | Hand-curated stubs + aliases (not scraped TripAdvisor) |

## Build & eval

```bash
cd services/agent
set PYTHONPATH=src
python -m agent.rag.build_corpus
python -m agent.rag.eval_chunking
python -m agent.rag.ingest --force-chunks
```

Chunking strategies are scored on a gold query set; the winner is stored in `data/rag/chunking_strategy.json`.

## Retrieval

1. Prefer **Chroma** + **`BAAI/bge-small-en-v1.5`** when `RAG_EMBEDDINGS=huggingface`.
2. Else BM25 over city-filtered chunks.
3. Place aliases (e.g. Niota/Neota/Nevta Dam) expand query matching.
4. Hours queries boost OSM / Google Places cards with `opening_hours`.
5. Empty match → `missing_data=true` (never invent tips).
