# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-page demo for a Semantic Web talk, framed as a multi-criteria place finder
("Onde Morar / Onde Abrir"): the user describes an ideal place in one natural-language
sentence (English or Portuguese) stacking arbitrary spatial constraints — positive
("within 400 m of a metro") and negative ("no major road within 200 m"). Claude Haiku
(via the Claude Code CLI in headless mode) translates it to a GeoSPARQL query, QLever
runs it against the full OpenStreetMap planet (with automatic fallback to the Wikidata
Query Service when QLever is unreachable), and the page renders the SPARQL, a results
table, and any WKT geometries on a Leaflet map with per-column colors.

## Commands

```bash
claude setup-token                       # once; then export the printed token:
export CLAUDE_CODE_OAUTH_TOKEN=...
pip install -r requirements.txt
uvicorn app:app --reload                 # serves http://localhost:8000
```

Prerequisite: the `claude` CLI (Claude Code) must be on PATH — it is the LLM engine.

There is no build step, no linter config, and no test suite.
`scripts/verify_qlever_patterns.sh` curl-verifies the CONSTRAINT STACK query patterns
against the live QLever endpoint (see "Verification status" below).

## Architecture

There are two independent frontends sharing one engine layer:

- **`app.py` + `templates/index.html`** — the original free-text demo (below).
- **`app2.py` + `templates/finder.html`** — "Bairro Certo", a language-first
  finder (`./start.sh --app app2`), QLever-only. It **imports app.py** for
  `generate_sparql`, `ensure_prefixes`, `strip_fences` and `ENGINES` — never
  duplicate that logic. Three-stage pipeline:
  1. **`POST /parse`** — Claude Haiku as a *parser*, not a SPARQL author: the
     user's sentence → criteria JSON (`PARSER_PROMPT`; kind/category/ideal
     distance/weight 1-3, city, mode, plus `unmapped` fragments the UI turns
     into a free-text chip). Far more reliable than full generation.
  2. **`POST /find` deterministic path** — `build_qlever` emits ONE wide-radius
     nearest-neighbor `spatialSearch:` block per criterion (near AND avoid) with
     `bindDistance`, and **no distance FILTERs**: `score_places` ranks candidates
     by weighted match score in Python instead of filtering (near: 100% up to the
     ideal distance, halving each extra ideal-distance beyond; avoid: linear ramp
     to full clearance). The finder therefore never returns "0 results" because a
     threshold was slightly too strict — it returns the best matches with a %.
  3. A `custom` criterion (from `unmapped` or typed) switches the whole request
     to the Haiku full-SPARQL pipeline (`compose_sentence` → `generate_sparql`);
     those places carry `dists` but no `score`.
  Category/city registries live in `CATEGORIES` / `CITIES` in app2.py.

The original pipeline lives in two files:

- **`app.py`** — FastAPI server with two routes:
  - `GET /` renders `templates/index.html`.
  - `POST /ask` takes `{question}` and runs the full pipeline:
    `pick_engine()` (health-probe QLever, cached 60 s; fall back to Wikidata) →
    `claude -p` headless subprocess (Haiku, `--tools ""`, `--output-format json`,
    `--no-session-persistence`, system prompt chosen per engine) → parse the JSON
    envelope's `result` field → `strip_fences` → `ensure_prefixes` (drops model-emitted
    PREFIX lines, prepends the engine's canonical prefix block) → POST to the engine's
    SPARQL endpoint → return `{sparql, head, results, error, engine, engine_label}`.
  - The claude subprocess runs with `cwd` set to a temp dir so it does NOT ingest this
    CLAUDE.md as context. Auth is `CLAUDE_CODE_OAUTH_TOKEN` (do not switch the code to
    `--bare` mode: bare mode does not read that env var, only `ANTHROPIC_API_KEY`).
- **`templates/index.html`** — vanilla JS + Leaflet, no framework, no build. Loads
  Leaflet from unpkg CDN. Includes an optional constraint-builder (`<details>` block)
  that only composes a Portuguese sentence into the `#question` input — the backend
  contract is unchanged by it.

### Dual engines (primary + backup)

`ENGINES` in `app.py` maps an engine key to `{endpoint, system_prompt, prefix_block,
label}`:

- **`qlever-osm`** (primary) — `https://qlever.dev/api/osm-planet`, no auth.
- **`wikidata`** (backup) — `https://query.wikidata.org/sparql` (Blazegraph). Requires
  a `User-Agent` header (sent to both engines). Used automatically when the QLever
  health probe fails — e.g. some routers/ISPs silently drop traffic to the Uni
  Freiburg IP range (observed 2026-07: TCP to ports 80/443 of qlever.dev times out on
  some home networks while working on cellular).

Each engine has ITS OWN system prompt and prefix block — the schemas are unrelated
(this is "Wall 3" in IDEAS.md). Never send an OSM-schema query to WDQS or vice versa.

### Error handling contract

`/ask` never raises to the client — every failure path (empty question, claude CLI
missing/nonzero-exit/timeout/bad-envelope, endpoint timeout/non-200/bad-JSON) returns
the same JSON shape with a human-readable `error` string and `head`/`results` set to
`null` (plus `engine`/`engine_label` when known). The frontend's `render()` keys off
this shape. Preserve it when editing either side.

### Frontend rendering rules (important)

- The map detects geometries **by value, not by column name**: any binding cell whose
  value matches `WKT_RE` (starts with `POINT`/`LINESTRING`/`POLYGON`/`MULTI*`,
  case-insensitive — WDQS returns `Point(...)`) is parsed and drawn. WKT stores
  coordinates as `lon lat`; Leaflet wants `[lat, lon]`, so `parseWKT`/`parseCoordList`
  swap them. This is why both system prompts insist every query project a geometry
  variable — without one the table renders but the map stays empty.
