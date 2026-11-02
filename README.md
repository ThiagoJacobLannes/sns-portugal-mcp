# sns-portugal-mcp

<!-- mcp-name: io.github.ThiagoJacobLannes/sns-portugal-mcp -->

> [!WARNING]
> **Deprecated — use [portugal-mcp](https://github.com/ThiagoJacobLannes/portugal-mcp) instead.** This server now lives on as the `saude` feature of portugal-mcp, together with IPMA weather, INE, Banco de Portugal, Parliament, public contracts and more. Install with `uvx portugal-mcp` (add `PORTUGAL_MCP_FEATURES=saude` to load only this part). Tool names: `find_institutions` → `sns_find_institutions`, `emergency_stats` → `sns_emergency_stats`, `surgery_wait_stats` → `sns_surgery_wait_stats`, `consultation_wait_stats` → `sns_consultation_wait_stats`, `search_datasets` → `sns_search_datasets`, `query_dataset` → `sns_query_dataset`. This repository gets no further updates.


MCP server for **official open-data statistics about Portugal's national health service (SNS)**, so Claude (or any MCP client) can answer "how busy is the emergency department at Santa Maria?", "how many people are waiting for surgery in the Norte region?" or "which SNS hospitals are near me?".

**Read-only, no key, no account.** It uses only public open data. Not affiliated with the SNS, SPMS or the Portuguese Ministry of Health.

> **Statistics, not the live queue.** The figures are official monthly statistics; the newest month is usually 2-3 months old and provisional. This server does **not** show live emergency-room waiting times. For those, see [tempos.min-saude.pt](https://tempos.min-saude.pt).
>
> **Not medical advice.** In an emergency call **112**. For health advice call **SNS 24** on **808 24 24 24**.

## Tools

### `find_institutions`
Finds SNS hospitals and local health units (ULS) by name, or the nearest ones to a coordinate. Each result says whether the place reports emergency, consultation and surgery data, and gives `stats_entity`: the name the statistics use for it. Statistics are published per ULS or hospital group, not per building.

Parameters: `query` (name, accent-insensitive), `lat` / `lon` (sort by distance), `emergency_only`, `limit`.

### `emergency_stats`
Emergency-department visits per month, by type (general, paediatric, obstetric, psychiatric) and by Manchester triage colour (red, orange, yellow, green, blue, white, not triaged), with each colour's share of the month. Covers one institution, one region, or the whole country.

Parameters: `institution`, `region` (`Norte` | `Centro` | `LVT` | `Alentejo` | `Algarve`), `period` (`"YYYY-MM"`, latest month to include), `months` (default 6).

### `surgery_wait_stats`
Surgery access per month:
- the surgical waiting list (LIC) at month end;
- the % of waiting patients still within the legal maximum guaranteed time (TMRG);
- the mean days from hospital admission to the operation (scheduled inpatient surgery);
- surgeries performed: scheduled (inpatient and day surgery) and urgent.

Same parameters as `emergency_stats`. The open data has no breakdown by specialty.

### `consultation_wait_stats`
Hospital outpatient consultations per month (first and follow-up), plus the % of first specialist consultations (referred by a family doctor) held within the TMRG. Same parameters as above.

### `search_datasets`
Searches the 149 datasets of the SNS Transparency portal by keyword. Titles are in Portuguese, so Portuguese keywords work best: `"ocupação"`, `"partos"`, `"rastreios"`, `"SNS 24"`, `"INEM"`. Returns each dataset's id, description and field names.

### `query_dataset`
Runs an ODSQL query (`where`, `select`, `group_by`, `order_by`, `limit`, `offset`) on any portal dataset. Use it for anything the tools above don't cover.

### Year-to-date values
Most hospital-activity datasets publish **year-to-date cumulative** values, so the December figure is the annual total. The curated tools return both the year-to-date value and a derived `in_month` value (this month minus the previous one). If you use `query_dataset` directly, do that subtraction yourself.

## Install

**Quickest** (needs [uv](https://docs.astral.sh/uv/)): no clone needed, it runs straight from PyPI.

```bash
claude mcp add sns-portugal -- uvx sns-portugal-mcp
```

For Claude Desktop, use `"command": "uvx", "args": ["sns-portugal-mcp"]`.

**From source:**

Requires Python 3.10+.

```bash
git clone https://github.com/ThiagoJacobLannes/sns-portugal-mcp
cd sns-portugal-mcp
python -m pip install -e .
```

On Windows, if you keep code in a cloud-synced folder (Google Drive, OneDrive), create the virtual environment **outside** it. Sync clients drop `desktop.ini` files into folders, which can crash packages on startup.

## Add to Claude Code

```bash
claude mcp add sns-portugal -- python -m sns_portugal_mcp.server
```

## Add to Claude Desktop

Merge `claude_desktop_config.snippet.json` into your Claude Desktop `claude_desktop_config.json`, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "sns-portugal": {
      "command": "python",
      "args": ["-m", "sns_portugal_mcp.server"]
    }
  }
}
```

## Data sources

| Source | What it gives | Access | Licence / terms |
|---|---|---|---|
| [SNS Transparency portal](https://transparencia.sns.gov.pt) ([Opendatasoft Explore API v2.1](https://transparencia.sns.gov.pt/api/explore/v2.1/catalog/datasets)) | 149 datasets of monthly SNS statistics, published by the SNS Executive Direction (DE-SNS) | Open, no key | No licence is set in any dataset's metadata (checked 2026-10-31). The portal's terms allow reuse under Portuguese public-sector-information law (Lei n.º 26/2016): cite the source, keep the data's integrity and context, and flag any changes you make. Some of the same datasets are mirrored on [dados.gov.pt](https://dados.gov.pt); there, some copies are marked CC-BY and others "not specified". |
| [tempos.min-saude.pt/institutions.json](https://tempos.min-saude.pt/institutions.json) | List of hospitals and ULS with coordinates and which data each reports | Public static file, no key | No licence stated. |

Datasets used by the curated tools:

| Tool | Datasets |
|---|---|
| `emergency_stats` | `atendimentos-por-tipo-de-urgencia-hospitalar-link`, `atendimentos-em-urgencia-triagem-manchester` |
| `surgery_wait_stats` | `inscritos-em-lic-dentro-do-tmrg-180-dias`, `demora-media-antes-da-cirurgia`, `intervencoes-cirurgicas` |
| `consultation_wait_stats` | `consultas-em-tempo-real`, `01_sica_evolucao-mensal-das-consultas-medicas-hospitalares` |

Data derived from these sources keeps the source's terms. The code in this repository is MIT-licensed.

**What this server does not do.** It does not call `tempos.min-saude.pt/api/*`: that API needs a key embedded in the site's own web app, and this server does not use other people's keys. It also does not scrape the live waiting-time report (an embedded Power BI report).

## How it works

The server calls the portal's public Explore API with `httpx` and aggregates in the query itself (`group_by tempo`, `sum(...)`), so regional and national totals are computed on the server side. Hospital names are matched without regard to accents. A hospital such as "Hospital de Faro" is mapped to the ULS it reports under ("ULS do Algarve") using the institution list. The dataset catalog, the institution list and the institution names are cached in memory for 6 hours. If an upstream call fails or a query is malformed, the tool returns a clear `{"error": ...}` and does not guess.

Environment overrides (optional): `SNS_ODS_BASE_URL`, `SNS_INSTITUTIONS_URL`.

## License

MIT
