import asyncio
import json
import logging
import re
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import app as core

log = logging.getLogger("Bairro Certo")

# osm tags for Qlever:
# Uses AND in the tuples when querying
CATEGORIES = {
    "metro": {
        "label": "estação de metrô",
        "osm": [("railway", "station"), ("station", "subway")],
    },
    "onibus": {
        "label": "ponto de ônibus",
        "osm": [("highway", "bus_stop")],
    },
    "parque": {"label": "parque", "osm": [("leisure", "park")]},
    "supermercado": {
        "label": "supermercado",
        "osm": [("shop", "supermarket")],
    },
    "padaria": {"label": "padaria", "osm": [("shop", "bakery")]},
    "farmacia": {
        "label": "farmácia",
        "osm": [("amenity", "pharmacy")],
    },
    "escola": {"label": "escola", "osm": [("amenity", "school")]},
    "hospital": {
        "label": "hospital",
        "osm": [("amenity", "hospital")],
    },
    "universidade": {
        "label": "universidade",
        "osm": [("amenity", "university")],
    },
    "restaurante": {
        "label": "restaurante",
        "osm": [("amenity", "restaurant")],
    },
    "cafe": {"label": "café", "osm": [("amenity", "cafe")]},
    "academia": {
        "label": "academia",
        "osm": [("leisure", "fitness_centre")],
    },
    "avenida": {
        "label": "avenida movimentada",
        "osm": [("highway", "primary")],
    },
    "praia": {"label": "praia", "osm": [("natural", "beach")]},
}

CITIES = {
    "lisboa": {"label": "Lisboa", "osm": "Q597"},
    "porto": {"label": "Porto", "osm": "Q36433"},
    "berlim": {"label": "Berlim", "osm": "Q64"},
    "paris": {"label": "Paris", "osm": "Q90"},
    "madri": {"label": "Madri", "osm": "Q2807"},
    "roma": {"label": "Roma", "osm": "Q220"},
    "londres": {"label": "Londres", "osm": "Q84"},
    "nova-york": {"label": "Nova York", "osm": "Q60"},
    "rio": {"label": "Rio de Janeiro", "osm": "Q8678"},
    "sao-paulo": {"label": "São Paulo", "osm": "Q174"},
    "brasilia": {"label": "Brasília", "osm": "Q119158"},
}

# Candidate ("the answer")
# **Returns neighborhood**
OSM_CANDIDATE_VALUES = '"suburb" "neighbourhood" "quarter"'

NEIGHBORHOODS_LIMIT = 30  # ranked places returned to the UI
CANDIDATE_SCAN_LIMIT = 400  # candidates fetched for scoring (no filters in SPARQL)

# The query shape (k-NN on centroids, city-narrowed, no distance filters) is
# documented in build_qlever's docstring - it encodes live-verified endpoint
# facts; don't "simplify" it back to raw geometries or planet-wide right sides.

app = FastAPI(title="Bairro Certo")

# Frontend templates
templates = Jinja2Templates(directory="templates")


class Criterion(BaseModel):
    kind: Literal["near", "avoid"]
    category: str
    distance: int = Field(gt=0, le=5000)  # meters - IDEAL distance, not a cutoff
    weight: int = Field(
        default=2, ge=1, le=3
    )  # 1 desejável · 2 importante · 3 essencial


class FindRequest(BaseModel):
    mode: Literal["morar", "abrir"]
    city: str
    criteria: list[Criterion] = Field(max_length=8)
    custom: str | None = None


class ParseRequest(BaseModel):
    text: str = Field(min_length=3, max_length=500)


def tag_triples(var: str, pairs: list) -> str:
    """SPARQL tag triples for a feature matching ALL pairs (inner colons escaped)."""
    return f"{var} " + " ; ".join(
        f'osmkey:{str(k).replace(":", "\\:")} "{v}"' for k, v in pairs
    ) + " ."


