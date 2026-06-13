"""
Interseção dos dois conjuntos de daados gerados. Reindexa X, M (eixo N) e
A, W, lat, lon, etc. (eixos N x N / N) para essa ordem comum.

Exemplo:
  uv run python adjust_data_order.py \\
      --tensor dataset_historico.npz --grafo grafo_hydrorivers.npz \\
      --saida dataset_stgnn.npz
"""

import argparse

import numpy as np


def carregar(tensor_path, grafo_path):
    t = np.load(tensor_path, allow_pickle=False)
    g = np.load(grafo_path, allow_pickle=False)
    return t, g


def alinhar_indices(estacoes_tensor, estacoes_grafo):
    """Interseção dos códigos de estação, em ordem determinística (sorted)."""
    set_t = set(estacoes_tensor.tolist())
    set_g = set(estacoes_grafo.tolist())
    comuns = sorted(set_t & set_g)

    idx_t = {c: i for i, c in enumerate(estacoes_tensor.tolist())}
    idx_g = {c: i for i, c in enumerate(estacoes_grafo.tolist())}

    pos_t = np.array([idx_t[c] for c in comuns], dtype=np.int64)
    pos_g = np.array([idx_g[c] for c in comuns], dtype=np.int64)

    so_tensor = sorted(set_t - set_g)
    so_grafo = sorted(set_g - set_t)
    return comuns, pos_t, pos_g, so_tensor, so_grafo


def relatorio(estacoes_tensor, estacoes_grafo, comuns, so_tensor, so_grafo):
    print(f"Estações no tensor (com dados): {len(estacoes_tensor)}")
    print(f"Estações no grafo (bacia 8, com lat/lon): {len(estacoes_grafo)}")
    print(f"Estações em comum (N final): {len(comuns)}")
    if so_tensor:
        print(f"  só no tensor (sem grafo, descartadas): {len(so_tensor)} "
              f"-> {so_tensor[:10]}{'...' if len(so_tensor) > 10 else ''}")
    if so_grafo:
        print(f"  só no grafo (sem dados, descartadas): {len(so_grafo)} "
              f"-> {so_grafo[:10]}{'...' if len(so_grafo) > 10 else ''}")


def main():
    ap = argparse.ArgumentParser(
        description="Alinha X/M (preprocessar.py) e A/W (build_graph.py) num único dataset."
    )
    ap.add_argument("--tensor", default="dataset_historico.npz",
                    help="Saída de preprocessar.py (X, M, timestamps, estacoes, features).")
    ap.add_argument("--grafo", default="grafo_hydrorivers.npz",
                    help="Saída de build_graph.py (A, W, nodes, lat, lon, ...).")
    ap.add_argument("--saida", default="dataset_stgnn.npz", help="Arquivo .npz de saída.")
    args = ap.parse_args()

    t, g = carregar(args.tensor, args.grafo)

    estacoes_tensor = t["estacoes"]
    estacoes_grafo = g["nodes"]

    comuns, pos_t, pos_g, so_tensor, so_grafo = alinhar_indices(
        estacoes_tensor, estacoes_grafo
    )
    relatorio(estacoes_tensor, estacoes_grafo, comuns, so_tensor, so_grafo)

    if not comuns:
        raise SystemExit("Nenhuma estação em comum entre tensor e grafo — verifique as entradas.")

    X = t["X"][:, pos_t, :]
    M = t["M"][:, pos_t, :]
    A = g["A"][np.ix_(pos_g, pos_g)]
    W = g["W"][np.ix_(pos_g, pos_g)]

    extras = {}
    for chave in ("lat", "lon", "snap_dist_km", "dist_foz_km", "main_riv", "strahler", "upland_skm"):
        if chave in g.files:
            extras[chave] = g[chave][pos_g]

    np.savez_compressed(
        args.saida,
        X=X,
        M=M,
        A=A,
        W=W,
        timestamps=t["timestamps"],
        estacoes=np.array(comuns),
        features=t["features"],
        **extras,
    )

    Tn, N, F = X.shape
    arestas = int(A.sum())
    print(f"\nDataset final: X/M [T={Tn} x N={N} x F={F}]  |  A/W [{N} x {N}]  |  arestas: {arestas}")
    print(f"Salvo: {args.saida}")


if __name__ == "__main__":
    main()