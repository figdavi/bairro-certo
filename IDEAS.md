\*\*USE claude CLI as the ai brain\*\*

# Project Ideas & Design Notes

NL → GeoSPARQL → QLever (OSM, + optional Wikidata) → Leaflet map.

---

## The core insight: the unit of "wow" is the constraint stack, not the question

Any _single_ question ("restaurants near me", "biggest lakes") is something Google
Maps already does better. You can't win that fight.

What no consumer app can do: **stack arbitrary spatial constraints nobody pre-built a
button for.**

> "An apartment within 400 m of a metro, AND within 800 m of a park, AND with a
> pharmacy + bakery on the same block, BUT not within 200 m of a major avenue."

Each consumer app ships only the filters its product team hardcoded. With GeoSPARQL,
every new combination is just a new sentence. That composability is where GeoSPARQL
_structurally_ shines.

Pitch: _"Describe your ideal place in one messy sentence — with as many overlapping
conditions as you want — and watch the map show exactly where on Earth satisfies all
of them at once."_

---

## Why I can't just "slap any RDF dataset on it and answer anything" — the four walls

SPARQL gives the plumbing for the Linked Data dream, but four things must ALL be true
for a dataset to be usable, and rarely are:

1. **Wall 1 — It must be a live, fast endpoint.** Most RDF is a dump file, not a
   queryable service. To query it you must load+index it (what QLever does). Real
   universe ≈ a dozen hosted endpoints (Wikidata, OSM, DBpedia, GeoNames, some gov).

2. **Wall 2 — You can only JOIN datasets that share identifiers.** Like a SQL foreign
   key. OSM↔Wikidata works only because humans tagged OSM features with
   `wikidata="Q…"`. No shared key → nothing to join on (label matching is fragile).
   The "**Linked**" in Linked Data is the hard, usually-missing part.

3. **Wall 3 — Every dataset has its own schema the LLM must already know.** The
   `SYSTEM_PROMPT` is pages of curl-verified facts about ONE endpoint. No reliable
   "point the LLM at an unknown endpoint and it figures out the schema."

4. **Wall 4 — A map answer needs geometry, which most data lacks.** Many datasets know
   "Putin born in Saint Petersburg" but not _where_ that is. OSM is central because it
   is the **geometry provider**; other datasets supply facts, OSM supplies place.

| The dream                 | The reality                                     |
| ------------------------- | ----------------------------------------------- |
| Query any RDF data        | Only data hosted as a fast endpoint (≈ a dozen) |
| Join anything to anything | Only where shared IDs/links exist               |
| LLM writes any query      | Only for schemas it's been taught               |
| Map any answer            | Only for entities that carry geometry           |

The project is interesting _because_ it surfs the small, genuinely-linked core
(OSM geometry + Wikidata facts) — powerful within the hubs' reach, not infinite.

---

## Can the AI solve Wall 2? Partly — it MOVES what you trust, in 3 ways

The AI doesn't remove Wall 2; it replaces a _data-level_ link with a _model-level_ or
_geometry-level_ one.

| Mechanism                   | Bridges via                      | Works when                     | Risk                                    |
| --------------------------- | -------------------------------- | ------------------------------ | --------------------------------------- |
| 1. LLM resolves from memory | a hardcoded IRI (`wd:Q7747`)     | entity is famous               | hallucinated IDs — silent, unverifiable |
| 2. AI orchestrates a lookup | a label match read from the data | entity has a searchable name   | slower, more complex, but reliable      |
| 3. **Spatial join**         | **geometry itself**              | both datasets have coordinates | point-in-polygon fuzziness              |

**Mechanism 3 is the special one for this project:** in the geo world, _location_ is a
universal foreign key. "Which OSM polygon contains this point?" (`ogc:sfContains`)
joins two datasets that share NOTHING but being about the same patch of Earth. No
`sameAs`, no matching labels needed — **the geometry IS the join key.**

→ Strong presentation point: **geometry is the one foreign key always present in a map
project**, so location (not hand-wired links) becomes the join. Another reason to keep
OSM at the center.

What the AI _cannot_ do: manufacture a link where there's no matchable handle at all
(no shared ID, no resolvable name, no geometry). There it can only guess — and a guess
injected as a constant is a confident hallucination, more dangerous than honest failure.

---

## Product concepts (one engine, pick a vertical)

> **DECISION (2026-07-03): ideas ① and ② were merged and built** as the single
> vertical "Onde Morar / Onde Abrir" — one constraint-stack engine with positive
> criteria (idea ①) plus spatial negation (idea ②'s avoid-the-competition filter).
> Idea ③ remains the "where this goes next" pitch. The AI brain is Claude Haiku via
> the Claude Code CLI (headless, OAuth token), per the note at the top of this file.

- **1 "Onde Morar" — multi-criteria relocation finder** ⭐
  Describe your dream neighborhood in one sentence; map highlights areas meeting ALL
  criteria. Relatable on stage (everyone has hunted for an apartment), and a 5-way
  spatial join Zillow/QuintoAndar can't express.

- **2 "Site Selection" — where to open a business**
  "Where should I open a vegan café? Near a university, walkable, but no vegan café
  within 1 km." The _avoid-the-competition_ (spatial negation) constraint is very SPARQL.
  Mirrors the real (paid) "location intelligence" industry.


- **3 "Painel do Prefeito" — public-works planning dashboard for city mayors**
  A decision-support dashboard that scans the city and flags **where new public
  infrastructure is needed**: residential clusters with no school / health post /
  pharmacy within walking distance, densely-built areas with no nearby park or square,
  neighborhoods poorly served by a road/transit connection, etc. Each gap is shown as a
  ranked, mapped "intervention candidate" (e.g. _"build a school here — N households,
  nearest school 2.3 km away"_).
  - **Why it shines:** it's idea ③ turned into a _governance product_ — overlapping
    coverage gaps across many service types at once, exactly the multi-constraint spatial
    reasoning no off-the-shelf GIS button does. The mayor types a goal in plain language
    ("onde faltam creches?") and the map answers.
  - **Why wow:** real institutional customer (city hall), social impact, and a clear
    decision it drives (where to spend the budget). Strong thesis framing.
  - **Feasibility:** high for the gap-detection core (`spatialSearch:` radius +
    `ogc:sfContains` + counting over residential features). "Where to put a _street_"
    is the hardest sub-case — true road planning implies routing/network analysis OSM
    SPARQL doesn't do natively; scope it as "connectivity gaps" (areas far from any
    classified road) rather than literal route design.

---

## Capability demos (each shows off a different GeoSPARQL primitive)

- **Containment** (`ogc:sfContains` + `admin_level`): castles in Portugal, UNESCO
  sites in Brazil, universities in Rio de Janeiro state.
- **Proximity / nearest** (`spatialSearch:` k-NN): pharmacies within 500 m, 5 nearest
  hospitals to downtown, Italian restaurants near the Eiffel Tower.
- **Superlatives** (precomputed `osm2rdf:area` / `length`): 10 largest lakes in Brazil,
  longest rivers, biggest urban parks.
- **Density / aggregation**: airports per country, museums per district (colored map).
- **Themed POI hunts** (pure tags — food/history/animals are native OSM!):
  zoos/aquariums, medieval ruins, vegan restaurants.
- **Federated finale** (Wikidata + OSM): "where was Russia's president born?",
  birthplaces of Brazilian Nobel laureates.
