# India travel data

Country scope: **India**. Each trip plans **one Indian city** (2–4 days).

## Files

| Path | Purpose |
|------|---------|
| `india_cities.json` | Catalog of supported Indian cities (coords + Overpass bbox + aliases) |
| `pois/<city_slug>.json` | Curated OSM-backed POI seeds used when Overpass is unavailable |
| `jaipur_pois_seed.json` | Legacy alias — prefer `pois/jaipur.json` |

## How POIs are resolved

1. Resolve city name against `india_cities.json` (must be in India catalog).
2. Query **OpenStreetMap Overpass** inside that city’s bbox.
3. If Overpass fails or returns too few named POIs, merge curated seeds from `pois/<slug>.json` when present.
4. If still empty → `missing_data=true` (no invented places).

Live Overpass works for **any** catalog city, even without a seed file. Seeds improve offline/demo reliability for popular destinations.

## Adding a city

1. Add an entry to `india_cities.json` (`name`, `state`, `lat`, `lon`, `bbox`, optional `aliases`).
2. Optionally add `pois/<slug>.json` with stops that include real `osm_type` / `osm_id`.
