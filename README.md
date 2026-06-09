# rs-flood-2024-graph-data

Coleta de dados hidrotelemétricos da **ANA** (Agência Nacional de Águas) para as
estações do Rio Grande do Sul durante as enchentes de 2024.

O script `ana_data.py` lê o catálogo nacional de estações telemétricas
(`ListaEstacoesTelemetricas.xml`), filtra as estações por UF e, opcionalmente,
baixa os dados hidrometeorológicos (vazão, nível e chuva) de cada estação no
período informado.

## Pré-requisitos

- [uv](https://docs.astral.sh/uv/) instalado
- Python 3.14+ (o próprio uv instala a versão necessária, se preciso)

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