def build_qlever(city_osm_qid: str, criteria: list[dict]) -> str:
    """Scoring-mode constraint stack. Two live-verified endpoint facts shape it:

    1. spatialSearch's k-NN join only matches POINT geometries on BOTH sides -
       ways/polygons are silently dropped (a polygon suburb disappears; the
       nearest highway=primary "found" was a stray node 918 km away). Fix:
       join on geof:centroid() of each geometry, while still projecting the
       real ?wkt so the map renders polygons.
    2. An unrestricted right side would compute centroids for every instance
       of the tag on the planet (~84 s for highway=primary). Fix: narrow each
       criterion's features to the city via the precomputed ogc:sfContains
       first (verified: full 3-criterion Lisbon query in ~1.5 s). Consequence:
       features just OUTSIDE the city boundary don't count, and a category
       with zero instances IN THE CITY empties the whole join - tags_exist()
       probes per city and such criteria are dropped into `skipped`.

    No maxDistance and no distance FILTERs: the join stays total, every
    candidate returns with every distance, and score_places() ranks them -
    the finder never returns 0 places because a threshold was too strict.
    bindDistance is in KILOMETRES (centroid-to-centroid).
    """
    dist_vars = [f"?dist_{i}" for i in range(len(criteria))]
    parts = [
        "SELECT ?cand ?name ?wkt " + " ".join(dist_vars) + " WHERE {",
        f'  ?city osmkey:wikidata "{city_osm_qid}" ; ogc:sfContains ?cand .',
        f"  ?cand osmkey:place ?pl . VALUES ?pl {{ {OSM_CANDIDATE_VALUES} }}",
        "  ?cand geo:hasGeometry/geo:asWKT ?wkt .",
        "  BIND(geof:centroid(?wkt) AS ?cpt)",
        "  OPTIONAL { ?cand osmkey:name ?name }",
    ]
    for i, c in enumerate(criteria):
        parts.append(
            "  SERVICE spatialSearch: {\n"
            f"    _:b{i} spatialSearch:algorithm spatialSearch:s2 ;\n"
            "        spatialSearch:left ?cpt ;\n"
            f"        spatialSearch:right ?fw{i} ;\n"
            "        spatialSearch:numNearestNeighbors 1 ;\n"
            f"        spatialSearch:bindDistance ?dist_{i} ;\n"
            f"        spatialSearch:payload ?f{i} .\n"
            f'    {{ ?cf{i} osmkey:wikidata "{city_osm_qid}" ; ogc:sfContains ?f{i} .\n'
            f"      {tag_triples(f'?f{i}', c['osm'])}\n"
            f"      ?f{i} geo:hasGeometry/geo:asWKT ?g{i} .\n"
            f"      BIND(geof:centroid(?g{i}) AS ?fw{i}) }}\n"
            "  }"
        )
    parts.append(f"}} LIMIT {CANDIDATE_SCAN_LIMIT}")
    return "\n".join(parts)


def score_places(head: dict, results: dict, criteria: list[dict]) -> list[dict]:
    """Rank candidates by weighted match score instead of filtering.

    near:  ideal distance d0 -> score 1.0 up to d0, halving every extra d0
           (600 m ideal: 600 m -> 100%, 1.2 km -> 50%, 1.8 km -> 25%).
    avoid: score ramps 0 -> 1 linearly until the ideal clearance d0 is reached.
    Total = weighted average -> 0-100%.
    """
    cols = (head or {}).get("vars", [])
    places = []
    for b in (results or {}).get("bindings", []):
        wkt = next(
            (
                b[c]["value"]
                for c in cols
                if b.get(c, {}).get("value") and WKT_RE.match(b[c]["value"])
            ),
            None,
        )
        name = b.get("name", {}).get("value")
        crit, wsum, ssum = [], 0.0, 0.0
        for i, c in enumerate(criteria):
            v = b.get(f"dist_{i}", {}).get("value")
            d = float(v) * 1000 if v is not None else None  # bindDistance is km
            d0 = c["distance"]
            if d is None:
                # No feature found at all: perfect for avoid, zero for near.
                s = 1.0 if c["kind"] == "avoid" else 0.0
            elif c["kind"] == "near":
                s = 1.0 if d <= d0 else 2 ** (-(d - d0) / d0)
            else:
                s = min(d / d0, 1.0)
            crit.append(
                {
                    "category": c["key"],
                    "label": c["label"],
                    "kind": c["kind"],
                    "dist": round(d) if d is not None else None,
                    "score": round(s * 100),
                }
            )
            wsum += c["weight"]
            ssum += c["weight"] * s
        places.append(
            {
                "name": name or "(sem nome)",
                "wkt": wkt,
                "score": round(100 * ssum / wsum) if wsum else 0,
                "crit": crit,
                "dists": {
                    c["category"]: c["dist"] for c in crit if c["dist"] is not None
                },
            }
        )
    places.sort(key=lambda p: p["score"], reverse=True)
    return places[:NEIGHBORHOODS_LIMIT]


