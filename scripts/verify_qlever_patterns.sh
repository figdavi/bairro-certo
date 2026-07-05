#!/usr/bin/env bash
# Verify the CONSTRAINT STACK patterns in SYSTEM_PROMPT against the live QLever
# OSM endpoint. Run this from a network that can reach qlever.dev (some
# routers/ISPs silently drop traffic to the Uni Freiburg IP range).
#
# RESULT (verified 2026-07-03): negating a spatialSearch is the hard part.
#   3a FILTER NOT EXISTS  -> 500 crash ("SpatialJoin needs two children")
#   3b MINUS              -> out-of-memory (16 GB)
#   3c OPTIONAL + !BOUND  -> HTTP 200 but WRONG: 0 rows (OPTIONAL does not restore
#                           candidates the inner spatial join dropped)
#   3d nearest + FILTER   -> CORRECT (test below). This is the form SYSTEM_PROMPT
#                           (app.py) and build_qlever (app2.py) now use.
# Note: 3c returning "rows: 0" looks like a pass but is a silent wrong-answer —
# compare against test 4's positive-only baseline, do not trust HTTP 200 alone.
set -u

ENDPOINT="https://qlever.dev/api/osm-planet"
PREFIXES='PREFIX osmkey:        <https://www.openstreetmap.org/wiki/Key:>
PREFIX osm2rdf:       <https://osm2rdf.cs.uni-freiburg.de/rdf#>
PREFIX geo:           <http://www.opengis.net/ont/geosparql#>
PREFIX ogc:           <http://www.opengis.net/rdf#>
PREFIX spatialSearch: <https://qlever.cs.uni-freiburg.de/spatialSearch/>
'

run() {
  local name="$1" query="$2"
  echo "=== $name ==="
  local t0=$(date +%s.%N)
  local resp http
  resp=$(curl -s --max-time 90 -w $'\n%{http_code}' "$ENDPOINT" \
    -H "Accept: application/sparql-results+json" \
    --data-urlencode "query=${PREFIXES}${query}")
  http=$(tail -n1 <<<"$resp")
  local body=$(sed '$d' <<<"$resp")
  local t1=$(date +%s.%N)
  local rows=$(python3 -c "import json,sys
try:
    d=json.loads(sys.stdin.read()); print(len(d['results']['bindings']))
except Exception as e:
    print('PARSE-ERROR')" <<<"$body")
  printf "HTTP %s | rows: %s | %.1fs\n" "$http" "$rows" "$(echo "$t1 - $t0" | bc)"
  if [ "$http" != "200" ]; then echo "$body" | head -c 500; echo; fi
  echo
}

CAND='?city osmkey:wikidata "Q597" ; ogc:sfContains ?cand .
  ?cand osmkey:railway "station" ;
        geo:hasGeometry/geo:asWKT ?wkt .'

METRO_NEAR_PARK='SERVICE spatialSearch: {
    _:a spatialSearch:algorithm spatialSearch:s2 ;
        spatialSearch:left ?wkt ;
        spatialSearch:right ?parkWkt ;
        spatialSearch:numNearestNeighbors 1 ;
        spatialSearch:maxDistance 500 ;
        spatialSearch:bindDistance ?distPark ;
        spatialSearch:payload ?park .
    { ?park osmkey:leisure "park" ; geo:hasGeometry/geo:asWKT ?parkWkt . }
  }'

NEAR_SUPER='SERVICE spatialSearch: {
    _:b spatialSearch:algorithm spatialSearch:s2 ;
        spatialSearch:left ?wkt ;
        spatialSearch:right ?supWkt ;
        spatialSearch:numNearestNeighbors 1 ;
        spatialSearch:maxDistance 500 ;
        spatialSearch:bindDistance ?distSup ;
        spatialSearch:payload ?sup .
    { ?sup osmkey:shop "supermarket" ; geo:hasGeometry/geo:asWKT ?supWkt . }
  }'

NEG_INNER='SERVICE spatialSearch: {
      _:c spatialSearch:algorithm spatialSearch:s2 ;
          spatialSearch:left ?wkt ;
          spatialSearch:right ?roadWkt ;
          spatialSearch:numNearestNeighbors 1 ;
          spatialSearch:maxDistance 200 ;
          spatialSearch:payload ?road .
      { ?road osmkey:highway "primary" ; geo:hasGeometry/geo:asWKT ?roadWkt . }
    }'

run "1. variable-left spatial join (stations near park)" \
"SELECT ?cand ?wkt ?distPark WHERE {
  $CAND
  $METRO_NEAR_PARK
} LIMIT 10"

run "2. two stacked SERVICE blocks (park AND supermarket)" \
"SELECT ?cand ?wkt ?distPark ?distSup WHERE {
  $CAND
  $METRO_NEAR_PARK
  $NEAR_SUPER
} LIMIT 10"

run "3a. negation: FILTER NOT EXISTS + spatialSearch" \
"SELECT ?cand ?wkt WHERE {
  $CAND
  FILTER NOT EXISTS { $NEG_INNER }
} LIMIT 10"

run "3b. negation: MINUS + spatialSearch" \
"SELECT ?cand ?wkt WHERE {
  $CAND
  MINUS { ?cand geo:hasGeometry/geo:asWKT ?wkt . $NEG_INNER }
} LIMIT 10"

