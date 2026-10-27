# Clipora AI

Turn long videos into short clips by chatting with an agent. Design docs live in `doc/` (start with `doc/rules.md`). For one walkthrough of the whole system, from upload to analysis, generation, editing and export, read `doc/how-it-works.md`; for the computer vision (YOLO, MediaPipe, speaker detection and the editor overlay) read `doc/computer-vision.md`.

## Layout

| Folder | What it is |
| --- | --- |
| `frontend/` | Next.js + Ant Design / Ant Design X (dark theme). Editor components are grouped by feature in `src/components/editor/` |
| `backend/services/api/` | FastAPI, all routes under `/api`. `routes/` is the HTTP layer, `services/` holds the business rules |
| `backend/services/worker/` | Queue workers: `python -m worker.run --queue ingest\|agent\|export --slots N` |
| `backend/packages/` | `core` (plan contract), `jobqueue`, `storage` (S3), `media` (ffmpeg), `vision` (YOLO), `rag`, `agent` (LangGraph) |
| `database/` | SQLAlchemy models and Alembic migrations (Python package `db`) |
| `infra/` | `docker/` (local Postgres and S3, Dockerfile), `s3/` (AWS bucket setup and policies), `scripts/dev.sh` |
| `doc/` | Product, architecture, design, rules and project memory |

## Local development

```bash
cp .env.example .env                       # fill in the LLM settings (Bedrock or local Ollama)
uv sync --all-packages --group dev --inexact
(cd frontend && npm install)
infra/scripts/dev.sh up                    # containers, migrations, API, workers, web on http://localhost:3001
infra/scripts/dev.sh status                # what is running; logs are in .dev/logs/
infra/scripts/dev.sh down                  # stop the app processes
uv run --no-sync python -m api.admin grant-admin you@example.com   # after signing in once as dev:<email>
```

Use `uv sync ... --inexact` and `uv run --no-sync`: a plain `uv sync --package X` removes other packages from the shared venv.

To use real S3 instead of the local server, create the bucket with `infra/s3/setup-dev-bucket.sh`, set `S3_BUCKET` and
`S3_REGION` in `.env`, remove the local `S3_*` lines, and copy existing objects with `infra/s3/copy_local_to_s3.py`.

## Checks

```bash
uv run --no-sync ruff check backend database infra
uv run --no-sync mypy backend/packages/*/src backend/services/*/src database/src
uv run --no-sync pytest                    # queue tests need TEST_DATABASE_URL, model tests need RUN_SLOW=1
cd frontend && npx tsc --noEmit && npm run lint && npx vitest run && npm run build
```

After changing API schemas: `uv run --no-sync python -m api.export_openapi`, then `npm run gen:api` in `frontend/`.