# ---------------------------------------------------------------- tag existence probe

_tags_exist_cache: dict[tuple, bool] = {}  # OSM data is static per process lifetime


async def tags_exist(city_osm_qid: str, pairs: list) -> bool:
    """1-row lookup: does this tag combo have ANY instance IN THE CITY?
    Guards the k-NN join in build_qlever: its right side is city-narrowed, so
    a criterion with zero instances in the city would empty the whole result
    (registry categories and LLM-resolved custom criteria alike)."""
    key = (city_osm_qid, tuple(tuple(p) for p in pairs))
    if key in _tags_exist_cache:
        return _tags_exist_cache[key]
    sparql = (
        core.PREFIX_BLOCK
        + f'\nSELECT ?f WHERE {{ ?c osmkey:wikidata "{city_osm_qid}" ; ogc:sfContains ?f . '
        + tag_triples("?f", pairs)
        + " } LIMIT 1"
    )
    # Short timeout: this is a 1-row index lookup; if it's slow the endpoint is
    # down anyway and the main query will surface the real error.
    data, err = await run_query(core.QLEVER_ENDPOINT, sparql, timeout=8.0)
    if err or data is None:
        # Endpoint hiccup: don't cache, assume it exists (the main query will tell).
        return True
    exists = bool(data.get("results", {}).get("bindings"))
    _tags_exist_cache[key] = exists
    return exists


# ---------------------------------------------------------------- custom criterion -> OSM tags

TAG_RESOLVER_PROMPT = """You translate ONE free-text place criterion (Portuguese or English) into OpenStreetMap tags. Output ONLY a JSON object - no prose, no markdown fences.

Schema:
{"kind": "near"|"avoid", "osm": [["key","value"], ...], "distance": <int meters>, "label": "<short label in Portuguese>"}

Rules:
- "osm" holds 1-3 [key, value] tag pairs that ALL apply to the SAME feature (they are ANDed). Use real, widely-used OSM tagging - examples: rio -> [["waterway","river"]]; lago -> [["natural","water"]]; praça -> [["place","square"]]; shopping -> [["shop","mall"]]; ciclovia -> [["highway","cycleway"]]; ferrovia -> [["railway","rail"]]; aeroporto -> [["aeroway","aerodrome"]]; igreja -> [["amenity","place_of_worship"]]; bar -> [["amenity","bar"]]; teatro -> [["amenity","theatre"]]. Prefer ONE pair unless two are truly needed.
- "kind": "avoid" when the text says longe/sem/evitar/nada de; otherwise "near".
- "distance": meters mentioned in the text (convert km); default 400 for near, 300 for avoid.
- "label": 2-3 word Portuguese label for a UI chip.
- If the concept has NO reasonable OSM tag (subjective ideas like "vista bonita", "bairro seguro", "gente simpática"), return {"osm": []}.
- If the text mixes several concepts, resolve only the most important one.
"""

_TAG_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_:]*$")
_TAG_VAL_RE = re.compile(r'^[^"\\\n]{1,60}$')


