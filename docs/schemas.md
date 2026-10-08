# LexAudit — Data Schemas (source of truth)

This file is the single source of truth for all inter-module data contracts
(GLOBAL RULES, working rule 2). Modules exchange only the structures defined
here.

Change control: if a schema must change, stop, describe the proposed change
in `lexaudit/TASK_NOTES.md`, and wait for human approval before touching code.

Conventions:

- Keys are `snake_case`; all IDs are strings.
- `Finding.quote` must be an exact substring of the owning block's `text`.
- `Finding.law_ref` values must come from live Adilet API responses — never
  hardcoded in code or prompts (working rule 3).
- User-facing text (`risk_explanation`, `proposed_text`, report content) is
  in Russian.

## Block

```json
{ "block_id": "b_0001", "type": "heading|paragraph|list_item|table_cell",
  "text": "cleaned plain text",
  "meta": { "style": "string|null", "table": "string|null",
            "row": "int|null", "col": "int|null", "order": "int" } }
```

## ContractDoc

```json
{ "doc_id": "d_xxxx", "filename": "contract.docx", "domain": null,
  "blocks": [Block], "stats": { "total_blocks": 0, "words": 0 } }
```

## Section (ARCH-1, approved addition)

```json
{ "section_id": "s_01", "title": "string|null", "block_ids": ["b_0001"],
  "text_llm": "joined analysis text",
  "skipped": [{ "block_id": "b_0002", "reason": "heading|empty|short|requisites" }] }
```

`block_ids` contains only blocks included in `text_llm`, in reading order.
Original blocks remain unchanged for report rendering. Content before the first
heading is titled «Преамбула». Nested headings remain within the parent section
until a heading of the same or higher level; headings are excluded from context.
Heading levels come from `meta.style` (Heading N / Заголовок N), defaulting to 1.
Sections exceeding `SECTION_MAX_CHARS` (default 6000) split at block boundaries
near 80% of the limit with one block of overlap where it fits and advances.
A single oversized block remains intact; overlap is omitted if two consecutive
blocks cannot fit within the limit. Skipped blocks are recorded once, in the
first chunk, including sections with no eligible content.

ARCH-2 adds `status: pending|drafted|needs_review` (default `pending`) and
`note: string|null` to Section. Draft failures and unmatched quotes set
`needs_review`; an empty valid list sets `drafted`. In-memory sections retain
exact block texts internally for quote validation; serialized sections can be
reconstructed from the ordered double-newline-separated text. If that mapping
is ambiguous, drafting returns an empty list with `block_text_mapping_unavailable`.

## DraftFinding (ARCH-2, approved addition)

```json
{ "draft_id": "df_s_01_001", "block_ids": ["b_0034"],
  "quote_hint": "подозрительная формулировка",
  "risk_hypothesis": "в чём возможный риск",
  "law_area": "защита прав потребителей",
  "search_queries": ["запрос для Adilet", "второй запрос"], "warnings": [] }
```

These are unverified hypotheses, not report findings or citations. Queries number
2–3; block IDs must belong to the section. Quote matching is case-insensitive
against a referenced block. After one repair, an unmatched quote is retained
with `warnings: ["quote_hint_not_found"]` and the section needs manual review.
Other invalid responses after repair return no hypotheses and mark the section
`needs_review` with `draft_validation_failed`; LLM failures use `llm_error`.
IDs are assigned locally per section to avoid cross-section collisions.

## CandidateArticle (ARCH-3, approved addition)

```json
{ "act_id": "real Adilet ID", "act_title": "act title",
  "url": "Adilet article link", "article_number": "article number from full text",
  "article_text": "full heading and paragraphs of the article" }
```

`gather_evidence` returns a dictionary from draft ID to at most three candidates.
Candidate selection is deterministic keyword matching with Russian suffix
normalization, ranked by matched keyword count and then act ID/article number.
Only in-force acts are included. No text is truncated. An article anchor is
included in the URL when available. Candidate relevance is a retrieval heuristic,
not proof of a violation; later verification must check applicability.

