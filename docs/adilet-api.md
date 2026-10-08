# Adilet API — discovery notes (T1.2, verified 2026-10-06 with real requests)

This document is the record of what actually exists on the Adilet portal,
verified with real HTTP requests on 2026-10-06. Nothing here is guessed:
every endpoint below was probed live.

## TL;DR

- **There is no public anonymous JSON API.** The new SPA at `adilet.zan.kz`
  searches via `GET /api/documents/search`, but that endpoint requires a user
  token (`Authorization: Bearer <access>`) — anonymous requests get
  `{"error":"Unauthorized","message":"Missing credentials. Use Authorization:
  Bearer … or X-API-Key: ***"}`.
- **The anonymous search is the legacy HTML portal** at `old.adilet.zan.kz`,
  server-rendered. The client parses its results pages (stdlib `re`, no
  extra deps).
- **Canonical document URLs** (used in `SearchHit.url`) are on the main
  host: `https://adilet.zan.kz/{lang}/docs/{act_id}` — those pages return
  HTTP 200 and are indexed by search engines (SEO shell + content).

## 1. New SPA (`adilet.zan.kz`) — what it does

`GET https://adilet.zan.kz/rus/` returns a 1.5 KB React shell
(`#root`, `<script src="/assets/index-CJZ1O8en.js">`).

From the JS bundle (`/assets/index-CJZ1O8en.js`, 535 KB, minified):

- Axios instance: `baseURL: "/api"`, `timeout: 20000`.
- Endpoints found in the bundle:
  | endpoint | notes |
  |---|---|
  | `GET /api/documents/search` | the SPA search; params passed through from the UI |
  | `GET /api/documents` | recent-docs list; `page_size` clamped to 1..200, `document_type` |
  | `GET /api/documents/{id}` | single document; response `data.document ?? data` |
  | `GET /api/documents/{id}/versions` | paginated, `page_size=500`, up to 10 pages |
  | `GET /api/documents/type-counts` | document-type counts |
  | `POST /api/auth/login`, `/auth/register`, `/auth/reset-password` | user auth |
  | `GET /api/auth/refresh`, `POST /api/auth/revoke` | token refresh/logout |
  | `GET /stats/online` | online counter |
- Auth: the request interceptor attaches `Authorization: Bearer <access>`
  from a stored session; there is **no guest/anonymous token flow**.
- Language map in the bundle: `ru → "rus"`, `kz → "kaz"`, `en → "eng"`.
- The bundle also contains the literal string `https://old.adilet.zan.kz`
  (used for the «Полная версия сайта» / full-version link) — the legacy
  portal is a first-class, intended fallback.

Live probes against the new API (all unauthenticated):

```
GET /api/documents/search?q=зарплата        → 401 {"error":"Unauthorized",…}
GET /api/documents/search?query=зарплата    → 401 (same)
POST /api/auth/login {}                     → 401 {"error":"UNAUTHENTICATED","message":"invalid credentials"}
GET  /api/auth/refresh                      → 200 empty body
```

Conclusion: the JSON API is unusable for LexAudit without a registered user
account, which is out of scope. (If an account ever becomes available,
`/api/documents/search` would be the upgrade path — the request/param shape
is what the SPA sends, but its JSON response was never observed.)

## 2. Legacy HTML portal (`old.adilet.zan.kz`) — the endpoint used

### Search results

```
GET https://old.adilet.zan.kz/{lang}/search/docs/{fulltext}={query}&pagesize={n}&page={p}
    lang ∈ rus | kaz | eng
    query — percent-encoded (UTF-8)
    pagesize — 10 (default) | 20 | 50 | 100
    page — 1-based
```

Verified facts:

- `?fulltext=…&page=…&pagesize=…` (query-string form) answers with
  **302 → the path form above** and sets a `JSESSIONID` cookie; the client
  therefore requests the path form directly (200 on first try).
- A **browser-like `User-Agent` is required**: requests with `curl/…` or no
  UA get stuck in a 302 redirect loop that never reaches a 200. The client
  sends `Mozilla/5.0 (X11; Linux x86_64) … Chrome/126.0 Safari/537.36`.
- Total hit count is in the breadcrumbs:
  `<span class="onlyprint">… Найдено: <strong>211</strong> документов</span>`
  (Kazakh: «Табылды: … құжаттар»).
- Spell-check suggestion (useful signal for bad queries):
  `<div id="spellcheck">Возможно, Вы имели в виду: <a …>заpплата</a></div>`.
- In-force filtering exists as a facet, e.g.
  `/rus/search/docs/ir=1_002` = «Труд» branch; the full branch list is on
  the homepage (`ir=1_…` with Russian branch names).
- Document links are always absolute-path: `/rus/docs/{act_id}`
  (Kazakh pages link `/kaz/docs/{act_id}`). The `act_id` grammar observed:
  `V11CB000106`, `K1500000375`, `P090000452_`, `G25CD00351M`,
  `Z1500000286`, `S2300000020`, `T1800000120`, `U910000541_` — letters +
  digits, sometimes trailing underscore. **The act_id is the last path
  segment; never invent or normalize it.**
