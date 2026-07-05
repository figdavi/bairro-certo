# Bairro Certo

Multi-criteria place finder: describe your ideal place in one messy sentence — stacking as many
spatial constraints as you want, positive ("within 400 m of a metro") and negative
("no major avenue within 200 m") — and the map shows where everything holds at once.

Claude Haiku (via the Claude Code CLI in headless mode) writes a GeoSPARQL query;
QLever runs it against the full OSM planet (automatic fallback to the Wikidata Query
Service when QLever is unreachable); the page shows the SPARQL, a results table, and
Leaflet geometries color-coded per column.

## Stack

- Python 3.11+, FastAPI, httpx (async).
- Claude Code CLI (`claude -p`, headless), model `claude-haiku-4-5`.
- Primary endpoint: QLever `https://qlever.dev/api/osm-planet` (no auth).
- Backup endpoint: Wikidata Query Service `https://query.wikidata.org/sparql`.
- Leaflet + vanilla JS frontend, no build step.

## Setup

```bash
pip install -r requirements.txt
./start.sh                       # everything else: token + .env + server
```

`start.sh` mints a long-lived OAuth token via `claude setup-token` on first run
(browser auth), saves it to `.env` (chmod 600, gitignored), and launches uvicorn with
the token exported. On later runs it reuses the saved token; `--new-token` forces a
fresh one, `--port N` changes the port (default 8000).

Then open <http://localhost:8000>. The `claude` CLI must be on PATH (or at
`~/.local/bin/claude`).

Manual equivalent:

```bash
claude setup-token               # prints a long-lived OAuth token (once)
export CLAUDE_CODE_OAUTH_TOKEN=...
uvicorn app:app --reload
```

## File layout

```
app.py                            # FastAPI server + claude CLI call + dual SPARQL engines
app2.py                           # guided finder backend (imports app.py; deterministic SPARQL + optional Haiku)
templates/index.html              # Free-text UI: form, constraint builder, SPARQL pre, table, map
templates/finder.html             # Guided finder UI: Transit-style wizard + live map (served by app2.py)
start.sh                          # one-shot startup: OAuth token → .env → uvicorn (--app app2 for the finder)
scripts/verify_qlever_patterns.sh # curl-verifies constraint-stack patterns against live QLever
requirements.txt
README.md
```
