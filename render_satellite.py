"""
Overlay do grafo Guaíba/Patos (STGNN) sobre imagem de satélite.

Basemap: Esri World Imagery (export em EPSG:4326, via urllib — sem dependências
geo pesadas). Desenha os trechos de rio (GeoJSON) e os nós (estações) por cima.

Rodar com: uv run python render_satellite.py
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="grafo_hydrorivers.npz")
    ap.add_argument("--geojson", default="fluxo_arestas.geojson")
    ap.add_argument("--saida", default="grafo_guaiba_satelite.png")
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

    m = args.margem
    xmin, xmax = lon[sel].min() - m, lon[sel].max() + m
    ymin, ymax = lat[sel].min() - m, lat[sel].max() + m
    print(f"Selecionadas {int(sel.sum())} estações | bbox "
          f"[{xmin:.2f},{ymin:.2f},{xmax:.2f},{ymax:.2f}]")
    print("Baixando imagem de satélite (Esri World Imagery)...")
    img = baixar_satelite(xmin, ymin, xmax, ymax)

    fig, ax = plt.subplots(figsize=(12, 13))
    ax.imshow(img, extent=[xmin, xmax, ymin, ymax], origin="upper", zorder=0)

    # arestas (rios) do sistema-alvo
    gj = json.load(open(args.geojson, encoding="utf-8"))
    n_edges = 0
    for ft in gj["features"]:
        p = ft["properties"]
        if p["montante"] in in_sys and p["jusante"] in in_sys:
            xs = [c[0] for c in ft["geometry"]["coordinates"]]
            ys = [c[1] for c in ft["geometry"]["coordinates"]]
            cor = "#ff9900" if p.get("fallback") else "#33ccff"
            ax.plot(xs, ys, color=cor, lw=1.1, alpha=0.85, zorder=2)
            n_edges += 1

    # nós coloridos por distância à foz
    sc = ax.scatter(lon[sel], lat[sel], c=dist_foz[sel], cmap="turbo",
                    s=45, edgecolor="white", linewidth=0.6, zorder=3)
    cbar = fig.colorbar(sc, ax=ax, shrink=0.55)
    cbar.set_label("Distância à foz (km)")

    sub = np.where(sel)[0]
    alvo = np.where(cod == args.estacao_final)[0]
    if len(alvo) and alvo[0] in set(sub.tolist()):
        foz = alvo[0]
    else:
        sem_jusante = np.where(A.sum(axis=1) == 0)[0]
        candidatos = [i for i in sem_jusante if i in set(sub.tolist())]
        foz = candidatos[0] if candidatos else sub[np.argmin(dist_foz[sub])]
    ax.scatter(lon[foz], lat[foz], marker="*", s=420, color="red",
               edgecolor="white", linewidth=0.8, zorder=4, label="Exutório (Guaíba)")

    ax.set_xlim(xmin, xmax); ax.set_ylim(ymin, ymax)
    ax.set_aspect(1.0 / np.cos(np.radians(float(lat[sel].mean()))))
    ax.set_title(f"Rede Guaíba/Patos sobre satélite — {int(sel.sum())} estações, "
                 f"{n_edges} arestas (STGNN)")
    ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
    ax.legend(loc="lower left")
    ax.text(0.99, 0.01, "Imagery: Esri, Maxar, Earthstar Geographics",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=7,
            color="white", alpha=0.8)
    fig.tight_layout()
    fig.savefig(args.saida, dpi=150)
    print(f"Salvo: {args.saida}")


if __name__ == "__main__":
    main()
