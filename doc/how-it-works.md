# Clipora AI: how it works, end to end

One walkthrough of what actually runs, from the moment a video is uploaded to the moment a short is downloaded, plus everything that runs around it. `architecture.md` is the detailed specification (tables, limits, endpoint list); this file follows a video through the system and points at the code that does each step. Last checked against the code: 2026-10-26.

---

## 1. The big picture

```
                                   Browser  (Next.js 16 + Ant Design 6 / Ant Design X, dark theme)
                                      |   polls GET /api/projects/{id} every 2.5 s while a job runs
                                      |   streams chat answers over SSE
            +-------------------------+--------------------------+
            | /api/*                  | /api/chat/*              | direct PUT / GET with presigned URLs
            v                         v                          v
      api  (FastAPI, :8000)     chat (FastAPI SSE, :8001)       S3  (uploads/, assets/, exports/)
            |                         |                          ^
            |  writes rows            |  routes messages,        |  upload source, read source,
            |  + enqueues jobs        |  streams answers         |  write every derived asset
            v                         v                          |
      +--------------------- Postgres (+ pgvector) --------------+----------------------------+
      |  projects, messages, edl_versions, exports, metadata_sets, transcript_chunks,         |
      |  feedback_events, agent_memories, app_settings, user_limits, llm_usage                |
      |  jobs  <- this table IS the queue (FOR UPDATE SKIP LOCKED, leases, retries)           |
      |  LangGraph checkpoints (edit graph and metadata graph state)                          |
      +------------------+-------------------------+-------------------------+---------------+
                         | claim                   | claim                   | claim
                worker-ingest (1 slot)      worker-agent (2 slots)     worker-export (1 slot)
                ffmpeg, Whisper, YOLO,      LangGraph agents,          ffmpeg render
                MediaPipe, embeddings       LLM calls (Groq/NVIDIA/
                                            Bedrock/Ollama)
```

Five ideas explain almost every design choice:

1. **The plan is the product.** The AI writes an **EDL** (edit decision list: which source ranges, in which order, with which crop and transitions). Nothing is rendered until the user presses Export.
2. **The EDL is the only contract** between the agent, the API, the browser preview and the export worker. Its Pydantic model lives in `backend/packages/core/src/core/edl.py`.
3. **Code decides, the LLM suggests.** The LLM proposes moments and wording; code snaps timestamps to words, applies edits, computes crops and validates.
4. **One Postgres job queue, no message broker.** Background work is a row in `jobs`. Workers claim rows; the browser polls the same rows for progress. Only chat answers stream (SSE).
5. **Same math in preview and export.** Time mapping, crop path and transitions are pure functions in `core`, ported once to TypeScript for the browser.

---

## 2. What is running

| Process | Local command (started by `infra/scripts/dev.sh up`) | Job |
| --- | --- | --- |
| **web** | `next dev -p 3001` (in `frontend/`) | The app at http://localhost:3001. Rewrites `/api/chat/*` to :8001 and `/api/*` to :8000 |
| **api** | `uvicorn api.main:app --port 8000` | Projects, uploads, plans, exports, metadata, account, admin. Never imports ML code |
| **chat** | `uvicorn chat.main:app --port 8001` | Routes each typed message (edit / question / chat / speaker) and streams answers |
| **worker-ingest** | `python -m worker.run --queue ingest --slots 1` | Analysis of a new video (8 stages) |
| **worker-agent** | `python -m worker.run --queue agent --slots 2` | Agent turns, ideas, metadata, learning |
| **worker-export** | `python -m worker.run --queue export --slots 1` | FFmpeg render of a plan |
| **postgres** | Docker (`infra/docker/compose.yml`) | Data, queue, vectors, graph checkpoints |
| **S3** | AWS bucket `clipora-dev-media-22e7cc` (ap-southeast-2, profile `personal`), or local RustFS on :9100 when `S3_ENDPOINT_URL` is set | All media |

`infra/scripts/dev.sh up | down | status`. Logs: `.dev/logs/<process>.log`. On AWS the same image runs as ECS services (general group: api, chat, worker-agent; heavy group: worker-ingest, worker-export) behind CloudFront and an ALB (`architecture.md` section 5).

---

## 3. A video's journey

### 3.1 Sign-in and limits

