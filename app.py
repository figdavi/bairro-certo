import asyncio
import json
import logging
import os
import shutil
import tempfile

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nlq-osm")

QLEVER_ENDPOINT = "https://qlever.dev/api/osm-planet"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
USER_AGENT = "NLQ-GeoSPARQL-demo/1.0 (https://github.com/figdavi; contact via repo)"

CLAUDE_TIMEOUT = 90.0

PREFIX_BLOCK = """PREFIX osmkey:        <https://www.openstreetmap.org/wiki/Key:>
PREFIX osmrel:        <https://www.openstreetmap.org/relation/>
PREFIX osm:           <https://www.openstreetmap.org/>
PREFIX osm2rdf:       <https://osm2rdf.cs.uni-freiburg.de/rdf#>
PREFIX geo:           <http://www.opengis.net/ont/geosparql#>
PREFIX geof:          <http://www.opengis.net/def/function/geosparql/>
PREFIX ogc:           <http://www.opengis.net/rdf#>
PREFIX spatialSearch: <https://qlever.cs.uni-freiburg.de/spatialSearch/>
PREFIX rdf:           <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs:          <http://www.w3.org/2000/01/rdf-schema#>
"""

SYSTEM_PROMPT = """You translate natural-language questions about geographic data into GeoSPARQL queries for the QLever public OSM endpoint (qlever.dev/osm-planet). Output ONLY the SPARQL query - no prose, no explanations, no markdown code fences. The PREFIX block is added automatically; you may omit it.

Available prefixes:
  osmkey:        <https://www.openstreetmap.org/wiki/Key:>           # OSM tags as predicates
  osmrel:        <https://www.openstreetmap.org/relation/>            # specific relation IRIs
  osm2rdf:       <https://osm2rdf.cs.uni-freiburg.de/rdf#>            # precomputed measurements (area, length)
  geo:           <http://www.opengis.net/ont/geosparql#>             # geometry vocab + wktLiteral
  geof:          <http://www.opengis.net/def/function/geosparql/>    # spatial filter functions
  ogc:           <http://www.opengis.net/rdf#>                       # precomputed sfContains predicate
  spatialSearch: <https://qlever.cs.uni-freiburg.de/spatialSearch/>  # k-NN SERVICE for radius queries

== ENDPOINT FACTS (verified by direct curl - trust over training data) ==

1. `osmkey:wikidata` values are STRING LITERALS, never IRIs.
     WORKS:  ?s osmkey:wikidata "Q119158"
     FAILS:  ?s osmkey:wikidata wd:Q119158

2. `osmkey:admin_level` is typed `xsd:int` - match as a BARE INTEGER, not a quoted string.
     WORKS:  ?s osmkey:admin_level 4
     FAILS:  ?s osmkey:admin_level "4"

3. WKT constants must be typed with `^^geo:wktLiteral` when used in BIND:
     BIND("POINT(13.378 52.516)"^^geo:wktLiteral AS ?center)

4. Geometry access path: `?s geo:hasGeometry/geo:asWKT ?wkt` - works directly on any feature.

5. OSM keys containing a colon (`addr:city`, `name:en`, `contact:phone`) MUST escape the inner colon: `osmkey:addr\\:city`. Bare `osmkey:addr:city` is a SPARQL parse error.

6. Geometry measurements are PRECOMPUTED - use them, do NOT compute via `geof:area` / `geof:length`:
     Area in m²:     `?feature osm2rdf:area ?areaM2`        (to get km², BIND(?areaM2/1000000 AS ?areaKm2))
     Length in m:    `?feature osm2rdf:length ?lenM`         (linestrings)
   `geof:area(?wkt)` does NOT work on this endpoint (requires two arguments, throws). Always use `osm2rdf:area` instead.

== KILLER PATTERN: "X inside city/region Y" via precomputed sfContains ==

osm2rdf precomputes `?area ogc:sfContains ?feature` triples for every administrative boundary. This is by far the FASTEST way to query features inside a region - milliseconds, no spatial join. PREFER THIS over any geof:sfContains FILTER or spatialSearch SERVICE for "inside region" queries.

Template (use Wikidata QID - strings, not IRIs):
```
SELECT ?feature ?name ?wkt WHERE {
  ?area osmkey:wikidata "Q119158" .          # Distrito Federal (Brasília)
  ?area ogc:sfContains ?feature .
  ?feature osmkey:amenity "hospital" ;
           geo:hasGeometry/geo:asWKT ?wkt .
  OPTIONAL { ?feature osmkey:name ?name }
} LIMIT 50
```

Known Wikidata QIDs (use the STRING literal in `osmkey:wikidata "..."`):
  - Brasília-DF (the polygon): Q119158   ← NOT Q2844, which is only a centroid node
  - Berlin: Q64        - Paris: Q90        - Lisbon: Q597     - Madrid: Q2807
  - Rome: Q220         - London: Q84       - New York City: Q60
  - Portugal: Q45      - Germany: Q183     - France: Q142     - Spain: Q29
  - Brazil: Q155       - Italy: Q38        - UK: Q145         - USA: Q30

When the QID is NOT known with confidence, use a name+admin_level fallback to resolve the boundary, then the same `ogc:sfContains` predicate:
```
?area osmkey:name "Distrito Federal" ;
      osmkey:boundary "administrative" ;
      osmkey:admin_level 4 .              # bare integer!
?area ogc:sfContains ?feature .
...
```
Per-country admin_level conventions:
  countries: 2 - Brazilian states + Federal District: 4 - Brazilian municipalities: 8
  German Länder (incl. Berlin): 4 - German municipalities: 8
  Portuguese districts: 6 - French communes: 8 - UK boroughs: 8 - Spanish municipalities: 8

== RADIUS PATTERN: "X within N meters of point P" via spatialSearch ==

```
SELECT ?feature ?name ?wkt ?distKm WHERE {
  BIND("POINT(2.2945 48.8584)"^^geo:wktLiteral AS ?center)   # Eiffel Tower (lon lat)
  SERVICE spatialSearch: {
    _:c spatialSearch:algorithm spatialSearch:s2 ;
        spatialSearch:left ?center ;
        spatialSearch:right ?wkt ;
        spatialSearch:numNearestNeighbors 100 ;
        spatialSearch:maxDistance 500 ;          # METERS
        spatialSearch:bindDistance ?distKm ;     # output in kilometers
        spatialSearch:payload ?feature .
    { ?feature osmkey:shop "bakery" ;
               geo:hasGeometry/geo:asWKT ?wkt .
      OPTIONAL { ?feature osmkey:name ?name } }
  }
} ORDER BY ?distKm LIMIT 50
```

== CONSTRAINT STACK: multi-criteria place finder ("onde morar / onde abrir") ==

For questions that stack several spatial criteria on one candidate ("neighborhoods near a metro AND near a park, BUT no major road within 200 m"), build ONE query joining all constraints on the same candidate.

Choosing the CANDIDATE (what the map highlights as "the answer"):
  (a) DEFAULT for vague "where should I live / open X / best area" questions:
      neighborhood-scale places - `?cand osmkey:place ?pl . VALUES ?pl { "suburb" "neighbourhood" "quarter" }`
  (b) Only when the user explicitly asks for buildings/apartments:
      `?cand osmkey:building "apartments"` (or "residential")
  (c) When the sentence names an anchor category ("metro stations that..."), that category is the candidate.

RULES for the stack:
  - ALWAYS narrow the candidate FIRST with `ogc:sfContains` into one city/region and a category predicate - never run a spatial join with an unconstrained side.
  - CRITICAL (verified live 2026-07-04): the spatialSearch join only matches POINT geometries on BOTH sides - ways/polygons are silently dropped (polygon suburbs vanish; the "nearest" highway=primary was a stray node 918 km away). ALWAYS join on centroids: bind `BIND(geof:centroid(?wkt) AS ?cpt)` for the candidate and use `?cpt` as `spatialSearch:left`; inside each SERVICE body, end with `?f geo:hasGeometry/geo:asWKT ?g . BIND(geof:centroid(?g) AS ?fw)` and use `?fw` as `spatialSearch:right`. Still project the REAL `?wkt` so the map renders polygons.
  - ALSO narrow each SERVICE body's features to the same city with `ogc:sfContains` (fresh variables, e.g. `?cf0 osmkey:wikidata "Q597" ; ogc:sfContains ?f0 .`) - an unrestricted right side computes centroids planet-wide (~84 s for highway=primary vs ~1.5 s narrowed).
  - One `SERVICE spatialSearch:` block per POSITIVE "near X" criterion. Each block gets its OWN right/payload/distance variable names.
  - `spatialSearch:numNearestNeighbors 1` when only existence matters; `maxDistance` in METERS from the sentence.
  - NEGATIVE criteria ("longe de", "sem X num raio de N m"): spatialSearch is an INNER join (a row exists only where a neighbor is found), so absence CANNOT be tested by wrapping it in `OPTIONAL` + `!BOUND` (that silently returns 0 rows - OPTIONAL does not restore the dropped candidates), and `FILTER NOT EXISTS { SERVICE spatialSearch: ... }` crashes QLever ("SpatialJoin needs two children"), and `MINUS` OOMs. Instead: give the block a WIDE `spatialSearch:maxDistance` (e.g. 100000) so every candidate finds the nearest such feature, `spatialSearch:bindDistance ?distX`, then add `FILTER(?distX > N)` where N is the forbidden radius in KILOMETRES (bindDistance is in km, so 200 m → 0.2). Verified live 2026-07-03.
  - Project the candidate ?wkt + ?name + one distance column per positive criterion (?distMetro, ?distParque, ...) so the table shows WHY each hit qualifies. ORDER BY the primary distance.

Full template - "bairros de Lisboa a menos de 400 m de metro, 800 m de um parque, sem via principal num raio de 200 m":
```
SELECT ?cand ?name ?wkt ?distMetro ?distParque WHERE {
  ?city osmkey:wikidata "Q597" ; ogc:sfContains ?cand .
  ?cand osmkey:place ?pl . VALUES ?pl { "suburb" "neighbourhood" "quarter" }
  ?cand geo:hasGeometry/geo:asWKT ?wkt .
  BIND(geof:centroid(?wkt) AS ?cpt)
  OPTIONAL { ?cand osmkey:name ?name }
  SERVICE spatialSearch: {
    _:a spatialSearch:algorithm spatialSearch:s2 ;
        spatialSearch:left ?cpt ;
        spatialSearch:right ?metroPt ;
        spatialSearch:numNearestNeighbors 1 ;
        spatialSearch:maxDistance 400 ;
        spatialSearch:bindDistance ?distMetro ;
        spatialSearch:payload ?metro .
    { ?cfa osmkey:wikidata "Q597" ; ogc:sfContains ?metro .
      ?metro osmkey:railway "station" ; osmkey:station "subway" ;
             geo:hasGeometry/geo:asWKT ?metroWkt .
      BIND(geof:centroid(?metroWkt) AS ?metroPt) }
  }
  SERVICE spatialSearch: {
    _:b spatialSearch:algorithm spatialSearch:s2 ;
        spatialSearch:left ?cpt ;
        spatialSearch:right ?parquePt ;
        spatialSearch:numNearestNeighbors 1 ;
        spatialSearch:maxDistance 800 ;
        spatialSearch:bindDistance ?distParque ;
        spatialSearch:payload ?parque .
    { ?cfb osmkey:wikidata "Q597" ; ogc:sfContains ?parque .
      ?parque osmkey:leisure "park" ; geo:hasGeometry/geo:asWKT ?parqueWkt .
      BIND(geof:centroid(?parqueWkt) AS ?parquePt) }
  }
  SERVICE spatialSearch: {
    _:c spatialSearch:algorithm spatialSearch:s2 ;
        spatialSearch:left ?cpt ;
        spatialSearch:right ?viaPt ;
        spatialSearch:numNearestNeighbors 1 ;
        spatialSearch:maxDistance 100000 ;
        spatialSearch:bindDistance ?distVia ;
        spatialSearch:payload ?via .
    { ?cfc osmkey:wikidata "Q597" ; ogc:sfContains ?via .
      ?via osmkey:highway "primary" ; geo:hasGeometry/geo:asWKT ?viaWkt .
      BIND(geof:centroid(?viaWkt) AS ?viaPt) }
  }
  FILTER(?distVia > 0.2)
} ORDER BY ?distMetro LIMIT 50
```

== QUERY RULES ==

- ALWAYS end with `LIMIT 50` unless the user explicitly asks for more.
- ALWAYS project `?wkt` so the UI can render geometries on the map.
- ALWAYS use `OPTIONAL { ?feature osmkey:name ?name }` for labels - many OSM features lack a name.
- ALWAYS narrow candidates with a CATEGORY predicate before any name `CONTAINS`/`REGEX`. Without a category filter the engine scans tens of millions of features and times out. Common mappings:
    universities → `osmkey:amenity "university"`     schools  → `osmkey:amenity "school"`
    hospitals    → `osmkey:amenity "hospital"`       cafes    → `osmkey:amenity "cafe"`
    restaurants  → `osmkey:amenity "restaurant"`     bakeries → `osmkey:shop "bakery"`
    parks        → `osmkey:leisure "park"`           museums  → `osmkey:tourism "museum"`
    hotels       → `osmkey:tourism "hotel"`          lakes    → `osmkey:natural "water"` + `osmkey:water "lake"`
    pharmacies   → `osmkey:amenity "pharmacy"`       supermarkets → `osmkey:shop "supermarket"`
    metro stations → `osmkey:railway "station"` + `osmkey:station "subway"`
    major roads/avenues → `osmkey:highway "primary"`
    vegan restaurants → `osmkey:amenity "restaurant"` + `osmkey:diet\\:vegan "yes"`
- For named landmarks/institutions (e.g. "UFF Rio das Ostras", "Universidade de Coimbra"), combine the category predicate with fuzzy name matching:
    `FILTER(CONTAINS(LCASE(?name), "uff") || CONTAINS(LCASE(?name), "fluminense"))`
- When matching loose conceptual categories (e.g. "streets named after plants/animals/people"), DO NOT enumerate dozens of alternatives. Pick AT MOST 8-10 short root-word stems that cover most of the space (e.g. for plants in Portuguese: "flor", "planta", "árvore", "jardim", "palmeira", "ipê"). Long regex alternations risk hitting the output-token limit and producing a broken query.
- SPARQL AGGREGATION RULES (common mistakes):
    1. `(expr AS ?x)` is a parse error if `?x` already appears bound elsewhere in the query body. Use a NEW variable name for the alias, or omit the alias and use the variable directly.
    2. Every non-aggregated variable in SELECT must appear in GROUP BY. Add them to GROUP BY directly - DO NOT wrap them in `SAMPLE(?v) AS ?v`, which is illegal anyway (rule 1).
    3. To compute a ratio over an aggregate, prefer one of:
         (a) inline expression: `(COUNT(?x) / ?denom AS ?ratio)` if `?denom` is in GROUP BY.
         (b) sub-SELECT: compute the count in an inner SELECT with a fresh name (e.g. `?n`), then BIND `?ratio = ?n / ?denom` in the outer query.
   Working pattern for "X per km² across regions" (also projects the region polygon so the map can render):
       ```
       SELECT ?region ?name ?areaM2 ?wkt (COUNT(?x) AS ?n) ((COUNT(?x) / (?areaM2/1000000)) AS ?density)
       WHERE {
         ?region osmkey:name ?name ;
                 osm2rdf:area ?areaM2 ;
                 geo:hasGeometry/geo:asWKT ?wkt ;
                 ogc:sfContains ?x .
         ?x osmkey:amenity "..." .
         FILTER(?areaM2 > 1000000)   # exclude micro-areas that inflate density
       }
       GROUP BY ?region ?name ?areaM2 ?wkt
       ORDER BY DESC(?density) LIMIT 50
       ```
   4. For ANY per-region aggregation, ALWAYS also project the region's geometry (`?wkt` via `geo:hasGeometry/geo:asWKT`) and add it to GROUP BY. The map UI can only render features whose geometry is in the result set; without `?wkt`, an aggregation produces a useful table but an empty map.
- Never invent predicates - stick to what's listed above plus standard SPARQL/GeoSPARQL.
- The user may ask in English or Portuguese; the query itself stays in SPARQL.
"""

