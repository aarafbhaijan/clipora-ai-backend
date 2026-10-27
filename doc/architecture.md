# Clipora AI: architecture

Source of truth for how the system is built. If code and this file disagree, stop and ask which is right, then fix whichever is wrong. Status: low-level design is done, high-level design and code are not started (see `memory.md`).

## 1. What the product is

A user uploads a video in any aspect ratio. An AI agent reads the transcript (LLM) and finds where the main speaker is (YOLO), then proposes an **edit plan**, not a rendered video. The user chats with the agent in plain language ("make a 1-minute short for Reels"), reviews the plan in an editor with a timeline, changes it by hand or by chat, and only then exports. FFmpeg renders once, on Export.

## 2. Principles

1. **The plan is the product.** The agent writes an EDL (edit decision list, JSON). Nothing is transcoded until the user presses Export.
2. **The EDL is the only contract** between the agent, the API, the browser preview and the export worker. Change it in `backend/packages/core` first, then everything else.
3. **Human in the loop.** The agent proposes, the user decides. Agent writes never overwrite user edits (versioned EDL, `If-Match`).
4. **One backend language.** Python for API and workers. TypeScript only for the Next.js frontend.
5. **A Postgres job queue and polling, no sockets.** Background work is a row in the `jobs` table that workers claim with `FOR UPDATE SKIP LOCKED`. There is no SQS, Celery or Redis. The browser polls the same table for status. Exception (decided 2026-10-25): chat replies that are not edits (questions about the video, small talk) stream over Server-Sent Events from the `chat` service; edits still go through the queue.
6. **Free-tier conscious.** ECS on EC2 (not EKS), one small RDS that also serves as the queue, no NAT gateway, open models for speech and embeddings.
7. **Code decides, the LLM suggests.** Timestamps are snapped by code, edits are applied by code, validation is code.
8. **One preview, one export, same math.** The crop path and the transition timing come from the same pure functions for the browser preview and for FFmpeg.

## 3. System overview

```
Browser (Next.js, Ant Design)
   | HTTPS
CloudFront ---- S3 (uploads/, assets/, exports/)         [presigned multipart upload, signed cookies]
   |
ALB (only reachable from CloudFront)
   |
ECS on EC2 (general group)           ECS on EC2 (heavy group)
  api (FastAPI)                        worker-ingest  (Whisper, YOLO, ffmpeg, embeddings)
  worker-agent (LangGraph, LLM)        worker-export  (ffmpeg)
   |                                   |
   +------------- jobs table in Postgres = the queue (ingest, agent, export), workers claim rows
   |
RDS Postgres (+ pgvector, + LangGraph checkpoints)       Cognito (JWT)       ECR (images)
```

Request flow for a new video: upload (browser to S3) -> `POST /uploaded` -> ingest job -> project `ready` -> suggest job posts idea cards -> user picks an idea or types a request -> agent turn -> EDL version 1 -> user edits (autosave) and chats -> Export -> download.

## 4. Processes

One monorepo (a uv workspace), one Python image, several entrypoints. The API image stays small: it imports only `core`, `db`, `jobqueue` and `storage`, never the media or ML packages. The queue package is named `jobqueue` because `queue` would shadow the Python standard library module.

| Process | Command | Imports |
| --- | --- | --- |
| api | `uvicorn api.main:app` | core, db, jobqueue, storage |
| worker-ingest | `python -m worker.run --queue ingest --slots 1` | core, db, jobqueue, storage, media, vision, rag |
| worker-agent | `python -m worker.run --queue agent --slots 4` | core, db, jobqueue, storage, agent, rag |
| worker-export | `python -m worker.run --queue export --slots 1` | core, db, jobqueue, storage, media |

Each queue loads only its own handler modules, so the export worker never imports torch.

Repo layout (frontend, backend, database and infrastructure are separate top-level folders):

```
clipora_ai/
  frontend/                 Next.js app on Ant Design
    src/app/                routes: /projects, /projects/[id], /settings, /admin
    src/components/editor/  the editor, one folder per feature: chat/, player/, timeline/, transcript/, metadata/, export/
    src/api/ src/lib/       generated API types and client; pure helpers (time mapping port, plan edits, stores)
  backend/
    services/api/           FastAPI: routes/ (thin HTTP layer, one module per resource) call services/ (business rules)
    services/worker/        queue worker loop and job handlers; entrypoints ingest, agent, export
    services/chat/          streaming chat (SSE): routes each typed message, streams answers and small talk
    packages/
      core/                 Pydantic models: EDL, job payloads, enums, error codes, time mapping, crop window and path,
                            transition catalogue, settings, pricing
      jobqueue/             jobs-table queue: enqueue, claim, lease, retry (SQLAlchemy only)
      storage/              S3 key builders, presign and multipart helpers
      media/                ffprobe and ffmpeg wrappers, sprite and waveform helpers
      vision/               YOLO detection and tracking (writes detections.json; the crop path itself is in core)
      rag/                  transcript chunking, embeddings, hybrid retrieval, memory helpers
      agent/                LangGraph edit graph, suggest task, metadata_graph/, nodes, prompts, edit-op applier
  database/                 SQLAlchemy models, sessions, Alembic migrations (Python package `db`)
  infra/
    docker/                 compose.yml (Postgres + pgvector, local S3) and the Python Dockerfile
    s3/                     bucket setup script, IAM policies, local-to-S3 copy
    scripts/dev.sh          start, stop and check the whole local stack
  doc/                      these documents
```

Python module names (`core`, `db`, `api`, `worker`, ...) do not depend on the folders, so moving a folder never changes an import. The workspace root `pyproject.toml` lists the members.

Frontend types are generated from the API's OpenAPI file. Pydantic models in `backend/packages/core` are the single source of truth.

