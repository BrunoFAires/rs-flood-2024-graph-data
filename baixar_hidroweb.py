"""
Baixa séries históricas (consistidas/brutas) de estações CONVENCIONAIS da ANA
via a API HidroWebService — complementa a telemetria (telemetriaws1, usada em
baixar_historico.py) para estações que não reportam telemetria, como as
estações de régua/CPRM do Lago Guaíba (ex.: Porto Alegre-CPRM 03051043,
Barra do Ribeiro 87480000).

Contrato da API (extraído de https://www.ana.gov.br/hidrowebservice/api-docs):
  Base: https://www.ana.gov.br/hidrowebservice
  Auth: GET /EstacoesTelemetricas/OAUth/v1
        headers: Identificador, Senha  -> envelope {status, items:{tokenautenticacao}}
        token Bearer JWT, validade 60 min.
  Inventário: GET /EstacoesTelemetricas/HidroInventarioEstacoes/v1
        query: "Código da Estação" (ou "Unidade Federativa"/"Código da Bacia")
  Séries convencionais (coleta manual), limite 366 dias por requisição:
        GET /EstacoesTelemetricas/HidroSerieCotas/v1   (nível/cota)
        GET /EstacoesTelemetricas/HidroSerieChuva/v1   (chuva)
        GET /EstacoesTelemetricas/HidroSerieVazao/v1   (vazão)
        query: "Código da Estação", "Tipo Filtro Data"=DATA_LEITURA,
               "Data Inicial (yyyy-MM-dd)", "Data Final (yyyy-MM-dd)"
        header: Authorization: Bearer <token>

CREDENCIAIS (NÃO versionar): defina as variáveis de ambiente
  ANA_IDENTIFICADOR / ANA_SENHA
ou crie um arquivo JSON (padrão: ana_credentials.json, já ignorável no git):
  {"identificador": "...", "senha": "..."}

Sem credenciais o script apenas explica como obtê-las e sai (inerte).

Saída: dados_hidroweb/<tipo>_<cod>_<ano>.json  (um arquivo por estação/tipo/ano,
retomável — pula o que já existe). Não modifica o dataset; é só coleta bruta.

Uso:
  uv run python baixar_hidroweb.py --inventario          # confere se as estações existem
  uv run python baixar_hidroweb.py                        # baixa cotas/chuva/vazão 2020-2025
  uv run python baixar_hidroweb.py --estacoes 03051043,87480000 --ano-inicio 2014
"""

import argparse
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://www.ana.gov.br/hidrowebservice"
OAUTH = "/EstacoesTelemetricas/OAUth/v1"
INVENTARIO = "/EstacoesTelemetricas/HidroInventarioEstacoes/v1"
SERIES = {
    "cotas": "/EstacoesTelemetricas/HidroSerieCotas/v1",
    "chuva": "/EstacoesTelemetricas/HidroSerieChuva/v1",
    "vazao": "/EstacoesTelemetricas/HidroSerieVazao/v1",
}

# Estações novas do grafo BHO sem (ou quase sem) telemetria — alvo principal.
ESTACOES_PADRAO = ["03051043", "87480000", "87460120"]

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

INSTRUCOES = """\
=== Sem credenciais da API HidroWebService ===
Esta API exige cadastro (gratuito) na ANA. Passos:
  1. Acesse a página de solicitação de acesso:
       https://www.snirh.gov.br/hidroweb/acesso-api
     (alternativa: solicite por e-mail à ANA informando nome completo, instituição
      e a motivação do uso; ver o manual oficial do HidroWebService).
  2. A ANA fornece um Identificador e uma Senha de acesso à API.
  3. Forneça as credenciais ao script de UMA destas formas (não versione!):
       a) variáveis de ambiente:
            export ANA_IDENTIFICADOR='seu_identificador'
            export ANA_SENHA='sua_senha'
       b) arquivo JSON (padrão ana_credentials.json):
            {"identificador": "seu_identificador", "senha": "sua_senha"}
  4. Rode de novo: uv run python baixar_hidroweb.py --inventario
O token de autenticação vale 60 min; o script reautentica sozinho quando expira.
"""


def carregar_credenciais(cred_path):
    ident = os.environ.get("ANA_IDENTIFICADOR")
    senha = os.environ.get("ANA_SENHA")
    if ident and senha:
        return ident, senha
    p = Path(cred_path)
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        ident = d.get("identificador") or d.get("ANA_IDENTIFICADOR")
        senha = d.get("senha") or d.get("ANA_SENHA")
        if ident and senha:
            return ident, senha
    return None, None


def _get(path, headers=None, params=None, timeout=120):
    url = BASE + path
    if params:
        # nomes de parâmetros têm espaços/parênteses: codifica como %20/%28/%29
        url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="")
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            body = {"error": str(e)}
        return e.code, body


