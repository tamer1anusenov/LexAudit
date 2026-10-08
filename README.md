# LexAudit

LexAudit reviews Russian-language DOCX contracts against the legislation of
Kazakhstan. It extracts the document, looks up laws in Adilet, checks article
texts, and builds an interactive HTML report with proposed corrections and
links to legal sources.

The project is a development prototype. Findings require human review; a
`needs_review` result means the system could not complete or substantiate a
check. Runtime and quality depend on the document, model, provider limits,
and availability of Adilet.

## Quick start

Requirements: **Python 3.11+**, an Alem.ai API key, and network access to Alem.ai
and Adilet. Run commands from the repository root.

```bash
git clone git@github.com:tamer1anusenov/LexAudit.git
cd LexAudit
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
```

Edit `.env` and set `ALEM_API_KEY`. Set `ALEM_MODEL` to a model your key can use;
the example uses `gemma4`, which was exercised in the recorded API probe.
See [provider notes](docs/alem-api.md) for tool-calling details.

Start the backend:

```bash
.venv/bin/python -m lexaudit.api
```

Open **http://127.0.0.1:8000**, upload a `.docx` file up to 10 MB, and follow its
status. When analysis finishes, open the report from the document list.
Stop the server with `Ctrl+C`.

## How analysis works

1. Extract paragraphs, headings, list items, and table cells into stable blocks.
2. Classify the legal domain and generate search hints.
3. Analyze the contract using the selected mode.
4. Validate quotes and legal references, then render an offline HTML report.

| Mode | Behavior | Current status |
|---|---|---|
| `per_block` | Sequential reasoning loop for each block, with Adilet tool calls and citation validation. | Default |
| `sections` | Pack blocks into sections; draft risk hypotheses; retrieve candidate articles in parallel; issue validated verdicts in parallel. | Experimental |

Select the experimental path for one server run:

```bash
AGENT_MODE=sections .venv/bin/python -m lexaudit.api
```

Or set `AGENT_MODE=sections` in `.env` and restart. The default remains
`per_block` until a human approves the A/B evaluation. A faster result does
not by itself establish equal legal coverage.

In section mode, headings, blocks shorter than 40 characters, and blocks
matching requisites patterns are excluded from LLM context. They remain in the
rendered contract. Long sections split between blocks with overlap where it
fits. Unverified hypotheses become manual-review items rather than confirmed
violations.

## CLI

Analyze a complete document:

```bash
.venv/bin/python -m lexaudit.cli run contract.docx --out out/ --prompts v1
```

Use smaller inputs while developing:

```bash
.venv/bin/python -m lexaudit.cli run contract.docx --max-blocks 5
.venv/bin/python -m lexaudit.cli run contract.docx --only-blocks b_0003,b_0007
```

Inspect extraction and section packing without calling the LLM:

```bash
.venv/bin/python -m lexaudit.extractor contract.docx
.venv/bin/python -m lexaudit.extractor sections contract.docx
```

The CLI writes `out/{doc_id}/` containing:

| Artifact | Contents |
|---|---|
| `blocks.json` | Extracted blocks selected for analysis |
| `classification.json` | Domain and search hints |
| `findings.json` | Validated findings |
| `trace.jsonl` | Analysis trace |
| `review.html` | Self-contained interactive report |

The report supports proposed edits, acceptance/rejection, manual editing,
local feedback, and clean HTML export. Decisions are stored in the browser's
localStorage; generated reports are not committed to Git.

## API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/` | Upload page and document list |
| `POST` | `/api/documents` | Upload a DOCX; returns `document_id` and `queued` |
| `GET` | `/api/documents/{id}` | Status, progress, and error |
| `GET` | `/api/documents/{id}/report` | Completed HTML report |
| `GET` | `/api/documents/{id}/findings` | Findings JSON |
| `GET` | `/healthz` | Service health |

```bash
curl -F 'file=@contract.docx' http://127.0.0.1:8000/api/documents
curl http://127.0.0.1:8000/api/documents/DOCUMENT_ID
```

Statuses: `queued → extracting → classifying → analyzing → building_report → done`,
or `failed`. Document records and the Adilet cache use SQLite. Analysis runs in
background threads; `PIPELINE_MODE=celery` is reserved and currently returns
HTTP 501 on upload. There is no authentication, and interrupted jobs do not
resume automatically after a server restart.

