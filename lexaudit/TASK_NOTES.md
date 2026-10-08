# TASK_NOTES

Per GLOBAL RULES this file is the channel for schema change proposals and
per-task decisions. Schema changes must be proposed here and approved by a
human before code is changed (working rule 2).

## Pending schema change proposals

### T2.1 — PII-safe block text (deferred)
- Extend `Block` with `text_llm: string|null`. `text` remains the original
  contract text for local display and extraction; `text_llm` contains its
  redacted form for any LLM input. The report restores originals using a
  local placeholder-to-original mapping that is never sent to the model.
- No `Finding` field changes are proposed: findings are validated against the
  redacted block text and restored for display with the same mapping.
- Proposed mapping contract: a JSON object from unique numbered placeholders
  (for example `[PERSON_1]`) to their original matched values.
- Deferred by the user's explicit instruction to send original contract text to
  the cloud LLM as-is. No PII redaction is applied by the API pipeline.

### T2.2 — Document API response contracts
- Add a `DocumentQueued` response schema with `document_id` and literal status
  `queued` for `POST /api/documents`.
- Add a `DocumentStatus` response schema with `document_id`, status
  (`queued|extracting|classifying|analyzing|building_report|done|failed`),
  progress (`n/m`), and nullable error for `GET /api/documents/{id}`.
- Findings responses use the existing `Finding` schema. Artifact paths and
  timestamps remain storage fields and are not returned by these endpoints.
- Approved; implementation may proceed.

## Decisions

### T2.2 — Document API response contracts
- The user requested a working upload and analysis service, approving these
  response contracts. Added `DocumentQueued` and `DocumentStatus` to
  `docs/schemas.md`.

### T2.2 — REST service
- Added a FastAPI upload/status/report/findings/health service backed by the
  SQLite `documents` table and a local worker thread pool.
- `PIPELINE_MODE=inline` is implemented; `celery` returns HTTP 501 until T2.3.
- Per explicit user instruction, uploaded contract text is sent to the cloud
  LLM without PII redaction.

### Runtime latency investigation
- A 123-block document had two concurrent uploads. The status records were
  separate, but the old API trace path used the extractor content hash, so
  both jobs appended to the same trace file.
- Agent tool results previously forwarded entire Adilet acts into the next
  model request; trace usage reached about 124k tokens for a single response.
- New API jobs use per-upload trace IDs, reuse one LLM client for a document,
  and send a configured, relevance-ranked excerpt of up to five articles and
  12,000 characters. The full Adilet act remains local for article validation.
- API startup now forces JSONL logging even when Uvicorn has already attached
  root handlers.
- Re-uploading identical bytes while that document is active now returns the
  active document ID instead of launching a duplicate LLM job.

### T1.1 — Scaffold, config, core schemas
- Package `lexaudit` created with the module layout from GLOBAl_RULES.md
  (config, extractor, adilet, llm, agent, pii, report, pipeline, api, prompts).
  Each module package ships a `__main__.py` stub so
  `python -m lexaudit.<module> --help` works from day one (working rule 9).
- `docs/schemas.md` created as the source of truth for the five core schemas
  (Block, ContractDoc, Finding, BlockVerdict, AnalysisResult).
- `ALEM_BASE_URL` ships as a placeholder (`https://api.alemdata.ai/v1`); the
  real value is to be verified in T1.4 and recorded in docs/alem-api.md.
- Logging: JSON lines to `logs/app.jsonl` + rich console; secret redaction
  filter; structured `extra=` fields (ts, endpoint/model, latency, status,
  token usage) are copied into JSON lines (working rule 5).
- `make run` currently invokes the CLI stub (prints version/help); it will be
  re-pointed at the pipeline / API when those tasks land.

### T1.2 — Adilet client + search_laws tool
- No anonymous JSON API exists on adilet.zan.kz: the SPA's
  `/api/documents/search` demands a user token (verified: 401 with
  "Missing credentials"). Per the brief's fallback, the client parses the
  legacy portal `old.adilet.zan.kz/{lang}/search/docs/…` HTML (stdlib `re`,
  no new dependencies). All findings are documented in docs/adilet-api.md,
  and 6 real captured responses live in tests/adilet_fixtures/.
