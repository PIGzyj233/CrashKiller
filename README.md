# CrashKiller

Full-stack C++ crash dump and multi-hundred-MB runtime log diagnostic platform engineered for single-host (<= 12 GB RAM, <= 150 GB disk) deployments.

## Architecture

- **Nginx Reverse Proxy (`docker/nginx.conf`)**: Serves the React 19 SPA bundle on `${HTTP_PORT:-8080}`, proxies `/api/` with streaming 2 GB multipart uploads (`client_max_body_size 2048m`, `proxy_request_buffering off`), disables buffering for SSE (`/api/v1/crashes/*/events`), and exposes WinDbg SymSrv (`/symsrv/`).
- **FastAPI Backend (`backend/app/main.py`)**: REST API v1 (`/api/v1/*`), SSE crash pipeline status streaming, API-key upload authentication (`X-API-Key` header or `api_key` form field), and `/symsrv` symbol download.
- **PostgreSQL 16 + `pg_trgm`**: Stores projects, symbols, issues, crash clusters, crash reports, compressed log digests, AI analysis runs, and dynamic runtime `system_settings`.
- **ARQ + Redis Workers (`backend/app/workers/`)**: Runs `symbol_ingest`, `parse_crash`, `ai_triage`, `resymbolicate`, and periodic `disk_gc` cron tasks with real-time Redis PubSub SSE events.
- **Breakpad & MiniDump Pipeline**: Converts `.pdb` + `.exe`/`.dll` via `dump_syms` (tracking `has_pe_unwind`), parses `.dmp` via `minidump-stackwalk --json`, and clusters normalized stack traces via SHA-256 `signature_hash` + weighted Jaccard + `pg_trgm` fuzzy recall.
- **256 KB Chunked Zstd Log Engine**: Streams mixed `UTF-8`/`UTF-8-BOM`/`GBK`/`GB18030` logs into 256 KB newline-aligned zstd chunks (`.log.zst` + `.idx.json` + `digest_json`) for O(log N) line pagination, timestamp-window inspection, and < 500 ms regex search with bounded memory.
- **AI ReAct Triage Agent**: OpenAI-compatible tool-calling loop with 8 read-only tools, strict JSON verdicts (`KNOWN_ISSUE`, `NEW_ISSUE`, `REGRESSION`, `NEEDS_REVIEW`), and step/token/timeout guardrails.

## Quickstart (Docker Compose)

```bash
cp .env.example .env
# Set ADMIN_USERNAME and ADMIN_PASSWORD in .env before starting the browser UI.
# Edit .env to set LLM_API_KEY / LLM_BASE_URL / LLM_MODEL if using live AI triage
docker compose up -d --build
```

Services started by `docker-compose.yml`:
- `nginx` binds to `http://127.0.0.1:${HTTP_PORT:-8080}` by default.
- `api`: runs `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000`
- `worker`: runs `arq app.workers.worker.WorkerSettings`
- `postgres`: `postgres:16-alpine` with `pg_trgm`
- `redis`: `redis:7-alpine`

Total container memory limits remain within 12 GB (`2048m + 512m + 2560m + 4608m + 256m = 9984m`).

The browser prompts for the configured admin username and password using HTTP Basic authentication. The UI and management API stay locked until both values are set. Set `HTTP_BIND_HOST` to publish on a different interface and use TLS at the reverse proxy for remote access. For a browser client on a separate origin, list its exact origin in `CORS_ORIGINS`.

## Disk Quota & 50 GiB Host Safety Floor

CrashKiller checks the default 150 GiB budget when admitting dumps, logs, and symbols. The total includes the application data directories and the measured size of its PostgreSQL database.

- Symbols use budgets of 15 GiB for converted files, 5 GiB for the public cache, and 15 GiB for optional raw archives.
- Logs have a 55 GiB budget, dumps 35 GiB, staging 10 GiB, and PostgreSQL 15 GiB. PostgreSQL keeps report metadata for historical analysis. Use volume quotas to cap physical disk use, including WAL, Redis, and temporary processing space.

Below `HOST_SAFETY_FLOOR_GB`, new raw symbol archives pause and automatic cleanup reclaims disk space until the floor is restored. It protects pinned crash and issue files, reports being processed, and active staging files. Standard age and quota cleanup still applies.

Manage quotas and retention in Settings. The administrator's force-emergency action explicitly clears eligible evidence files. The normal cleanup action applies the configured retention and quotas.

Symbol uploads stream into staging and conversions run one at a time per data volume. On Linux, each converter process has a 1.5 GiB address-space limit and a 128 MiB symbol output limit.

## Seed Demo Data

```bash
docker compose exec api python scripts/seed_demo_data.py
```

Running the seed script is idempotent and populates the `demo-game` project, Breakpad symbols, issues, clusters, analyzed crashes, compressed logs, and AI agent traces.

## CLI Tool (`cli/crashkiller_cli.py`)

Upload PDB/PE symbol pairs recursively (unpaired `.exe`/`.dll` binaries without a matching `.pdb` are skipped):

```bash
backend/.venv/bin/python cli/crashkiller_cli.py upload-symbols \
  --server http://localhost:8080 \
  --project demo-game \
  --api-key ck_your_api_key \
  --app-version 1.4.2 \
  --dir ./build/Release \
  --fail-on-error
```

Upload a minidump and logs and wait for live SSE completion:

```bash
backend/.venv/bin/python cli/crashkiller_cli.py upload-crash \
  --server http://localhost:8080 \
  --project demo-game \
  --api-key ck_your_api_key \
  --app-version 1.4.2 \
  --session-id sess-20261124-001 \
  --dump ./crash.dmp \
  --log ./game.log \
  --wait
```

Machine uploads and `--wait` use the project API key. The key grants status access for crashes in that project. Crash details, logs, dumps, and project administration require the admin credential.

## Windows C++ Uploader & WinDbg SymSrv

See [`cli/cpp_uploader_example/README.md`](cli/cpp_uploader_example/README.md) for building the Windows C++17 `WinHTTP` multipart uploader (`crashkiller_uploader`).

Enable `ARCHIVE_RAW_SYMBOLS` before uploading symbols for WinDbg or Visual Studio. Downloads require admin authentication by default. On a trusted loopback-only deployment, `ALLOW_UNAUTHENTICATED_SYMSRV=true` enables passwordless debugger access. Configure the debugger with this symbol search path.

```text
SRV*C:\symcache*http://localhost:8080/symsrv
```

## Local Development & Testing

Backend unit and integration tests:

```bash
cd backend
.venv/bin/pytest tests -v
.venv/bin/ruff check app tests alembic scripts ../cli
```

Frontend typecheck and production build:

```bash
cd frontend
npm run typecheck && npm run build
```

End-to-end suite:

```bash
CRASHKILLER_E2E_REQUIRE_APP=1 backend/.venv/bin/pytest tests_e2e/ -v
```
