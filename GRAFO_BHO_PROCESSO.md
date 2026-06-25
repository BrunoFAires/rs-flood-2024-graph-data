# Building the Station Graph from ANA BHO 2017

**Purpose.** This document describes, end to end, how the directed station graph
for the STGNN flood dataset is built from the official ANA hydrography (Base
Hidrográfica Ottocodificada — BHO 2017 5K). It is written to be reportable: it
records the data source, the method, the parameters, the results, and the known
limitations.

**Date:** 2026-06-23
**Scripts:** `baixar_bho.py` (fetch), `build_graph_bho.py` (build)
**Outputs:** `grafo_bho.npz`, `fluxo_arestas_bho.geojson`
**Renderers:** `render_graph.py`, `render_satellite.py`

> The previous prototype (`build_graph.py`) used **HydroRIVERS** (HydroSHEDS/WWF),
> which the project roadmap classifies as a *fallback* source. This BHO pipeline
> is the roadmap's *primary* source and supersedes the prototype. The old files
> were kept intact; the BHO pipeline lives in new, separate files.

---

## 1. What the graph represents

Each **node** is an ANA hydrotelemetric station (basin `bacia=8`, the
Guaíba/Lagoa dos Patos basin). Each **directed edge** `montante → jusante`
(upstream → downstream) means water flows from one station to the next along the
real river network, with the edge **weight** being the along-river flow distance
in km. The resulting graph is the `A` (adjacency) the STGNN consumes, together
with per-node static attributes.

---

## 2. Data sources

| Input | File | Description |
| --- | --- | --- |
| **Stations (nodes)** | `estacoes_rs.csv` | ANA station catalog. Filtered to `bacia=8` → **288 stations** with lat/lon. |
| **Hydrography (edges)** | ANA **BHO 2017 5K — trecho de drenagem** | Official national river network; drainage segments carrying downstream topology. |

**BHO service endpoint (public ArcGIS FeatureServer, SNIRH):**

```
https://www.snirh.gov.br/arcgis/rest/services/SPR/BHO2017_5K_TRECHODRENAGEM/FeatureServer/0
```

The national layer holds **464,067** drainage segments. We download only the Rio
Grande do Sul region (bounding box), giving **~30,000** segments.

### BHO fields used and how they map to the graph

| BHO field | Meaning | Used as |
| --- | --- | --- |
| `COTRECHO` | Unique segment id | Internal reach id |
| `NUTRJUS` | `COTRECHO` of the next **downstream** segment (`0` = mouth) | **Topology** (downstream pointer) |
| `NUDISTBACT` | Distance along drainage to the basin outlet (km) | `dist_foz_km` (and edge weight) |
| `NUAREAMONT` | Upstream drainage area (km²) | `upland_skm` |
| `NUSTRAHLER` | Strahler stream order | `strahler` |
| `COBACIA` | Otto/Pfafstetter basin code | Region context (`8…` = Guaíba/Patos) |
| *(derived)* | Terminal segment reached by walking `NUTRJUS` | `main_riv` (river-system id) |

This is the key advantage over HydroRIVERS: BHO provides **official downstream
topology** (`NUTRJUS`) and consistent hydrological attributes, and its drainage
**passes through the Lago Guaíba / Lagoa dos Patos** natively.

---

## 3. Step 1 — Fetch the BHO hydrography (`baixar_bho.py`)

The script queries the FeatureServer and writes a single GeoJSON cache.

- **Spatial filter:** RS bounding box `(-58.5, -34.6, -49.0, -26.5)` in EPSG:4326.
- **Fields requested:** `COTRECHO, NUTRJUS, COBACIA, NUDISTBACT, NUAREAMONT, NUSTRAHLER, NUCOMPTREC`.
- **Reprojection:** `outSR=4326` so geometry comes back as lon/lat.
- **Pagination:** the server caps responses (`maxRecordCount=1000`) and may return
  short pages under transfer limits, so the script pages by `resultOffset`,
  advancing by the number of features actually returned until the known count is
  reached. Stable ordering via `orderByFields=COTRECHO`.
- **Robustness:** retries with backoff; relaxed TLS verification (public,
  read-only data, mirroring `render_satellite.py`).