- New settings (no schema changes, so no approval needed):
  `ADILET_SEARCH_BASE_URL` (default `https://old.adilet.zan.kz`, where
  search is served) and `SEARCH_CACHE_TTL_DAYS` (default 7).
  `ADILET_BASE_URL` stays the canonical base for `SearchHit.url`.
- A browser-like User-Agent is mandatory: non-browser UAs get stuck in a
  302 loop on old.adilet.zan.kz. Browser UA is the client default.
- `in_force` semantics: only `status_yts` («Утративший силу») → false;
  `status_new`/`status_upd`/`status_err`/absent → true (verified live:
  2015 Labor Code K1500000414 = upd → in force; 2007 code K070000251_ =
  yts → not).
- `SearchHit` is strict (`extra="forbid"`, frozen): exactly act_id, title,
  snippet, url, in_force.
- `python -m lexaudit.adilet "query" [--limit N] [--language rus|kaz|eng]`
  prints JSON; it wires the central logging (JSONL + rich) like cli.py.

### T1.3 — Full text and local law mirror
- Added `Article` and `FullAct` to `docs/schemas.md` using the structures
  specified in the T1.3 brief; added their public lookup contract.
- Full text is fetched from the verified legacy HTML document route and uses
  the existing retry and per-request logging path. SQLite `acts` and
  `articles` rows are keyed by act ID and language; `ACT_REFRESH_DAYS`
  defaults to 90 and `force=True` bypasses the cache read.
- Captured HTML uses paragraph IDs such as `z327` rather than the brief's
  illustrative `p...` form, so `Article.anchor` stores the actual first body
  paragraph identifier (without `#`).

### T1.4 — Alem.ai LLM client
- Added `ChatResult` to `docs/schemas.md` as specified in the task contract.
- `lexaudit.llm.client` is the sole Alem.ai chat-completions integration.
  It uses the OpenAI SDK, three total attempts for retryable failures, JSONL
  request logging with bounded content, and configurable native/JSON/auto
  tool modes.
- The Alem Plus documentation lists `https://llm.alem.ai/v1` as its
  OpenAI-compatible base URL. Authenticated probes of plain, native-tool,
  and JSON-protocol requests are recorded in `docs/alem-api.md` by
  `python -m lexaudit.llm.ping`.

### T1.5 — Versioned prompts
- Added the Russian v1 system, classification, finding contract, tool JSON
  protocol, and diagnostic prompts under `lexaudit/prompts/v1/`.
- `lexaudit.prompts.registry.load(version, name)` loads prompt Markdown;
  passing `None` selects `PROMPTS_VERSION`. `list_versions()` enumerates
  installed prompt versions.
- Moved the JSON tool-protocol instructions and LLM probe wording from Python
  into versioned prompt files so model instructions can be reviewed there.

### T1.6 — Legal Agent core
- Added `adilet/tools.py` with OpenAI function schemas and dispatch for the
  search and full-text Adilet contracts.
- Added a per-block agent loop, schema/evidence validation, one repair attempt,
  typed Adilet/LLM fallbacks, and JSONL analysis traces at
  `logs/analysis_{doc_id}.jsonl`.
- Added `AGENT_MODE`, `AGENT_MAX_STEPS`, and the configured summary limits;
  only `per_block` mode is accepted for this phase.
- `python -m lexaudit.agent --doc FILE [--blocks ID ...]` runs the analysis
  against all or selected DOCX blocks and prints the `AnalysisResult` JSON.

### T1.7 — Domain classifier
- Added `pipeline.classify_document(doc)` with first-30-block input capped at
  8000 characters, strict output validation, and one prompt-driven repair call.
- The classifier prompt requires 3–6 concrete Russian Adilet queries; an
  invalid repair response returns the documented `general` fallback.
# ARCH-1 — Section chunker

User approved additive Section and SkippedBlock schemas; existing schemas are
unchanged. Added extractor/sections.py and the extractor sections CLI, with
SECTION_MAX_CHARS configuration (default 6000). Nested headings belong to their
parent, filtered blocks retain reasons, and oversized sections split between
blocks near 80% with overlap where feasible. Indivisible oversized blocks remain
intact. This task does not connect sections to the agent or change reports.
# ARCH-2 — Draft phase