- Document pages: `https://adilet.zan.kz/{lang}/docs/{act_id}` → 200
  (new-SPA shell with SEO title `<title>{title} - ИПС «Әділет»</title>`);
  the legacy host `old.adilet.zan.kz/{lang}/docs/{act_id}` also 200 and
  serves full server-rendered text (useful for T1.3 act-text fetching).

### Result item structure (one per `<div class="post_holder">`)

```html
<div class="post_holder">
  <h4 class="post_header">
    <span class="post_number">1.</span>
    <a href="/rus/docs/V11CB000106">Об организации и объемах общественных
      работ за счет средств местного бюджета</a>
  </h4>
  <span class="status status_yts">Утративший силу</span>
  <p>Постановление акимата … от 1 апреля 2011 года № 105. … Утратило силу
     постановлением … от 07 июля 2016 года № 125</p>
  <blockquote>, Талдысай, Богеткол, Таскожа)   1 мин. <b>зарплата</b>     2 …
  </blockquote>
</div>
```

Fields mapped to `SearchHit`:

| SearchHit field | source |
|---|---|
| `act_id` | href of the `<h4 class="post_header">` link, last segment |
| `title` | link text (tags stripped, whitespace collapsed, entities unescaped) |
| `snippet` | first `<p>` after the header (document metadata: kind, date, №, registration); falls back to `<blockquote>` excerpt |
| `url` | `https://adilet.zan.kz/{lang}/docs/{act_id}` (main host, canonical) |
| `in_force` | `false` iff `<span class="status status_yts">` present, else `true` |

Observed status classes (rus / kaz):

| class | rus | kaz | in_force |
|---|---|---|---|
| `status_yts` | Утративший силу | Күшін жойған | **false** |
| `status_new` | Новый | Жаңа | true |
| `status_upd` | Обновленный | Жаңартылған | true |
| `status_err` | Исправление ошибки | — | true |
| *(no span at all)* | — | — | true |

Sanity check: the 2015 Labor Code `K1500000414` returns
`status_upd`/«Обновленный» → `in_force: true`; the superseded 2007 code
`K070000251_` returns `status_yts` → `in_force: false`. Both verified live.

## 3. Captured fixtures (offline tests)

Saved under `tests/adilet_fixtures/` (real responses, 2026-10-06):

| file | request |
|---|---|
| `search_salary_rus_10.html` | `GET /rus/search/docs/fulltext=зарплата&pagesize=10&page=1` |
| `search_labor_code_rus_10.html` | `GET /rus/search/docs/fulltext=трудовой кодекс&pagesize=10&page=1` |
| `search_lease_rus_10.html` | `GET /rus/search/docs/fulltext=аренда помещения&pagesize=10&page=1` |
| `search_salary_kaz_10.html` | `GET /kaz/search/docs/fulltext=жалақы&pagesize=10&page=1` |
| `document_K1500000414_labor_code_rus.html` | `GET /rus/docs/K1500000414` (2015 Labor Code, full text) |
| `document_K1500000375_business_code_rus.html` | `GET /rus/docs/K1500000375` (2015 Entrepreneurial Code) |

## 4. Client behavior (lexaudit/adilet/client.py)

- `httpx.Client`, `timeout=15 s`, `follow_redirects=True`, browser UA.
- Retries: 2 extra attempts, exponential backoff (1.5 s, 3 s), on
  network errors/timeouts and HTTP 429/5xx. 4xx → immediate
  `AdiletClientError`. Exhausted retries → `AdiletUnavailable`.
- Every request logged to `logs/app.jsonl` (structured extras: `endpoint`,
  `latency_ms`, `status`) — working rule 5; no secrets are ever logged.
- Cache: SQLite table `search_cache` in `DATABASE_PATH`; key =
  SHA-256(language + "\0" + normalized query) where normalization =
  casefold + whitespace collapse; value = the raw hit dicts;
  TTL = `SEARCH_CACHE_TTL_DAYS` (default 7, 0 disables). Zero-hit queries
  are not cached.
- `limit > 10` fetches additional pages (portal page size 10), 0.5 s
  courtesy delay between pages.

## 5. Gotchas

- The new SPA at `adilet.zan.kz` serves the 1.5 KB shell for *any*
  unknown path (e.g. `/rus/docs/K1500000375`) — a 200 from the main host
  does NOT mean the server rendered the document; use the legacy host for
  real text.
- `old.adilet.zan.kz` answers 302 with a JSESSIONID for the query-string
  form and for non-browser UAs; always use the path form + browser UA.
- Titles may contain escaped entities (`&quot;`, `&lt;*&gt;`); the parser
  unescapes them.
- Full-text fetches use the legacy server-rendered document page at
  `https://old.adilet.zan.kz/{lang}/docs/{act_id}`. The captured documents
  place article headings in paragraphs beginning `Статья N.` and commonly
  expose paragraph IDs such as `z327` (article heading anchors use a separate
  `name="zN"`). The mirror stores a first body paragraph ID without `#` for
  deep links. Some acts may have no such ID, in which case `anchor` is null.
- The full-text mirror uses SQLite tables `acts` and `articles`, keyed by act
  ID and language. `ACT_REFRESH_DAYS` controls its age limit (default 90); a
  forced refresh bypasses the mirror read. A successful fetch replaces the
  act's article rows atomically.
- One result block = one `post_holder`; a page shows 10 blocks (or fewer on
  the last page / zero for no results).
