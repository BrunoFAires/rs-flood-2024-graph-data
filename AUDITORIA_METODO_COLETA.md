# Audit of the current data-collection method (ANA telemetry)

**Goal:** verify that the existing pipeline collects and transforms the right
data correctly, before relying on it for the report.

**Date:** 2026-06-24
**Scope:** `baixar_historico.py` (download) → `preprocessar.py` (tensor build) →
`dataset_historico.npz`.

**Verdict:** ✅ The method is sound and faithful. Round-trip and value-level
checks match exactly. One real download gap was found and fixed; the remaining
caveats are documented assumptions, not errors.

---

## 1. Method under audit

- **Source:** ANA telemetry SOAP service
  `https://telemetriaws1.ana.gov.br/ServiceANA.asmx/DadosHidrometeorologicos`,
  queried per `(station, year)` over 2020–2025 (`baixar_historico.py`,
  resumable, one XML per request).
- **Variables parsed:** `Nivel`, `Chuva`, `Vazao`, keyed by `CodEstacao` +
  `DataHora` (`preprocessar.py:ler_xml`).
- **Transform:** parse → bucket to the hour → deduplicate (multiple sub-hourly
  readings in one bucket) → aggregate (`nível`/`vazão` = mean, `chuva` = sum) →
  place on a continuous hourly grid → build the missingness mask `M`.
- **Output:** `X` and `M` of shape `[T × N × F]`, `F = [nivel, chuva, vazao,
  vento_vel, vento_dir]` (wind from Open-Meteo).

---

## 2. Checks performed and results

### 2.1 Field parsing — ✅ correct
A raw record contains exactly `CodEstacao, DataHora, Vazao, Nivel, Chuva`,
matching the parser tags. The `DataHora` value has a trailing space
(`'2024-12-31 23:45:00 '`) which the parser strips before `strptime`. No fields
are missed or mismapped.

### 2.2 Round-trip fidelity — ✅ exact
Independently re-parsing the raw XMLs for station `87242000` and counting
observed hours, versus the saved tensor:

| | nível observed hours |
| --- | --- |
| Re-parsed from raw XML | 27,184 |
| `dataset_historico.npz` | 27,184 |
| **Difference** | **0** |

### 2.3 Aggregation / dedup — ✅ correct
For hours with 4 sub-hourly readings, the tensor value equals the expected
aggregate (verified to 1e-3):

| UTC hour | nível readings | expected mean | tensor | chuva sum | tensor |
| --- | --- | --- | --- | --- | --- |
| 2020-05-04 16:00 | 4 | 42.500 | 42.500 | 0.000 | 0.000 |
| 2020-05-04 17:00 | 4 | 44.750 | 44.750 | 0.000 | 0.000 |
| 2020-05-04 18:00 | 4 | 46.500 | 46.500 | 0.000 | 0.000 |

`nível`/`vazão` averaged and `chuva` summed per hour, as intended.

### 2.4 Download completeness — ✅ (one gap fixed)
- On disk: **1,721** XML files for 288 bacia-8 stations × 6 years (= 1,728
  expected).
- The 7 "missing" files were:
  - `90000001` (6 files) — the **artificial Lagoa dos Patos wind-only node**; it
    has no ANA telemetry **by design**. Not a gap.
  - **`86406000` / 2024 — a genuine gap.** This is a BHO graph node with ~70%
    coverage, and the missing year was **2024, the flood year**. It was
    **re-downloaded** (8,520 records); the raw set is now complete (1,722 files).

> ⚠️ Action required to fold this in: `dataset_historico.npz` was built before the
> fix and does **not** yet include `86406000`/2024. Re-run
> `uv run python preprocessar.py` to regenerate the tensor with this data.

### 2.5 Empty responses — ✅ expected, not errors
**779** of the files contain `"Sem dados"` — genuine empty responses from ANA for
sparse stations/years, correctly treated as no-data (mask `M = 0`). This is the
source of the many low-coverage nodes, not a collection bug.

### 2.6 Timezone alignment — ✅ assumption supported by evidence
`preprocessar.py` assumes ANA `DataHora` is **Brasília local time
(America/São_Paulo)** and converts to **UTC** to align with the Open-Meteo wind
(UTC). Evidence check on the 2024 flood peak:

| Station | Peak nível timestamp (our data) | Local (Brasília) |
| --- | --- | --- |
| `87242000` (Terminal Catsul) | 2024-05-05 08:00 UTC | **2024-05-05 05:00** |

The Lago Guaíba reached its historic record in the **early morning of 5 May
2024**, matching the 05:00 local peak. Had the data actually been UTC and been
wrongly shifted, the peak would fall at an implausible hour. This supports the
timezone handling as correct.

---

## 3. Documented assumptions / limitations

- **Timezone:** the Brasília-local assumption is supported by the flood-peak
  check but is not contractually guaranteed by ANA; worth a one-line note in the
  methodology. A 3-hour misalignment would be the failure mode if it were wrong.
- **Hourly resolution:** sub-hourly telemetry is bucketed to the hour; `chuva` is
  summed (incremental mm), `nível`/`vazão` averaged (instantaneous state).
- **Coverage:** mean coverage is moderate and uneven across nodes; the
  keep/drop-threshold decision is tracked separately (see
  `cobertura_por_no_bho.csv`).
- **Scope:** telemetry only. Conventional stations missed by telemetry are
  addressed by the planned HidroWebService path (see
  `COLETA_ALTERNATIVA_HIDROWEB.md`).

---

## 4. Conclusion

The current collection method gathers the **correct variables** from the correct
ANA endpoint and transforms them **faithfully** — round-trip and value-level
checks are exact, and the timezone handling is supported by the flood-peak
evidence. The only concrete defect (a missing 2024 file for a relevant node) was
found and fixed. The dataset should be regenerated once to incorporate it.
