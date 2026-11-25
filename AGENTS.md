# CrashKiller Agent Guidelines

CrashKiller is a single-host C++ crash dump, Breakpad symbol, chunked zstd log, and AI triage platform (FastAPI + PostgreSQL 16 + Redis/ARQ + React 19/Vite/Tailwind + Nginx), engineered for <= 12 GB RAM and <= 150 GiB disk deployments.

## Project Layout

- `backend/app/` — FastAPI backend:
  - `api/v1/` & `api/deps.py`: REST endpoints (`/api/v1/*`), SSE streams, WinDbg `/symsrv/`, and auth/DI helpers.
  - `db/`: SQLAlchemy 2.0 async models (`models.py`), base/timestamps (`base.py`), engine/session (`session.py`).
  - `schemas/`: Pydantic request/response models.
  - `services/`: Domain logic (`dump_parser`, `symbol_service`, `ms_symbol_service`, `clustering`, `log_engine`, `storage`, `ai_agent`, `llm_providers`, `issue_workflow`, `settings_service`, `events`).
  - `workers/`: ARQ worker config & `TaskDispatcher` (`worker.py`), task implementations (`tasks.py`), durable crash job tracking (`jobs.py`).
- `backend/alembic/versions/` — Sequential Alembic migrations (`0001_*`, `0002_*`, ...).
- `backend/scripts/seed_demo_data.py` — Idempotent demo data seeder (`demo-game` project).
- `backend/tests/` — Async unit and API integration tests (SQLite in-memory + mocked external tools).
- `frontend/src/` — React 19 + TypeScript SPA:
  - `pages/` & `components/`: UI views and reusable widgets.
  - `api/`: Typed HTTP/SSE client (`client.ts`) and DTOs (`types.ts`).
  - `i18n/`: `i18next` setup (`index.ts`, default locale `zh-CN`) and dictionaries (`locales/zh-CN.ts`, `locales/en-US.ts`).
  - `utils/` & `index.css`: Shared helpers and global styles.
- `tests_e2e/` — Tiered end-to-end suite (`tier1`–`tier5`, `slow`, `frontend`) covering API, worker, CLI, and frontend build.
- `cli/` — Python CLI uploader (`crashkiller_cli.py`) and Windows C++17 WinHTTP example (`cpp_uploader_example/`).
- `docker/` — `backend.Dockerfile` (builds Rust `dump_syms` & `minidump-stackwalk` + `cabextract`), `frontend.Dockerfile`, and `nginx.conf`.
- `docs/architecture_plan.md` — System architecture reference.

## Environment & Commands

> **Platform note:** Backend code uses `fcntl` file locks (`storage.py`) and `tests_e2e/` uses executable Python shims (`chmod 0o755`), so **backend Python tests and E2E tests require Linux or WSL** (or Docker). Frontend `npm` commands run on both Windows and Linux.

### Full Stack (Docker Compose)

```bash
cp .env.example .env            # Set ADMIN_USERNAME and ADMIN_PASSWORD (UI/API fail closed without both)
docker compose up -d --build    # Starts postgres, redis, api, worker, nginx on http://127.0.0.1:8080
docker compose exec api python scripts/seed_demo_data.py
```

### Backend (from `backend/`, Python >= 3.12 on Linux/WSL)

```bash
python -m pip install -e '.[dev]'
python -m pytest tests -v
ruff check app tests alembic scripts ../cli
```

### Frontend (from `frontend/`)

```bash
npm ci
npm run dev                     # Vite dev server (:5173), proxies /api, /symsrv, /health to VITE_API_PROXY_TARGET (default :8080)
npm run typecheck               # tsc --noEmit (strict, noUnusedLocals, noUnusedParameters)
npm run build                   # Production bundle into dist/
```

### End-to-End Suite (from repo root on Linux/WSL)

```bash
CRASHKILLER_E2E_REQUIRE_APP=1 python -m pytest tests_e2e/ -v
CRASHKILLER_E2E_REQUIRE_APP=1 python -m pytest tests_e2e/ -m tier1 -v
```

`tests_e2e/` runs in-process with SQLite, `memory://` Redis, and fake `dump_syms`/`minidump-stackwalk` shims (no Docker required). Available markers in `tests_e2e/pytest.ini`: `tier1` (core features), `tier2` (boundaries/quotas), `tier3` (pairwise interactions), `tier4` (full workflow/stress), `tier5` (adversarial/security), `slow`, `frontend`.

## Key Engineering Invariants

