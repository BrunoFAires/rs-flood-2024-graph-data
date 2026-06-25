"""
Constrói o grafo direcionado de estações para o STGNN usando a rede hidrográfica
oficial da ANA — BHO 2017 5K (Base Hidrográfica Ottocodificada), a fonte
*primária* do roadmap (substitui o protótipo via HydroRIVERS de build_graph.py).

Diferenças vs. build_graph.py (HydroRIVERS):
  * topologia oficial via NUTRJUS (COTRECHO do próximo trecho de jusante);
  * geometria mais fina (5K), melhor "snap" das estações;
  * a drenagem da BHO atravessa o Lago Guaíba / Lagoa dos Patos nativamente, então
    NÃO são necessários os "hacks" de fallback por lagoa (EXCLUIR/FORCAR/polígono
    OSM): o caminho de jusante alcança o exutório sozinho.

Lógica:
  1. carrega as estações bacia=8 (Guaíba/Patos) com lat/lon;
  2. lê os trechos da BHO no RS (NUTRJUS = jusante; NUDISTBACT = dist. à foz da
     bacia; NUAREAMONT = área de drenagem a montante; NUSTRAHLER = ordem);
  3. "snap" de cada estação ao trecho mais próximo;
  4. de cada estação, caminha para jusante (NUTRJUS) até a 1ª outra estação ->
     aresta dirigida (montante -> jusante), peso = distância de escoamento (km);
  5. exporta a adjacência A (N x N), atributos dos nós e um GeoJSON dos caminhos.

Entrada: bho/bho_rs_trechos.geojson (baixe com: uv run python baixar_bho.py).
Saídas (mesmo schema de build_graph.py, p/ os renderizadores funcionarem igual):
  grafo_bho.npz          -> A, W, nodes, lat, lon, snap_dist_km, dist_foz_km,
                            main_riv, strahler, upland_skm
  fluxo_arestas_bho.geojson -> LineStrings seguindo os rios (para visualização)

Requer: numpy  (rodar com: uv run python build_graph_bho.py).
"""

import argparse
import csv
import json
import math
from collections import defaultdict

import numpy as np

# Caixa envolvente do RS (com margem), igual a build_graph.py.
RS_BBOX = (-58.5, -34.6, -49.0, -26.5)  # xmin, ymin, xmax, ymax

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


def _coords_para_pts(geom):
    """Achata a geometria (Line/MultiLineString) num array (k,2) lon,lat."""
    t = geom["type"]; c = geom["coordinates"]
    if t == "LineString":
        seqs = [c]
    elif t == "MultiLineString":
        seqs = c
    else:
        return None
    pts = [p for seq in seqs for p in seq]
    if len(pts) < 1:
        return None
    return np.asarray(pts, dtype=np.float64)[:, :2]


def _f(x, default=0.0):
    return default if x is None else float(x)


def _i(x, default=0):
    return default if x is None else int(x)


def ler_bho(geojson_path):
    """Lê os trechos da BHO: next_down (COTRECHO->NUTRJUS) e atributos/geometria.
    Trechos de lagoa/costa às vezes têm campos nulos (sem dist./área a montante);
    tratamos como 0 (estão na foz, distância ~0)."""
    with open(geojson_path, encoding="utf-8") as f:
        gj = json.load(f)
    next_down = {}      # cotrecho -> cotrecho de jusante (NUTRJUS; 0 = foz)
    reach = {}          # cotrecho -> dados do trecho
    for ft in gj["features"]:
        p = ft["properties"]
        hid = _i(p.get("COTRECHO"))
        next_down[hid] = _i(p.get("NUTRJUS"))
        pts = _coords_para_pts(ft["geometry"])
        if pts is None:
            continue
        bbox = (float(pts[:, 0].min()), float(pts[:, 1].min()),
                float(pts[:, 0].max()), float(pts[:, 1].max()))
        reach[hid] = {
            "pts": pts,
            "bbox": bbox,
            "dist_dn": _f(p.get("NUDISTBACT")),
            "cobacia": str(p.get("COBACIA") or ""),
            "strahler": _i(p.get("NUSTRAHLER")),
            "upland": _f(p.get("NUAREAMONT")),
        }
    return next_down, reach, len(gj["features"])