## Configuration

All application settings are read through
[settings.py](lexaudit/config/settings.py). The complete example is
[.env.example](.env.example).

| Setting | Purpose |
|---|---|
| `ALEM_API_KEY`, `ALEM_BASE_URL`, `ALEM_MODEL` | Provider credentials, endpoint, and model |
| `LLM_TOOL_MODE` | `auto`, `native`, or JSON protocol fallback |
| `LLM_TIMEOUT_SECONDS`, `LLM_RETRIES` | Timeout and retry policy |
| `AGENT_MODE` | `per_block` or `sections` |
| `SECTION_MAX_CHARS` | Section size target; default 6000 |
| `EVIDENCE_CONCURRENCY`, `VERDICT_CONCURRENCY` | Parallel workers; default 6 each |
| `EVIDENCE_ACTS` | Acts fetched per search query; default 2 |
| `SEARCH_CACHE_TTL_DAYS`, `ACT_REFRESH_DAYS` | Search cache and full-act freshness |
| `API_MAX_WORKERS` | Concurrent document workers; default 2 |
| `DATABASE_PATH`, `LOG_DIR`, `PIPELINE_OUTPUT_DIR` | Local storage paths |
| `PROMPTS_VERSION` | Versioned instruction set; default `v1` |

Provider rate limits can make high concurrency counterproductive. Tune worker
counts to the account's limits. Prompts live in `lexaudit/prompts/v1/` so legal
methodology can be reviewed independently of application code.

## Tests and evaluation

Run the test suite:

```bash
.venv/bin/python -m pytest -q
```

Fixtures contain planted issues in tourism, labor, and lease contracts, plus a
clean control. The four generated DOCX files are versioned; arbitrary local
contracts are ignored. They can also be regenerated:

```bash
.venv/bin/python tests/fixtures/generate_fixtures.py
```

Evaluate one mode or compare both:

```bash
.venv/bin/python -m lexaudit.eval run --fixtures tests/fixtures/ --prompts v1
.venv/bin/python -m lexaudit.eval run --fixtures tests/fixtures/ --prompts v1 --both
```

Evaluation makes real, billable LLM requests and queries Adilet. It writes
`out/eval_report.md`; `--both` also records the comparison in
[task notes](lexaudit/TASK_NOTES.md). Metrics include planted-issue recall,
false positives on `clean.docx`, provider request count, tokens, and wall time.
The two modes share the Adilet mirror, so cache warmth can affect timings.

An authenticated provider probe is available:

```bash
.venv/bin/python -m lexaudit.llm.ping
```

It tests plain completion, native tools, and JSON protocol and updates
`docs/alem-api.md` with the observed results.

## Data handling and current limitations

Contract text is currently sent to the configured cloud LLM **without PII
redaction**; the `pii/` package is a scaffold. Logs and traces can contain
contract text and model responses. API keys are redacted from structured logs,
but that is not contract anonymization. Local uploads, databases, reports,
logs, environments, and `.env` files are excluded from Git.

The service is intended for local development and supervised evaluation.
Authentication, durable job queues, automatic restart recovery, and production
quality guarantees are not implemented.

## Repository layout

```text
lexaudit/
  extractor/   DOCX blocks and legal sections
  adilet/      Search client, SQLite cache, full-act mirror
  llm/         Alem.ai wrapper and protocol probe
  prompts/     Versioned Russian instructions
  agent/       Legacy loop, draft, evidence, verdict, validation, traces
  pipeline/    Classification and analysis dispatch
  report/      Jinja2 templates and standalone HTML builder
  api/         FastAPI service and vanilla JavaScript upload page
  eval/        Fixture evaluation and mode comparison
  config/      Environment-backed settings
  pii/         Placeholder package for future redaction
  cli.py       End-to-end command-line pipeline
docs/          Schemas and provider integration notes
tests/         Tests, generated contracts, Adilet response fixtures
```

Read [GLOBAl_RULES.md](GLOBAl_RULES.md) before changing the project. Inter-module
contracts are defined in [docs/schemas.md](docs/schemas.md); implementation
history and evaluation results are in [lexaudit/TASK_NOTES.md](lexaudit/TASK_NOTES.md).
