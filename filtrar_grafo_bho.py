"""
Filtra o grafo BHO removendo estações sem dados (ou abaixo de um limiar de
cobertura) e RECONECTA as arestas, preservando o sentido do fluxo.

Por que "reconectar" e não só apagar linhas/colunas: o grafo é uma in-floresta
(cada estação aponta para a 1ª estação de jusante). Se removermos um nó
intermediário, suas montantes ficariam sem destino. Então, para cada nó removido,
ligamos suas montantes ao 1º nó SOBREVIVENTE de jusante (concatenando a geometria
dos trechos e somando as distâncias). Se uma cadeia termina num sorvedouro
removido (ex.: o nó artificial 90000001), o nó órfão é religado ao sobrevivente
de jusante geograficamente mais próximo (aresta de fallback), garantindo um único
componente conectado ao exutório.

Cobertura = % de horas com QUALQUER telemetria (nível/chuva/vazão) observada,
medida na máscara M do dataset histórico. "Sem dados" = 0 observações.

Entrada:  grafo_bho.npz, fluxo_arestas_bho.geojson, dataset_historico.npz
Saída:    grafo_bho_com_dados.npz, fluxo_arestas_bho_com_dados.geojson
          (mesmo schema do build_graph_bho.py -> renderizadores funcionam igual)

Uso:
  uv run python filtrar_grafo_bho.py                      # remove só os 0% (sem dados)
  uv run python filtrar_grafo_bho.py --min-cobertura 10   # remove < 10% de cobertura
"""

import argparse
import json
import math

import numpy as np

LAT0 = -30.0
KX = 111.32 * math.cos(math.radians(LAT0))
KY = 110.57
TELE = ["nivel", "chuva", "vazao"]


def cobertura_telemetria(cod, dataset):
    d = np.load(dataset, allow_pickle=True)
    est = d["estacoes"].astype(str)
    feats = list(d["features"].astype(str))
    M = d["M"]
    di = {c: i for i, c in enumerate(est)}
    tele = [feats.index(f) for f in TELE]
    cov = {}
    for c in cod:
        if c in di:
            cov[c] = 100.0 * (M[:, di[c], tele].max(axis=1)).mean()
        else:
            cov[c] = 0.0
    return cov