async def resolve_custom(text: str) -> tuple[dict | None, str | None]:
    """Claude Haiku as a VOCABULARY resolver: free-text criterion -> OSM tags.
    The result joins the deterministic pipeline like any registry category.
    Returns ({kind, osm, distance, label}, None); osm=[] means "no tag fits"."""
    raw, err = await core.ask_llm(text, TAG_RESOLVER_PROMPT)
    if err or not raw:
        return None, err or "O modelo não retornou nada."
    try:
        data = json.loads(core.strip_fences(raw))
    except Exception:
        return None, f"Interpretação inválida do modelo:\n{raw[:300]}"
    pairs = []
    for p in data.get("osm") or []:
        if (
            isinstance(p, (list, tuple))
            and len(p) == 2
            and _TAG_KEY_RE.match(str(p[0]))
            and _TAG_VAL_RE.match(str(p[1]))
        ):
            pairs.append((str(p[0]), str(p[1])))
    if not pairs:
        return {"osm": []}, None
    kind = data.get("kind") if data.get("kind") in ("near", "avoid") else "near"
    try:
        distance = int(data.get("distance", 400))
    except (TypeError, ValueError):
        distance = 400
    return {
        "kind": kind,
        "osm": pairs[:3],
        "distance": min(max(distance, 50), 5000),
        "label": str(data.get("label") or text)[:40],
    }, None


PARSER_PROMPT = f"""You extract structured place-finding criteria from a sentence in Portuguese or English. Output ONLY a JSON object - no prose, no markdown fences.

Schema:
{{
  "mode": "morar" | "abrir" | null,
  "city": <city key> | null,
  "criteria": [{{"kind": "near"|"avoid", "category": <category key>, "distance": <int meters>, "weight": 1|2|3}}],
  "unmapped": [<verbatim fragments you could not map to a category key>]
}}

City keys: {", ".join(CITIES.keys())} (null if no city mentioned).
Category keys (map synonyms - mercado->supermercado, colégio/creche->escola, faculdade->universidade, ginásio->academia, padoca->padaria, posto de saúde/clínica->hospital, trânsito/rua movimentada->avenida):
{", ".join(f"{k} ({v['label']})" for k, v in CATEGORIES.items())}

Rules:
- "longe de", "sem", "evitar", "nada de" -> kind "avoid"; everything else -> "near".
- distance: use the meters the user gives (convert km); default 600 for near, 300 for avoid.
- weight: 3 if "essencial", "tem que", "preciso", "obrigatório"; 1 if "seria bom", "de preferência", "idealmente", "se possível"; else 2.
- A fragment that names no known category (e.g. "perto do rio", "vista para o mar") goes VERBATIM into "unmapped" - never invent category keys.
- "abrir"/"montar um negócio/loja/café..." -> mode "abrir"; "morar"/"viver"/"apartamento para mim" -> "morar"; unclear -> null.
"""


async def parse_text(text: str) -> tuple[dict | None, str | None]:
    raw, err = await core.ask_llm(text, PARSER_PROMPT)
    if err or not raw:
        return None, err or "O modelo não retornou nada."
    try:
        data = json.loads(core.strip_fences(raw))
    except Exception:
        return None, f"Interpretação inválida do modelo:\n{raw[:500]}"
    out = {"mode": None, "city": None, "criteria": [], "unmapped": []}
    if data.get("mode") in ("morar", "abrir"):
        out["mode"] = data["mode"]
    if data.get("city") in CITIES:
        out["city"] = data["city"]
    for c in data.get("criteria", []):
        try:
            crit = Criterion(
                **{
                    k: c[k]
                    for k in ("kind", "category", "distance", "weight")
                    if k in c
                }
            )
        except Exception:
            continue
        if crit.category in CATEGORIES:
            out["criteria"].append(crit.model_dump())
    out["unmapped"] = [str(u)[:120] for u in data.get("unmapped", [])][:4]
    return out, None


# ---------------------------------------------------------------- result shaping

WKT_RE = re.compile(r"^\s*(POINT|LINESTRING|POLYGON|MULTI)", re.I)