## 5. AWS deployment

| Piece | Choice and reason |
| --- | --- |
| Compute | ECS on EC2 with two capacity groups: general (api, worker-agent) and heavy (worker-ingest, worker-export). Bridge networking. EKS was rejected because its control plane is not free. |
| Storage | S3, block all public access. Prefixes `uploads/`, `assets/`, `exports/`. Presigned multipart upload (16 MB parts, 4 in parallel). Lifecycle rule aborts incomplete multipart uploads after 1 day. Bucket CORS allows the app origin to PUT (upload parts, exposing `ETag`) and GET (proxy, waveform, transcript; the Fit preview draws proxy frames to a canvas). |
| Edge | CloudFront in front of the app, media and API. `/api/*` goes to the ALB, everything else to the web app. ALB allows only the CloudFront prefix list. v1 serves assets and downloads with short-lived presigned S3 URLs; CloudFront signed cookies replace them once the distribution and key pair exist. |
| Auth | Cognito. The API verifies the JWT against the JWKS and filters every query by `user_id`. |
| Queue | The `jobs` table in RDS, no separate service. Workers poll it every 1 s (section 9). SQS and Celery were rejected: see `memory.md`. |
| Database | RDS Postgres `db.t4g.micro`, with the pgvector extension. Also holds LangGraph checkpoints and the job queue. Check the connection count (section 9) before choosing worker counts. |
| Network | S3 gateway endpoint, no NAT gateway. |
| Images | ECR. |
| LLM | Amazon Bedrock (Converse API through langchain-aws), task-role auth. Model ids come from configuration: Amazon Nova Lite (main) and Nova Micro (small calls), using tool-calling structured output. Claude models can be swapped in with `LLM_STRUCTURED_METHOD=json_schema`. Groq (OpenAI-compatible API, key in Secrets Manager) and Ollama can serve any tier instead (section 11.5). |
| Secrets and config | Secrets Manager for secrets, task definition environment for the rest. |
| Logs and metrics | CloudWatch Logs (structured JSON), CloudWatch alarms on failed jobs and queue age. |

## 6. Data model (13 tables)

| Table | Key columns | Notes |
| --- | --- | --- |
| users | id, cognito_sub (unique), email, role, learn_enabled, created_at | Created on first authenticated request. role: admin or editor, default editor (section 16) |
| projects | id, user_id, title, status, source_key, upload_id, size_bytes, duration_s, width, height, fps, current_version, deleted_at, created_at, updated_at | Soft delete first, S3 purge later. Index (user_id, created_at desc). `size_bytes` counts toward the storage limit |
| messages | id, project_id, thread, role, kind, content, payload (jsonb), edl_version, created_at | thread: edit or metadata. kind: text, suggestions, choices, metadata. Index (project_id, thread, created_at) |
| edl_versions | id, project_id, version, edl (jsonb), author, parent_version, created_at | Append only. Unique (project_id, version). author: agent or user |
| jobs | id, project_id (null for distill), user_id, kind, queue, payload (jsonb), status, stage, percent, stage_markers (jsonb), error_code, error_detail, attempts, max_attempts, timeout_s, run_after, locked_by, lease_expires_at, created_at, started_at, finished_at | kind: ingest, agent, suggest, metadata, distill, export. One row per background job, and the row is also the queue (section 9). Indexes: (project_id, created_at desc) and a partial index on (queue, run_after) for rows in queued |
| exports | id, project_id, edl_version, job_id, status, output_key, srt_key, created_at | Keys `exports/{project}/{export_id}.mp4` and `.srt` |
| metadata_sets | id, project_id, version, edl_version, export_id, platform, language, tone, fields (jsonb), author, parent_version, created_at | Append only. Highest version is current |
| transcript_chunks | id, project_id, cue_from, cue_to, start_s, end_s, text, embedding vector(384), tsv | Written by ingest stage 7. No vector index in v1 |
| feedback_events | id, user_id, project_id, target_kind, target_ref, signal, reason, diff (jsonb), created_at | Append only. Written only if `learn_enabled` |
| agent_memories | id, user_id, kind, text, embedding vector(384), evidence_count, status, source_event_ids, created_at, updated_at, last_used_at | Max 50 active per user. status: active, pending, disabled |
| app_settings | key (primary key), value (jsonb), updated_by, updated_at | Application-wide limits, editable by an admin. Keys and defaults in section 16 |
| user_limits | user_id, key, value (jsonb), updated_by, updated_at | Per-user override of one `app_settings` key. Primary key (user_id, key). No row means the global value applies |
| llm_usage | id, user_id, project_id (null for distill), job_id, job_kind, node, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, created_at | Append only, one row per LLM call including retries. Index (user_id, created_at). Never holds prompt or response text. Kept when a project is deleted |

Status values: `projects.status` uploading, ingesting, ready, failed. `jobs.status` queued, running, waiting_input (agent and metadata only), succeeded, failed. Running goes back to queued when a retry is due, and waiting_input goes back to queued when the user replies. `exports.status` queued, rendering, done, failed.

Invariants:
- At most one active job per (project, kind), enforced by a partial unique index on jobs in queued, running or waiting_input. A job with no project (distill) follows the same rule per (user, kind).
- A job row is inserted in the same transaction as the write that causes it (a chat message, a project becoming ready, an export finishing). Never enqueue after the commit.
- `projects.current_version` always equals the highest `edl_versions.version`, changed in the same transaction.
- A message and the EDL version it points to are written in one transaction.

## 7. The EDL

