"""
Baixa o histórico plurianual de dados hidrometeorológicos da ANA para as
estações de uma UF/bacia, consultando o endpoint telemétrico ano a ano.

Projetado para alimentar o dataset STGNN (ver STGNN_DATASET_ROADMAP.md):
- uma requisição por (estação, ano);
- um arquivo por (estação, ano) -> permite retomar de onde parou (resumable);
- rede via biblioteca padrão (urllib); reutiliza helpers de ana_data.py
  (que importa `requests` — disponível no ambiente do uv).

Exemplo:
  python baixar_historico.py --xml ListaEstacoesTelemetricas.xml \\
      --uf RS --bacia 8 --ano-inicio 2020 --saida dados_historicos
"""

import argparse
import ssl
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from ana_data import ANA_ENDPOINT, carregar_estacoes, filtrar_estacoes_por_uf

# A ANA usa cadeia de certificados que algumas instalações não validam;
# o dado é público, então seguimos sem verificação de certificado.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def filtrar_por_bacia(
    estacoes: List[Dict[str, Optional[str]]],
    bacias: List[str],
) -> List[Dict[str, Optional[str]]]:
    """Mantém apenas estações cujo campo 'bacia' está em `bacias`."""
    alvo = {b.strip() for b in bacias}
    return [e for e in estacoes if (e.get("bacia") or "").strip() in alvo]


def janelas_anuais(ano_inicio: int, ano_fim: int) -> List[tuple[int, str, str]]:
    """Gera (ano, dataInicio, dataFim) para cada ano no formato DD/MM/YYYY."""
    return [
        (ano, f"01/01/{ano}", f"31/12/{ano}")
        for ano in range(ano_inicio, ano_fim + 1)
    ]


def baixar_periodo(
    cod_estacao: str,
    data_inicio: str,
    data_fim: str,
    timeout: int = 120,
) -> str:
    """Faz uma requisição ao endpoint da ANA e retorna o XML (stdlib)."""
    query = urllib.parse.urlencode({
        "codEstacao": cod_estacao,
        "dataInicio": data_inicio,
        "dataFim": data_fim,
    })
    url = f"{ANA_ENDPOINT}?{query}"
    with urllib.request.urlopen(url, timeout=timeout, context=_SSL_CTX) as resp:
        return resp.read().decode("utf-8", "replace")


def contar_registros(xml_text: str) -> int:
    """Conta os registros DadosHidrometereologicos no XML (0 se vazio/erro)."""
    if "Sem dados" in xml_text:
        return 0
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return 0
    return sum(
        1 for el in root.iter()
        if el.tag.split("}")[-1] == "DadosHidrometereologicos"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Baixa histórico plurianual da ANA (uma requisição por estação/ano)."
    )
    parser.add_argument("--xml", required=True, help="Caminho do ListaEstacoesTelemetricas.xml.")
    parser.add_argument("--uf", default="RS", help="UF desejada. Padrão: RS.")
    parser.add_argument(
        "--bacia",
        default="8",
        help="Códigos de bacia separados por vírgula (ex.: 8 ou 7,8). Vazio = todas.",
    )
    parser.add_argument("--ano-inicio", type=int, default=2020, help="Primeiro ano. Padrão: 2020.")
    parser.add_argument(
        "--ano-fim",
        type=int,
        default=2025,
        help="Último ano (inclusive). Padrão: 2025 — janela fixa de anos completos "
             "(evita o ano corrente parcial/futuro e mantém o dataset reproduzível).",
    )
    parser.add_argument("--saida", default="dados_historicos", help="Diretório de saída.")
    parser.add_argument(
        "--incluir-inativas",
        action="store_true",
        help="Inclui estações inativas.",
    )
    parser.add_argument(
        "--pausa",
        type=float,
        default=0.5,
        help="Pausa (s) entre requisições. Padrão: 0.5.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lista o que seria baixado, sem acessar a rede.",
    )
    args = parser.parse_args()

    estacoes = carregar_estacoes(args.xml)
    estacoes = filtrar_estacoes_por_uf(
        estacoes, uf=args.uf, somente_ativas=not args.incluir_inativas
    )
    if args.bacia.strip():
        estacoes = filtrar_por_bacia(estacoes, args.bacia.split(","))

    janelas = janelas_anuais(args.ano_inicio, args.ano_fim)
    saida = Path(args.saida)
    saida.mkdir(parents=True, exist_ok=True)

    total_tarefas = len(estacoes) * len(janelas)
    print(f"Estações: {len(estacoes)}  |  Anos: {args.ano_inicio}-{args.ano_fim}  |  "
          f"Tarefas (estação×ano): {total_tarefas}")
    print(f"Saída: {saida}/dados_ana_<cod>_<ano>.xml\n")

    baixados = pulados = vazios = erros = 0

    for i, estacao in enumerate(estacoes, start=1):
        cod = estacao.get("cod_estacao")
        if not cod:
            continue

        for ano, di, df in janelas:
            destino = saida / f"dados_ana_{cod}_{ano}.xml"

            if destino.exists():
                pulados += 1
                continue

            rotulo = f"[{i}/{len(estacoes)}] {cod} {ano}"
            if args.dry_run:
                print(f"  BAIXARIA {rotulo}")
                baixados += 1
                continue

            try:
                xml_text = baixar_periodo(cod, di, df)
            except Exception as e:  # noqa: BLE001 - rede instável; registra e segue
                erros += 1
                print(f"  ERRO {rotulo}: {e}")
                # Recuo maior em caso de erro para não martelar o endpoint instável.
                time.sleep(max(args.pausa, 2.0))
                continue

            n = contar_registros(xml_text)
            # Só grava em sucesso (com ou sem dados); falhas ficam para a próxima execução.
            destino.write_text(xml_text, encoding="utf-8")
            if n:
                baixados += 1
                print(f"  OK   {rotulo}: {n} registros")
            else:
                vazios += 1
                print(f"  VAZIO {rotulo}: sem dados no período")

            time.sleep(args.pausa)

    print(f"\nResumo -> com dados: {baixados}  vazios: {vazios}  "
          f"pulados(já existiam): {pulados}  erros: {erros}")


if __name__ == "__main__":
    main()