Added versioned Russian draft and draft_repair prompts, DraftFinding schema,
and agent/draft.py. One tool-free completion per section (temperature 0.2,
max_tokens 1500), with at most one repair. Unknown blocks and malformed outputs
fail safely; unmatched quotes after repair retain a warning and require review.
Section gains draft status/note as additive metadata; exact source block texts
are retained privately while packing for robust quote checks. No agent pipeline
integration or verified report findings are changed in this task.
# ARCH-3 — Evidence phase

Added deterministic parallel Adilet candidate retrieval, CandidateArticle schema,
EVIDENCE_CONCURRENCY=6 and EVIDENCE_ACTS=2 settings, and a versioned Russian
query-reformulation prompt. Shared per-run fetch futures deduplicate repeated
queries/acts. Keywords use simple suffix normalization; candidates are capped
at three complete articles, with live IDs and optional paragraph anchors.
One bounded LLM reformulation is allowed only after no candidates; failures
remain local to the draft and are logged/marked. DraftFinding has additive
grounding metadata and optional article-number hints (not generated by draft).
This task does not connect evidence collection to the end-to-end pipeline.
# ARCH-4 — Verdict phase

Added Russian verdict/repair prompts and agent/verdict.py. Grounded drafts run
with VERDICT_CONCURRENCY=6, temperature 0.1, max_tokens 900, one repair maximum.
Exact quotes, candidate source identity and unchanged placeholder multiplicity
are mechanically checked. Null rejects an unsupported hypothesis. Ungrounded
drafts and section failures produce manual-review verdicts; review takes priority.
Existing report/evaluation schemas stay unchanged. Stats cover only this phase.
Evidence input is trusted gather_evidence output; this phase never invents or
re-fetches acts. Main pipeline integration is outside this brief.
# ARCH-5 — Wire-in and A/B referee

Added pipeline/analyze.py dispatch and connected CLI, API and agent entrypoint.
AGENT_MODE defaults to per_block; sections performs draft → evidence → verdict
and uses the existing AnalysisResult/report contract. Section traces and stats
include every phase. Optional client factories allow tracking without duplicating
provider transport. Eval --both uses isolated per-fixture logs to count actual
provider requests (including classification/retries) and token usage, and writes
a comparison to eval_report.md and this file. Only the human selects a new
default. The clean fixture is excluded from planted-issue recall denominators.

## ARCH-5 — A/B evaluation

## Сравнение режимов

| Файл | Режим | Найдено / посажено | Recall | FP clean | LLM-запросы | Токены | Время, с | Статус |
|---|---|---:|---:|---:|---:|---:|---:|---|
| 1. Договор Субаренды_Шаблон1 (1) (3).docx | per_block | 0/0 | — | — | 297 | 729732 | 126.44 | OK |
| 1. Договор Субаренды_Шаблон1 (1) (3).docx | sections | 0/0 | — | — | 33 | 85778 | 71.84 | OK |
| clean.docx | per_block | 0/1 | 0.0% | 0 | 50 | 86575 | 50.01 | OK |
| clean.docx | sections | 0/1 | 0.0% | 0 | 8 | 5912 | 5.96 | OK |
| labor.docx | per_block | 4/4 | 100.0% | — | 62 | 231391 | 127.64 | OK |
| labor.docx | sections | 1/4 | 25.0% | — | 16 | 24383 | 27.17 | OK |
| lease.docx | per_block | 0/3 | 0.0% | — | 52 | 151330 | 83.81 | OK |
| lease.docx | sections | 0/3 | 0.0% | — | 11 | 119150 | 49.61 | OK |
| tourism.docx | per_block | 2/3 | 66.7% | — | 112 | 252144 | 176.84 | OK |
| tourism.docx | sections | 1/3 | 33.3% | — | 18 | 30303 | 42.08 | OK |
| **Итого** | **per_block** | 6/11 | 54.5% | 0 | 573 | 1451172 | 564.75 | OK |
| **Итого** | **sections** | 2/11 | 18.2% | 0 | 86 | 265526 | 196.65 | OK |

LLM-запросы включают классификацию и повторные HTTP-попытки; токены — usage провайдера.
Время — полный CLI-пайплайн. Режимы используют общее зеркало Adilet: второй запуск может выиграть от кеша.
Документы без посаженных проблем не входят в знаменатель recall; FP измеряются только на clean.docx.
Режим по умолчанию остаётся per_block; решение принимает человек.