```json
{
  "version": 3,
  "source": { "key": "uploads/u1/p1/source.mp4", "duration_s": 612.4, "width": 1920, "height": 1080, "fps": 30 },
  "output": { "aspect": "9:16", "width": 1080, "height": 1920, "fps": 30, "framing": "crop", "target_duration_s": 60 },
  "segments": [
    { "id": "s1", "src_start": 42.0, "src_end": 61.5, "reason": "Strong hook", "score": 0.91,
      "crop": [ { "t": 42.0, "cx": 0.41, "cy": 0.5, "zoom": 1.0 }, { "t": 42.5, "cx": 0.43, "cy": 0.5, "zoom": 1.0 } ] },
    { "id": "s2", "src_start": 130.2, "src_end": 158.0, "reason": "Key insight", "score": 0.87,
      "crop": [ { "t": 130.2, "cx": 0.52, "cy": 0.5, "zoom": 1.0 } ] }
  ],
  "transitions": {
    "default": { "type": "fade", "duration_s": 0.4 },
    "overrides": [ { "from": "s1", "to": "s2", "type": "slide_left", "duration_s": 0.5 } ]
  },
  "audio_fade_ms": 80,
  "captions": { "enabled": true, "style": "bold-center", "edits": { "c14": "Corrected text for cue 14" } },
  "meta": { "candidates": [ { "src_start": 300.0, "src_end": 318.5, "score": 0.74, "reason": "Good story, weaker hook" } ], "run": { "prompt_version": "...", "model": "..." } }
}
```

Rules:
- Segments are in output order. Output start of a segment is derived, never stored.
- `output.width` and `height` are authoritative. `aspect` is a label (9:16, 1:1, 4:5, 16:9, 21:9 or custom W:H). Default size: short side 1080, both sides even.
- `framing`: `crop` fills the frame by cropping. `fit` shows the whole picture over a blurred copy and ignores `crop`.
- Crop window at zoom 1: with source aspect S and output aspect T, `w0 = min(1, T/S)` of the width, `h0 = min(1, S/T)` of the height. Zoom divides both. 16:9 to 9:16 gives 0.3164 x 1. Zoom is fixed at 1.0 in v1.
- `segments[].crop` keyframes are in **source time**, linear interpolation. Trimming, reordering or changing a transition never moves them. The API recomputes a segment's keyframes whenever its range changes.
- Any transition other than `cut` overlaps the end of one segment with the start of the next, so output is shorter by that duration.
- `meta` is ignored by export.
- `meta.short = { id, title }` names the generated video a plan belongs to. The agent sets a new one for each fresh plan (title from the idea or request); revisions and user edits carry it forward, so versions group into generated videos without a schema change. Versions made before this tag inherit from their parent, or are titled from the request that produced them.

Validation (API on every write, worker again before use):

| Rule | Limit |
| --- | --- |
| Segment range | 0 <= src_start < src_end <= source.duration_s |
| Minimum segment length | 1.0 s, and at least 0.5 s longer than its adjacent transition durations |
| Duplicate source ranges | Overlap of at most 0.25 s |
| Output duration | Sum of segment lengths minus transition durations. Agent output within 10% of target. User edits 3 to 180 s |
| Output size | Even width and height, each 240 to 4096 |
| Transitions | Duration 0.1 to 2.0 s, type in the catalogue, overrides name adjacent segments |
| Crop keyframes | Per segment, sorted by t, first at or before src_start, last at or after src_end, window inside the frame |
| Segment count | 1 to 30 |

Transition catalogue (defined once in `backend/packages/core`, used by preview and export):

| Type | FFmpeg `xfade` name | Browser preview |
| --- | --- | --- |
| cut | none | switch |
| fade | fade | opacity |
| dip_black | fadeblack | through black |
| dip_white | fadewhite | through white |
| dissolve | dissolve | opacity (approximate) |
| slide_left / slide_right | slideleft / slideright | translate |
| wipe_left / wipe_right | wipeleft / wiperight | clip-path |
| circle_open | circleopen | circular clip-path (approximate) |
| zoom_in | zoomin | scale and fade (approximate) |
| pixelize | pixelize | opacity stand-in, labelled approximate |

Versioning: `PATCH /edl` carries `If-Match: version`. 409 `version_conflict` returns the latest. Agent writes use the same rule.

## 8. API (33 endpoints)

All paths are under `/api` (for example `POST /api/projects`), so CloudFront can send `/api/*` to the ALB on the same origin as the web app. OpenAPI is served at `/api/openapi.json` and exported to `backend/services/api/openapi/openapi.json` for the frontend's generated types. Every request carries a Cognito token. Errors are `{ "error": { "code", "message", "details" } }`. Codes: 400 bad_request, unsupported_media; 401 unauthenticated; 403 forbidden, quota_exceeded; 404 not_found; 409 agent_busy, metadata_busy, version_conflict, export_in_progress; 413 file_too_large; 422 edl_invalid (also no_plan, fields_invalid, duration_too_long, setting_invalid); 429 rate_limited. `quota_exceeded`, `file_too_large` and `duration_too_long` carry `details: { key, limit, current }`.