- The **first** WKT-bearing column is treated as the candidate (blue, pin markers);
  subsequent WKT columns are constraint features (distinct colors, circle markers),
  with a legend keyed by column name. So in constraint-stack queries the candidate's
  `?wkt` must be projected first.

## The system prompts are the core asset

`SYSTEM_PROMPT` (QLever/OSM) and `SYSTEM_PROMPT_WDQS` (Wikidata) in `app.py` are the
heart of this project. They encode curl-verified facts that contradict typical
SPARQL/training assumptions. Treat these as ground truth and do not "correct" them
toward generic GeoSPARQL/Wikidata idioms.

QLever/OSM facts:

- `osmkey:wikidata` values are **string literals** (`"Q64"`), never `wd:` IRIs.
- `osmkey:admin_level` is `xsd:int` — match as a **bare integer** (`4`), not `"4"`.
- Geometry measurements are **precomputed**: use `osm2rdf:area` / `osm2rdf:length`.
  `geof:area(?wkt)` does **not** work on this endpoint.
- "X inside region Y" should use the precomputed `?area ogc:sfContains ?feature`
  predicate (milliseconds) rather than a spatial-join FILTER or spatialSearch SERVICE.
- Keys with colons (`addr:city`) must escape the inner colon: `osmkey:addr\:city`.
- Radius queries use the `spatialSearch:` SERVICE (k-NN, distance in meters).
- **`spatialSearch` only matches POINT geometries on both sides** (verified live
  2026-07-04): ways/polygons are silently dropped — polygon suburbs vanish from
  the left side, and the "nearest highway=primary" to Lisbon was a stray node
  918 km away. Join on `geof:centroid(...)` of each geometry (BIND on both
  sides) while projecting the real `?wkt` for the map. Also narrow each SERVICE
  body's features to the city via `ogc:sfContains` — an unrestricted right side
  computes centroids planet-wide (~84 s for highway=primary vs ~1.5 s narrowed).
- CONSTRAINT STACK (multi-criteria): candidate geometry as `spatialSearch:left`, one
  SERVICE block per positive criterion with distinct variables. **Negation is the
  tricky part** — `spatialSearch:` is an INNER join (a row exists only where a
  neighbor is found within `maxDistance`), so "no X within N m" CANNOT be expressed
  by testing absence:
  - `FILTER NOT EXISTS { SERVICE spatialSearch: ... }` **crashes QLever** with an
    `isConstructed()` assertion ("SpatialJoin needs two children") — the outer `?wkt`
    isn't propagated into the subquery, so the join loses a child.
  - `MINUS` fails (out-of-memory, tries to allocate 16 GB).
  - `OPTIONAL { SERVICE spatialSearch: ... } FILTER(!BOUND(?d))` **silently returns 0
    rows** — `OPTIONAL` does NOT restore candidates the inner spatial join dropped.

  The working form: give the negative block a WIDE `maxDistance` (e.g. 100000) so
  every candidate finds its nearest such feature, `bindDistance ?d`, then
  `FILTER(?d > N)` with N the forbidden radius **in kilometres** (bindDistance is km;
  200 m → `0.2`). The radius must exceed the metro area's extent or a candidate with
  no feature in range is wrongly dropped. Verified live 2026-07-03.

Wikidata/WDQS facts (curl-verified 2026-07-03):

- Entities are IRIs (`wd:Q597`), the opposite of the OSM engine's string literals.
- Coordinates via `wdt:P625` → `"Point(lon lat)"^^geo:wktLiteral`.
- Radius via `SERVICE wikibase:around` — radius is a **quoted string in km**.
- `geof:distance(a, b)` returns **kilometers**.
- **Blazegraph bug**: `FILTER NOT EXISTS` containing a `geof:distance` filter,
  combined with `LIMIT`, **silently returns 0 rows** (the same WHERE clause under
  `COUNT(*)` returns the right answer). Spatial negation must use `MINUS`, re-binding
  the candidate's coordinate inside the MINUS block.

### Verification status

The QLever CONSTRAINT STACK section was curl-verified live on 2026-07-03 via
`scripts/verify_qlever_patterns.sh`. Results: variable-left spatial join (test 1) ✅,
stacked positive SERVICE blocks (test 2) ✅, `FILTER NOT EXISTS` negation (test 3a)
❌ **500 SpatialJoin crash**, `MINUS` negation (test 3b) ❌ out-of-memory, `OPTIONAL` +
`!BOUND` negation (test 3c) ❌ **silently returns 0 rows** (OPTIONAL does not restore
rows the inner spatial join drops — a wrong result, not a crash). The only correct
negation is **nearest-feature-within-a-wide-radius + `FILTER(dist > N_km)`**, verified
to include/exclude the right candidates (Lisbon: 17 neighborhoods near metro → 12 after
"avoid hospital within 1 km", exactly the 5 with a hospital <1 km removed). The
negation template in `SYSTEM_PROMPT` (app.py) and `build_qlever` (app2.py) use this
form.

When changing query behavior, edit the system prompts rather than adding
post-processing in Python. The only Python-side query mutations are `ensure_prefixes`
(canonical per-engine PREFIX block) and `strip_fences` (removing markdown fences). New
endpoint facts belong in the prompts' "ENDPOINT FACTS" / "QUERY RULES" sections,
verified against the live endpoint first.
