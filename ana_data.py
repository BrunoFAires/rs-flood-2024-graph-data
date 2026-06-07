import argparse
import csv
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode

import requests

ANA_ENDPOINT = "https://telemetriaws1.ana.gov.br/ServiceANA.asmx/DadosHidrometeorologicos"


def get_text(parent: ET.Element, tag_name: str) -> Optional[str]:
    """
    Busca o texto de uma tag filha ignorando namespace.
    """
    for child in parent:
        if child.tag.split("}")[-1] == tag_name:
            return child.text.strip() if child.text else None
    return None


def parse_municipio_uf(valor: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not valor or "-" not in valor:
        return valor, None

    municipio, uf = valor.rsplit("-", 1)
    return municipio.strip(), uf.strip().upper()


def carregar_estacoes(xml_path: str) -> List[Dict[str, Optional[str]]]:
    tree = ET.parse(xml_path)
    root = tree.getroot()

    estacoes = []

    for table in root.iter():
        if table.tag.split("}")[-1] != "Table":
            continue

        municipio_uf = get_text(table, "Municipio-UF")
        municipio, uf = parse_municipio_uf(municipio_uf)

        estacao = {
            "nome_estacao": get_text(table, "NomeEstacao"),
            "cod_estacao": get_text(table, "CodEstacao"),
            "bacia": get_text(table, "Bacia"),
            "sub_bacia": get_text(table, "SubBacia"),
            "operadora": get_text(table, "Operadora"),
            "responsavel": get_text(table, "Responsavel"),
            "municipio_uf": municipio_uf,
            "municipio": municipio,
            "uf": uf,
            "latitude": get_text(table, "Latitude"),
            "longitude": get_text(table, "Longitude"),
            "altitude": get_text(table, "Altitude"),
            "cod_rio": get_text(table, "CodRio"),
            "nome_rio": get_text(table, "NomeRio"),
            "origem": get_text(table, "Origem"),
            "status_estacao": get_text(table, "StatusEstacao"),
        }

        estacoes.append(estacao)

    return estacoes


def filtrar_estacoes_por_uf(
    estacoes: List[Dict[str, Optional[str]]],
    uf: str,
    somente_ativas: bool = True
) -> List[Dict[str, Optional[str]]]:
    """
    Retorna estações de uma UF.
    """
    uf = uf.upper().strip()

    resultado = [
        estacao
        for estacao in estacoes
        if estacao.get("uf") == uf
    ]

    if somente_ativas:
        resultado = [
            estacao
            for estacao in resultado
            if (estacao.get("status_estacao") or "").lower() == "ativo"
        ]

    return resultado


def buscar_dados_hidrometeorologicos(
    cod_estacao: str,
    data_inicio: str,
    data_fim: str,
    timeout: int = 60
) -> str:
    """
    Datas no formato DD/MM/YYYY.
    Exemplo:
      data_inicio='01/04/2024'
      data_fim='01/08/2024'
    """
    params = {
        "codEstacao": cod_estacao,
        "dataInicio": data_inicio,
        "dataFim": data_fim,
    }

    response = requests.get(
        ANA_ENDPOINT,
        params=params,
        timeout=timeout
    )

    response.raise_for_status()
    return response.text


def salvar_estacoes_csv(estacoes: List[Dict[str, Optional[str]]], output_path: str) -> None:
    if not estacoes:
        print("Nenhuma estação para salvar.")
        return

    campos = list(estacoes[0].keys())

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=campos, delimiter=";")
        writer.writeheader()
        writer.writerows(estacoes)

    print(f"Arquivo salvo: {output_path}")


def salvar_resposta_ana(
    cod_estacao: str,
    resposta_xml: str,
    output_dir: str
) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    file_path = output / f"dados_ana_{cod_estacao}.xml"

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(resposta_xml)

    return file_path


def main():
    parser = argparse.ArgumentParser(
        description="Percorre XML de estações ANA e busca dados hidrometeorológicos."
    )

    parser.add_argument(
        "--xml",
        required=True,
        help="Caminho do XML ListaEstacoesTelemetricas."
    )

    parser.add_argument(
        "--uf",
        required=True,
        help="UF desejada. Exemplo: RS, SC, PR."
    )

    parser.add_argument(
        "--data-inicio",
        default="01/04/2024",
        help="Data inicial no formato DD/MM/YYYY."
    )

    parser.add_argument(
        "--data-fim",
        default="01/08/2024",
        help="Data final no formato DD/MM/YYYY."
    )

    parser.add_argument(
        "--saida-estacoes",
        default="estacoes_filtradas.csv",
        help="CSV de saída com as estações filtradas."
    )

    parser.add_argument(
        "--saida-ana",
        default="dados_ana",
        help="Diretório para salvar as respostas XML da ANA."
    )

    parser.add_argument(
        "--baixar-dados",
        action="store_true",
        help="Se informado, baixa dados hidrometeorológicos da ANA para cada estação filtrada."
    )

    parser.add_argument(
        "--incluir-inativas",
        action="store_true",
        help="Inclui estações inativas no filtro."
    )

    args = parser.parse_args()

    estacoes = carregar_estacoes(args.xml)

    estacoes_uf = filtrar_estacoes_por_uf(
        estacoes,
        uf=args.uf,
        somente_ativas=not args.incluir_inativas
    )

    print(f"Total de estações no XML: {len(estacoes)}")
    print(f"Estações encontradas para {args.uf.upper()}: {len(estacoes_uf)}")

    salvar_estacoes_csv(estacoes_uf, args.saida_estacoes)

    if not args.baixar_dados:
        return

    estacoes_para_consulta = estacoes_uf

    for i, estacao in enumerate(estacoes_para_consulta, start=1):
        cod_estacao = estacao.get("cod_estacao")

        if not cod_estacao:
            continue

        print(f"[{i}/{len(estacoes_para_consulta)}] Buscando ANA codEstacao={cod_estacao}...")

        try:
            resposta = buscar_dados_hidrometeorologicos(
                cod_estacao=cod_estacao,
                data_inicio=args.data_inicio,
                data_fim=args.data_fim
            )

            arquivo = salvar_resposta_ana(
                cod_estacao=cod_estacao,
                resposta_xml=resposta,
                output_dir=args.saida_ana
            )

            print(f"  OK: {arquivo}")

        except requests.HTTPError as e:
            print(f"  Erro HTTP para estação {cod_estacao}: {e}")

        except requests.RequestException as e:
            print(f"  Erro de conexão para estação {cod_estacao}: {e}")

        time.sleep(0.5)


if __name__ == "__main__":
    main()