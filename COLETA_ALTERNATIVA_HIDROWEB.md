# Alternative data source: ANA HidroWebService (conventional series)

**Status:** documented / prepared, **not yet active** — blocked on obtaining ANA
API credentials. This is a planned improvement, included here for the report.

**Date:** 2026-06-24
**Script:** `baixar_hidroweb.py` (inert without credentials)

---

## 1. Motivation — the gap this fills

The current pipeline collects data from ANA's **telemetry** service
(`telemetriaws1.ana.gov.br/ServiceANA.asmx/DadosHidrometeorologicos`, see
`baixar_historico.py`). Telemetry covers automatic stations only. Measured
against the BHO graph (257 nodes), **98 nodes have < 10% telemetry coverage, and
89 have exactly 0%** (see `cobertura_por_no_bho.csv` and
`grafo_bho_cobertura_satelite.png`).

Many of those empty nodes are **conventional stations** (manually read / régua /
CPRM gauges) that never report through telemetry, but whose historical series do
exist in ANA's **HidroWeb** database. Notable examples among the new BHO lake
nodes:

| Code | Station | Telemetry | Likely in HidroWeb |
| --- | --- | --- | --- |
| `03051043` | Porto Alegre – CPRM | none | yes (conventional/CPRM) |
| `87480000` | Barra do Ribeiro – Lago Guaíba | none | yes |
| `87460120` | Ipanema | trace (17 recs) | possibly |

The Porto Alegre gauge in particular is a high-value station for the 2024 flood.
Recovering these via HidroWeb would let us **keep more graph nodes** instead of
dropping them as empty.

---

## 2. The alternative: ANA HidroWebService REST API

ANA exposes a newer REST API that serves the **conventional (manual-collection)
historical series** — raw (*bruto*) and consistent (*consistido*) — for cotas
(level), chuva (rainfall) and vazão (flow).

- **Base URL:** `https://www.ana.gov.br/hidrowebservice`
- **OpenAPI spec:** `https://www.ana.gov.br/hidrowebservice/api-docs`
- **Swagger UI:** `https://www.ana.gov.br/hidrowebservice/swagger-ui/index.html`

**Validated live (2026-06-24):** the service is up; endpoints respond; auth is
enforced (probing without a token returns `401 Unauthorized`).

### Endpoints used

| Endpoint | Purpose |
| --- | --- |
| `GET /EstacoesTelemetricas/OAUth/v1` | Authenticate (headers `Identificador`, `Senha`) → Bearer JWT token |
| `GET /EstacoesTelemetricas/HidroInventarioEstacoes/v1` | Confirm a station exists / its metadata |
| `GET /EstacoesTelemetricas/HidroSerieCotas/v1` | Level (cota) series — conventional |
| `GET /EstacoesTelemetricas/HidroSerieChuva/v1` | Rainfall series — conventional |
| `GET /EstacoesTelemetricas/HidroSerieVazao/v1` | Flow (vazão) series — conventional |

### Request contract (series endpoints)

- Query parameters: `Código da Estação` (integer), `Tipo Filtro Data`
  (`DATA_LEITURA`), `Data Inicial (yyyy-MM-dd)`, `Data Final (yyyy-MM-dd)`.
- Auth header: `Authorization: Bearer <token>`.
- **Limit: 366 days per request** → the fetcher pages by calendar year.
- Token validity: **60 minutes** → the fetcher re-authenticates automatically on
  a `401`.
- Response: a generic envelope `{status, code, message, items}`; `items` holds
  the records.

---

## 3. Prepared tooling — `baixar_hidroweb.py`

A fetcher mirroring `baixar_historico.py`, ready to run once credentials exist:

- **Inert without credentials:** prints registration instructions and exits.
- Reads credentials from env vars `ANA_IDENTIFICADOR` / `ANA_SENHA`, or from a
  git-ignored `ana_credentials.json`.
- `--inventario` mode confirms the target stations exist before pulling.
- Downloads `cotas`/`chuva`/`vazão` per station per year into `dados_hidroweb/`,
  one JSON per `(tipo, station, year)`, **resumable** (skips existing).
- Defaults to the empty lake nodes (`03051043`, `87480000`, `87460120`),
  overridable via `--estacoes`.

> Security: `ana_credentials.json` and `dados_hidroweb/` are in `.gitignore`; the
> API token must never be committed.

---

## 4. How to obtain credentials (pending action)

1. Request API access at **https://www.snirh.gov.br/hidroweb/acesso-api**
   ("Solicite Acesso API"). It is free; provide full name, institution, and a
   short justification (academic research — STGNN dataset for the 2024 RS
   floods). ANA also accepts the request by email per the HidroWebService manual.
2. ANA returns an **Identificador** and **Senha**.
3. Provide them to the script (env vars or `ana_credentials.json`).
4. Run: `uv run python baixar_hidroweb.py --inventario` then
   `uv run python baixar_hidroweb.py`.

A draft request email (Portuguese) was prepared separately.

---

## 5. Caveats to validate when active

- These endpoints serve **conventional** stations; the `--inventario` step will
  confirm whether each target code (e.g. the leading-zero CPRM code `03051043`)
  is registered with available series, or whether the correct HIDRO code must be
  resolved first.
- HidroWeb series are typically **daily** (cotas/vazão) — coarser than telemetry.
  Integrating them into the hourly tensor would require a resampling/merge
  decision (e.g. forward-fill within the day, or a separate daily channel).
- The consistency level (`bruto` vs `consistido`) should be recorded per series.

---

## 6. Summary

The HidroWebService is a validated, concrete path to recover data for the
conventional stations that telemetry misses — especially the Porto Alegre and
Lago Guaíba gauges. Tooling is in place (`baixar_hidroweb.py`); the only blocker
is the free ANA registration. Until then, the current dataset relies on telemetry
only, and empty conventional nodes remain candidates to drop.