ARCH-3 adds DraftFinding `grounding: pending|candidates_found|ungrounded`
(default `pending`) and optional `suggested_article_numbers: [str]` (default []).
The draft prompt still prohibits suggesting article numbers; the optional field
supports upstream hints, which only match numbers actually present in a fetched
act. No corresponding articles are invented. Each query takes EVIDENCE_ACTS
acts (default 2); findings run with EVIDENCE_CONCURRENCY workers (default 6).
No candidates triggers one tool-free query-reformulation completion and one
search round, without a JSON repair. LLM transport retries remain those of the
existing wrapper. Failures are logged and recorded in warnings; they do not
stop other findings. Empty results are `ungrounded`, not a clean verdict.

## Finding

```json
{ "finding_id": "f_001", "block_id": "b_0001",
  "severity": "high|medium|low", "confidence": "confirmed|needs_review",
  "quote": "exact substring of the block's text",
  "proposed_text": "replacement for the quote",
  "risk_explanation": "why this violates RK law, in Russian",
  "law_ref": { "act_id": "string from Adilet", "act_title": "string",
               "article": "12", "clause": "2|null",
               "url": "https://adilet.zan.kz/..." } }
```

## BlockVerdict

ARCH-4 `verdict_findings(doc, sections, draft_findings, evidence)` returns the
existing AnalysisResult schema. Each grounded draft yields one Finding or JSON
null (hypothesis rejected). Evidence is the trusted output of gather_evidence.
Validation requires an exact non-empty quote in the referenced block's
`text_llm`, matching candidate act ID/article/title/URL, and unchanged placeholder
counts. One repair is allowed; persistent errors become `needs_review` with
`validation_failed`. Ungrounded drafts use `no_evidence_found`; provider errors
use `llm_error`. Section review status propagates to included and skipped blocks;
review takes precedence over risk. Other blocks default to `ok`. Phase statistics
count only verdict requests, their tokens, and elapsed time. No existing Finding,
BlockVerdict or AnalysisResult fields change.

```json
{ "block_id": "b_0001", "verdict": "ok|risk|skip|needs_review", "note": "string|null" }
```

## AnalysisResult

```json
{ "doc_id": "d_xxxx", "findings": [Finding], "verdicts": [BlockVerdict],
  "stats": { "blocks": 0, "llm_calls": 0, "tool_calls": 0,
             "tokens": 0, "duration_s": 0.0 } }
```

## Article

```json
{ "number": "12", "heading": "string|null",
  "paragraphs": ["string"], "anchor": "string|null" }
```

## FullAct

```json
{ "act_id": "string", "title": "string", "url": "string",
  "in_force": true, "fetched_at": "ISO-8601 timestamp",
  "articles": [Article] }
```

`Article` and `FullAct` are the Adilet full-text mirror contract. `anchor`
contains an HTML fragment identifier without `#`, suitable for appending to
`FullAct.url`. `get_article(act_id, article_number)` returns `Article | null`.

## ChatResult

```json
{ "content": "string", "tool_calls": [
    { "name": "string", "arguments": {} } ],
  "usage": { "prompt_tokens": 0, "completion_tokens": 0 },
  "latency_ms": 0.0 }
```

`ChatResult` is returned from the Alem.ai client. Tool call `arguments` is a
JSON object decoded from either native function calling or the JSON protocol.

## ClassificationResult

```json
{ "domain": "tourism|labor|real_estate|services|supply|lease|general",
  "rationale": "string", "search_hints": ["Russian Adilet query", "..."] }
```

`search_hints` contains 3–6 concrete Russian queries for Adilet. The
classification fallback after an invalid initial and repair response is
`{"domain":"general","rationale":"...","search_hints":[]}`.

## DocumentQueued

```json
{ "document_id": "string", "status": "queued" }
```

Returned after a document upload has been accepted for processing.

## DocumentStatus

```json
{ "document_id": "string",
  "status": "queued|extracting|classifying|analyzing|building_report|done|failed",
  "progress": "0/0", "error": "string|null" }
```

`progress` is the completed/total block count. Findings endpoints return an
array of `Finding` objects.