- Every request carries a bearer token. On AWS it is a Cognito JWT checked against the JWKS. Locally it is `dev:you@example.com` (`backend/services/api/src/api/auth.py`).
- A `users` row is created on first request. Roles: **editor** (default) or **admin**. The first admin is made with `python -m api.admin grant-admin <email>`.
- Limits come from `core/settings.py`: per-user override (`user_limits`), else app-wide (`app_settings`), else the code default: 5 active videos, 1 GB per file, 5 GB total, 60 min per video. Admins change them on the Settings page.

### 3.2 Upload (browser → S3, never through the API)

```
Browser                         api                                   S3
  | POST /projects  (title, size, type, duration?, srt?)  ------------->|
  |     quota check under an advisory lock on the user                 |
  |     project row status=uploading, S3 multipart started            |
  | <-- project_id, upload_id, part size 16 MB                        |
  | POST /projects/{id}/upload/part-urls  (batches of 20, 15 min)     |
  | PUT part 1..n  (4 in parallel, 3 retries each)  --------------------------------> uploads/{user}/{project}/source.ext
  | POST /projects/{id}/uploaded                                       |
  |     complete multipart, check real size, status=ingesting         |
  |     + INSERT jobs(kind=ingest)  in the same transaction            |
```

- Client: `frontend/src/lib/uploads.ts` (a layout-level upload manager, so switching videos does not stop an upload; shown in `ActiveUploads`).
- Server: `backend/services/api/src/api/routes/projects.py`, `services/quotas.py`, `backend/packages/storage`.

### 3.3 Analysis: the ingest job (worker-ingest)

`backend/services/worker/src/worker/handlers/ingest.py`. Eight stages. Each stage writes its result to S3 and records a **stage marker** on the job row, so a retry or a lost worker resumes from the last finished stage. Progress (`jobs.stage`, `jobs.percent`) is what the history list and the editor show.

| # | Stage | What happens | Writes to `assets/{user}/{project}/` |
| --- | --- | --- | --- |
| 1 | probe | ffprobe: duration, size, fps. Rejects no video / no audio / too long (`duration_too_long`, no retry) | (project row) |
| 2 | transcribe | faster-whisper `small` (int8, CPU, word timestamps, VAD) on 16 kHz mono. Skipped when the user gave an SRT. Words grouped into cues `c1, c2, …` (≤ 2 lines × 42 chars, ≤ 7 s) | `transcript.json`, `transcript.srt` |
| 3 | proxy | 720p (never above the source) H.264 CRF 23 with a keyframe every 48 frames, for exact seeking in the browser. The editor's **Full quality** button plays the original upload instead | `proxy-720p.mp4` |
| 4 | thumbnails | One frame per 2 s, packed into sprite sheets; a poster frame | `thumbs/*.jpg`, `poster.jpg` |
| 5 | waveform | Min/max per 50 ms, int8, gzipped | `waveform.bin.gz` |
| 6 | detect | YOLOv8n people at 3 fps, ByteTrack keeps an id per person | `detections.json` |
| 7 | speakers | **Who talks when.** 12 fps head crops → MediaPipe `jawOpen`, correlated with audio loudness in 3 s windows over the transcript's speech (`vision/lips.py`, `core/speakers.py`) | `detections-speakers.json` (detections + `turns`) |
| 8 | index | Transcript chunks (3 cues, 1 overlap) embedded with multilingual-e5-small (384-d) plus a full-text vector, into `transcript_chunks` | (Postgres) |

When all stages are done, one transaction sets the project to **ready** and enqueues a **suggest** job.

### 3.4 Ideas (suggest job, worker-agent)

`worker/handlers/suggest.py` → `agent/suggest.py`. One structured LLM call reads the transcript and returns a summary, topics and 4–6 short ideas (title, why, length, aspect, style, anchors as cue ranges). Code turns anchors into times and drops ideas it cannot place. The ideas are posted as a `suggestions` message (idea cards in the chat). They are discarded if the user already typed something. "More ideas" (`POST /projects/{id}/ideas`) runs it again.

### 3.5 The chat: one message, four routes

Every typed message goes to the **chat service** (`backend/services/chat/src/chat/turn.py`), which routes it with `agent/conversation.py`: instant wording rules first, the small model only when the wording is unclear.