def calcular_main_riv(reach, next_down):
    """Id do sistema fluvial de cada trecho = COTRECHO do trecho terminal
    alcançado descendo por NUTRJUS (até NUTRJUS=0 ou sair da região)."""
    memo = {}
    for h0 in reach:
        path = []
        cur = h0
        while cur in reach and cur not in memo:
            path.append(cur)
            nd = next_down.get(cur, 0)
            if nd == 0 or nd not in reach:
                root = cur                 # terminal dentro dos nossos dados
                break
            cur = nd
        else:
            root = memo.get(cur, cur)
        for h in path:
            memo[h] = root
    return memo


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


def snap_estacoes(nodes, reach, main_riv):
    hids = list(reach.keys())
    bb = np.array([reach[h]["bbox"] for h in hids])          # (K,4)
    verts_km = [_scale(reach[h]["pts"]) for h in hids]
    for n in nodes:
        px, py = n["lon"] * KX, n["lat"] * KY
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
        n["main_riv"] = main_riv.get(best_h, best_h)
        n["strahler"] = reach[best_h]["strahler"]
        n["upland"] = reach[best_h]["upland"]


def construir_arestas(nodes, next_down, max_passos=200000):
    """De cada estação, caminha por NUTRJUS até a 1ª outra estação a jusante."""
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
            nd = next_down.get(r, 0)
            if nd == r:                    # proteção contra autorreferência
                break
            r = nd
            passos += 1

        if achou is None:
            continue                       # sem estação a jusante (exutório local)
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
                           "dist_km": round(peso, 2), "fallback": False},
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)


def filtrar_componente_final(nodes, arestas, sd, estacao_final):
    """Mantém só as estações no mesmo componente (ligações em qualquer direção)
    da estação final, descartando o resto."""
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
    final_idx = next((i for i, nd in enumerate(nodes) if nd["cod"] == estacao_final), None)
    if final_idx is None:
        print(f"Aviso: estação final {estacao_final} não está entre os nós; "
              f"mantendo o maior componente.")
        from collections import Counter
        raiz = Counter(find(i) for i in range(n)).most_common(1)[0][0]
    else:
        raiz = find(final_idx)
    keep = [i for i in range(n) if find(i) == raiz]
    remap = {old: new for new, old in enumerate(keep)}
    nodes2 = [nodes[i] for i in keep]
    sd2 = sd[keep]
    arestas2 = [(remap[u], remap[v], peso, reaches)
                for u, v, peso, reaches in arestas
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
    ap = argparse.ArgumentParser(description="Grafo de estações via ANA BHO 2017 5K (oficial).")
    ap.add_argument("--estacoes", default="estacoes_rs.csv")
    ap.add_argument("--bho", default="bho/bho_rs_trechos.geojson",
                    help="GeoJSON dos trechos BHO (baixe com baixar_bho.py).")
    ap.add_argument("--saida-npz", default="grafo_bho.npz")
    ap.add_argument("--saida-geojson", default="fluxo_arestas_bho.geojson")
    ap.add_argument("--estacao-final", default="87450004",
                    help="Código da estação considerada exutório do grafo.")
    args = ap.parse_args()

    nodes = carregar_estacoes_bacia8(args.estacoes)
    print(f"Estações bacia=8: {len(nodes)}")
    print(f"Lendo BHO: {args.bho} (arquivo grande, pode levar ~30 s)...")
    next_down, reach, total = ler_bho(args.bho)
    print(f"Trechos BHO lidos: {total}  |  com geometria: {len(reach)}")

    main_riv = calcular_main_riv(reach, next_down)
    snap_estacoes(nodes, reach, main_riv)
    sd = np.array([n["snap_dist_km"] for n in nodes])
    print(f"Snap dist (km): mediana {np.median(sd):.2f}  p90 {np.percentile(sd,90):.2f}  "
          f"max {sd.max():.2f}  | suspeitos >2km: {(sd>2).sum()}")

    arestas = construir_arestas(nodes, next_down)

    n_antes = len(nodes)
    nodes, arestas, sd = filtrar_componente_final(nodes, arestas, sd, args.estacao_final)
    print(f"Estações conectadas (direta ou indiretamente) à estação final: "
          f"{len(nodes)}/{n_antes}")

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
    print(f"Sistemas fluviais distintos (terminal NUTRJUS): {sist}")

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
