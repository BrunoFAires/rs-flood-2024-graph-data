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
import urllib.parse
import urllib.request
from collections import defaultdict

import numpy as np
import shapefile
from shapely.affinity import scale as shp_scale
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import linemerge, polygonize, unary_union

# Caixa envolvente do RS (com margem) para filtrar trechos candidatos ao snap.
RS_BBOX = (-58.5, -34.6, -49.0, -26.5)  # xmin, ymin, xmax, ymax

# Estações dentro/próximas do polígono do Lago Guaíba que não devem receber fallback
# (87450004) e a estação na saída do Guaíba para a Lagoa dos Patos (90000001)
# representam o lago no grafo.
EXCLUIR_FALLBACK = {"87450004", "87450020", "87460120", "87480000"}

# Estação que representa a saída do Guaíba para a Lagoa dos Patos
FORCAR_FALLBACK = {"90000001"}

LAT0 = -30.0
KX = 111.32 * math.cos(math.radians(LAT0))  # km por grau de longitude
KY = 110.57                                 # km por grau de latitude


def carregar_estacoes_bacia8(csv_path):
    nodes = []
    vistos = set()
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            if (r.get("bacia") or "").strip() != "8":
                continue
            cod = r["cod_estacao"]
            if cod in vistos:
                continue
            try:
                lon = float(r["longitude"]); lat = float(r["latitude"])
            except (TypeError, ValueError):
                continue
            vistos.add(cod)
            nodes.append({"cod": cod, "lat": lat, "lon": lon,
                          "nome": r["nome_estacao"], "rio": r["nome_rio"]})
    return nodes


OVERPASS_API = "https://overpass-api.de/api/interpreter"


def baixar_lagoa_guaiba(path, rel_id=87400, nome="Lago Guaíba", timeout=150):
    """Baixa o polígono do Lago Guaíba e salva como GeoJSON em `path`."""
    query = f"[out:json][timeout:120];rel({rel_id});(._;>;);out geom;"
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(
        OVERPASS_API, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "build_graph.py (STGNN bacia8)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())

    ways = [LineString([(p["lon"], p["lat"]) for p in el["geometry"]])
            for el in data["elements"]
            if el["type"] == "way" and "geometry" in el]
    if not ways:
        raise RuntimeError(f"Nenhuma geometria retornada pelo Overpass para a relation {rel_id}.")

    poligonos = list(polygonize(linemerge(unary_union(ways))))
    poligono = max(poligonos, key=lambda p: p.area)

    gj = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": mapping(poligono),
        "properties": {"name": nome, "osm_id": rel_id},
    }]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(gj, f)


def carregar_lagoas_km(geojson_path, excluir_nomes=("Lagoa dos Patos",)):
    """Polígonos do Lago Guaíba / Lagoa dos Patos (OSM), escalados para km (mesma
    projeção local usada em _scale) para medir distância da foz dos rios.
    Nomes em `excluir_nomes` são desconsiderados"""
    gj = json.load(open(geojson_path, encoding="utf-8"))
    return [shp_scale(shape(f["geometry"]), xfact=KX, yfact=KY, origin=(0, 0))
            for f in gj["features"]
            if f["properties"].get("name") not in excluir_nomes]


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


def construir_arestas(nodes, next_down, reach, max_passos=200000,
                       estacao_final=None, lagoas_km=None, limiar_lagoa_km=5.0):
    reach_to_stations = defaultdict(list)
    for i, n in enumerate(nodes):
        reach_to_stations[n["snap_hid"]].append(i)

    final_idx = None
    if estacao_final is not None:
        for i, n in enumerate(nodes):
            if n["cod"] == estacao_final:
                final_idx = i
                break

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
            arestas.append((i, achou, peso, list(caminho_reaches), False))
            continue

        # sem outra estação a jusante: tenta fallback até a estação final, se
        if final_idx is None or final_idx == i or lagoas_km is None:
            continue
        if n["cod"] in EXCLUIR_FALLBACK:
            continue
        ultimo = caminho_reaches[-1]
        if ultimo not in reach:
            continue
        pts_km = _scale(reach[ultimo]["pts"])
        px, py = pts_km[-1]
        ponto = Point(px, py)
        dentro_limiar = any(poly.distance(ponto) <= limiar_lagoa_km for poly in lagoas_km)
        if not dentro_limiar and n["cod"] not in FORCAR_FALLBACK:
            continue
        v = nodes[final_idx]
        # distância geográfica real entre as estações (não DIST_DN_KM, que não
        # é comparável entre sistemas fluviais distintos do HydroRIVERS).
        peso = math.hypot((v["lon"] - n["lon"]) * KX, (v["lat"] - n["lat"]) * KY)
        if n["cod"] in FORCAR_FALLBACK:
            # saída do Guaíba para a Lagoa dos Patos
            arestas.append((final_idx, i, peso, list(caminho_reaches), True))
        else:
            arestas.append((i, final_idx, peso, list(caminho_reaches), True))
    return arestas