| Intent | Example | What happens |
| --- | --- | --- |
| **edit** | "make a 30 s short about passion", "remove the second clip" | Saved as a user message + an **agent job** in one transaction (same path as `POST /projects/{id}/messages`). The browser shows the job's progress chain |
| **question** | "what does he say about money?" | Answered right away from the transcript, **streamed token by token** over SSE, with `[cNN]` citations turned into clickable sources |
| **chat** | "thanks!" | Short streamed reply |
| **speaker** | "who is speaking at 1:20?" | Answered from the cached detections and turns (no model call); the editor turns on the speaker overlay |

SSE events: `status`, `route`, `token`, `done`, `error`, and a `: ping` every 10 s so proxies keep the stream open. A reply to an open question from the edit graph always goes back to the graph. Picking an idea card sends its settings as a ready-made edit request.

### 3.6 Generation: the edit graph (agent job)

`backend/packages/agent/src/agent/edit_graph.py`, a LangGraph whose state is checkpointed in Postgres (thread id = project id), so a conversation can pause and resume.

```
load_context ─► parse_intent ─┬─► post_question ─► ask_user (pause: job "waiting_input") ─► parse_intent
                              ├─► retrieve ─► find_candidates ─┬─► select_and_order ─► build_crop ─► assemble_edl ─► critic ─► save_and_reply ─► await_feedback
                              │                                └─► reply_no_moments ─► await_feedback        ▲ retry ┘        │ 409 conflict
                              ├─► revise_plan  (edit ops on the current plan) ─────────────────────────────────────────────► save_and_reply
                              ├─► answer_from_video ─► await_feedback
                              └─► reply_metadata ─► await_feedback
```

| Node | Kind | Does |
| --- | --- | --- |
| load_context | code | Transcript, detections (with speaker turns), current EDL, top 5 learned preferences |
| parse_intent | LLM (main) | Request → `Intent`: plan / question / correction / metadata, target length, aspect, style, focus |
| post_question / ask_user | code + interrupt | Asks one question with quick replies when the request is too vague; the job waits |
| retrieve | LLM (small) + code | Rewrites the focus into 1–3 queries; hybrid search (vector + full text, reciprocal rank fusion) over `transcript_chunks` |
| find_candidates | LLM (main) | Up to 15 candidate moments as cue ranges |
| select_and_order | LLM + code | Picks and orders segments within 10 % of the target length; chooses transitions from the style. **Code snaps every boundary to a word** |
| build_crop | code | `core.crop.crop_path`: for each segment, keyframes that follow whoever is talking (cuts on speaker change), else the main person |
| assemble_edl | code | Builds the EDL, validates it (`core/validation.py`) |
| critic | LLM (main) | Checks the plan matches the request; up to 2 retries |
| save_and_reply | code | One transaction: new `edl_versions` row (author agent), `projects.current_version`, assistant message with the plan. A new plan gets `meta.short = {id, title}` (an "edited video") |
| revise_plan | LLM → edit ops | Changes are ops applied by code (remove, trim, add, reorder, set_target, set_transition, set_aspect), never a rewritten EDL |
| answer_from_video | LLM | Question answered with checked cue citations |
| await_feedback | interrupt | Turn ends (job succeeded); the next message starts a new job that resumes the same thread |

Every model call goes through the LLM client (section 4.2): structured output validated by Pydantic, 2 retries with the validation error, usage row, archived prompt.

### 3.7 Editing in the browser

`frontend/src/components/editor/` (`EditorPage.tsx`).

- **Left: tabs.** Chat (Ant Design X bubbles, think blocks for plans, streamed Markdown answers, retry), **Edited videos (n)** (one card per `meta.short`; "Edit this video" loads that version), **Transcript** (click a cue to seek, edit caption text, add a range), **Title & hashtags** (metadata).
- **Right: player.** Two `<video>` layers on the proxy inside an output-aspect box; the next clip is pre-seeked so cuts and transitions play smoothly. Crop and transitions use the same math as export (TypeScript port of `core.timemap`, tested against the same vectors). Click to play or pause, volume and mute, aspect picker, **Speaker** toggle (green box on the person the crop follows, red on others; never exported).
- **Bottom: timeline.** Waveform above thumbnails, zoom with a thin scrollbar, hover cursor, View / Edit modes (drag to reorder, trim edges, split with S, delete), unused-moment candidates, and a **Speakers** lane while the overlay is on.
- **Saving.** Each change is an immer patch in a Zustand store (undo/redo). Autosave waits 800 ms, then `PATCH /projects/{id}/edl` with `If-Match: <version>`. The API validates, recomputes crop keyframes only for segments whose range or the output size changed, and appends a new version (author user). A 409 (the agent saved meanwhile) opens "keep my edits / take theirs".
- **Polling.** `GET /projects/{id}` every 2.5 s only while a job is queued or running. Polls never replace a plan with an older version, and never reload the video.