**Result:** `bho/bho_rs_trechos.geojson` — **30,203 segments (~357 MB)**,
LineString geometry in lon/lat. (This file is a cache; the build reads it
directly and it does not need to be regenerated unless the region changes.)

```bash
uv run python baixar_bho.py            # → bho/bho_rs_trechos.geojson
```

---

## 4. Step 2 — Build the graph (`build_graph_bho.py`)

Algorithm:

1. **Load stations.** Read `estacoes_rs.csv`, keep `bacia=8`, de-duplicate by
   station code → 288 candidate nodes with lat/lon. (`carregar_estacoes_bacia8`)

2. **Read BHO.** Parse the GeoJSON into two structures
   (`ler_bho`): a `next_down` map (`COTRECHO → NUTRJUS`) for topology, and a
   `reach` map holding each segment's geometry and attributes. Segments on the
   lake/coast occasionally have null numeric fields (no upstream distance/area);
   these are treated as `0` (they sit at the mouth, distance ≈ 0).

3. **River-system id (`calcular_main_riv`).** For each segment, walk `NUTRJUS`
   downstream until the mouth (`0`) or the edge of the downloaded region; the
   terminal segment's `COTRECHO` becomes the system id (`main_riv`), memoized.

4. **Snap stations to the network (`snap_estacoes`).** Each station is attached
   to the nearest drainage segment using point-to-polyline distance in a local
   km projection (longitude/latitude scaled by `KX`/`KY` around `LAT0 = -30°`).
   A bounding-box pre-filter (~0.15°) limits the candidate segments per station.
   The station inherits that segment's `dist_foz_km`, `main_riv`, `strahler`,
   `upland_skm`.

5. **Build directed edges (`construir_arestas`).** From each station's segment,
   follow `NUTRJUS` downstream until the first segment that another station
   snapped to → emit a directed edge `upstream → downstream`. The edge **weight**
   is the flow distance `max(0, dist_foz[upstream] − dist_foz[downstream])` km.
   Because BHO routes through the Guaíba/Patos, these chains reach the outlet
   natively — **no lagoon fallback, no manual station exclusion lists** (unlike
   the HydroRIVERS prototype).

6. **Keep the outlet component (`filtrar_componente_final`).** Using union-find
   over the edges (undirected), keep only the stations connected to the
   designated outlet station (`--estacao-final`, default `87450004`), and reindex
   the node order. Stations not connected to the outlet are dropped.

7. **Export.**
   - `grafo_bho.npz`: `A` (N×N adjacency, uint8), `W` (weights), `nodes`
     (station codes), `lat`, `lon`, `snap_dist_km`, `dist_foz_km`, `main_riv`,
     `strahler`, `upland_skm`. **Same schema as the HydroRIVERS graph**, so the
     existing renderers work unchanged.
   - `fluxo_arestas_bho.geojson`: edges as LineStrings following the rivers, for
     visualization (properties `montante`, `jusante`, `dist_km`, `fallback`).

```bash
uv run python build_graph_bho.py       # → grafo_bho.npz + fluxo_arestas_bho.geojson
```

---

## 5. Results

| Metric | Value |
| --- | --- |
| Candidate stations (`bacia=8`) | 288 |
| BHO segments read (RS) | 30,203 (29,420 with geometry) |
| **Nodes in final graph** | **257** (connected to outlet) |
| **Directed edges** | **256** |
| Connected components | **1** |
| Distinct river systems | **1** (unified Guaíba/Patos) |
| Stations with a downstream neighbor | 256 / 257 (one terminal outlet) |
| Snap distance — median / p90 / max | **0.23 / 0.97 / 4.61 km** |
| Snap distance > 2 km (to review) | 16 stations |

The graph is a single tree-like component draining to one outlet, built purely
from official topology.

### Comparison with the HydroRIVERS prototype

| | HydroRIVERS (`grafo_hydrorivers.npz`) | **BHO 2017 (`grafo_bho.npz`)** |
| --- | --- | --- |
| Nodes | 251 | **257** (superset: all 251 kept + 6 new) |
| Edges | 256 | 256 |
| Connected components | required manual lagoon hacks | **1, native** |
| Lake-outlet handling | `EXCLUIR_FALLBACK` / `FORCAR_FALLBACK` lists + OSM lake polygon + distance heuristic | **none needed** |
| Snap precision | coarser (~4 km reaches) | **finer (5K; median 0.23 km)** |
| Topology source | `NEXT_DOWN` (global, coarse) | `NUTRJUS` (official, ottocoded) |

