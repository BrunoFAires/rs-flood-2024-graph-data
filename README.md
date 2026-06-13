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

## Pipeline do dataset STGNN

Os scripts abaixo transformam os XMLs da ANA no dataset para a STGNN (nós =
estações, arestas = rede de drenagem).

```bash
# 1. Baixar histórico plurianual (uma requisição por estação/ano; retomável)
#    Janela padrão: 2020–2025 (anos completos). Ajuste com --ano-inicio/--ano-fim.
uv run python baixar_historico.py --xml ListaEstacoesTelemetricas.xml \
    --uf RS --bacia 8 --ano-inicio 2020 --saida dados_historicos

# 2. Pré-processar: XMLs -> tensor [T x N x F] + máscara de ausências
uv run python preprocessar.py --entrada dados_historicos --saida dataset_historico.npz

# 3. Construir o grafo de estações (HydroRIVERS NEXT_DOWN)
#    Requer o HydroRIVERS South America em ./hydrorivers (HydroRIVERS_v10_sa.shp).
uv run python build_graph.py --estacoes estacoes_rs.csv

# 4. Renderizar o grafo (mapa simples e overlay em satélite)
uv run python render_graph.py
uv run python render_satellite.py

# 5. (Opcional) Mapa de calor da cobertura/máscara do dataset
uv run python render_mask.py

# 6. Alinhar tensor (X, M) e grafo (A, W) num único arquivo
#    (interseção das estações presentes nos dois artefatos, mesma ordem de nós)
uv run python adjust_data_order.py \
    --tensor dataset_historico.npz --grafo grafo_hydrorivers.npz \
    --saida dataset_stgnn.npz
```

| Script | Papel | Principais saídas |
| --- | --- | --- |
| `baixar_historico.py` | download plurianual da ANA (stdlib, retomável) | `dados_historicos/dados_ana_<cod>_<ano>.xml` |
| `preprocessar.py` | tensor `X` + máscara `M` (numpy) | `*.npz` (`X`, `M`, `timestamps`, `estacoes`, `features`) |
| `build_graph.py` | grafo dirigido de fluxo (pyshp) | `grafo_hydrorivers.npz`, `fluxo_arestas.geojson` |
| `render_graph.py` / `render_satellite.py` | figuras do grafo (matplotlib) | `grafo_guaiba*.png` |
| `render_mask.py` | mapa de calor da cobertura (máscara `M`) | `cobertura_mascara.png` |
| `adjust_data_order.py` | alinha estações de `X`/`M` e `A`/`W` (interseção + reordenação) | `dataset_stgnn.npz` (`X`, `M`, `A`, `W`, `estacoes`, ...) |

### Detalhe: como o `preprocessar.py` monta o tensor

Transforma os XMLs brutos da ANA (amostragem irregular — 15/30/60 min, timestamps
repetidos) em um tensor regular para STGNNs:

1. **Parse** — extrai `(estação, data/hora, {nível, chuva, vazão})` de cada XML.
2. **Reamostragem horária** — arredonda cada leitura para a hora cheia.
3. **Agregação na hora** — `nível`/`vazão` = **média** (estado instantâneo do rio);
   `chuva` = **soma** (mm acumulados na hora).
4. **Grade contínua** — linha do tempo hora a hora, sem buracos, comum a todas as
   estações (pré-requisito da STGNN).
5. **Máscara** — `M = 1` onde houve leitura, `M = 0` onde faltou.

Saída (`.npz`):

- `X` — `float32 [T x N x F]` com os valores (`F = [nível, chuva, vazão]`);
- `M` — `uint8  [T x N x F]`, `1` = observado / `0` = ausente;
- `timestamps` (ISO, grade horária), `estacoes` (códigos = nós), `features` (nomes).

A ausência (~48% na janela 2020–2025) **não é erro**: reflete a amostragem irregular
e estações que não medem as três variáveis. A máscara `M` é o que permite à STGNN
ignorar o que não foi medido e aprender a preencher essas lacunas. Para visualizar a
cobertura, rode `render_mask.py` (gera `cobertura_mascara.png`).