| Endpoint | Purpose |
| --- | --- |
| POST /projects | title, filename, size, content_type, duration_s (from the browser, optional), srt_text (optional). Checks the user's limits (section 16). Returns project_id, upload_id, part_size_mb |
| POST /projects/{id}/upload/part-urls | Presigned PUT URLs (15 min) |
| POST /projects/{id}/uploaded | Completes multipart, enqueues ingest |
| GET /projects | History list with active_job and last_export per item, cursor paging |
| GET /projects/{id} | Project, active_job, messages, edl, assets (proxy, thumbs, waveform, transcript, srt) |
| PATCH /projects/{id} | Rename |
| DELETE /projects/{id} | Soft delete |
| POST /projects/{id}/messages | content and or suggestion_id. 409 agent_busy |
| PATCH /projects/{id}/edl | If-Match version, body edl. Returns version and EDL with recomputed crops |
| GET /projects/{id}/edl/versions | Version history, newest first: version, author, parent, length, aspect, segments, and the generated video it belongs to (`short_id`, `short_title`) |
| GET /projects/{id}/edl/versions/{v} | One version, for Undo and restore |
| POST /projects/{id}/ideas | More ideas: a suggest job with `more`, posted even after the user has chatted. 409 agent_busy if one is running |
| GET /projects/{id}/speaker | YOLO person boxes (from `detections.json`, made once at ingest) and the main speaker track, chosen with `core.crop.main_track`, the same rule the crop path and the export use. Feeds the editor's speaker overlay |
| POST /jobs/{id}/retry | The Retry button on a failed task (not for `duration_too_long`) |
| POST /projects/{id}/exports | edl_version optional |
| GET /exports/{id} | status, percent, download_url, srt_url |
| POST /projects/{id}/metadata | Start the metadata graph (options optional) |
| POST /projects/{id}/metadata/messages | Answer or request a change |
| GET /projects/{id}/metadata | Current set, versions, messages, active_job, stale flag |
| PATCH /projects/{id}/metadata | If-Match version, user edits create a new version |
| POST /projects/{id}/feedback | Thumbs and reasons |
| GET /projects/{id}/search | Full-text transcript search (no embeddings in the API) |
| GET /memories, PATCH /memories/{id}, DELETE /memories/{id} | What Clipora has learned |
| GET /me | role, learn_enabled, effective limits and current use (active videos, storage) |
| PATCH /me | learn_enabled |
| GET /health | ALB target group |
| POST /api/chat/projects/{id}/messages | Chat service (not the API). Routes a typed message: edit → same as POST /messages (job queued, event `route`); question or small talk → streamed reply as SSE (`status`, `route`, `token`, `done`, `error`, plus `: ping` keep-alives every 10 s). Questions are answered from the whole transcript (trimmed head and tail when long) with `[cNN]` citations turned into sources |
| GET /admin/settings, PATCH /admin/settings | Admin only. Read and change `app_settings` |
| GET /admin/users | Admin only. Users with role, active videos, storage used, tokens and cost for a date range, cursor paging |
| GET /admin/users/{id} | Admin only. Effective limits, overrides, and usage by project, by job kind and by day |
| PATCH /admin/users/{id} | Admin only. role, and `user_limits` overrides (null removes one) |
| GET /admin/usage | Admin only. Totals for a date range, grouped by user, day or job kind |

Polling: the editor polls `GET /projects/{id}` every 2.5 s while a job is queued or running (not while `waiting_input`). The sidebar list polls every 3 s while anything is active, every 30 s otherwise, and on focus.

## 9. Jobs and the Postgres queue

The `jobs` table is the queue. Six kinds of job run on three queues.

| Job kind | Queue | Slots per container | Timeout | Attempts |
| --- | --- | --- | --- | --- |
| ingest | ingest | 1 | 60 min | 3, backoff 60 s |
| agent (a turn) | agent | 4 (mostly waiting on the LLM) | 5 min | 3, backoff 5 s on LLM 429 or 5xx |
| suggest | agent | shares the agent slots | 2 min | same as agent |
| metadata | agent | shares the agent slots | 3 min | same as agent |
| distill | agent | shares the agent slots | 2 min | same as agent |
| export | export | 1 | 30 min | 2 |

Timeouts are starting values: measure on the real instance and set to about three times the longest run.

How it works:

- **Enqueue.** `backend/packages/jobqueue` provides `enqueue(tx, kind, payload)`, which inserts a `queued` row inside the caller's transaction.
- **Claim.** A free slot claims one job with the statement below. `FOR UPDATE SKIP LOCKED` lets many workers claim at once without taking the same row. The claim also picks up jobs whose lease has expired.
- **Lease and heartbeat.** A claim sets `locked_by` and a 120 s lease. Every write of `stage` and `percent` renews it, and a heartbeat thread renews it every 30 s otherwise. If a renewal finds another worker holds the job, the worker stops its task (including any ffmpeg child) and writes nothing more. A worker that died has its job claimed again after at most 2 minutes. A sweep marks a job `failed` with `error_code = lease_lost` when the lease expired and no attempts are left.
- **Timeout.** A watchdog stops a task that runs past `timeout_s`, and that counts as a failed attempt.
- **Retry.** On an error or a lost lease, if `attempts < max_attempts` the row goes back to `queued` with `run_after = now() + backoff`. Otherwise it becomes `failed` with an `error_code` and a trimmed `error_detail`. The Retry button sets the row to `queued`, resets `attempts` to 0 and keeps the stage markers.
- **Waking.** Idle slots poll every 1 s with a small random delay, using a partial index on (queue, run_after). LISTEN and NOTIFY could replace polling later, but they do not work through a transaction-mode connection pooler.
- **Per-user limit.** The claim skips a user who already has 2 running jobs. Two workers could pass that check together, so the claim first takes `pg_try_advisory_xact_lock` on the user. Test this with two workers.
- **Worker process.** `python -m worker.run --queue <name> --slots <n>` starts one claim loop. Each slot runs one job in a thread.
- **Connections.** A worker uses at most `slots + 2` database connections (the agent worker adds its checkpointer pool, section 11.1). Add every process and the API pool, and check the total against the RDS connection limit before choosing counts.
- **Waiting jobs.** A job in `waiting_input` holds no lease.
- **Idempotent handlers.** Every handler is safe to run twice and records stage markers, so a repeat run after a lost lease resumes.

The claim runs in one short transaction (`jobqueue.claim`):