def autenticar(identificador, senha):
    st, body = _get(OAUTH, headers={"Identificador": identificador, "Senha": senha})
    if st not in (200, 201):
        raise RuntimeError(f"Falha na autenticação ({st}): {body}")
    items = body.get("items") or {}
    # procura uma chave de token no envelope
    token = None
    if isinstance(items, dict):
        for k, v in items.items():
            if "token" in k.lower() and isinstance(v, str) and v:
                token = v
                break
    if not token:
        raise RuntimeError(f"Token não encontrado na resposta de auth: {body}")
    return token


class Cliente:
    """Cliente com token e reautenticação automática em 401."""
    def __init__(self, identificador, senha):
        self.ident = identificador
        self.senha = senha
        self.token = autenticar(identificador, senha)

    def get(self, path, params=None):
        hdr = {"Authorization": f"Bearer {self.token}"}
        st, body = _get(path, headers=hdr, params=params)
        if st == 401:                    # token expirou -> reautentica e tenta 1x
            self.token = autenticar(self.ident, self.senha)
            hdr = {"Authorization": f"Bearer {self.token}"}
            st, body = _get(path, headers=hdr, params=params)
        return st, body


def n_items(body):
    it = body.get("items")
    if isinstance(it, list):
        return len(it)
    if isinstance(it, dict):
        return 1
    return 0


def inventario(cli, estacoes):
    print("== Inventário (HidroInventarioEstacoes) ==")
    for cod in estacoes:
        st, body = cli.get(INVENTARIO, {"Código da Estação": int(cod)})
        it = body.get("items")
        if st == 200 and it:
            reg = it[0] if isinstance(it, list) else it
            nome = reg.get("Estacao_Nome") or reg.get("nome") or "?"
            tipo = reg.get("Tipo_Estacao") or reg.get("tipo") or "?"
            print(f"  {cod}: OK — {nome} (tipo={tipo})")
        else:
            print(f"  {cod}: não encontrado / sem retorno (status {st})")


def baixar(cli, estacoes, tipos, ano_inicio, ano_fim, saida, pausa):
    saida = Path(saida)
    saida.mkdir(parents=True, exist_ok=True)
    baixados = pulados = vazios = erros = 0
    for cod in estacoes:
        for tipo in tipos:
            path = SERIES[tipo]
            for ano in range(ano_inicio, ano_fim + 1):
                destino = saida / f"{tipo}_{cod}_{ano}.json"
                if destino.exists():
                    pulados += 1
                    continue
                params = {
                    "Código da Estação": int(cod),
                    "Tipo Filtro Data": "DATA_LEITURA",
                    "Data Inicial (yyyy-MM-dd)": f"{ano}-01-01",
                    "Data Final (yyyy-MM-dd)": f"{ano}-12-31",
                }
                rotulo = f"{tipo} {cod} {ano}"
                try:
                    st, body = cli.get(path, params)
                except Exception as e:  # noqa: BLE001 - rede instável
                    erros += 1
                    print(f"  ERRO  {rotulo}: {e}")
                    time.sleep(max(pausa, 2.0))
                    continue
                if st != 200:
                    erros += 1
                    print(f"  ERRO  {rotulo}: status {st} {body.get('message','')}")
                    time.sleep(max(pausa, 2.0))
                    continue
                destino.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
                n = n_items(body)
                if n:
                    baixados += 1
                    print(f"  OK    {rotulo}: {n} registros")
                else:
                    vazios += 1
                    print(f"  VAZIO {rotulo}: sem dados")
                time.sleep(pausa)
    print(f"\nResumo -> com dados: {baixados}  vazios: {vazios}  "
          f"pulados: {pulados}  erros: {erros}")


def main():
    ap = argparse.ArgumentParser(description="Baixa séries convencionais da ANA via HidroWebService.")
    ap.add_argument("--estacoes", default=",".join(ESTACOES_PADRAO),
                    help="Códigos separados por vírgula.")
    ap.add_argument("--tipos", default="cotas,chuva,vazao",
                    help="Tipos de série: cotas,chuva,vazao.")
    ap.add_argument("--ano-inicio", type=int, default=2020)
    ap.add_argument("--ano-fim", type=int, default=2025)
    ap.add_argument("--saida", default="dados_hidroweb")
    ap.add_argument("--credenciais", default="ana_credentials.json")
    ap.add_argument("--inventario", action="store_true",
                    help="Só consulta o inventário (confere se as estações existem).")
    ap.add_argument("--pausa", type=float, default=0.5)
    args = ap.parse_args()

    ident, senha = carregar_credenciais(args.credenciais)
    if not (ident and senha):
        print(INSTRUCOES)
        raise SystemExit(0)

    estacoes = [c.strip() for c in args.estacoes.split(",") if c.strip()]
    tipos = [t.strip() for t in args.tipos.split(",") if t.strip() in SERIES]

    print(f"Autenticando na API HidroWebService...")
    cli = Cliente(ident, senha)
    print("Autenticado. Token obtido.\n")

    if args.inventario:
        inventario(cli, estacoes)
        return
    inventario(cli, estacoes)
    print()
    baixar(cli, estacoes, tipos, args.ano_inicio, args.ano_fim, args.saida, args.pausa)


if __name__ == "__main__":
    main()
