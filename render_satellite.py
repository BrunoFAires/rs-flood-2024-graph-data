"""
Overlay do grafo Guaíba/Patos (STGNN) sobre imagem de satélite.

Basemap: Esri World Imagery (export em EPSG:4326, via urllib — sem dependências
geo pesadas). Desenha os trechos de rio (GeoJSON) e os nós (estações) por cima.

Modos (--modo):
  ambos        — figura lado a lado: sem / com arestas artificiais (padrão)
  sem-fallback — apenas arestas reais
  com-fallback — arestas reais + artificiais (fallback)

Rodar com: uv run python render_satellite.py [--modo ambos|sem-fallback|com-fallback]
"""

import argparse
import io
import json
import ssl
import urllib.parse
import urllib.request

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ESRI = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/export")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def baixar_satelite(xmin, ymin, xmax, ymax, largura=2000):
    altura = int(largura * (ymax - ymin) / (xmax - xmin))
    q = urllib.parse.urlencode({
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bboxSR": 4326, "imageSR": 4326,
        "size": f"{largura},{altura}",
        "format": "png", "f": "image",
    })
    with urllib.request.urlopen(f"{ESRI}?{q}", timeout=90, context=_CTX) as r:
        return plt.imread(io.BytesIO(r.read()), format="png")