### 3.8 Title, description and hashtags (metadata job)

`backend/packages/agent/src/agent/metadata_graph/graph.py`. Started from the Title & hashtags tab. `route_message → understand_short → choose_platform → (ask_user) → write_fields → check → save_and_reply → await_feedback`, with `revise_fields` for follow-ups. Platform limits are data in `core/platforms.py`; a code check then an LLM critic reject any claim not in the transcript. Each result is an append-only `metadata_sets` version tied to the plan version.

### 3.9 Export (worker-export)

`POST /projects/{id}/exports` → export job → `worker/handlers/export.py` with `media/export.py`:

1. **Prepare** – load and re-validate the EDL version, download the source, check disk space.
2. **Render each segment** – a per-frame crop script (`sendcmd` + `crop`) from the keyframes, scale to the output size, 30 fps, short audio fades at cuts. `fit` framing puts the whole picture over a blurred copy.
3. **Join and finish** – concat for cuts, `xfade`/`acrossfade` for transitions, burn captions (`captions.ass`), loudness normalise, H.264 CRF 18 + AAC, faststart. All cuts and no burned captions: the video is copied from the segments (encoded once). Scaling uses Lanczos.

Output: `exports/{project}/{export_id}.mp4` and `.srt`; the browser gets presigned download links. Progress weights: segments 70 %, final pass 25 %, upload 5 %.

### 3.10 Learning (feedback → distill → memories)

When learning is on for the user, thumbs, the diff between an agent plan and the user's next edit, exports and corrections become `feedback_events`. A **distill** job (`worker/handlers/distill.py`, `agent/distill.py`) turns them into short preference statements (length, aspect, style, transitions, titles, avoid) in `agent_memories`, with embeddings. An inferred preference activates after 3 events across 2 projects; `load_context` puts the top 5 into the prompt as data. Users can view, edit, delete or switch learning off.

---

## 4. Machinery used by every step

### 4.1 The job queue (`backend/packages/jobqueue`, `backend/services/worker`)

| Kind | Queue | Started by |
| --- | --- | --- |
| ingest | ingest | upload finished |
| suggest | agent | ingest finished, "More ideas" |
| agent | agent | an edit message or an idea pick |
| metadata | agent | Title & hashtags tab |
| distill | agent | feedback events |
| export | export | Export button |

- **Enqueue** inside the same transaction as the write that causes it.
- **Claim** with `FOR UPDATE SKIP LOCKED`; at most 2 running jobs per user (advisory lock).
- **Lease** of 120 s, renewed by progress writes and a 30 s heartbeat; a dead worker's job is reclaimed. A watchdog enforces timeouts; retries back off; throttling from the LLM becomes `rate_limited`.
- **One active job per (project, kind)**: a second edit while the agent works gets 409 `agent_busy`.
- `waiting_input` jobs (the agent asked a question) hold no lease and resume on the reply.
- The worker loop survives database restarts. The Retry button re-queues a failed job and keeps its stage markers.

### 4.2 The LLM layer (`backend/packages/agent/src/agent/llm.py`, `config.py`)

Three **tiers**, each with its own provider and model:

| Tier | Used for | Provider / model setting |
| --- | --- | --- |
| main | intent, candidates, selection, critic, ideas, metadata | `LLM_PROVIDER`, `LLM_MODEL_ID` |
| small | message routing, query rewrite, verifier, distill | `LLM_SMALL_PROVIDER`, `LLM_SMALL_MODEL_ID` |
| chat | streamed answers and small talk | `LLM_CHAT_PROVIDER`, `LLM_CHAT_MODEL_ID` |

Providers: **groq** and **nvidia** (OpenAI-compatible APIs via langchain-openai), **bedrock** (Converse API via langchain-aws), **ollama** (local). Keys come from `.env` locally and Secrets Manager on AWS. Current local setup: all three tiers on Groq (`openai/gpt-oss-120b` main, `openai/gpt-oss-20b` small and chat); NVIDIA (`deepseek-ai/deepseek-v4.1-flash`) is configured and can take the main tier by uncommenting two lines in `.env`.

