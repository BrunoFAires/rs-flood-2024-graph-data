"""
Mapa interativo do grafo Guaíba sobre satélite.
Rodar com: uv run python render_html.py
"""

import argparse
import json

import folium
import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="grafo_hydrorivers.npz")
    ap.add_argument("--geojson", default="fluxo_arestas.geojson")
    ap.add_argument("--saida", default="grafo_guaiba.html")
    ap.add_argument("--estacao-final", default="87450004")
    args = ap.parse_args()

    d = np.load(args.npz, allow_pickle=False)
    cod = d["nodes"]; lat = d["lat"]; lon = d["lon"]
    dist_foz = d["dist_foz_km"]; rio = None

    centro = [float(lat.mean()), float(lon.mean())]
    m = folium.Map(location=centro, zoom_start=8, tiles=None)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/"
              "World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri, Maxar, Earthstar Geographics",
        name="Satélite (Esri)",
        max_zoom=19,
    ).add_to(m)

    #arestas
    gj = json.load(open(args.geojson, encoding="utf-8"))
    arestas_fg = folium.FeatureGroup(name="Arestas (rios)")
    for ft in gj["features"]:
        p = ft["properties"]
        coords = [[c[1], c[0]] for c in ft["geometry"]["coordinates"]]
        cor = "#ff9900" if p.get("fallback") else "#33ccff"
        folium.PolyLine(
            coords, color=cor, weight=2, opacity=0.85,
            tooltip=f"{p['montante']} → {p['jusante']} ({p['dist_km']} km)"
                    + (" [fallback]" if p.get("fallback") else ""),
        ).add_to(arestas_fg)
    arestas_fg.add_to(m)

    #estações
    nos_fg = folium.FeatureGroup(name="Estações")
    for i in range(len(cod)):
        is_final = cod[i] == args.estacao_final
        cor = "red" if is_final else "#1f78ff"
        raio = 7 if is_final else 4
        folium.CircleMarker(
            location=[float(lat[i]), float(lon[i])],
            radius=raio,
            color="white", weight=1, fill=True,
            fill_color=cor, fill_opacity=0.9,
            tooltip=f"{cod[i]}  |  dist. à foz: {dist_foz[i]:.1f} km"
                    + ("  [EXUTÓRIO]" if is_final else ""),
        ).add_to(nos_fg)
    nos_fg.add_to(m)

    folium.LayerControl().add_to(m)
    m.save(args.saida)
    print(f"Salvo: {args.saida}  ({len(cod)} estações, {len(gj['features'])} arestas)")


if __name__ == "__main__":
    main()
