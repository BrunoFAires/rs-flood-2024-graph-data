# rs-flood-2024-graph-data

Coleta de dados hidrotelemétricos da **ANA** (Agência Nacional de Águas) para as
estações do Rio Grande do Sul durante as enchentes de 2024.

O script `ana_data.py` lê o catálogo nacional de estações telemétricas
(`ListaEstacoesTelemetricas.xml`), filtra as estações por UF e, opcionalmente,
baixa os dados hidrometeorológicos (vazão, nível e chuva) de cada estação no
período informado.

## Pré-requisitos

- [uv](https://docs.astral.sh/uv/) instalado
- Python 3.10+ (o próprio uv instala a versão necessária, se preciso)

## Instalação

Clonar o repositório e instalar as dependências:

```bash
uv sync
```

O comando cria automaticamente o ambiente virtual (`.venv`) e instala as
dependências travadas no `uv.lock`.

## Como executar

Não é necessário ativar o ambiente virtual — use `uv run`.

### Filtrar estações (sem baixar dados)

Gera um CSV apenas com as estações da UF informada:

```bash
uv run python ana_data.py \
  --xml ListaEstacoesTelemetricas.xml \
  --uf RS \
  --saida-estacoes estacoes_rs.csv
```

### Filtrar estações e baixar os dados da ANA

Adicione `--baixar-dados` para baixar o XML de dados de cada estação filtrada:

```bash
uv run python ana_data.py \
  --xml ListaEstacoesTelemetricas.xml \
  --uf RS \
  --data-inicio 01/04/2024 \
  --data-fim 01/08/2024 \
  --baixar-dados \
  --saida-ana dados_hidrotelemetricos_enchente
```

## Argumentos

| Argumento            | Obrigatório | Padrão                  | Descrição                                                        |
| -------------------- | ----------- | ----------------------- | ---------------------------------------------------------------- |
| `--xml`              | sim         | —                       | Caminho do XML `ListaEstacoesTelemetricas`.                      |
| `--uf`               | sim         | —                       | UF desejada (ex.: `RS`, `SC`, `PR`).                             |
| `--data-inicio`      | não         | `01/04/2024`            | Data inicial no formato `DD/MM/YYYY`.                            |
| `--data-fim`         | não         | `01/08/2024`            | Data final no formato `DD/MM/YYYY`.                              |
| `--saida-estacoes`   | não         | `estacoes_filtradas.csv`| CSV de saída com as estações filtradas.                         |
| `--saida-ana`        | não         | `dados_ana`             | Diretório para salvar as respostas XML da ANA.                  |
| `--baixar-dados`     | não         | (desligado)             | Baixa os dados hidrometeorológicos de cada estação filtrada.    |
| `--incluir-inativas` | não         | (desligado)             | Inclui também as estações inativas no filtro.                   |

## Saídas

- **CSV de estações** (`--saida-estacoes`): uma linha por estação, separado por
  `;`, codificação UTF-8 com BOM.
- **Dados da ANA** (`--saida-ana`): um arquivo `dados_ana_<codigo>.xml` por
  estação, com registros horários de vazão, nível e chuva. Estações sem dados no
  período retornam um XML com a mensagem *"Sem dados para esta estação"*.

### Estação artificial (Lago Guaíba → Lagoa dos Patos)

Quando `--uf RS`, o `ana_data.py` adiciona automaticamente ao CSV de saída uma
estação artificial **"Estação Lagoa dos Patos 1"** (`cod_estacao=90000001`,
`bacia=8`, sem dados reais — `origem=VENTO`). Ela não existe na ANA: representa
a saída do Lago Guaíba para a Lagoa dos Patos e é usada pelo `build_graph.py`
como destino de um fallback "artificial" no grafo (ver abaixo).

## Pipeline do dataset STGNN

Os scripts abaixo transformam os XMLs da ANA no dataset para a STGNN (nós =
estações, arestas = rede de drenagem).

```bash
# 1. Gerar o catálogo de estações da bacia (cod, lat/lon, NEXT_DOWN, etc.)
uv run python ana_data.py \
  --xml ListaEstacoesTelemetricas.xml \
  --uf RS \
  --saida-estacoes estacoes_rs.csv

# 2. Baixar histórico plurianual (uma requisição por estação/ano; retomável)
#    Janela padrão: 2020–2025 (anos completos). Ajuste com --ano-inicio/--ano-fim.
uv run python baixar_historico.py --xml ListaEstacoesTelemetricas.xml \
    --uf RS --bacia 8 --ano-inicio 2020 --saida dados_historicos

# 3. Aumentar dados: histórico de vento (Open-Meteo, uma requisição por estação)
#    Período inferido automaticamente do que existe em dados_historicos/
#    (inclui a estação artificial 90000001). --aumento escolhe o catálogo
#    de variáveis (ver AUMENTOS em aumentar_dados.py); padrão: vento.
uv run python aumentar_dados.py --aumento vento --estacoes estacoes_rs.csv \
    --dados-historicos dados_historicos --saida dados_vento

# 4. Pré-processar: XMLs + vento -> tensor [T x N x F] + máscara de ausências
uv run python preprocessar.py --entrada dados_historicos \
    --entrada-vento dados_vento --saida dataset_historico.npz

# 5. Construir o grafo de estações (HydroRIVERS NEXT_DOWN)
#    Requer o HydroRIVERS South America em ./hydrorivers (HydroRIVERS_v10_sa.shp).
uv run python build_graph.py --estacoes estacoes_rs.csv

# 6. Renderizar o grafo (mapa simples e overlay em satélite)
uv run python render_graph.py
uv run python render_satellite.py

# 7. (Opcional) Mapa de calor da cobertura/máscara do dataset
uv run python render_mask.py

# 8. Alinhar tensor (X, M) e grafo (A, W) num único arquivo
#    (interseção das estações presentes nos dois artefatos, mesma ordem de nós)
uv run python adjust_data_order.py \
    --tensor dataset_historico.npz --grafo grafo_hydrorivers.npz \
    --saida dataset_stgnn.npz
```

| Script | Papel | Principais saídas |
| --- | --- | --- |
| `ana_data.py` | gera o catálogo de estações da bacia a partir do XML da ANA | `estacoes_rs.csv` |
| `baixar_historico.py` | download plurianual da ANA (stdlib, retomável) | `dados_historicos/dados_ana_<cod>_<ano>.xml` |
| `aumentar_dados.py` | aumenta o dataset com variáveis externas do Open-Meteo (stdlib, retomável; catálogo `AUMENTOS`) | `dados_vento/vento_<cod>.json` |
| `preprocessar.py` | tensor `X` + máscara `M` (numpy) | `*.npz` (`X`, `M`, `timestamps`, `estacoes`, `features`) |
| `build_graph.py` | grafo dirigido de fluxo (pyshp) | `grafo_hydrorivers.npz`, `fluxo_arestas.geojson` |
| `render_graph.py` / `render_satellite.py` | figuras do grafo (matplotlib) | `grafo_guaiba*.png` |
| `render_mask.py` | mapa de calor da cobertura (máscara `M`) | `cobertura_mascara.png` |
| `adjust_data_order.py` | alinha estações de `X`/`M` e `A`/`W` (interseção + reordenação) | `dataset_stgnn.npz` (`X`, `M`, `A`, `W`, `estacoes`, ...) |

### Detalhe: exutório e fallback do Lago Guaíba (`build_graph.py`)

O grafo usa **`87450004` (CAIS MAUÁ C6, Rio Guaíba)** como exutório
(`--estacao-final`). Após montar as arestas via `NEXT_DOWN` do HydroRIVERS, o
grafo é filtrado para manter somente as estações conectadas (direta ou
indiretamente) a essa estação (`filtrar_componente_final`).

O HydroRIVERS não modela o Lago Guaíba como um corpo d'água conectado: rios que
desembocam no lago terminam sem `NEXT_DOWN`. Para contornar isso:

- `--lagoas` (padrão `lagoa_guaiba.geojson`) é o polígono do Lago Guaíba (OSM,
  baixado automaticamente via Overpass se o arquivo não existir. Estações cujo trecho final do rio cai a até
  `--limiar-lagoa-km` (padrão 5 km) do polígono recebem uma aresta de
  *fallback* até `87450004`.
- `EXCLUIR_FALLBACK` (`87242000`, `87450020`, `87460120`, `87480000`): estações
  dentro/perto do lago que **não** recebem esse fallback — apenas `87450004` e
  a estação de saída para a Lagoa dos Patos (`90000001`) representam o lago no
  grafo. Estações que ficam sem nenhuma ligação após essa exclusão são
  removidas pelo filtro de componente conexa.
- `FORCAR_FALLBACK` (`90000001`, a "Estação Lagoa dos Patos 1" — ver seção
  acima): recebe uma aresta `87450004 → 90000001`, sentido oposto ao fallback
  normal, pois representa a saída do Guaíba para a Lagoa dos Patos, desenhada
  como linha reta direta no GeoJSON, ignorando o limite normal de distância
  (`--limiar-conector-km`, padrão 40 km) usado para os demais conectores de
  fallback.

### Detalhe: como o `preprocessar.py` monta o tensor

Transforma os XMLs brutos da ANA (amostragem irregular — 15/30/60 min, timestamps
repetidos) em um tensor regular para STGNNs:

1. **Parse** — extrai `(estação, data/hora, {nível, chuva, vazão})` de cada XML da ANA,
   e `(estação, data/hora, {vento_vel, vento_dir})` de cada JSON do Open-Meteo (`dados_vento/`).
2. **Timezone** — o `DataHora` da ANA é horário de Brasília (`America/Sao_Paulo`); o
   vento do Open-Meteo é UTC. O `DataHora` da ANA é convertido para UTC antes de
   virar chave da grade horária, para as duas fontes representarem o mesmo instante real.
3. **Reamostragem horária** — arredonda cada leitura para a hora cheia.
4. **Agregação na hora** — `nível`/`vazão`/`vento_vel`/`vento_dir` = **média** (estado instantâneo);
   `chuva` = **soma** (mm acumulados na hora).
5. **Grade contínua** — linha do tempo hora a hora, sem buracos, comum a todas as
   estações (pré-requisito da STGNN).
6. **Vento mais próximo** — se uma hora da ANA não tem aferição de vento exatamente coincidente, usa-se a aferição de vento mais próxima da mesma estação, dentro de `LIMITE_VENTO_PROXIMO`
7. **Máscara** — `M = 1` onde houve leitura, `M = 0` onde faltou.

Saída (`.npz`):

- `X` — `float32 [T x N x F]` com os valores (`F = [nível, chuva, vazão, vento_vel, vento_dir]`);
- `M` — `uint8  [T x N x F]`, `1` = observado / `0` = ausente;
- `timestamps` (ISO, grade horária), `estacoes` (códigos = nós), `features` (nomes).

A estação artificial `90000001` (sem dados ANA) entra com `M=0` em
`nível/chuva/vazão` e `M=1` em `vento_vel`/`vento_dir` onde o Open-Meteo respondeu.

A ausência (~48% na janela 2020–2025) **não é erro**: reflete a amostragem irregular
e estações que não medem todas as variáveis. A máscara `M` é o que permite à STGNN
ignorar o que não foi medido e aprender a preencher essas lacunas. Para visualizar a
cobertura, rode `render_mask.py` (gera `cobertura_mascara.png`).
