"""
Transforma os XMLs hidrometeorológicos da ANA + histórico de vento
no tensor pronto para STGNN.

Saída (ver STGNN_DATASET_ROADMAP.md, §6):
  X : float32 [T tempos x N estações x F variáveis]   F = [nivel, chuva, vazao, vento]
  M : uint8   [T x N x F]   1 = observado, 0 = ausente
  + eixos: timestamps (ISO, grade horária contínua), códigos de estação, nomes das variáveis

Pipeline: parse -> dedup -> reamostra p/ hora -> alinha numa grade comum -> máscara.
Agregação por hora: nivel/vazao = média (estado instantâneo); chuva = soma (mm acumulados);
vento = média.

Estações sem dados ANA mas com vento (ex.: 90000001) entram no tensor com
M=0 em nivel/chuva/vazao e M=1 em vento onde o Open-Meteo respondeu.

Timezone: o DataHora da ANA é horário de Brasília (America/Sao_Paulo) e o
vento do Open-Meteo é UTC. O DataHora é convertido para UTC antes de virar
a chave da grade horária, para as duas fontes ficarem alinhadas no mesmo
instante real. Quando não há aferição de vento exatamente na mesma hora,usa-se a aferição de vento mais próxima da mesma estação (até LIMITE_VENTO_PROXIMO).

Somente biblioteca padrão + numpy.

Exemplo:
  python preprocessar.py --entrada dados_hidrotelemetricos_enchente \\
      --entrada-vento dados_vento --saida dataset_2024.npz
"""

import argparse
import bisect
import glob
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import re

import numpy as np

FEATURES = ["nivel", "chuva", "vazao", "vento_vel", "vento_dir"]
# Como agregar várias leituras dentro da mesma hora:
AGG = {"nivel": "mean", "chuva": "sum", "vazao": "mean", "vento_vel": "mean", "vento_dir": "mean"}
_TAG = lambda t: t.split("}")[-1]

# DataHora da ANA vem em horário de Brasília (America/Sao_Paulo) o vento do Open-Meteo vem em UTC. Convertendo a ANA para UTC, as duas grades casam.
_TZ_ANA = ZoneInfo("America/Sao_Paulo")
_TZ_UTC = ZoneInfo("UTC")

# Tolerância para casar uma leitura de vento com uma hora da ANA quando não
# há aferição exatamente na mesma hora (ex.: bordas do período após a
# conversão de timezone, ou falhas pontuais do Open-Meteo).
LIMITE_VENTO_PROXIMO = timedelta(hours=3)


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
        hora_local = ts.replace(minute=0, second=0, microsecond=0, tzinfo=_TZ_ANA)
        hora = hora_local.astimezone(_TZ_UTC).replace(tzinfo=None)
        valores = {
            "nivel": _parse_float(campos.get("Nivel")),
            "chuva": _parse_float(campos.get("Chuva")),
            "vazao": _parse_float(campos.get("Vazao")),
        }
        cod = (campos.get("CodEstacao") or "").strip()
        yield cod, hora, valores


def ler_vento(path):
    """Extrai (cod_estacao, datahora_hora, {"vento_vel", "vento_dir"}) de um JSON do Open-Meteo."""
    stem = Path(path).stem.removeprefix("vento_")
    cod = re.sub(r"_\d{4}$", "", stem)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    horario = data.get("hourly") or {}
    tempos = horario.get("time") or []
    vel = horario.get("wind_speed_10m") or []
    dire = horario.get("wind_direction_10m") or []
    for i, t in enumerate(tempos):
        try:
            hora = datetime.strptime(t, "%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        v = vel[i] if i < len(vel) else None
        d = dire[i] if i < len(dire) else None
        yield cod, hora, {
            "vento_vel": float(v) if v is not None else None,
            "vento_dir": float(d) if d is not None else None,
        }


def coletar(entrada, entrada_vento=None, data_min=None, data_max=None):
    """
    Lê todos os XMLs (ANA) e JSONs de vento (Open-Meteo) e agrega por
    (estacao, hora, feature).
    Dedup é implícito: várias leituras na mesma hora caem no mesmo balde.
    Registros fora de [data_min, data_max] são descartados (defesa contra
    timestamps inválidos, ex.: datas futuras que esticam a grade).

    Para horas da ANA sem aferição de vento exatamente coincidente, usa a
    leitura de vento mais próxima da mesma estação (até LIMITE_VENTO_PROXIMO).

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

    if not entrada_vento:
        return baldes, len(arquivos), descartados

    vento_por_estacao = {}
    arquivos_vento = sorted(glob.glob(str(Path(entrada_vento) / "*.json")))
    for path in arquivos_vento:
        for cod, hora, valores in ler_vento(path):
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
            vento_por_estacao.setdefault(cod, []).append((hora, valores))

    for leituras in vento_por_estacao.values():
        leituras.sort(key=lambda par: par[0])

    for (cod, hora), balde in baldes.items():
        if balde["vento_vel"][1] > 0:
            continue
        leituras = vento_por_estacao.get(cod)
        if not leituras:
            continue
        horas_vento = [h for h, _ in leituras]
        idx = bisect.bisect_left(horas_vento, hora)
        candidatos = [i for i in (idx - 1, idx) if 0 <= i < len(horas_vento)]
        if not candidatos:
            continue
        melhor = min(candidatos, key=lambda i: abs(horas_vento[i] - hora))
        if abs(horas_vento[melhor] - hora) > LIMITE_VENTO_PROXIMO:
            continue
        _, valores = leituras[melhor]
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
    ap.add_argument("--entrada-vento", default="dados_vento",
                    help="Diretório com os arquivos vento_<cod>.json (Open-Meteo). "
                         "Vazio/inexistente = ignora vento.")
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

    entrada_vento = args.entrada_vento if Path(args.entrada_vento).is_dir() else None
    print(f"Lendo XMLs de: {args.entrada}  |  vento de: {entrada_vento or '(nenhum)'}  "
          f"(janela válida: {data_min} .. {data_max})")
    baldes, n_arq, descartados = coletar(args.entrada, entrada_vento, data_min, data_max)
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
