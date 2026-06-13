"""
Renderiza o grafo de fluxo da rede Guaíba/Patos (STGNN) como PNG.

Usa grafo_hydrorivers.npz (nós + atributos) e fluxo_arestas.geojson (caminhos
seguindo os rios). Filtra para o maior sistema fluvial (MAIN_RIV), que é a rede
Guaíba/Patos. Nós coloridos por distância à foz; tamanho por área de drenagem.

Rodar com: uv run python render_graph.py
"""

import argparse
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from collections import Counter


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="grafo_hydrorivers.npz")
    ap.add_argument("--geojson", default="fluxo_arestas.geojson")
    ap.add_argument("--saida", default="grafo_guaiba.png")
    ap.add_argument("--main-riv", type=int, default=None,
                    help="Sistema a plotar; padrão = o maior (Guaíba/Patos).")
    args = ap.parse_args()

    d = np.load(args.npz, allow_pickle=False)
    cod = d["nodes"]; lat = d["lat"]; lon = d["lon"]
    mr = d["main_riv"]; dist_foz = d["dist_foz_km"]; upland = d["upland_skm"]

    alvo = args.main_riv or Counter(mr.tolist()).most_common(1)[0][0]
    sel = mr == alvo
    idx = {c: i for i, c in enumerate(cod)}
    in_sys = set(cod[sel].tolist())
    n_sys = int(sel.sum())

    fig, ax = plt.subplots(figsize=(11, 13))

    # arestas seguindo os rios (apenas dentro do sistema-alvo)
    gj = json.load(open(args.geojson, encoding="utf-8"))
    n_edges = 0
    for ft in gj["features"]:
        p = ft["properties"]
        if p["montante"] in in_sys and p["jusante"] in in_sys:
            xs = [c[0] for c in ft["geometry"]["coordinates"]]
            ys = [c[1] for c in ft["geometry"]["coordinates"]]
            ax.plot(xs, ys, color="#4a90d9", lw=0.9, alpha=0.7, zorder=1)
            # seta no sentido do fluxo (montante -> jusante)
            u, v = idx[p["montante"]], idx[p["jusante"]]
            ax.annotate("", xy=(lon[v], lat[v]), xytext=(lon[u], lat[u]),
                        arrowprops=dict(arrowstyle="->", color="#2c6cb0",
                                        alpha=0.5, lw=0.7), zorder=2)
            n_edges += 1

    # nós: cor = distância à foz, tamanho = área de drenagem (log)
    sc = ax.scatter(lon[sel], lat[sel], c=dist_foz[sel], cmap="viridis_r",
                    s=12 + 8 * np.log1p(upland[sel]), edgecolor="k",
                    linewidth=0.3, zorder=3)
    cbar = fig.colorbar(sc, ax=ax, shrink=0.6)
    cbar.set_label("Distância à foz (km) — escuro = mais a jusante")

    # destaca o exutório (menor distância à foz)
    sub = np.where(sel)[0]
    foz = sub[np.argmin(dist_foz[sub])]
    ax.scatter(lon[foz], lat[foz], marker="*", s=320, color="red",
               edgecolor="k", zorder=4, label="Exutório (Guaíba/Patos)")

    ax.set_title(f"Rede de fluxo Guaíba/Patos — {n_sys} estações, {n_edges} arestas\n"
                 f"(STGNN, via HydroRIVERS; MAIN_RIV={alvo})")
    ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
    ax.legend(loc="lower left"); ax.grid(alpha=0.2)
    ax.set_aspect(1.0 / np.cos(np.radians(-30)))
    fig.tight_layout()
    fig.savefig(args.saida, dpi=150)
    print(f"Salvo: {args.saida}  ({n_sys} nós, {n_edges} arestas no sistema {alvo})")


if __name__ == "__main__":
    main()