def main():
    ap = argparse.ArgumentParser(description="Filtra estações sem dados e reconecta o grafo BHO.")
    ap.add_argument("--grafo", default="grafo_bho.npz")
    ap.add_argument("--geojson", default="fluxo_arestas_bho.geojson")
    ap.add_argument("--dataset", default="dataset_historico.npz")
    ap.add_argument("--saida-npz", default="grafo_bho_com_dados.npz")
    ap.add_argument("--saida-geojson", default="fluxo_arestas_bho_com_dados.geojson")
    ap.add_argument("--min-cobertura", type=float, default=0.0,
                    help="Mantém estações com cobertura > este valor (%%). Padrão 0 = remove só as sem dados.")
    ap.add_argument("--remover-artificiais", action="store_true",
                    help="Também remove os nós artificiais (cod 9000000x). Padrão: mantém.")
    args = ap.parse_args()

    g = np.load(args.grafo, allow_pickle=True)
    cod = g["nodes"].astype(str)
    lat = g["lat"]; lon = g["lon"]; A = g["A"]; W = g["W"]
    dist_foz = g["dist_foz_km"]; main_riv = g["main_riv"]
    strahler = g["strahler"]; upland = g["upland_skm"]; snap = g["snap_dist_km"]
    N = len(cod)

    cov = cobertura_telemetria(cod, args.dataset)
    # nós artificiais (ex.: 90000001, Lagoa dos Patos) — mantidos por padrão mesmo
    # sem telemetria, pois representam o exutório/contexto do grafo.
    artificial = np.array([c.startswith("9000000") for c in cod])
    survive = np.array([cov[c] > args.min_cobertura for c in cod])
    if not args.remover_artificiais:
        survive = survive | artificial
    print(f"Nós no grafo BHO: {N}")
    print(f"Artificiais mantidos: {int((artificial & survive).sum())} "
          f"({', '.join(cod[artificial & survive]) or '—'})")
    print(f"Removidos (cobertura <= {args.min_cobertura:.0f}%): {int((~survive).sum())}  |  "
          f"sobreviventes: {int(survive.sum())}")

    # próximo de jusante (out-degree <= 1)
    nxt = {}
    for u in range(N):
        v = np.where(A[u] == 1)[0]
        if len(v):
            nxt[u] = int(v[0])

    # geometria das arestas originais, por (cod_montante, cod_jusante)
    gj = json.load(open(args.geojson, encoding="utf-8"))
    geo = {(f["properties"]["montante"], f["properties"]["jusante"]):
           f["geometry"]["coordinates"] for f in gj["features"]}

    # 1) bypass-rewire: cada sobrevivente liga ao 1º sobrevivente de jusante
    new_edges = []   # (u, v, peso, coords, fallback)
    for u in range(N):
        if not survive[u]:
            continue
        cur = u; peso = 0.0; caminho = [u]; alvo = None
        while cur in nxt:
            v = nxt[cur]
            peso += float(W[cur, v])
            caminho.append(v)
            if survive[v]:
                alvo = v
                break
            cur = v
        if alvo is not None and alvo != u:
            coords = []
            for a, b in zip(caminho[:-1], caminho[1:]):
                seg = geo.get((cod[a], cod[b]))
                if seg:
                    coords.extend([[float(x), float(y)] for x, y in seg])
            if len(coords) < 2:
                coords = [[float(lon[u]), float(lat[u])], [float(lon[alvo]), float(lat[alvo])]]
            new_edges.append((u, alvo, peso, coords, False))

    # 2) exutório = sobrevivente com menor distância à foz
    surv_idx = np.where(survive)[0]
    outlet = int(surv_idx[np.argmin(dist_foz[surv_idx])])
    # nós sobreviventes que ficaram sem aresta de saída (órfãos), exceto o exutório
    tem_saida = {u for u, *_ in new_edges}
    orfaos = [u for u in surv_idx if u not in tem_saida and u != outlet]
    # religa cada órfão ao sobrevivente de jusante (menor dist_foz) mais próximo
    nfb = 0
    for u in orfaos:
        cand = [v for v in surv_idx if dist_foz[v] < dist_foz[u] and v != u]
        if not cand:
            cand = [outlet]
        v = min(cand, key=lambda v: math.hypot((lon[v]-lon[u])*KX, (lat[v]-lat[u])*KY))
        peso = math.hypot((lon[v]-lon[u])*KX, (lat[v]-lat[u])*KY)
        coords = [[float(lon[u]), float(lat[u])], [float(lon[v]), float(lat[v])]]
        new_edges.append((u, v, peso, coords, True))
        nfb += 1

    # 3) reindexa para os sobreviventes
    keep = list(surv_idx)
    remap = {old: i for i, old in enumerate(keep)}
    n2 = len(keep)
    A2 = np.zeros((n2, n2), dtype=np.uint8)
    W2 = np.zeros((n2, n2), dtype=np.float32)
    for u, v, peso, _, _ in new_edges:
        A2[remap[u], remap[v]] = 1
        W2[remap[u], remap[v]] = peso

    # componentes (via união-find não-direcionado)
    pai = list(range(n2))
    def find(x):
        while pai[x] != x:
            pai[x] = pai[pai[x]]; x = pai[x]
        return x
    for u, v, *_ in new_edges:
        pai[find(remap[u])] = find(remap[v])
    ncomp = len({find(x) for x in range(n2)})

    print(f"Arestas: {len(new_edges)}  (reconexões de bypass: {len(new_edges)-nfb}, fallback: {nfb})")
    print(f"Exutório (menor dist. à foz): {cod[outlet]}")
    print(f"Componentes conexas: {ncomp}")

    np.savez_compressed(
        args.saida_npz,
        A=A2, W=W2,
        nodes=np.array([cod[i] for i in keep]),
        lat=np.array([lat[i] for i in keep], dtype=np.float32),
        lon=np.array([lon[i] for i in keep], dtype=np.float32),
        snap_dist_km=np.array([snap[i] for i in keep], dtype=np.float32),
        dist_foz_km=np.array([dist_foz[i] for i in keep], dtype=np.float32),
        main_riv=np.array([main_riv[i] for i in keep]),
        strahler=np.array([strahler[i] for i in keep], dtype=np.int16),
        upland_skm=np.array([upland[i] for i in keep], dtype=np.float32),
    )
    feats = []
    for u, v, peso, coords, fb in new_edges:
        feats.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"montante": cod[u], "jusante": cod[v],
                           "dist_km": round(peso, 2), "fallback": fb},
        })
    with open(args.saida_geojson, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    print(f"\nSalvo: {args.saida_npz}  e  {args.saida_geojson}")


if __name__ == "__main__":
    main()