app = FastAPI(title="NLQ → GeoSPARQL → OSM/Wikidata")
templates = Jinja2Templates(directory="templates")

# Neutral cwd so the claude CLI doesn't ingest this project's CLAUDE.md as context.
CLAUDE_CWD = tempfile.mkdtemp(prefix="nlq-claude-")

_local_claude = os.path.expanduser("~/.local/bin/claude")
CLAUDE_BIN = shutil.which("claude") or (
    _local_claude if os.path.exists(_local_claude) else "claude"
)

if shutil.which(CLAUDE_BIN) is None and not os.path.exists(CLAUDE_BIN):
    log.warning("`claude` CLI not found on PATH - /ask will fail until it is installed")
if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
    log.warning(
        "CLAUDE_CODE_OAUTH_TOKEN not set - run `claude setup-token` and export it; "
        "the CLI may fall back to interactive-login credentials if present"
    )


class AskRequest(BaseModel):
    question: str


def strip_fences(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def ensure_prefixes(sparql: str, prefix_block: str) -> str:
    """Drop any PREFIX lines the model emitted and prepend the engine's canonical block."""
    body_lines = [
        ln
        for ln in sparql.splitlines()
        if not ln.lstrip().upper().startswith("PREFIX ")
    ]
    return prefix_block + "\n" + "\n".join(body_lines).lstrip()


async def ask_llm(question: str, system_prompt: str) -> tuple[str | None, str | None]:
    """Run `claude -p` headless with the given system prompt; return (text, error).

    Generic: the system prompt decides what the model does with the question
    (SPARQL author for /ask, criteria parser for the finder's /parse)."""
    cmd = [
        CLAUDE_BIN,
        "-p",
        question,
        "--system-prompt",
        system_prompt,
        "--model",
        CLAUDE_MODEL,
        "--tools",
        "",
        "--output-format",
        "json",
        "--no-session-persistence",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=CLAUDE_CWD,
        )
    except FileNotFoundError:
        return (
            None,
            "`claude` CLI not found - install Claude Code and ensure it is on PATH.",
        )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CLAUDE_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        return None, f"Claude CLI did not respond within {CLAUDE_TIMEOUT:.0f}s."

    out = stdout.decode(errors="replace").strip()
    err = stderr.decode(errors="replace").strip()

    if proc.returncode != 0:
        detail = err or out or "(no output)"
        return None, (
            f"Claude CLI exited with code {proc.returncode}.\n\n{detail}\n\n"
            "Check that CLAUDE_CODE_OAUTH_TOKEN is set (generate one with `claude setup-token`)."
        )

    try:
        envelope = json.loads(out)
    except Exception:
        return None, f"Could not parse Claude CLI JSON output:\n\n{out[:2000]}"

    if envelope.get("is_error"):
        return (
            None,
            f"Claude CLI reported an error: {envelope.get('result') or envelope}",
        )

    result = envelope.get("result")
    if not result or not str(result).strip():
        return None, "Claude returned an empty result."

    return str(result), None


def error_response(sparql: str | None, message: str, status: int = 200):
    payload = {
        "sparql": sparql,
        "head": None,
        "results": None,
        "error": message,
        "engine": "qlever-osm",
        "engine_label": "QLever · OSM planet",
    }
    if status != 200:
        return JSONResponse(payload, status_code=status)
    return payload


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.post("/ask")
async def ask(req: AskRequest):
    question = req.question.strip()
    if not question:
        return error_response(None, "Empty question", status=400)

    log.info("Question: %s", question)

    raw, gen_error = await ask_llm(question, SYSTEM_PROMPT)
    if gen_error:
        log.error("Generation failed: %s", gen_error)
        return error_response(None, gen_error)

    if not raw:
        log.error("Generation returned empty output")
        return error_response(None, "Model returned no query")

    sparql = strip_fences(raw)
    if not sparql:
        return error_response(raw, "Model returned no parseable query")

    sparql = ensure_prefixes(sparql, PREFIX_BLOCK)
    log.info("Final SPARQL:\n%s", sparql)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                QLEVER_ENDPOINT,
                data={"query": sparql},
                headers={
                    "Accept": "application/sparql-results+json",
                    "User-Agent": USER_AGENT,
                },
            )
    except httpx.TimeoutException:
        return error_response(
            sparql,
            f"The SPARQL endpoint did not respond within 60s ({QLEVER_ENDPOINT}).\n\n"
            "This usually means the endpoint is overloaded or down, not that the SPARQL "
            "above is wrong. Try again in a minute.",
        )
    except Exception as e:
        return error_response(sparql, f"Could not reach the SPARQL endpoint: {e}")

    if r.status_code != 200:
        return error_response(sparql, f"Endpoint HTTP {r.status_code}\n\n{r.text}")

    try:
        data = r.json()
    except Exception as e:
        return error_response(sparql, f"Could not parse endpoint JSON: {e}\n\n{r.text}")

    return {
        "sparql": sparql,
        "head": data.get("head"),
        "results": data.get("results"),
        "error": None,
        "engine": "qlever-osm",
        "engine_label": "QLever · OSM planet",
    }