Every call:
- structured output (`json_schema` or a forced tool call), validated by Pydantic, 2 retries with the error;
- reasoning text kept out of replies;
- one `llm_usage` row (tokens as reported, cost from `core/pricing.py` when the provider publishes a price, else null) — this feeds the admin token page per editor;
- prompt and response archived to S3 under `assets/{user}/{project}/llm/{job}/`, never logged;
- transcript, memories and user text are wrapped as data blocks, never instructions.

### 4.3 Storage layout (S3)

```
uploads/{user}/{project}/source.<ext>          original upload (presigned multipart)
assets/{user}/{project}/transcript.json|.srt   ingest
                        proxy.mp4, poster.jpg, thumbs/, waveform.bin.gz
                        detections.json, detections-speakers.json
                        llm/{job_id}/{seq}-{node}.json            archived LLM calls
exports/{project}/{export_id}.mp4|.srt         renders
```

Bucket: public access blocked, SSE-S3, CORS for the app origin, lifecycle rule aborts stale multipart uploads. The browser only ever gets short-lived presigned URLs (cached 45 min so polls return stable URLs).

### 4.4 Data (Postgres, Alembic migrations in `database/`)

`projects` (status, current version) · `edl_versions` (append-only plans, author agent/user, `meta.short` groups them into edited videos) · `messages` (edit and metadata threads) · `jobs` (queue + progress + stage markers) · `exports` · `metadata_sets` · `transcript_chunks` (pgvector + full text) · `feedback_events`, `agent_memories` · `app_settings`, `user_limits` · `llm_usage` · LangGraph checkpoint tables. Every query is filtered by `user_id`; only `/admin/*` reads across users, and never project content.

### 4.5 Admin and usage

`/admin` (admins only): users with role, active videos, storage, tokens and cost by date range; per-user limits and role changes; app-wide settings. Token usage is per editor, per project, per job kind and per day, from `llm_usage`.

---

## 5. Failures and recovery

| What fails | What happens |
| --- | --- |
| Upload part | Retried 3 times; a closed tab leaves "Upload interrupted" |
| Ingest stage | Job retries (3 attempts) and resumes from the last stage marker; Retry button after that |
| Video too long | `duration_too_long`, no retry |
| LLM invalid output | 2 retries with the validation error, then the turn fails with a message |
| LLM rate limit | Job marked `rate_limited`; the chat shows "usage limit" |
| User edit vs agent save | 409; the agent reapplies its ops on the new version, the user chooses keep/take |
| Worker dies | Lease expires, another worker reclaims the job within ~2 min |
| Export | Partial files deleted; Retry offered |

---

## 6. Where to find things

| Area | Path |
| --- | --- |
| EDL, validation, time mapping, crop path, speakers, settings, prices | `backend/packages/core/src/core/` |
| Queue | `backend/packages/jobqueue/` |
| S3 helpers | `backend/packages/storage/` |
| ffmpeg / ffprobe, export plan, captions, waveform | `backend/packages/media/` |
| YOLO detection, lip measurement | `backend/packages/vision/` |
| Chunking, embeddings, hybrid search | `backend/packages/rag/` |
| LLM client, edit graph, ideas, metadata graph, routing, distill | `backend/packages/agent/src/agent/` |
| HTTP API (routes → services) | `backend/services/api/src/api/` |
| Streaming chat | `backend/services/chat/src/chat/` |
| Worker loop and job handlers | `backend/services/worker/src/worker/` |
| Database models and migrations | `database/` |
| Web app (editor, chat, player, timeline, admin) | `frontend/src/` |
| Docker, S3 setup, dev script | `infra/` |

## 7. Everyday commands

| Task | Command |
| --- | --- |
| Start / stop / status | `infra/scripts/dev.sh up` · `down` · `status` |
| Migrate the database | `uv run alembic -c database/alembic.ini upgrade head` (run by `up`) |
| Make an admin | `uv run python -m api.admin grant-admin you@example.com` |
| Add speaker turns to older videos | `uv run python -m worker.backfill_speakers [project_id ...]` |
| Regenerate API types for the web app | `uv run python -m api.export_openapi` then `npm run gen:api` in `frontend/` |
| Tests | `uv run pytest backend` · `npm run typecheck && npm test` in `frontend/` |
| Switch LLM providers | edit the `LLM_*` lines in `.env`, then restart (`architecture.md` 11.5) |
