PROJECT: LexAudit — AI legal auditor for Kazakhstan contracts. Input: .docx contract.
Output: interactive HTML report flagging clauses that violate RK law, each with a fix
and a citation from Adilet (adilet.zan.kz). No citation = no recommendation.

STACK (fixed, do not change or add frameworks):
- Python 3.11+, python-docx, httpx, Jinja2, FastAPI, uvicorn, pydantic v2,
  pydantic-settings, openai (as generic OpenAI-compatible client), pytest, rich.
- SQLite by default (PostgreSQL only in Phase 3).
- Frontend: vanilla HTML/CSS/JS only, no build tools, no CDN dependencies.

DIRECTORY LAYOUT:
lexaudit/
  config/        # settings.py, .env.example
  extractor/     # docx -> JSON blocks
  adilet/        # API client, tools, cache
  llm/           # Alem.ai client wrapper
  agent/         # analysis loop, validation, trace
  pii/           # redaction (Phase 2)
  report/        # templates/, builder
  pipeline/      # orchestration
  api/           # FastAPI (Phase 2)
  prompts/       # v1/, v2/... versioned prompt files
  tests/fixtures/  # sample contracts + expected results
  docs/          # schemas.md, adilet-api.md, alem-api.md
  cli.py, TASK_NOTES.md

WORKING RULES:
1. Touch ONLY the files of the current task. Do not refactor other modules.
2. All inter-module communication goes through the schemas in docs/schemas.md.
   If a schema must change: stop, describe the proposed change in TASK_NOTES.md,
   and wait for human approval.
3. Never hardcode law article numbers or Adilet act IDs in code or prompts.
   They may only come from live Adilet API responses.
4. If a real API (Adilet, Alem.ai) differs from what a brief assumes: verify the
   real behavior, adapt, and document it in docs/adilet-api.md / docs/alem-api.md.
   Do not guess silently.
5. Every external HTTP call and every LLM call must be logged to logs/ as JSONL
  (ts, endpoint/model, latency, status, token usage). Never log secrets.
6. All configuration via .env through config/settings.py. No magic values in code.
7. On any error: raise a typed exception or mark the item as failed — never
   swallow errors and continue silently.
8. Code identifiers and comments in English. User-facing text, prompts, and
   report content in Russian (contracts are in Russian).
9. Every module must be runnable standalone: `python -m lexaudit.<module> --help`.