```sql
-- 1. Lock up to 10 claimable rows without blocking other workers
SELECT id, user_id FROM jobs
WHERE queue = :queue AND attempts < max_attempts
  AND ((status = 'queued' AND run_after <= now())
    OR (status = 'running' AND lease_expires_at < now()))
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 10;

-- 2. For each candidate in order: skip it unless this succeeds and the user has fewer than 2 live running jobs
SELECT pg_try_advisory_xact_lock(hashtextextended(:user_id, 0));
SELECT count(*) FROM jobs WHERE user_id = :user_id AND status = 'running' AND lease_expires_at >= now();

-- 3. Claim the first one that passes
UPDATE jobs
SET status = 'running', locked_by = :worker, lease_expires_at = now() + interval '120 seconds',
    attempts = attempts + 1, started_at = coalesce(started_at, now())
WHERE id = :id;
```

Every later write by the worker (progress, stage marker, success, failure) includes `AND locked_by = :worker AND status = 'running'`; zero rows updated means the lease was lost and the worker stops. On shutdown (ECS stop, Spot reclaim) a worker releases its jobs back to `queued` without spending an attempt.

## 10. Ingest (8 stages, marker after each)

| # | Stage | What |
| --- | --- | --- |
| 1 | Probe | ffprobe. Reject files with no video or no audio, and files longer than the user's `max_video_duration_s` (`duration_too_long`, no retry) |
| 2 | Transcribe | faster-whisper `small`, int8, CPU, word timestamps, VAD, 16 kHz mono. Skipped if the user supplied an SRT |
| 3 | Proxy | Short side 720 px (never larger than the source), Lanczos, source aspect kept, H.264 CRF 23, GOP 48 with no scene-cut keyframes (fast, exact seeking). Key `proxy-<short>p.mp4`, so a rebuilt copy never hits a browser cache. `python -m worker.backfill_proxy` rebuilds older copies. Changed 2026-10-27 from 360 px CRF 28: cropped 9:16 previews showed about 200 px wide and looked blurry |
| 4 | Thumbnails | One frame per 2 s, sprite sheets, short side 90 px |
| 5 | Waveform | Min and max per 50 ms bucket, 20 per second, int8, gzip |
| 6 | Detect | 3 fps, YOLOv8n person class, confidence 0.35, ByteTrack ids |
| 7 | Speakers | Who talks when (section 12.2): 12 fps lip readings matched to the audio. Writes `detections-speakers.json` (the detections plus `turns`), which readers prefer over `detections.json` |
| 8 | Index | Chunks of 3 cues with 1 overlap, multilingual embedding model (384-d, e.g. multilingual-e5-small), stored with a full-text vector |

Cues: at most 2 lines of 42 characters and 7 s, breaking at sentence ends or pauses over 0.6 s. Stable ids `c1`, `c2`, and so on. Written to `transcript.json` and `transcript.srt`. Ingest also writes `poster.jpg` (history thumbnail), sprite sheets, `waveform.bin.gz` (signed int8 min and max pairs, 20 per second, stored gzipped without Content-Encoding) and `detections.json`. When ingest finishes the project is `ready` and a `suggest` job is enqueued in the same transaction.

## 11. Agents

All LLM calls use Pydantic structured output (Bedrock Converse through `langchain-aws` `ChatBedrockConverse`, JSON-schema output, no forced tool choice). No sampling parameters: since 2026-10-21 current Claude models reject `temperature`, `top_p` and `top_k` with a 400, so depth is controlled by model choice and effort, and determinism comes from code-side validation. `LLM_SEND_TEMPERATURE=1` re-enables them only for older models that still accept them. Timestamps from the LLM are hints: code snaps each boundary to the nearest word or sentence boundary within 0.5 s and rejects ranges that do not snap. The transcript goes in a delimited block that the prompt declares to be data. Each call times out at 60 s, retries twice with the validation error appended, and its prompt and response are saved. Every call, retries included, goes through one LLM client wrapper (`agent/llm.py`) that writes an `llm_usage` row from the provider's reported token counts (section 16) and saves the prompt and response to S3 at `assets/{user}/{project}/llm/{job_id}/{seq}-{node}.json` (learning runs use `assets/{user}/learning/llm/`). Neither is ever logged. Model ids come from `LLM_MODEL_ID` and `LLM_SMALL_MODEL_ID` (defaults to the main model); `BEDROCK_REGION` is required.

### 11.0 Intent router and streamed chat

Every typed message goes to the chat service first. `agent.conversation.route` decides edit / question / chat from the wording (instant rules), and asks the small model only when the wording is ambiguous; a reply to the edit graph's open question always goes back to the graph. Edits are queued as agent jobs exactly as before. Questions and small talk stream from the model chosen by `LLM_CHAT_MODEL_ID` (defaults to the main model), through `LLMClient.stream_text`, which records usage and archives like every other call. The user and assistant messages are saved in the edit thread, so the history and the agent see them.

**Speaker requests** ("identify the speaker", "who is speaking") route to a fourth intent, `speaker`: the chat service reads the cached detections (YOLO is never re-run per request), replies with what was found, and the editor switches on the speaker overlay.

### 11.1 Edit graph (LangGraph, `thread_id = project_id`, Postgres checkpointer)

The checkpointer (`langgraph-checkpoint-postgres` `PostgresSaver`) creates and owns its own tables through `.setup()`, outside Alembic; they are the one exception to "schema changes go through migrations". Its pool (`AGENT_CHECKPOINT_POOL`, default 4) adds to the agent worker's `slots + 2` connections in section 9.