run "3c. negation: OPTIONAL + !BOUND" \
"SELECT ?cand ?wkt WHERE {
  $CAND
  OPTIONAL {
    SERVICE spatialSearch: {
      _:c spatialSearch:algorithm spatialSearch:s2 ;
          spatialSearch:left ?wkt ;
          spatialSearch:right ?roadWkt ;
          spatialSearch:numNearestNeighbors 1 ;
          spatialSearch:maxDistance 200 ;
          spatialSearch:bindDistance ?distRoad ;
          spatialSearch:payload ?road .
      { ?road osmkey:highway \"primary\" ; geo:hasGeometry/geo:asWKT ?roadWkt . }
    }
  }
  FILTER(!BOUND(?distRoad))
} LIMIT 10"

# Working negation on a RARE feature (hospital) so survivors remain — this is what
# distinguishes the correct form from broken 3c: 3c returns 0 here too, 3d returns >0.
run "3d. negation: nearest + FILTER(dist > N_km)  [THE WORKING FORM]" \
"SELECT ?cand ?wkt ?distHosp WHERE {
  $CAND
  SERVICE spatialSearch: {
    _:d spatialSearch:algorithm spatialSearch:s2 ;
        spatialSearch:left ?wkt ;
        spatialSearch:right ?hospWkt ;
        spatialSearch:numNearestNeighbors 1 ;
        spatialSearch:maxDistance 100000 ;
        spatialSearch:bindDistance ?distHosp ;
        spatialSearch:payload ?hosp .
    { ?hosp osmkey:amenity \"hospital\" ; geo:hasGeometry/geo:asWKT ?hospWkt . }
  }
  FILTER(?distHosp > 0.2)
} LIMIT 10"

# Pure k-NN (numNearestNeighbors WITHOUT maxDistance): the join must be TOTAL —
# every candidate returns with the distance to its nearest beach, even if that is
# tens of km away. This is what makes build_qlever's scoring mode never lose a
# candidate to a data-poor category (e.g. "beach" in London). Expect rows == the
# positive-only candidate count, with large ?distBeach values.
run "5. pure k-NN, no maxDistance (London stations → nearest beach) [SCORING FORM]" \
"SELECT ?cand ?wkt ?distBeach WHERE {
  ?city osmkey:wikidata \"Q84\" ; ogc:sfContains ?cand .
  ?cand osmkey:railway \"station\" ;
        geo:hasGeometry/geo:asWKT ?wkt .
  SERVICE spatialSearch: {
    _:e spatialSearch:algorithm spatialSearch:s2 ;
        spatialSearch:left ?wkt ;
        spatialSearch:right ?beachWkt ;
        spatialSearch:numNearestNeighbors 1 ;
        spatialSearch:bindDistance ?distBeach ;
        spatialSearch:payload ?beach .
    { ?beach osmkey:natural \"beach\" ; geo:hasGeometry/geo:asWKT ?beachWkt . }
  }
} LIMIT 10"

run "4. flagship: Lisbon neighborhoods, 2 positive + 1 negative" \
"SELECT ?cand ?name ?wkt ?distMetro ?distPark WHERE {
  ?city osmkey:wikidata \"Q597\" ; ogc:sfContains ?cand .
  ?cand osmkey:place ?pl . VALUES ?pl { \"suburb\" \"neighbourhood\" \"quarter\" }
  ?cand geo:hasGeometry/geo:asWKT ?wkt .
  OPTIONAL { ?cand osmkey:name ?name }
  SERVICE spatialSearch: {
    _:a spatialSearch:algorithm spatialSearch:s2 ;
        spatialSearch:left ?wkt ;
        spatialSearch:right ?metroWkt ;
        spatialSearch:numNearestNeighbors 1 ;
        spatialSearch:maxDistance 400 ;
        spatialSearch:bindDistance ?distMetro ;
        spatialSearch:payload ?metro .
    { ?metro osmkey:railway \"station\" ; osmkey:station \"subway\" ;
             geo:hasGeometry/geo:asWKT ?metroWkt . }
  }
  SERVICE spatialSearch: {
    _:b spatialSearch:algorithm spatialSearch:s2 ;
        spatialSearch:left ?wkt ;
        spatialSearch:right ?parkWkt ;
        spatialSearch:numNearestNeighbors 1 ;
        spatialSearch:maxDistance 800 ;
        spatialSearch:bindDistance ?distPark ;
        spatialSearch:payload ?park .
    { ?park osmkey:leisure \"park\" ; geo:hasGeometry/geo:asWKT ?parkWkt . }
  }
  SERVICE spatialSearch: {
    _:c spatialSearch:algorithm spatialSearch:s2 ;
        spatialSearch:left ?wkt ;
        spatialSearch:right ?viaWkt ;
        spatialSearch:numNearestNeighbors 1 ;
        spatialSearch:maxDistance 100000 ;
        spatialSearch:bindDistance ?distVia ;
        spatialSearch:payload ?via .
    { ?via osmkey:highway \"primary\" ; geo:hasGeometry/geo:asWKT ?viaWkt . }
  }
  FILTER(?distVia > 0.2)
} ORDER BY ?distMetro LIMIT 50"