- **Auth boundaries:** `api_v1_router` enforces HTTP Basic admin auth (`require_admin`, plus same-origin/CORS check on mutating requests) by default. Only endpoints explicitly listed in `_PROJECT_AUTH_ENDPOINTS` (`backend/app/api/v1/router.py`: `upload_crash`, `upload_symbol`, `get_crash_status`, `stream_crash_events`) accept project API keys (`X-API-Key` header or `api_key` form field). `/symsrv/` requires admin auth unless `ALLOW_UNAUTHENTICATED_SYMSRV=true`.
- **Database & migrations:** Production runs PostgreSQL 16 (`asyncpg`, `pg_trgm`) migrated by `alembic upgrade head` on `api` startup; tests run on `sqlite+aiosqlite` via `Base.metadata.create_all`. Keep models portable across both dialects, and whenever `backend/app/db/models.py` changes, add a sequential migration in `backend/alembic/versions/` (`NNNN_<slug>.py`) with both `upgrade()` and `downgrade()`.
- **Worker tasks & `memory://` mode:** `TaskDispatcher` (`backend/app/workers/worker.py`) dispatches via ARQ/Redis in production and tracked `asyncio` tasks when `REDIS_URL=memory://`. Register every new background task in both `TASK_REGISTRY` and `WorkerSettings.functions`.
- **Runtime settings overrides:** `.env` values in `backend/app/config.py` can be overridden at runtime via the `system_settings` table. For storage quotas, retention, or LLM config, resolve effective values through `settings_service.get_effective_app_settings()` / `get_effective_storage()` rather than raw `get_settings()`.
- **Issue lifecycle audit trail:** Always mutate issue status through `backend/app/services/issue_workflow.py` (`transition_issue_status`) so `issue_status_events` stays accurate.
- **Streaming, SSE & reverse proxy:** Never buffer large crash dumps, logs, or PDBs into memory—stream through `StorageManager` staging and respect quota/safety-floor checks (`HOST_SAFETY_FLOOR_GB`). When adding a new SSE endpoint, update both `docker/nginx.conf` (unbuffered SSE `location` regex) and `frontend/vite.config.ts` if needed.
- **API & UI contract sync:** When changing backend request/response schemas (`backend/app/schemas/`), update `frontend/src/api/types.ts` and `frontend/src/api/client.ts` in the same change.

## Engineering Principles

- **Single responsibility & <= 1,000-line ceiling:** Keep every code file focused on one clear responsibility and under **1,000 lines**. Do not pile new responsibilities onto large files; when a file approaches or exceeds 1,000 lines, split it into cohesive modules/subcomponents along domain boundaries while preserving existing public exports and routes.
- **Thin edges, pure core:** Keep FastAPI routes (`api/v1/`) and React pages thin—parse, authorize, and delegate. Isolate deterministic domain logic (clustering, stack normalization, log parsing, workflow transitions, formatting) from I/O so it stays easy to reason about and unit-test.
- **Parse, don't validate (type-driven design):** Validate and normalize untrusted inputs once at system boundaries (Pydantic schemas, typed API DTOs), then pass explicit typed models/enums/discriminated unions internally. Avoid untyped `dict[str, Any]`, TypeScript `any`, and loose stringly-typed states.
- **Dependency injection & testability:** Inject external collaborators (`AsyncSession`, `StorageManager`, subprocess `exec_fn`, LLM/HTTP clients) rather than coupling to hidden globals, consistent with `create_app(...)` and `TaskDispatcher`.
- **Fail fast & explicit error handling:** Never silently swallow exceptions. Handle expected failures with explicit, actionable error responses/states; enforce explicit timeouts, output/size limits, and cleanup (`try`/`finally` or async context managers) on all subprocess, network, and LLM calls.
- **Idempotency & async safety:** Design worker jobs, ingestion pipelines, migrations, and scripts to be retry-safe (idempotent transitions, atomic writes/renames). Never block the `asyncio` event loop with synchronous I/O or heavy CPU work; stream large files instead of buffering in memory.
- **Security by default:** Keep auth fail-closed, use `hmac.compare_digest` for secrets, use parameterized SQLAlchemy queries, canonicalize/validate all filesystem and symbol paths against traversal, and avoid unsafe raw HTML rendering in React.
- **Structured observability:** Log concise, contextual diagnostics (`crash_id`, `issue_id`, `project_id`, stage, failure reason) and never log secrets, API keys, or Basic auth headers.
- **Pragmatic simplicity (YAGNI) & clean diffs:** Prefer clear, direct code over premature abstraction. Keep diffs scoped to the task—do not leave dead code, commented-out blocks, or unrelated formatting churn.

## Coding Style & Conventions

- **Python:** 4-space indentation, `snake_case`, Python 3.12 type syntax (`from __future__ import annotations`, `X | None`), 100-char line limit. Ruff enforces `E`, `F`, `I`, `UP`, `B` (ignoring `B008`).
- **TypeScript / React:** 2-space indentation, `PascalCase` components, `camelCase` functions/variables, strict TypeScript (`noUnusedLocals`, `noUnusedParameters`). Use React Query (`@tanstack/react-query`) and `frontend/src/api/client.ts` for data fetching.
- **i18n:** Default locale is `zh-CN`. Update **both** `frontend/src/i18n/locales/zh-CN.ts` and `frontend/src/i18n/locales/en-US.ts` for every UI copy change; avoid hardcoded user-facing strings.

## Verification Expectations

- Run targeted tests while iterating, then run full checks for touched layers before finishing:
  - Backend / CLI changes: `ruff check app tests alembic scripts ../cli` and `python -m pytest tests -v` (plus relevant `tests_e2e/` markers for cross-layer or API workflow changes).
  - Frontend changes: `npm run typecheck && npm run build`.
- Name test files `test_*.py` and test functions `test_*` (`asyncio_mode = "auto"` is configured in both pytest suites). Add a focused regression test whenever fixing a bug or changing behavior not covered by existing tests.

## Git & Security Hygiene

- Write short imperative commit subjects matching repo history (e.g., `Add crash session ID tracking`, `Fix symbol resymbolication queue`).
- In PRs, summarize the change, list verification commands run, link related issues, and include screenshots for visible UI changes.
- Never commit `.env`, `data/`, crash dumps (`*.dmp*`), logs (`*.log`), symbols (`*.pdb`, `*.sym`, `*.exe`, `*.dll`), or local build artifacts.
