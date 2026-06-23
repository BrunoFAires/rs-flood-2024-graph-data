"""
Aumenta o dataset com variáveis horárias de fontes externas para as estações da bacia 8 listadas em estacoes_rs.csv (inclui
estações artificiais).

Cada "aumento" é uma entrada do catálogo AUMENTOS abaixo: um conjunto de
variáveis horárias do Open-Meteo, salvas em <saida>/<prefixo>_<cod>.json.
Para adicionar uma nova fonte (ex.: precipitação, temperatura), basta
acrescentar uma entrada ao catálogo.

Uma requisição por estação (o Open-Meteo aceita o intervalo completo numa
única chamada); um arquivo por estação -> permite retomar de onde parou.

Exemplo:
  python aumentar_dados.py --aumento vento --estacoes estacoes_rs.csv --saida dados_vento
"""

import argparse
import csv
import glob
import json
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path

from preprocessar import ler_xml

OPEN_METEO_ENDPOINT = "https://archive-api.open-meteo.com/v1/archive"

# Catálogo de aumentos disponíveis. Cada entrada descreve uma chamada ao Open-Meteo
AUMENTOS = {
    "vento": {
        "variaveis": ["wind_speed_10m", "wind_direction_10m"],
        "saida": "dados_vento",
        "prefixo": "vento",
    },
}

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def carregar_estacoes_bacia8(csv_path):
    estacoes = []
    with open(csv_path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            if (r.get("bacia") or "").strip() != "8":
                continue
            try:
                lat = float(r["latitude"]); lon = float(r["longitude"])
            except (TypeError, ValueError):
                continue
            estacoes.append({"cod": r["cod_estacao"], "lat": lat, "lon": lon})
    return estacoes


def periodo_dados_historicos(entrada="dados_historicos"):
    """Min/max das horas observadas nos XMLs da ANA."""
    minimo = maximo = None
    for path in glob.glob(str(Path(entrada) / "*.xml")):
        for _, hora, _ in ler_xml(path):
            if minimo is None or hora < minimo:
                minimo = hora
            if maximo is None or hora > maximo:
                maximo = hora
    if minimo is None:
        return None, None
    return minimo.strftime("%Y-%m-%d"), maximo.strftime("%Y-%m-%d")


def baixar_open_meteo(lat, lon, data_inicio, data_fim, variaveis, timeout=150):
    query = urllib.parse.urlencode({
        "latitude": f"{lat:.6f}",
        "longitude": f"{lon:.6f}",
        "start_date": data_inicio,
        "end_date": data_fim,
        "hourly": ",".join(variaveis),
        "timezone": "UTC",
    })
    url = f"{OPEN_METEO_ENDPOINT}?{query}"
    with urllib.request.urlopen(url, timeout=timeout, context=_SSL_CTX) as resp:
        return resp.read().decode("utf-8")


def main():
    ap = argparse.ArgumentParser(description="Aumenta o dataset com variáveis horárias do Open-Meteo, por estação.")
    ap.add_argument("--aumento", default="vento", choices=sorted(AUMENTOS),
                    help="Qual aumento do catálogo AUMENTOS baixar (padrão: vento).")
    ap.add_argument("--estacoes", default="estacoes_rs.csv", help="CSV de estações (bacia 8).")
    ap.add_argument("--saida", default=None,
                    help="Diretório de saída (padrão: definido pelo aumento escolhido).")
    ap.add_argument("--dados-historicos", default="dados_historicos",
                    help="Diretório com os XMLs da ANA, usado para definir o período "
                         "(min/max das horas observadas) quando --data-inicio/--data-fim "
                         "não são informados.")
    ap.add_argument("--data-inicio", default=None, help="Data inicial (YYYY-MM-DD).")
    ap.add_argument("--data-fim", default=None, help="Data final (YYYY-MM-DD).")
    ap.add_argument("--ano", default=None,
                    help="Atalho para --data-inicio YYYY-01-01 --data-fim YYYY-12-31 e "
                         "salva como <prefixo>_<cod>_<ano>.json (não sobrescreve arquivos "
                         "sem sufixo de ano já existentes).")
    ap.add_argument("--pausa", type=float, default=0.5, help="Pausa (s) entre requisições.")
    ap.add_argument("--dry-run", action="store_true", help="Lista o que seria baixado, sem acessar a rede.")
    args = ap.parse_args()

    aumento = AUMENTOS[args.aumento]
    variaveis, prefixo = aumento["variaveis"], aumento["prefixo"]

    sufixo_ano = ""
    if args.ano:
        sufixo_ano = f"_{args.ano}"
        args.data_inicio = args.data_inicio or f"{args.ano}-01-01"
        args.data_fim    = args.data_fim    or f"{args.ano}-12-31"

    data_inicio, data_fim = args.data_inicio, args.data_fim
    if not data_inicio or not data_fim:
        di, df = periodo_dados_historicos(args.dados_historicos)
        if not di:
            raise SystemExit(
                f"Não há XMLs em {args.dados_historicos!r} para inferir o período. "
                "Informe --data-inicio/--data-fim ou --ano explicitamente."
            )
        data_inicio, data_fim = data_inicio or di, data_fim or df
    args.data_inicio, args.data_fim = data_inicio, data_fim

    estacoes = carregar_estacoes_bacia8(args.estacoes)
    saida = Path(args.saida or aumento["saida"])
    saida.mkdir(parents=True, exist_ok=True)

    nome_arquivo = f"{prefixo}_<cod>{sufixo_ano}.json"
    print(f"Aumento: {args.aumento} (variáveis: {', '.join(variaveis)})")
    print(f"Estações: {len(estacoes)}  |  Período: {args.data_inicio}..{args.data_fim}")
    print(f"Saída: {saida}/{nome_arquivo}\n")

    baixados = pulados = erros = 0
    for i, e in enumerate(estacoes, start=1):
        destino = saida / f"{prefixo}_{e['cod']}{sufixo_ano}.json"
        rotulo = f"[{i}/{len(estacoes)}] {e['cod']}"

        if destino.exists():
            pulados += 1
            continue

        if args.dry_run:
            print(f"  BAIXARIA {rotulo} (lat={e['lat']}, lon={e['lon']})")
            baixados += 1
            continue

        try:
            texto = baixar_open_meteo(e["lat"], e["lon"], args.data_inicio, args.data_fim, variaveis)
            json.loads(texto)
        except Exception as err:
            erros += 1
            print(f"  ERRO {rotulo}: {err}")
            time.sleep(max(args.pausa, 2.0))
            continue

        destino.write_text(texto, encoding="utf-8")
        baixados += 1
        print(f"  OK   {rotulo}")
        time.sleep(args.pausa)

    print(f"\nResumo -> baixados: {baixados}  pulados(já existiam): {pulados}  erros: {erros}")


if __name__ == "__main__":
    main()
