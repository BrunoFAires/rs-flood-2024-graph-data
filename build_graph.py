"""
Constrói o grafo direcionado de estações para o STGNN (TODO grupo C) usando a
rede hidrográfica HydroRIVERS (HydroSHEDS) — protótipo, alternativa à BHO da ANA.

Lógica:
  1. carrega as estações bacia=8 (Guaíba/Patos) com lat/lon;
  2. lê os trechos do HydroRIVERS (campo NEXT_DOWN = próximo trecho de jusante,
     0 = foz; DIST_DN_KM = distância à foz; MAIN_RIV = id do sistema fluvial);
  3. "snap" de cada estação ao trecho mais próximo;
  4. de cada estação, caminha para jusante até a 1ª outra estação -> aresta dirigida
     (montante -> jusante), peso = distância de escoamento (km) via DIST_DN_KM;
  5. exporta a adjacência A (N x N), atributos dos nós, e um GeoJSON dos caminhos.

Saídas:
  grafo_hydrorivers.npz  -> A, nodes, lat, lon, snap_dist_km, dist_foz_km, main_riv, strahler
  fluxo_arestas.geojson  -> LineStrings seguindo os rios (para visualização)

Requer: pyshp, numpy  (rodar com: uv run python build_graph.py).
"""

import argparse
import csv
import glob
import json
import math
from collections import defaultdict

import numpy as np
import shapefile

# Caixa envolvente do RS (com margem) para filtrar trechos candidatos ao snap.
RS_BBOX = (-58.5, -34.6, -49.0, -26.5)  # xmin, ymin, xmax, ymax
LAT0 = -30.0
KX = 111.32 * math.cos(math.radians(LAT0))  # km por grau de longitude
KY = 110.57                                 # km por grau de latitude


def carregar_estacoes_bacia8(csv_path):
    nodes = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            if (r.get("bacia") or "").strip() != "8":
                continue
            try:
                lon = float(r["longitude"]); lat = float(r["latitude"])
            except (TypeError, ValueError):
                continue
            nodes.append({"cod": r["cod_estacao"], "lat": lat, "lon": lon,
                          "nome": r["nome_estacao"], "rio": r["nome_rio"]})
    return nodes


def bbox_intersecta(b, B):
    return not (b[2] < B[0] or b[0] > B[2] or b[3] < B[1] or b[1] > B[3])


def ler_hydrorivers(shp_path):
    """
    Uma passada streaming: next_down completo (todos os trechos) +
    geometria/atributos só dos trechos dentro da bbox do RS (para snap/caminho).
    """
    sf = shapefile.Reader(shp_path)
    campos = [fld[0] for fld in sf.fields[1:]]
    ix = {c: i for i, c in enumerate(campos)}

    next_down = {}                       # hid -> hid de jusante (0 = foz)
    reach = {}                           # hid -> dados do trecho (só bbox RS)
    total = 0
    for sr in sf.iterShapeRecords():
        rec = sr.record
        hid = int(rec[ix["HYRIV_ID"]])
        nd = int(rec[ix["NEXT_DOWN"]])
        next_down[hid] = nd
        total += 1
        bbox = sr.shape.bbox
        if bbox_intersecta(bbox, RS_BBOX):
            pts = np.asarray(sr.shape.points, dtype=np.float64)  # (k,2) lon,lat
            reach[hid] = {
                "pts": pts,
                "bbox": bbox,
                "dist_dn": float(rec[ix["DIST_DN_KM"]]),
                "main_riv": int(rec[ix["MAIN_RIV"]]),
                "strahler": int(rec[ix["ORD_STRA"]]),
                "upland": float(rec[ix["UPLAND_SKM"]]),
            }
    return next_down, reach, total


def _scale(pts):
    out = np.empty_like(pts)
    out[:, 0] = pts[:, 0] * KX
    out[:, 1] = pts[:, 1] * KY
    return out


def dist_ponto_polilinha(px, py, verts):
    """Menor distância (km) de um ponto a uma polilinha (vértices já em km)."""
    if len(verts) == 1:
        return math.hypot(verts[0, 0] - px, verts[0, 1] - py)
    A = verts[:-1]; B = verts[1:]
    AB = B - A
    AP = np.column_stack((px - A[:, 0], py - A[:, 1]))
    denom = (AB ** 2).sum(1)
    t = np.where(denom > 0, (AP * AB).sum(1) / denom, 0.0)
    t = np.clip(t, 0.0, 1.0)
    proj = A + AB * t[:, None]
    return float(np.hypot(proj[:, 0] - px, proj[:, 1] - py).min())


def snap_estacoes(nodes, reach):
    hids = list(reach.keys())
    bb = np.array([reach[h]["bbox"] for h in hids])          # (K,4)
    verts_km = [_scale(reach[h]["pts"]) for h in hids]
    for n in nodes:
        px, py = n["lon"] * KX, n["lat"] * KY
        # candidatos: trechos cuja bbox está a ~0.15° do ponto
        m = 0.15
        cand = np.where((bb[:, 0] - m <= n["lon"]) & (bb[:, 2] + m >= n["lon"]) &
                        (bb[:, 1] - m <= n["lat"]) & (bb[:, 3] + m >= n["lat"]))[0]
        if len(cand) == 0:
            cand = range(len(hids))
        best_d, best_h = float("inf"), None
        for i in cand:
            d = dist_ponto_polilinha(px, py, verts_km[i])
            if d < best_d:
                best_d, best_h = d, hids[i]
        n["snap_hid"] = best_h
        n["snap_dist_km"] = best_d
        n["dist_dn"] = reach[best_h]["dist_dn"]
        n["main_riv"] = reach[best_h]["main_riv"]
        n["strahler"] = reach[best_h]["strahler"]
        n["upland"] = reach[best_h]["upland"]