async def run_query(endpoint: str, sparql: str, timeout: float = 60.0):
    """POST a query; returns (data, error). Mirrors app.py's error handling."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                endpoint,
                data={"query": sparql},
                headers={
                    "Accept": "application/sparql-results+json",
                    "User-Agent": core.USER_AGENT,
                },
            )
    except httpx.TimeoutException:
        return (
            None,
            f"O endpoint SPARQL não respondeu em {timeout:.0f}s ({endpoint}). Tente de novo em instantes.",
        )
    except Exception as e:
        return None, f"Não foi possível alcançar o endpoint SPARQL: {e}"
    if r.status_code != 200:
        return None, f"Endpoint HTTP {r.status_code}\n\n{r.text[:1000]}"
    try:
        return r.json(), None
    except Exception as e:
        return None, f"Resposta do endpoint não é JSON válido: {e}"


def payload(
    sparql,
    head=None,
    results=None,
    error=None,
    places=None,
    question=None,
    skipped=None,
):
    return {
        "sparql": sparql,
        "head": head,
        "results": results,
        "error": error,
        "engine": "qlever-osm",
        "engine_label": "QLever · OSM planet",
        "places": places or [],
        "question": question,
        "skipped": skipped or [],
    }


# ---------------------------------------------------------------- routes


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "finder.html")


@app.post("/parse")
async def parse(req: ParseRequest):
    """Natural-language sentence -> structured criteria the UI shows as chips."""
    parsed, err = await parse_text(req.text.strip())
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return {**parsed, "error": None}


@app.post("/find")
async def find(req: FindRequest):
    if req.city not in CITIES:
        return JSONResponse(
            payload(None, error=f"Cidade desconhecida: {req.city}"),
            status_code=400,
        )
    for c in req.criteria:
        if c.category not in CATEGORIES:
            return JSONResponse(
                payload(None, error=f"Categoria desconhecida: {c.category}"),
                status_code=400,
            )
    if not req.criteria and not req.custom:
        return JSONResponse(
            payload(None, error="Nenhum critério informado"), status_code=400
        )

    question = None
    skipped: list[str] = []

    # Registry criteria carry their tag pairs from CATEGORIES.
    resolved = [
        {
            "key": c.category,
            "label": CATEGORIES[c.category]["label"],
            "osm": CATEGORIES[c.category]["osm"],
            "kind": c.kind,
            "distance": c.distance,
            "weight": c.weight,
        }
        for c in req.criteria
    ]

    # A free-text criterion is resolved by the LLM into OSM tags and then joins
    # the SAME deterministic pipeline (k-NN + scoring) as any registry category.
    if req.custom and req.custom.strip():
        text = req.custom.strip()
        rc, err = await resolve_custom(text)
        if err or rc is None:
            return payload(
                None, error=err or "Não consegui interpretar o critério livre."
            )
        if not rc["osm"]:
            skipped.append(text[:40])
        else:
            resolved.append(
                {
                    "key": "custom",
                    "label": rc["label"],
                    "osm": rc["osm"],
                    "kind": rc["kind"],
                    "distance": rc["distance"],
                    "weight": 2,
                }
            )
            tags = " + ".join(f"{k}={v}" for k, v in rc["osm"])
            question = (
                f'critério livre "{text}" interpretado como {tags} '
                f"({'perto' if rc['kind'] == 'near' else 'longe'}, ~{rc['distance']} m)"
            )
            log.info("Custom criterion resolved: %s", question)

    # Probe each tag combo (cached per city): with the join's right side
    # city-narrowed, a criterion with zero instances in the city would empty
    # the whole result - drop it into `skipped` instead.
    city_qid = CITIES[req.city]["osm"]
    if resolved:
        checks = await asyncio.gather(
            *(tags_exist(city_qid, r["osm"]) for r in resolved)
        )
        skipped += [r["label"] for r, ok in zip(resolved, checks) if not ok]
        resolved = [r for r, ok in zip(resolved, checks) if ok]
    if not resolved:
        return payload(
            None,
            skipped=skipped,
            question=question,
            error=f"Nenhum dos critérios tem esse tipo de lugar mapeado em "
            f"{CITIES[req.city]['label']}. Tente outras categorias.",
        )

    sparql = core.PREFIX_BLOCK + "\n" + build_qlever(CITIES[req.city]["osm"], resolved)

    log.info("Finder SPARQL:\n%s", sparql)
    data, error = await run_query(core.QLEVER_ENDPOINT, sparql)
    if error or not data:
        return payload(
            sparql,
            error=error if error else "Erro ao rodar query GeoSPARQL",
            question=question,
            skipped=skipped,
        )

    head, results = data.get("head"), data.get("results")
    return payload(
        sparql,
        head=head,
        results=results,
        places=score_places(head, results, resolved),
        question=question,
        skipped=skipped,
    )