Progress stage names written to `jobs.stage` (shown in the chat's progress chain): understanding, finding moments, building plan, framing the speaker, checking, saving; revisions use changing the plan and questions searching the video. Nodes: `load_context` (transcript, detections, latest EDL, top 5 learned preferences) -> `parse_intent` (LLM; `Intent` has kind plan, question, correction or metadata, target_duration_s, aspect, style, focus, targets) -> optional `ask_user` (interrupt, with quick replies) -> `retrieve` (when there is a focus or a question) -> `find_candidates` (LLM, up to 15 ranges) -> `select_and_order` (LLM plus code, within 10% of target, picks default transition from style) -> `build_crop` (code) -> `assemble_edl` (code, validates) -> `critic` (LLM, up to 2 retries) -> `save_and_reply` (one transaction, new EDL version plus message) -> `await_feedback` (interrupt) -> `revise_plan`. A question goes `retrieve` -> `answer_from_video` -> `await_feedback`.

Two interrupts end the job. `ask_user` sets the job to `waiting_input` and the next message sets the same row back to `queued`. `await_feedback` sets it to `succeeded` and the next message creates a new job. Both resume with `graph.invoke(Command(resume=message), config={"configurable": {"thread_id": project_id}})`.

Revisions are edit ops applied by code. The LLM never writes a full EDL during a revision.

| Op | Fields |
| --- | --- |
| remove | id |
| trim | id, src_start and or src_end (API recomputes that crop) |
| add | src_start, src_end, after_id |
| reorder | ids[] |
| set_target | seconds (reruns select_and_order) |
| set_transition | from, to or all, type, duration_s |
| set_aspect | aspect or width and height, framing (API recomputes every crop) |

If `save_and_reply` gets a 409, `revise_plan` reloads the current EDL and reapplies its ops, skipping any that no longer fit and saying so.

### 11.2 Ideas (`agent.suggest`, outside the graph)

Runs once after ingest. One structured call returns a summary, up to 6 topics and 4 to 6 ideas (title, why, format, target_duration_s, aspect, style, focus, anchors as cue ranges). Code resolves anchors to times and drops ideas with none. Fewer than 3 survivors adds generic ones. Posted as a message of kind `suggestions`. Discarded if the user already sent a message. Choosing an idea (`suggestion_id`) copies its settings into the user message as a ready `Intent`.

### 11.3 Metadata graph (separate LangGraph, `backend/packages/agent/metadata_graph/`)

Writes title (with 2 alternates), thumbnail title (2 to 5 words, at most 28 characters, text only, never an image), description, recommended platform (up to 2 alternates), hashtags (3 to 8), and domain (from a fixed list of about 30, plus optional subtopic). Nodes: `load_short` -> `understand_short` -> `choose_platform` -> optional `ask_user` (only if domain confidence is under 0.6, language unknown or no speech) -> `write_fields` -> `check` (code, then an LLM critic that fails any claim not in the cues, up to 2 rewrites) -> `save_and_reply` -> `await_feedback` -> `revise_fields`. Revisions are field ops: set_field, regenerate_field, set_platform, set_tone, set_language. Each result is a new append-only `metadata_sets` row tied to an `edl_version`. Platform limits live as data in `backend/packages/core/platforms.py` and must be re-checked before launch. No thumbnail generation, no posting, no scheduling in v1.

### 11.4 Retrieval (RAG) and learning

- Hybrid search per project: top 20 by vector similarity plus top 20 by full text, merged by reciprocal rank fusion (k = 60), top 8 returned with cue ids and times. A small LLM call rewrites the request into 1 to 3 queries. Exact scan inside one project, no ANN index.
- Answers about the video must cite cue ids. Code checks the ids exist and an LLM verifier checks the answer is supported. Otherwise the agent says it could not find it.
- Learning is in-context, not training. Signals (thumbs with reasons, diffs between an agent version and the user's next version, exports, chat corrections, metadata edits, standing instructions) go to `feedback_events`. `memory.distill` turns them into short preference statements of fixed kinds (length, aspect, style, transitions, titles, avoid). An inferred statement becomes active after 3 events across 2 projects and a one-time confirmation toast. A standing instruction is active at once. `load_context` retrieves the top 5 matching memories into the prompt as data. The user's latest message always wins. Users can read, edit and delete every memory and can switch learning off.

### 11.5 LLM providers per tier

Calls run in three tiers: **main** (planning, ideas, metadata), **small** (intent routing, query rewrite, verifier, distill) and **chat** (streamed answers). Each tier picks its provider: `LLM_PROVIDER` for main, `LLM_SMALL_PROVIDER` and `LLM_CHAT_PROVIDER` (default: the same). A tier on a different provider must name its own model (`LLM_SMALL_MODEL_ID`, `LLM_CHAT_MODEL_ID`). Providers:

- **bedrock**: `ChatBedrockConverse`, `BEDROCK_REGION`, `LLM_STRUCTURED_METHOD`.
- **groq**: Groq's OpenAI-compatible API (`https://api.groq.com/openai/v1`) through langchain-openai `ChatOpenAI`, `GROQ_API_KEY` (a secret: `.env` locally, Secrets Manager on AWS, never in code or logs). Structured output uses Groq's `json_schema` response format (`GROQ_STRUCTURED_METHOD=function_calling` as a fallback); replies are still validated by Pydantic. Reasoning is kept out of the text: `include_reasoning: false` for gpt-oss, `reasoning_format: hidden` for Qwen. Chat models: `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`, `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`. Canopy Labs (Orpheus) and Whisper on Groq are speech models, not usable as agents.
- **nvidia**: NVIDIA's API catalog (`https://integrate.api.nvidia.com/v1`, OpenAI-compatible) through the same `ChatOpenAI` client, `NVIDIA_API_KEY` (secret, as above). Structured output defaults to a forced tool call (`NVIDIA_STRUCTURED_METHOD=function_calling`): on NVIDIA's reasoning models it skips most of the thinking and measured 2.5 s against 12.7 s for `json_schema`. Reasoning arrives in a separate `reasoning_content` field, so replies carry only the answer. Models used: `deepseek-ai/deepseek-v4.1-flash`, `nvidia/nemotron-3.5-lightning-30b-a3b`. The catalog publishes no per-token prices, so NVIDIA calls record tokens with a null cost. Some catalog models are not enabled for every account (404 "Function not found").
- **ollama**: local testing only.

Example: agentic work on Groq, chat on a local model: `LLM_PROVIDER=groq`, `LLM_MODEL_ID=openai/gpt-oss-120b`, `LLM_SMALL_MODEL_ID=openai/gpt-oss-20b`, `LLM_CHAT_PROVIDER=ollama`, `LLM_CHAT_MODEL_ID=llama3.2:latest`. Prices for usage costs are in `core/pricing.py`, read from the provider's own price list; Groq's Llama models have no public price, so their calls record tokens with a null cost.

## 12. Speaker tracking and crop path

Pure function `core.crop.crop_path(detections, segments, ...)`, plain Python with no numpy, so the API can recompute crops without importing vision. `backend/packages/vision` only produces `detections.json`:
1. Compute `w0` and `h0` from source and output aspect.
2. Pick the main track: largest sum of box area times confidence inside the selected segments.
3. Track x if `w0 < 1`, y if `h0 < 1`. Hold a missing track for up to 1 s, then fall back to the frame center.
4. Smooth per segment: median over 5 samples, then EMA with alpha 0.2.
5. Dead zone 0.04 of the frame size on that axis, maximum speed 0.15 of the frame size per second.
6. Clamp so the window stays inside the frame.
7. Work per segment in source time with its own keyframes (overlapping segments in a transition keep separate crops). Smoothing restarts at each segment.
8. Emit keyframes every 0.5 s plus segment start and end.

Filter values are starting points: tune on three or four real videos. One main speaker in v1.

### 12.1 Speaker overlay (editor only)

The editor can draw the detections over the video: a **green** box for the person the crop follows at that moment and **red** boxes for other people, mapped through the current crop window (or the whole frame in Fit and source playback). It shows the user exactly what a portrait (or any aspect) export will keep in frame, because it uses the same rule as the crop path (`frontend/src/lib/speakers.ts` mirrors `core.crop.focus_spans`). The person talking is labelled ("Person 2 · talking"). While the boxes are on, the timeline shows a **Speakers** lane: one coloured bar per person per turn; click to jump. The overlay is a canvas in the browser only; the export never draws it.

### 12.2 Who talks when (ingest stage 7)

`vision.lips.measure_lips` decodes the video at 12 fps (short side 720), cuts out each YOLO person's head, enlarges it to 256 px and runs MediaPipe's face landmarker (`face_landmarker.task`, pinned by sha256; `FACE_MODEL` points the worker image at a baked-in copy). Its `jawOpen` score is the mouth opening. Each sample also records the audio loudness 80 ms later (the mouth opens about a frame before the sound is heard). `core.speakers.speaker_turns` then judges speech (transcript words) in 3 s windows: a person talks when their mouth opening correlates with the loudness at 0.3 or more and beats the next person by 0.1. A listener's mouth does not follow the voice. When several mouths do, they are the same person shown twice (a monitor, a mirror), and the largest on screen wins. One unclear window between two of the same person is filled; turns of one person less than 1.5 s apart join.

The crop follows the talker (`core.crop.focus_spans`): it cuts to each turn of 1.5 s or more, holds the last talker through pauses, and follows the main person before anyone talks. Smoothing restarts at each cut. Without turns (no people, no speech, no audio, or faces too small or turned away) it follows the main person as before.

`GET /projects/{id}/speaker` adds `analysed`, `turns` (with "Person N" labels in order of first talking) and `cut_min_s`. The chat's speaker intent answers "who is speaking?" with a table of turns and "who is speaking at 1:20?" with the turn at that moment. Videos ingested before this stage: `python -m worker.backfill_speakers [project_id ...]`. Saved plans keep their crop until their clips or output size change.

Limits: ByteTrack gives a person a new id after a camera cut, so one person can appear as two labels; low-resolution or side-on faces give few readings; a person shown twice can still be picked as the smaller copy when the larger face is not readable in that window.

## 13. Export (three passes)

1. **Prepare.** Load and validate the EDL version, download the source. Keep free disk at twice the source size.
2. **Render each segment.** Write `crop_i.cmd` (per-frame x and y from the keyframes, times from segment start) and run ffmpeg with `sendcmd` plus `crop`, `scale={out_w}:{out_h}`, `setsar=1`, `fps=30`. Same size, frame rate and timebase for every segment. 80 ms audio fades only at cut boundaries. In `fit` mode use a split, a scaled and blurred background (`boxblur=20:5`), a scaled foreground and an overlay.
3. **Join and finish.** All cuts: concat demuxer. Otherwise `xfade` and `acrossfade` in one filter graph with `offset_k = (len_1 + ... + len_k) - (d_1 + ... + d_k)`. A cut inside a chain splits it, joined with the `concat` filter. Then burn `captions.ass`, `loudnorm=I=-16:TP=-1.5:LRA=11`, H.264 CRF 18, AAC 160k, faststart. When every boundary is a cut and no captions are burned, the final pass copies the segments' video (`-c:v copy`) so the picture is encoded once; only the audio is filtered. Segment scaling uses `flags=lanczos`. The final pass pins the length with `-frames:v N` (N = output duration x 30), `-fps_mode passthrough` and `atrim` on the audio; re-timing with `-r 30` made cut joins one frame long. Directions: `slideleft` and `wipeleft` bring the next clip in from the right edge moving left, and the `*right` types mirror that; the preview must match.

Captions: cues inside each segment, `captions.edits` applied, shifted to output time minus overlaps, split at segment edges. Writes `captions.ass` (burned) and `captions.srt` (download). Upload `{export_id}.mp4` and `.srt` with multipart. Progress: `-progress pipe:1`, weights segments 70, final pass 25, upload 5. Failure deletes partial files; retry is offered.

## 14. Frontend

- Next.js App Router, Ant Design v6 with Ant Design X v2 (confirm versions at install), `@ant-design/nextjs-registry`. Only Ant Design components (see `design.md`). Dark theme only.
- API calls go to same-origin `/api/*` (Next.js rewrites locally, CloudFront in production). Types are generated from `backend/services/api/openapi/openapi.json` with openapi-typescript.
- Server state: TanStack Query. Edit state: Zustand store with immer patches `{ edl, version, dirty, past[], future[] }`, kept per project id (last 3 in memory). Player state is a second store `{ playing, tOut, segIndex }`.
- Autosave debounces 800 ms then `PATCH /edl` with `If-Match`. On 409 show keep-my-edits (rebase) or take-the-new-plan.
- Preview: two `<video>` elements on the proxy, inside a clipping box with the output aspect ratio. Layer B pre-seeks before the boundary (lead = transition duration + 0.5 s, 0.5 s for a cut). Time comes from `requestVideoFrameCallback`: `tOut = outStart(i) + (video.currentTime - src_start(i))`. Each layer applies its own segment's crop. Transitions animate opacity, translate or clip-path.
- Time mapping in `backend/packages/core` (`core.timemap.src_to_out` and `out_to_src`, both layers inside an overlap). The browser needs the same math at 60 fps, so `frontend` has a TypeScript port that must pass the shared vectors in `backend/packages/core/src/core/vectors/timemap.json`, the same file the Python tests run. Cue clicks seek through these; a cue outside the edit switches the preview to the source.
- Uploads run in a layout-level upload manager so switching videos does not stop them.

## 15. Security and failure handling

- Block all public bucket access. Presigned URLs scoped to `uploads/{user}/{project}/`. Assets by CloudFront signed cookies.
- Every query filters by `user_id`. 403 if a project belongs to someone else. The only exception is the admin routes in section 16, which read account fields, limits and usage across users, never project content.
- Transcript text, learned memories and user messages are data, never instructions, in every prompt.
- Failures: upload part retries 3 times; ingest failure offers Retry and resumes from stage markers; invalid LLM output retries twice then fails the turn with a message; a user/agent collision returns 409 and the agent reapplies; export failure deletes partial files; a reclaimed Spot instance loses its workers, and their jobs are claimed again when the lease expires (about 2 minutes).

## 16. Settings, roles and usage

**Roles.** `users.role` is `admin` or `editor` (every normal user). New users are editors. The first admin is set with a one-off command (`python -m api.admin grant-admin <email>`), never through the UI. An admin can change roles in `PATCH /admin/users/{id}` but cannot remove the last admin. The API checks the role in a `require_admin` dependency on every `/admin/*` route and returns 403 `forbidden` otherwise.

**What an admin can see.** Account fields, limits, counts, storage and LLM usage of every user. Never another user's videos, transcripts, messages, plans or memories. Admin routes live in `backend/services/api/src/api/admin/` and are the only code allowed to read across users (the exception to the `user_id` rule in section 15).

**Settings.** Keys are defined once in `backend/packages/core/settings.py` as a Pydantic model with a type, default and allowed range per key. `PATCH /admin/settings` and `PATCH /admin/users/{id}` validate against it (422 `setting_invalid`). The effective value for a user is their `user_limits` row, else the `app_settings` row, else the code default.

| Key | Default | Checked at |
| --- | --- | --- |
| max_active_videos | 5 | `POST /projects`. Counts projects with `deleted_at` null. Deleting a video frees the slot at once |
| max_file_size_mb | 1024 | `POST /projects` (declared size, 413 `file_too_large`) and `POST /uploaded` (real size from S3) |
| max_storage_mb | 5120 | `POST /projects`. Sum of `size_bytes` of the user's projects with `deleted_at` null, plus the new file |
| max_video_duration_s | 3600 | `POST /projects` when the browser sends `duration_s`, and always at ingest stage 1 (authoritative) |

`POST /projects` takes `pg_advisory_xact_lock` on the user before counting, so two uploads at once cannot both pass the check. Limit failures return 403 `quota_exceeded` with the key, limit and current value. Lowering a limit never deletes or blocks existing videos. It only stops new uploads. The per-user limit of 2 running jobs (section 9) stays a code constant in v1.

**Token usage.** The LLM client wrapper writes one `llm_usage` row per call with the token counts the provider returns (never estimated), the job kind and graph node, and `cost_usd`. Cost is computed at write time from a price table in `backend/packages/core/pricing.py` (per model, per million input, output, cache-read and cache-write tokens), so later price changes do not rewrite history. The prices must be filled in from the provider's price page when the provider is chosen. The row is written in its own transaction, so a call is counted even when the job later fails. Whisper and the embedding model run locally and are not counted.

## 17. Not verified yet (spike before relying on it)

- Wipe and slide directions in the browser preview match ffmpeg's (export side verified, see `memory.md` section 6).
- The same three export checks on the ffmpeg in the worker image (verified on ffmpeg 7.1 on macOS only).
- Preview and export agree on crop position and transition timing (render a 20 s clip with two transitions and compare).
- The embedding model on real, multilingual transcripts; the retrieval similarity threshold.
- Platform limits for titles, descriptions and hashtags.
- antd `Table` virtual scroll with the real cue row height. (Ant Design X 2.9.0 requires antd ^6.1.1: checked 2026-10-25.)
- Timeouts, and three concurrent exports on the heavy instance.
- The claim query under several concurrent workers: no job runs twice at once, the limit of 2 running jobs per user holds, and a killed worker's job is claimed again after the lease.
- Total database connections (API pool plus every worker) against the `db.t4g.micro` limit, and the load from 1 s polling.