def exportar_geojson(nodes, arestas, reach, path, limiar_conector_km=40.0):
    feats = []
    for u, v, peso, reaches, fallback in arestas:
        coords = []
        for h in reaches:
            if h in reach:
                coords.extend([[float(x), float(y)] for x, y in reach[h]["pts"]])
        forcar = (nodes[u]["cod"] in FORCAR_FALLBACK
                  or nodes[v]["cod"] in FORCAR_FALLBACK)
        if len(coords) < 2 or (fallback and forcar):
            coords = [[nodes[u]["lon"], nodes[u]["lat"]],
                      [nodes[v]["lon"], nodes[v]["lat"]]]
        elif fallback:
            # conecta o início do caminho à estação de montante, se próximo
            ux0, uy0 = coords[0]
            d0 = math.hypot((nodes[u]["lon"] - ux0) * KX, (nodes[u]["lat"] - uy0) * KY)
            if d0 <= limiar_conector_km:
                coords.insert(0, [nodes[u]["lon"], nodes[u]["lat"]])
            # conecta o fim do caminho à estação de jusante, se próximo
            ux1, uy1 = coords[-1]
            d1 = math.hypot((nodes[v]["lon"] - ux1) * KX, (nodes[v]["lat"] - uy1) * KY)
            if d1 <= limiar_conector_km:
                coords.append([nodes[v]["lon"], nodes[v]["lat"]])
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"montante": nodes[u]["cod"], "jusante": nodes[v]["cod"],
                           "dist_km": round(peso, 2), "fallback": fallback},
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)


def filtrar_componente_final(nodes, arestas, sd, estacao_final):
    """Mantém apenas as estações que pertencem ao mesmo componente (ligações
    em qualquer direção) da estação final, descartando o resto."""
    n = len(nodes)
    pai = list(range(n))
    def find(x):
        while pai[x] != x:
            pai[x] = pai[pai[x]]; x = pai[x]
        return x
    for u, v, *_ in arestas:
        ra, rb = find(u), find(v)
        if ra != rb:
            pai[ra] = rb
    final_idx = next(i for i, nd in enumerate(nodes) if nd["cod"] == estacao_final)
    raiz = find(final_idx)
    keep = [i for i in range(n) if find(i) == raiz]
    remap = {old: new for new, old in enumerate(keep)}
    nodes2 = [nodes[i] for i in keep]
    sd2 = sd[keep]
    arestas2 = [(remap[u], remap[v], peso, reaches, fb)
                for u, v, peso, reaches, fb in arestas
                if u in remap and v in remap]
    return nodes2, arestas2, sd2


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
    ap.add_argument("--estacao-final", default="87242000",
                    help="Código da estação considerada exutório do grafo.")
    ap.add_argument("--lagoas", default="lagoa_guaiba.geojson",
                    help="GeoJSON com os polígonos do Lago Guaíba/Lagoa dos Patos.")
    ap.add_argument("--limiar-lagoa-km", type=float, default=5.0,
                    help="Distância máx. (km) do fim de um rio até a lagoa para criar fallback.")
    ap.add_argument("--limiar-conector-km", type=float, default=40.0,
                    help="Distância máx. (km) para desenhar o conector de fallback no GeoJSON.")
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

    lagoas_km = None
    if args.lagoas:
        if not glob.glob(args.lagoas):
            print(f"{args.lagoas} não encontrado, baixando Lago Guaíba via Overpass (OSM)...")
            try:
                baixar_lagoa_guaiba(args.lagoas)
            except (OSError, RuntimeError) as e:
                print(f"Aviso: falha ao baixar {args.lagoas} ({e}), sem fallback via lagoa.")
        if glob.glob(args.lagoas):
            lagoas_km = carregar_lagoas_km(args.lagoas)

    arestas = construir_arestas(nodes, next_down, reach,
                                 estacao_final=args.estacao_final,
                                 lagoas_km=lagoas_km,
                                 limiar_lagoa_km=args.limiar_lagoa_km)

    n_antes = len(nodes)
    nodes, arestas, sd = filtrar_componente_final(nodes, arestas, sd, args.estacao_final)
    print(f"Estações conectadas (direta ou indiretamente) à estação final: "
          f"{len(nodes)}/{n_antes}")

    N = len(nodes)
    A = np.zeros((N, N), dtype=np.uint8)
    W = np.zeros((N, N), dtype=np.float32)
    for u, v, peso, _, _ in arestas:
        A[u, v] = 1
        W[u, v] = peso
    com_jusante = len({u for u, *_ in arestas})
    nfallback = sum(1 for *_, fb in arestas if fb)
    ncomp = componentes(N, arestas)
    sist = len({n["main_riv"] for n in nodes})
    print(f"Arestas (montante->jusante): {len(arestas)}  (fallback p/ lagoa: {nfallback})")
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
    exportar_geojson(nodes, arestas, reach, args.saida_geojson,
                     limiar_conector_km=args.limiar_conector_km)
    print(f"\nSalvo: {args.saida_npz}  e  {args.saida_geojson}")


if __name__ == "__main__":
    main()