The BHO graph **recovered 6 nodes the prototype had dropped** — all lake/outlet
stations on Lago Guaíba around Porto Alegre (e.g. Usina do Gasômetro, Ipanema,
Barra do Ribeiro, Terminal Catsul). Four of them are exactly the codes the old
script hardcoded into `EXCLUIR_FALLBACK`, because HydroRIVERS has no drainage
through the lake body; BHO does, so they now snap and connect natively.

### Data availability for the 6 new nodes

Cross-referenced against the mask `M` in `dataset_historico.npz` (52,632 hourly
steps; telemetry features only):

| Code | Station | Any-telemetry observed |
| --- | --- | --- |
| `87242000` | Terminal Catsul Guaíba | 77.1% (usable) |
| `87450020` | Usina do Gasômetro | 26.6% (partial) |
| `87450100` | Ipiranga (Arroio Dilúvio) | 2.2% (trace) |
| `03051043` | Porto Alegre – CPRM | 0% |
| `87460120` | Ipanema | 0% |
| `87480000` | Barra do Ribeiro | 0% |

Empty-telemetry nodes are **not specific to these six** — across all 257 BHO
nodes, 88 have 0% telemetry (median coverage ~51.5%). The graph deliberately
keeps such nodes (they still carry Open-Meteo wind and matter for topology). The
keep-vs-drop decision is the project-wide coverage-threshold question (roadmap
§F), not specific to this change.

---

## 6. Visual outputs

Generated with the existing renderers against the new files:

```bash
uv run python render_graph.py     --npz grafo_bho.npz --geojson fluxo_arestas_bho.geojson \
                                  --saida grafo_bho_estrutura.png --fonte "ANA BHO 2017 5K"
uv run python render_satellite.py --npz grafo_bho.npz --geojson fluxo_arestas_bho.geojson \
                                  --saida grafo_bho_satelite.png
```

- `grafo_bho_estrutura.png` — schematic flow network, nodes colored by distance
  to outlet, outlet highlighted.
- `grafo_bho_satelite.png` — the same network overlaid on Esri satellite imagery.
- `nos_novos_bho.png` — zoom on Lago Guaíba highlighting the 6 new nodes.

A small, non-breaking change was made to `render_graph.py`: an optional
`--fonte` flag (default `"HydroRIVERS"`) so the title reflects the data source.

---

## 7. Reproduction (full pipeline)

```bash
uv run python baixar_bho.py            # 1. fetch BHO hydrography (cached)
uv run python build_graph_bho.py       # 2. build grafo_bho.npz + geojson
uv run python render_graph.py     --npz grafo_bho.npz --geojson fluxo_arestas_bho.geojson --saida grafo_bho_estrutura.png --fonte "ANA BHO 2017 5K"
uv run python render_satellite.py --npz grafo_bho.npz --geojson fluxo_arestas_bho.geojson --saida grafo_bho_satelite.png
```

---

## 8. Limitations and open items

- **16 stations snap > 2 km** from their nearest segment (max 4.61 km); worth a
  visual check for any misassigned edges.
- **31 of 288 `bacia=8` stations were dropped** as not connected to the outlet
  component (mostly isolated/coastal). They are excluded from `grafo_bho.npz`.
- **Coverage threshold not yet decided:** empty-telemetry nodes (88/257) are kept
  as graph/wind-only; a project-level rule should decide whether to drop them.
- **Region clip:** the graph relies on the RS bounding box capturing the full
  downstream chains for `bacia=8`. This holds for the Guaíba/Patos basin (its
  outlet is inside the box); extending to other basins would require widening the
  fetch.
- **BHO version:** BHO 2017 5K is marked "desatualizada" in the ANA catalog (a
  newer Base Hidrográfica Atlas-Estudos exists). 2017 5K was chosen as the
  roadmap's named primary source; migrating to the newer base is a possible
  future step.
