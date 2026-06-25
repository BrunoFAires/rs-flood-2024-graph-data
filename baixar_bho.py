"""
Baixa os trechos de drenagem da ANA BHO 2017 5K (Base Hidrográfica
Ottocodificada) para a região do RS, a partir do FeatureServer público do SNIRH.

A BHO é a fonte hidrográfica *primária* do roadmap (oficial, nacional,
topologicamente consistente). Campos-chave por trecho:
  COTRECHO   -> id único do trecho
  NUTRJUS    -> COTRECHO do próximo trecho de jusante (0 = exutório/foz)
  COBACIA    -> código Otto/Pfafstetter (bacia=8 -> Atlântico Sul/Guaíba-Patos)
  NUDISTBACT -> distância (km) ao longo da drenagem até a foz da bacia
  NUAREAMONT -> área de drenagem a montante (km²)
  NUSTRAHLER -> ordem de Strahler
  NUCOMPTREC -> comprimento do trecho (km)

Pagina o serviço (maxRecordCount=1000) com resultOffset e salva tudo num único
GeoJSON em EPSG:4326 (lon/lat), que serve de cache para o build_graph_bho.py.

Uso: uv run python baixar_bho.py            # bbox do RS -> bho/bho_rs_trechos.geojson
"""

import argparse
import json
import os
import ssl
import time
import urllib.parse
import urllib.request

SVC = ("https://www.snirh.gov.br/arcgis/rest/services/SPR/"
       "BHO2017_5K_TRECHODRENAGEM/FeatureServer/0/query")

# Caixa envolvente do RS (mesma de build_graph.py): xmin, ymin, xmax, ymax
RS_BBOX = (-58.5, -34.6, -49.0, -26.5)

CAMPOS = ["COTRECHO", "NUTRJUS", "COBACIA", "NUDISTBACT",
          "NUAREAMONT", "NUSTRAHLER", "NUCOMPTREC"]

# O servidor às vezes apresenta certificado intermediário incompleto; como só
# lemos dados públicos, relaxamos a verificação (igual a render_satellite.py).
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _consulta(params, timeout=120, tentativas=4):
    body = urllib.parse.urlencode(params).encode("utf-8")
    ultimo_erro = None
    for t in range(tentativas):
        try:
            req = urllib.request.Request(
                SVC, data=body, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "User-Agent": "baixar_bho.py (STGNN bacia8)"})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return json.loads(r.read())
        except (OSError, ValueError) as e:
            ultimo_erro = e
            print(f"  tentativa {t+1} falhou ({e}); aguardando...")
            time.sleep(3 * (t + 1))
    raise RuntimeError(f"consulta falhou após {tentativas} tentativas: {ultimo_erro}")


def contar(bbox, where):
    d = _consulta({
        "where": where,
        "geometry": ",".join(map(str, bbox)),
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
        "returnCountOnly": "true", "f": "json",
    })
    return int(d["count"])


def baixar(bbox, where, total, page=1000):
    # O servidor pode devolver páginas mais curtas que `page` por limite de
    # transferência; por isso paginamos por resultOffset até atingir `total`
    # (ou uma página vazia), avançando pelo nº realmente recebido.
    feats = []
    offset = 0
    while offset < total:
        d = _consulta({
            "where": where,
            "geometry": ",".join(map(str, bbox)),
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
            "outFields": ",".join(CAMPOS),
            "outSR": 4326,                 # devolve lon/lat
            "orderByFields": "COTRECHO",   # paginação estável
            "resultOffset": offset,
            "resultRecordCount": page,
            "returnGeometry": "true",
            "f": "geojson",
        })
        lote = d.get("features", [])
        if not lote:
            break
        feats.extend(lote)
        offset += len(lote)
        print(f"  {len(feats)}/{total} trechos baixados...")
    return feats


def main():
    ap = argparse.ArgumentParser(description="Baixa BHO 2017 5K (trecho de drenagem) p/ o RS.")
    ap.add_argument("--saida", default="bho/bho_rs_trechos.geojson")
    ap.add_argument("--where", default="1=1",
                    help="Filtro SQL (ex.: \"COBACIA LIKE '8%%'\" só Guaíba/Patos).")
    ap.add_argument("--bbox", default=",".join(map(str, RS_BBOX)),
                    help="xmin,ymin,xmax,ymax em graus (EPSG:4326).")
    args = ap.parse_args()

    bbox = tuple(float(x) for x in args.bbox.split(","))
    os.makedirs(os.path.dirname(args.saida) or ".", exist_ok=True)

    n = contar(bbox, args.where)
    print(f"Trechos BHO no filtro: {n}  (bbox={bbox}, where={args.where!r})")
    feats = baixar(bbox, args.where, n)
    gj = {"type": "FeatureCollection", "features": feats}
    with open(args.saida, "w", encoding="utf-8") as f:
        json.dump(gj, f)
    print(f"\nSalvo: {args.saida}  ({len(feats)} trechos)")


if __name__ == "__main__":
    main()
