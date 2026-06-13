"""
Transforma os XMLs hidrometeorológicos da ANA no tensor pronto para STGNN.

Saída (ver STGNN_DATASET_ROADMAP.md, §6):
  X : float32 [T tempos x N estações x F variáveis]   F = [nivel, chuva, vazao]
  M : uint8   [T x N x F]   1 = observado, 0 = ausente
  + eixos: timestamps (ISO, grade horária contínua), códigos de estação, nomes das variáveis

Pipeline: parse -> dedup -> reamostra p/ hora -> alinha numa grade comum -> máscara.
Agregação por hora: nivel/vazao = média (estado instantâneo); chuva = soma (mm acumulados).

Somente biblioteca padrão + numpy.

Exemplo:
  python preprocessar.py --entrada dados_hidrotelemetricos_enchente --saida dataset_2024.npz
"""

import argparse
import glob
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

FEATURES = ["nivel", "chuva", "vazao"]
# Como agregar várias leituras dentro da mesma hora:
AGG = {"nivel": "mean", "chuva": "sum", "vazao": "mean"}
_TAG = lambda t: t.split("}")[-1]


def _parse_float(txt):
    if txt is None:
        return None
    txt = txt.strip()
    if not txt:
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def ler_xml(path):
    """Extrai (cod_estacao, datahora_hora, {feature: valor}) de um arquivo XML."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return
    for el in root.iter():
        if _TAG(el.tag) != "DadosHidrometereologicos":
            continue
        campos = {_TAG(ch.tag): ch.text for ch in el}
        dh = (campos.get("DataHora") or "").strip()
        if not dh:
            continue
        try:
            ts = datetime.strptime(dh, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        hora = ts.replace(minute=0, second=0, microsecond=0)
        valores = {
            "nivel": _parse_float(campos.get("Nivel")),
            "chuva": _parse_float(campos.get("Chuva")),
            "vazao": _parse_float(campos.get("Vazao")),
        }
        cod = (campos.get("CodEstacao") or "").strip()
        yield cod, hora, valores


def coletar(entrada, data_min=None, data_max=None):
    """
    Lê todos os XMLs e agrega por (estacao, hora, feature).
    Dedup é implícito: várias leituras na mesma hora caem no mesmo balde.
    Registros fora de [data_min, data_max] são descartados (defesa contra
    timestamps inválidos, ex.: datas futuras que esticam a grade).
    Retorna: (dict[(cod, hora)] -> {feature: (soma, contagem)}, n_arquivos, n_descartados).
    """
    baldes = {}
    descartados = 0
    arquivos = sorted(glob.glob(str(Path(entrada) / "*.xml")))
    for path in arquivos:
        for cod, hora, valores in ler_xml(path):
            if not cod:
                continue
            if (data_min and hora < data_min) or (data_max and hora > data_max):
                descartados += 1
                continue
            chave = (cod, hora)
            balde = baldes.setdefault(chave, {f: [0.0, 0] for f in FEATURES})
            for f, v in valores.items():
                if v is not None:
                    balde[f][0] += v
                    balde[f][1] += 1
    return baldes, len(arquivos), descartados


def grade_horaria(t0, t1):
    """Grade horária contínua de t0 a t1 (inclusive)."""
    n = int((t1 - t0).total_seconds() // 3600) + 1
    return [t0 + timedelta(hours=i) for i in range(n)]


def construir_tensor(baldes):
    estacoes = sorted({cod for cod, _ in baldes})
    horas_obs = [h for _, h in baldes]
    t0, t1 = min(horas_obs), max(horas_obs)
    tempos = grade_horaria(t0, t1)

    idx_estacao = {c: i for i, c in enumerate(estacoes)}
    idx_tempo = {h: i for i, h in enumerate(tempos)}

    T, N, F = len(tempos), len(estacoes), len(FEATURES)
    X = np.full((T, N, F), np.nan, dtype=np.float32)
    M = np.zeros((T, N, F), dtype=np.uint8)

    for (cod, hora), balde in baldes.items():
        ti, ni = idx_tempo[hora], idx_estacao[cod]
        for fi, f in enumerate(FEATURES):
            soma, cont = balde[f]
            if cont:
                X[ti, ni, fi] = soma if AGG[f] == "sum" else soma / cont
                M[ti, ni, fi] = 1
    return X, M, tempos, estacoes


def relatorio(X, M, tempos):
    T, N, F = X.shape
    print(f"\nTensor X: [T={T} x N={N} x F={F}]  features={FEATURES}")
    print(f"Período: {tempos[0]} .. {tempos[-1]}  ({T} horas, grade contínua)")
    print(f"Estações (nós): {N}")
    obs = M.sum()
    print(f"Preenchimento global: {100*obs/M.size:.1f}% observado "
          f"({100*(1-obs/M.size):.1f}% ausente)")
    for fi, f in enumerate(FEATURES):
        cov = M[:, :, fi].mean() * 100
        nos = (M[:, :, fi].sum(axis=0) > 0).sum()
        print(f"  {f:6s}: {cov:5.1f}% das células observadas | {nos}/{N} estações têm algum dado")


def main():
    ap = argparse.ArgumentParser(description="Gera tensor STGNN a partir dos XMLs da ANA.")
    ap.add_argument("--entrada", default="dados_historicos",
                    help="Diretório com os arquivos dados_ana_*.xml "
                         "(padrão: dados_historicos, o histórico plurianual).")
    ap.add_argument("--saida", default="dataset_historico.npz", help="Arquivo .npz de saída.")
    ap.add_argument("--data-min", default=None,
                    help="Descarta registros antes desta data (YYYY-MM-DD). Padrão: sem limite.")
    ap.add_argument("--data-max", default=None,
                    help="Descarta registros depois desta data (YYYY-MM-DD). Padrão: agora "
                         "(defende contra timestamps futuros inválidos).")
    args = ap.parse_args()

    data_min = datetime.strptime(args.data_min, "%Y-%m-%d") if args.data_min else None
    data_max = (datetime.strptime(args.data_max, "%Y-%m-%d") if args.data_max
                else datetime.now())

    print(f"Lendo XMLs de: {args.entrada}  (janela válida: {data_min} .. {data_max})")
    baldes, n_arq, descartados = coletar(args.entrada, data_min, data_max)
    print(f"Arquivos lidos: {n_arq}  |  células (estação×hora) com leitura: {len(baldes)}"
          f"  |  registros descartados (fora da janela): {descartados}")

    X, M, tempos, estacoes = construir_tensor(baldes)
    relatorio(X, M, tempos)

    np.savez_compressed(
        args.saida,
        X=X,
        M=M,
        timestamps=np.array([t.isoformat() for t in tempos]),
        estacoes=np.array(estacoes),
        features=np.array(FEATURES),
    )
    print(f"\nSalvo: {args.saida}  ({Path(args.saida).stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