def _draw_panel(ax, img, extent, lon, lat, dist_foz, sel, edges_real, edges_fallback,
                foz_idx, include_fallback, aspect):
    xmin, xmax, ymin, ymax = extent
    ax.imshow(img, extent=[xmin, xmax, ymin, ymax], origin="upper", zorder=0)

    for xs, ys in edges_real:
        ax.plot(xs, ys, color="#33ccff", lw=1.1, alpha=0.85, zorder=2)

    if include_fallback:
        for xs, ys in edges_fallback:
            ax.plot(xs, ys, color="#ff9900", lw=1.1, alpha=0.85, zorder=2)

    sc = ax.scatter(lon[sel], lat[sel], c=dist_foz[sel], cmap="turbo",
                    s=45, edgecolor="white", linewidth=0.6, zorder=3,
                    vmin=dist_foz[sel].min(), vmax=dist_foz[sel].max())

    ax.scatter(lon[foz_idx], lat[foz_idx], marker="*", s=420, color="red",
               edgecolor="white", linewidth=0.8, zorder=4, label="Exutório (Guaíba)")

    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect(aspect)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.legend(loc="lower left", fontsize=8)
    ax.text(0.99, 0.01, "Imagery: Esri, Maxar, Earthstar Geographics",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=6,
            color="white", alpha=0.8)
    return sc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="grafo_hydrorivers.npz")
    ap.add_argument("--geojson", default="fluxo_arestas.geojson")
    ap.add_argument("--saida", default=None,
                    help="Arquivo de saída. Padrão depende do --modo.")
    ap.add_argument("--modo", default="ambos",
                    choices=["ambos", "sem-fallback", "com-fallback"],
                    help="ambos (lado a lado), sem-fallback ou com-fallback.")
    ap.add_argument("--main-riv", type=int, default=None)
    ap.add_argument("--estacao-final", default="87450004",
                    help="Código da estação a destacar como exutório (estrela).")
    ap.add_argument("--margem", type=float, default=0.25, help="Margem em graus ao redor dos nós.")
    args = ap.parse_args()

    d = np.load(args.npz, allow_pickle=False)
    cod = d["nodes"]; lat = d["lat"]; lon = d["lon"]
    mr = d["main_riv"]; dist_foz = d["dist_foz_km"]
    A = d["A"]

    if args.main_riv:
        sel = mr == args.main_riv
    else:
        sel = np.ones(len(cod), dtype=bool)
    in_sys = set(cod[sel].tolist())
    n_nos = int(sel.sum())

    m = args.margem
    xmin, xmax = lon[sel].min() - m, lon[sel].max() + m
    ymin, ymax = lat[sel].min() - m, lat[sel].max() + m
    print(f"Selecionadas {n_nos} estações | bbox "
          f"[{xmin:.2f},{ymin:.2f},{xmax:.2f},{ymax:.2f}]")
    print("Baixando imagem de satélite (Esri World Imagery)...")
    img = baixar_satelite(xmin, ymin, xmax, ymax)

    # separar arestas reais e artificiais
    gj = json.load(open(args.geojson, encoding="utf-8"))
    edges_real = []
    edges_fallback = []
    for ft in gj["features"]:
        p = ft["properties"]
        if p["montante"] in in_sys and p["jusante"] in in_sys:
            xs = [c[0] for c in ft["geometry"]["coordinates"]]
            ys = [c[1] for c in ft["geometry"]["coordinates"]]
            if p.get("fallback"):
                edges_fallback.append((xs, ys))
            else:
                edges_real.append((xs, ys))

    print(f"Arestas reais: {len(edges_real)} | artificiais (fallback): {len(edges_fallback)}")

    sub = np.where(sel)[0]
    alvo = np.where(cod == args.estacao_final)[0]
    if len(alvo) and alvo[0] in set(sub.tolist()):
        foz = alvo[0]
    else:
        sem_jusante = np.where(A.sum(axis=1) == 0)[0]
        candidatos = [i for i in sem_jusante if i in set(sub.tolist())]
        foz = candidatos[0] if candidatos else sub[np.argmin(dist_foz[sub])]

    extent = [xmin, xmax, ymin, ymax]
    aspect = 1.0 / np.cos(np.radians(float(lat[sel].mean())))

    from matplotlib.lines import Line2D

    _DEFAULTS = {
        "ambos": "grafo_guaiba_satelite_comparacao.png",
        "sem-fallback": "grafo_guaiba_satelite_sem_fallback.png",
        "com-fallback": "grafo_guaiba_satelite_com_fallback.png",
    }
    saida = args.saida or _DEFAULTS[args.modo]

    if args.modo == "ambos":
        fig, axes = plt.subplots(1, 2, figsize=(22, 13))

        sc0 = _draw_panel(axes[0], img, extent, lon, lat, dist_foz, sel,
                          edges_real, edges_fallback, foz, include_fallback=False, aspect=aspect)
        axes[0].set_title(
            f"Sem arestas artificiais\n"
            f"{n_nos} estações | {len(edges_real)} arestas reais",
            fontsize=11,
        )

        _draw_panel(axes[1], img, extent, lon, lat, dist_foz, sel,
                    edges_real, edges_fallback, foz, include_fallback=True, aspect=aspect)
        axes[1].set_title(
            f"Com arestas artificiais (fallback)\n"
            f"{n_nos} estações | {len(edges_real)} reais + {len(edges_fallback)} artificiais"
            f" = {len(edges_real) + len(edges_fallback)} total",
            fontsize=11,
        )

        fig.subplots_adjust(left=0.05, right=0.88, top=0.93, bottom=0.08, wspace=0.08)
        cbar_ax = fig.add_axes([0.90, 0.15, 0.018, 0.65])
        fig.colorbar(sc0, cax=cbar_ax, label="Distância à foz (km)")
        legend_elements = [
            Line2D([0], [0], color="#33ccff", lw=2, label="Aresta real (HydroRIVERS)"),
            Line2D([0], [0], color="#ff9900", lw=2, label="Aresta artificial (fallback)"),
        ]
        fig.legend(handles=legend_elements, loc="lower center", ncol=2, fontsize=9,
                   framealpha=0.8, bbox_to_anchor=(0.5, 0.01))
        fig.suptitle("Rede Guaíba/Patos — comparação de arestas (STGNN)", fontsize=13, y=0.98)

    else:
        include_fallback = args.modo == "com-fallback"
        fig, ax = plt.subplots(figsize=(12, 13))

        sc = _draw_panel(ax, img, extent, lon, lat, dist_foz, sel,
                         edges_real, edges_fallback, foz, include_fallback=include_fallback,
                         aspect=aspect)

        if include_fallback:
            titulo = (
                f"Com arestas artificiais (fallback)\n"
                f"{n_nos} estações | {len(edges_real)} reais + {len(edges_fallback)} artificiais"
                f" = {len(edges_real) + len(edges_fallback)} total"
            )
            legend_elements = [
                Line2D([0], [0], color="#33ccff", lw=2, label="Aresta real (HydroRIVERS)"),
                Line2D([0], [0], color="#ff9900", lw=2, label="Aresta artificial (fallback)"),
            ]
            fig.legend(handles=legend_elements, loc="lower center", ncol=2, fontsize=9,
                       framealpha=0.8, bbox_to_anchor=(0.5, 0.01))
        else:
            titulo = (
                f"Sem arestas artificiais\n"
                f"{n_nos} estações | {len(edges_real)} arestas reais"
            )

        ax.set_title(titulo, fontsize=11)
        fig.colorbar(sc, ax=ax, shrink=0.55, label="Distância à foz (km)")

    if args.modo != "ambos":
        fig.tight_layout()
    fig.savefig(saida, dpi=150, bbox_inches="tight")
    print(f"Salvo: {saida}")


if __name__ == "__main__":
    main()