def construir_arestas(nodes, next_down, max_passos=200000):
    reach_to_stations = defaultdict(list)
    for i, n in enumerate(nodes):
        reach_to_stations[n["snap_hid"]].append(i)

    arestas = []
    for i, n in enumerate(nodes):
        r = next_down.get(n["snap_hid"], 0)
        caminho_reaches = [n["snap_hid"]]
        passos = 0
        achou = None
        while r != 0 and passos < max_passos:
            caminho_reaches.append(r)
            if r in reach_to_stations:
                outros = [j for j in reach_to_stations[r] if j != i]
                if outros:
                    achou = outros[0]
                    break
            r = next_down.get(r, 0)
            passos += 1
        if achou is not None:
            v = nodes[achou]
            peso = max(0.0, n["dist_dn"] - v["dist_dn"])
            arestas.append((i, achou, peso, list(caminho_reaches)))
    return arestas


def exportar_geojson(nodes, arestas, reach, path):
    feats = []
    for u, v, peso, reaches in arestas:
        coords = []
        for h in reaches:
            if h in reach:
                coords.extend([[float(x), float(y)] for x, y in reach[h]["pts"]])
        if len(coords) < 2:
            coords = [[nodes[u]["lon"], nodes[u]["lat"]],
                      [nodes[v]["lon"], nodes[v]["lat"]]]
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"montante": nodes[u]["cod"], "jusante": nodes[v]["cod"],
                           "dist_km": round(peso, 2)},
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)


def componentes(n, arestas):
    pai = list(range(n))
    def find(x):
        while pai[x] != x:
            pai[x] = pai[pai[x]]; x = pai[x]
        return x
    for u, v, *_ in arestas:
        pai[find(u)] = find(v)
    return len({find(x) for x in range(n)})


def main():
    ap = argparse.ArgumentParser(description="Grafo de estações via HydroRIVERS (protótipo grupo C).")
    ap.add_argument("--estacoes", default="estacoes_rs.csv")
    ap.add_argument("--shp", default=None, help="HydroRIVERS .shp (auto-detecta se vazio).")
    ap.add_argument("--saida-npz", default="grafo_hydrorivers.npz")
    ap.add_argument("--saida-geojson", default="fluxo_arestas.geojson")
    args = ap.parse_args()

    shp = args.shp
    if not shp:
        achados = glob.glob("hydrorivers/**/*.shp", recursive=True)
        if not achados:
            raise SystemExit(
                "HydroRIVERS não encontrado: baixe o shapefile South America "
                "(HydroRIVERS_v10_sa) de https://www.hydrosheds.org/products/hydrorivers "
                "e extraia em ./hydrorivers/, ou informe o caminho via --shp."
            )
        shp = achados[0]
    nodes = carregar_estacoes_bacia8(args.estacoes)
    print(f"Estações bacia=8: {len(nodes)}")
    print(f"Lendo HydroRIVERS: {shp} (pode levar 1-2 min)...")
    next_down, reach, total = ler_hydrorivers(shp)
    print(f"Trechos totais (SA): {total}  |  na bbox do RS: {len(reach)}")

    snap_estacoes(nodes, reach)
    sd = np.array([n["snap_dist_km"] for n in nodes])
    print(f"Snap dist (km): mediana {np.median(sd):.2f}  p90 {np.percentile(sd,90):.2f}  "
          f"max {sd.max():.2f}  | suspeitos >2km: {(sd>2).sum()}")

    arestas = construir_arestas(nodes, next_down)
    N = len(nodes)
    A = np.zeros((N, N), dtype=np.uint8)
    W = np.zeros((N, N), dtype=np.float32)
    for u, v, peso, _ in arestas:
        A[u, v] = 1
        W[u, v] = peso
    com_jusante = len({u for u, *_ in arestas})
    ncomp = componentes(N, arestas)
    sist = len({n["main_riv"] for n in nodes})
    print(f"Arestas (montante->jusante): {len(arestas)}")
    print(f"Estações com vizinho de jusante: {com_jusante}/{N}")
    print(f"Componentes conexas (via arestas): {ncomp}")
    print(f"Sistemas fluviais distintos (MAIN_RIV): {sist}")

    np.savez_compressed(
        args.saida_npz,
        A=A, W=W,
        nodes=np.array([n["cod"] for n in nodes]),
        lat=np.array([n["lat"] for n in nodes], dtype=np.float32),
        lon=np.array([n["lon"] for n in nodes], dtype=np.float32),
        snap_dist_km=sd.astype(np.float32),
        dist_foz_km=np.array([n["dist_dn"] for n in nodes], dtype=np.float32),
        main_riv=np.array([n["main_riv"] for n in nodes]),
        strahler=np.array([n["strahler"] for n in nodes], dtype=np.int16),
        upland_skm=np.array([n["upland"] for n in nodes], dtype=np.float32),
    )
    exportar_geojson(nodes, arestas, reach, args.saida_geojson)
    print(f"\nSalvo: {args.saida_npz}  e  {args.saida_geojson}")


if __name__ == "__main__":
    main()
