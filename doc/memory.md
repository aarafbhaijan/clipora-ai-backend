# Clipora AI: project memory

Living notes for Claude Code. Read this after `rules.md`. Keep it short and true: edit entries that go stale, do not just append. Newest first inside each section. Dates are YYYY-MM-DD.

## 1. Status (update every session)

- Phase: v1 code written, not yet run end to end. Monorepo at the repo root: `frontend` (Next.js), `backend/services/api` (FastAPI), `backend/services/worker`, `packages/*`. See `README.md` to run it.
- Last updated: 2026-10-25.
- Verified: ruff, mypy strict (87 files) and pytest (54 passed, 12 skipped) on the Python side; tsc, lint, vitest (12) and `next build` on the web side; the three ffmpeg export spikes (section 6).
- Not run yet: anything needing Postgres or S3 (Docker was not running), any live Bedrock call (no AWS credentials, no model ids), a browser session against a live API.
- Next step: start Docker, run the migration and the queue tests (`TEST_DATABASE_URL`), then an end-to-end upload, ingest, plan and export on a 60 s sample. Set `LLM_MODEL_ID` and `BEDROCK_REGION` for the agent. Fill `core/pricing.py` from the Bedrock price page.
- Not a git repository yet. Nothing committed.

## 2. Project in brief

Clipora AI turns long videos into short clips through a chat with an agent. The agent outputs an edit plan (EDL), the user reviews it in a timeline editor, and FFmpeg renders only on export. Owner: Arav. Full detail: `prd.md`, `architecture.md`, `design.md`.

## 3. Decisions made (with reasons)

| Date | Decision | Why |
| --- | --- | --- |
| 2026-10-26 | NVIDIA API catalog as a fourth provider (`LLM_PROVIDER=nvidia`, `NVIDIA_API_KEY`), sharing an OpenAI-compatible factory with Groq. Requested for video editing (main tier) with `deepseek-ai/deepseek-v4.1-flash` | User request. DeepSeek V4.1 Flash on the hosted endpoint did not answer during setup (no reply in 90-240 s) while Nemotron answered in 1-2 s. The switch lines are in `.env` |
| 2026-10-26 | Groq as an LLM provider, chosen per tier (`LLM_PROVIDER`, `LLM_SMALL_PROVIDER`, `LLM_CHAT_PROVIDER`): agentic work on Groq (gpt-oss-120b main, gpt-oss-20b small), chat can stay on local Ollama. Through langchain-openai against Groq's OpenAI-compatible API; key in `GROQ_API_KEY` | User request. Groq gpt-oss is cheap ($0.15/$0.60 per M for 120b) and fast; Bedrock quota is still 0 |
| 2026-10-26 | Who talks when: MediaPipe jawOpen per tracked person at 12 fps, correlated with audio loudness in 3 s windows (architecture 12.2). Crop cuts to the talker | User request. Corner-based lip motion (OpenCV YuNet) and transcript-only matching showed no usable signal on real videos; jawOpen against loudness did (0.31 vs 0.0 control) |
| 2026-10-25 | Speaker overlay: `GET /projects/{id}/speaker` serves the cached YOLO boxes and the main track (same rule as the crop path); the editor draws green (speaker) and red (others) boxes, display only. "Who is speaking" is a chat intent that turns it on. YOLO is not re-run per request | User request: see where YOLO found the speaker; the export must not include the boxes and must follow the green box |
| 2026-10-25 | Intent router + streamed chat: typed messages go to a new `chat` service (SSE). Edits are routed to the existing job queue; questions about the video and small talk stream back word by word, answered from the whole transcript with citations | User request: "if it's a video change request then editing; questions or casual conversation should stream". User chose SSE over fast polling. Rule 6 amended |
| 2026-10-25 | LLM for testing and launch: Amazon Nova Lite (main) and Nova Micro (small calls) on Bedrock, Sydney, via tool-calling structured output (`LLM_STRUCTURED_METHOD=function_calling`; Nova has no JSON-schema output mode). Ollama stays as a free local fallback. Ollama on GPU EC2 rejected for now | Cheapest by far at our volume: about $0.002 per plan versus a GPU instance billed by the hour. Prices from AWS's public price list are in `core/pricing.py` |
| 2026-10-25 | S3 bucket `clipora-dev-media-22e7cc` in Sydney (ap-southeast-2); the organization's guardrail denied Mumbai. Local RustFS kept for offline work | User created the bucket; CORS and lifecycle added by `infra/s3/setup-dev-bucket.sh` |
| 2026-10-25 | AWS native means managed AWS for hosting (ECS on EC2, RDS, S3, CloudFront, Cognito, Bedrock, Secrets Manager, CloudWatch). The Postgres job queue, Whisper, YOLO, FFmpeg and open embeddings stay | User confirmed: keep the Postgres queue and the open-source tools |
| 2026-10-25 | LLM provider: Amazon Bedrock through langchain-aws `ChatBedrockConverse`. Model ids from env, never hard-coded. No sampling parameters (current models reject them since 2026-10-21) | User choice (Bedrock); API change |
| 2026-10-25 | YOLO and FFmpeg run as queue workers (worker-ingest, worker-export), not as HTTP services | User confirmed |
| 2026-10-25 | Dark theme only, soft and pleasant, from antd's dark algorithm; Ant Design X agent components (Think, ThoughtChain, Attachments, Suggestion, Sources, Actions) | User request |
| 2026-10-25 | Queue package named `jobqueue` (stdlib `queue` clash); new `database`; `crop_path` moved to `core` (pure Python) so the API can recompute crops; API under `/api` | Needed to build it; settles the earlier API-imports-vision conflict |
| 2026-10-25 | The browser keeps a TypeScript port of the time mapping, checked against shared vectors in `core/vectors/timemap.json` | Preview needs the math at 60 fps; rules.md rule 5 amended |
| 2026-10-24 | Admin settings and roles: `app_settings` (global limits) plus `user_limits` (per-user overrides), `users.role` admin or editor, admin-only `/admin` API and screen. Limits: 5 active videos (deleting frees the slot), 1 GB per file, 5 GB total per user, 60 min per video | User request. Replaces the PRD non-goal "roles" and the fixed 60 min / 5 GB source limit. Admins see usage and counts, never project content |
| 2026-10-24 | `llm_usage` table: one row per LLM call with the provider's token counts and an estimated cost from a price table in `backend/packages/core/pricing.py`. Admin sees it per editor, by project, feature and day | User request ("tokens visible for each editor"). Cost stored at write time so price changes do not rewrite history |
| 2026-10-24 | Single Python backend (FastAPI plus our own queue workers), no Node BFF | One language for API, workers and agents, less to run |
| 2026-10-24 | Polling, no websockets | Simpler, cheaper, enough for status updates |
| 2026-10-24 | Background jobs use a Postgres queue (the `jobs` table, `FOR UPDATE SKIP LOCKED`, leases), replacing SQS plus Celery plus local Redis | Arav asked why not SQL. A job row can be inserted in the same transaction as the write that causes it, which closes the gap where the row exists but the message was never sent (or the reverse). The `jobs` table already existed for status, so SQS meant a second source of truth. Fewer services (no SQS, no Redis, no broker config), local dev needs only Postgres, and the per-user limit and retry state are plain SQL. Cost: workers poll, and database connections grow with worker slots |
| 2026-10-24 | ECS on EC2 instead of EKS | EKS control plane is not free-tier. ECS with two EC2 capacity groups (general, heavy) |
| 2026-10-24 | RDS Postgres with pgvector, also holds LangGraph checkpoints and the vector index | One database, no extra service |
| 2026-10-24 | The agent produces an EDL, never a rendered video. Export renders once | Human in the loop and cheap iteration |
| 2026-10-24 | YOLO runs at ingest and is cached as `detections.json` | Agent turns stay fast |
| 2026-10-24 | Crop keyframes are per segment in source time | Overlapping clips in a transition need independent crops, and trims never move them |
| 2026-10-24 | Any input and output aspect ratio, with `crop` or `fit` framing | User request |
| 2026-10-24 | 12 transition types, a default plus per-boundary overrides, changeable by the user | User request |
| 2026-10-24 | SRT cues with stable ids, shown in a transcript panel, editable, optional user-supplied SRT | User request. The SRT interpretation was my reading, confirm with Arav |
| 2026-10-24 | History sidebar lists all videos with live state, and switching never stops work | User request |
| 2026-10-24 | Ideas card after ingest (separate task `agent.suggest`), plus quick replies on questions | User request |
| 2026-10-24 | Separate metadata graph (title, thumbnail title, description, platform, hashtags, domain). No thumbnail images | User request |
| 2026-10-24 | RAG over the transcript (pgvector plus full text, RRF), and in-context learning from feedback via `agent_memories`. No model training in v1 | User request. "Reinforcement" read as learning from feedback |
| 2026-10-24 | Seeking from subtitle cues and words, through `srcToOut` and `outToSrc` | User request |
| 2026-10-24 | UI uses only Ant Design components: antd, Ant Design X, `@ant-design/icons`. Ant Design Pro not used | User request |

## 4. Rejected options

- SQS plus Celery (and Redis locally): a second store for job state, a dual-write gap, more services. Revisit only if job volume grows past what one small Postgres handles, or if sub-second job start becomes a requirement. A Redis- or SQS-backed queue could then replace `backend/packages/jobqueue` without changing handlers.
- EKS (cost). Node.js BFF (extra language). Websockets (not needed). Ant Design Pro as the template (admin console, not an editor). Fine-tuning or reward-model training in v1 (data first). A vector index (ANN) in v1 (a project has a few hundred chunks, exact scan is enough).

## 5. Assumptions I made that Arav has not confirmed

- At most 2 running jobs per user, the rest queue.
- Metadata "domain" means a category from a fixed list of about 30, plus an optional subtopic.
- "Reinforcement and AI correction" means feedback-driven learning plus self-checking, not weight training.
- An inferred preference needs 3 events across 2 projects, and a confirmation toast, before it is saved.
- Embedding model: an open multilingual model with 384 dimensions, for example multilingual-e5-small.
- Queue numbers are my starting values: 120 s lease, 30 s heartbeat, 1 s polling, 3 attempts for ingest and agent jobs, 2 for export.
- v1 limits: one main speaker, agent output 30 to 90 s, user edits 3 to 180 s. Upload limits are now admin settings (`architecture.md` section 16).
- "Attachments for conversations" means the source videos a user uploads (one per project). If it meant attaching other files or past exports to a chat, that is a new feature.
- The storage limit counts source videos only, not exports, proxies or thumbnails.
- The first admin is created with a one-off command, not through the UI. Role is stored in the database, not in Cognito groups.
- "Editor" in the token request means every normal (non-admin) user.
- Section numbers in the LLD doc: 12 is the metadata graph, 13 retrieval and learning, 14 errors and test plan.

## 6. Not verified (do a spike before relying on it)

See `architecture.md` section 17. Record the result here when done:

| Item | Result | Date |
| --- | --- | --- |
| ffmpeg `crop` per-frame x and y through `sendcmd` | Works. Frames matched static crops at the same x (34-45 dB PSNR, wrong-x control 3.25 dB); full export pans as planned. ffmpeg 7.1, macOS | 2026-10-25 |
| Every transition name in the catalogue exists in ffmpeg | All 11 xfade names render. slideleft/wipeleft enter from the right edge moving left. Preview side still to check. ffmpeg 7.1 | 2026-10-25 |
| Export length equals `core.timemap.output_duration` | Exact frame counts on 3 plans (600, 615, 630 frames). Container timestamp off by up to one frame at a concat join | 2026-10-25 |
| Preview and export agree on crop position and transition timing | not run (export side measured, preview not) | |
| Embedding model quality on real multilingual transcripts, similarity threshold | not run | |
| Platform limits for title, description, hashtags | not checked | |
| Ant Design X's required antd version | Ant Design X 2.9.0 needs antd ^6.1.1. Using antd 6.6.7 | 2026-10-25 |
| antd `Table` virtual scroll with real cue rows | not checked | |
| Timeouts, three concurrent exports on the heavy instance | not run | |
| Queue claim query with several concurrent workers: no double run, per-user limit of 2 holds under a race, lease reclaim after a killed worker | not run | |
| Total database connections (API pool plus all worker slots) against the db.t4g.micro limit, and the load from 1 s polling | not checked | |
| The chosen LLM provider returns input, output and cache token counts on every call, including structured-output retries; prices for `pricing.py` | not checked | |

## 7. Open questions for Arav

1. Default upload limits are now settings (5 videos, 1 GB per file, 5 GB total, 60 min). Confirm the defaults, and the languages Whisper must handle.
2. Bedrock model ids to use (main and small) and the monthly budget per project. Provider decided: Bedrock.
3. Cognito sign-in methods.
4. Retention of source videos and exports.
5. One caption style or several.
6. Whether users may upload their own SRT.
7. The domain list, and platform limits.
8. Whether training data from users is ever used (the PRD currently says no, which is a proposal).
9. Whether storage limits should also count exports.
10. Whether the LLD doc artifact should get the settings, roles and usage design and the implementation decisions too (only these five files have them so far).
11. Bedrock's JSON-schema structured output on the chosen model ids is unverified; the agent sends flat ops for that reason.

## 8. Glossary

- **EDL**: edit decision list. The JSON plan. Never shown to users as that name.
- **Segment**: one clip in the plan (`src_start` to `src_end` in the source).
- **Cue**: one subtitle entry (about 2 lines, up to 7 s) with a stable id `cN`.
- **Source time / output time**: time in the original video / time in the short. Convert only with `srcToOut` and `outToSrc`.
- **Proxy**: the 720p copy used by the editor (the **Full quality** button plays the original instead).
- **Candidate**: a good range the agent did not pick, kept in `meta.candidates`.
- **Interrupt**: a point where the graph stops and waits for the user (`ask_user`, `await_feedback`).
- **Idea**: a suggestion card made right after ingest.
- **Memory**: a learned preference in `agent_memories`.
- **Metadata set**: one version of title, thumbnail title, description, platform, hashtags and domain.

## 9. Where things live

- LLD: the "Clipora AI LLD" doc artifact (14 sections including errors and the test plan). It has drawings of the edit graph, the metadata graph and the retrieval and learning loop.
- Published architecture page: "Clipora AI Architecture" artifact (out of date: single project, 9:16 only, old EDL sample).
- These five files are the working summary for Claude Code. If the LLD changes, update them.

## 10. Session log

- 2026-10-27: Chat failed with "Sorry, I couldn't answer" on Groq's free tier: requests over its 8,000-token per-request cap (transcript plus an earlier transcript-sized answer in history) came back 413 and were swallowed. Chat now learns the cap from the 413, retries once with a transcript and history cut to fit, logs failures, and shows a specific message for 413/429. New `transcript` intent answers "show the transcript" and "who said what" from the transcript and speaker turns with no model call. Earlier answers are cut to 2,000 characters in history. Fixed streamed token usage being counted twice (Groq sends usage twice). Doc: `doc/computer-vision.md`.
- 2026-10-27 (later): Square plans from 16:9 sources always failed ("I could not build a plan that fits"): crop centres were rounded to 4 decimals after clamping, so an edge position (0.28125) became 0.2812 and the window left the frame by 0.00005; the validator rejected it and the LLM retries could not fix it. `core.crop._centre` now rounds inward. Regression test added.
- 2026-10-27 (quality): Editor copy 360p CRF 28 -> 720p CRF 23 (Lanczos, never above the source; new key name; `worker.backfill_proxy` rebuilds old ones). Export: Lanczos scaling, final CRF 20 -> 18, and all-cut exports without burned captions copy the segments' video (one encode). New `assets.source_url` and a **Full quality** toggle in the editor (plays the original, falls back to the copy if the browser can't play it). The size picker says how much the export enlarges the source and offers the source size.
- 2026-10-25 (restructure): Split the repo into frontend/, backend/ (services/api, services/worker, packages/*), database/ and infra/ (docker, s3, scripts). API routes are a thin HTTP layer over api/services/ (conversation, plans, exports, quotas, learning, jobs); editor components grouped by feature. Behaviour unchanged: OpenAPI spec byte-identical, 66 Python and 12 web tests pass, editor checked in Chrome. Snapshot of the old layout kept outside the repo.
- 2026-10-25: Started the code. Built core, db (13 tables, migration 0001), jobqueue, storage, the API (33 endpoints) and the worker loop; three parallel forks built the frontend, the ingest and export workers with rag, and the LangGraph agent. Ran the ffmpeg spikes. Updated architecture.md, rules.md, design.md and this file. Postgres, S3 and Bedrock paths not run.
- 2026-10-24 (latest): Added admin settings (`app_settings`, `user_limits`), roles (`users.role`), upload limits and `llm_usage` token tracking to `architecture.md` (sections 6, 8, 10, 11, 15, new 16; "not verified" moved to 17), `prd.md` (FR-30 to FR-33), `design.md` (`/admin`, upload quota), `rules.md` (hard rules 10, 15, 16; frontend rules renumbered 17 to 19) and this file. LLD doc artifact not updated. No code.
- 2026-10-24 (later): Replaced SQS plus Celery plus Redis with a Postgres job queue in the LLD (section 6 and the jobs table), `architecture.md`, `rules.md` and this file. Not run or measured yet; see section 6.
- 2026-10-24: Designed the product and wrote the LLD in one session. Added any aspect ratio, SRT cues, transitions, history sidebar, ideas, metadata graph, RAG, learning loop, subtitle seeking and the Ant Design rule. Wrote these five files. No code written.

## 11. How to update this file

- At the end of a session, change section 1, add a line to section 10, and move anything resolved out of sections 5 to 7 (into 3 or 6).
- If a decision changes, edit its row and add the date and reason, do not leave two rows that disagree.
- Never record secrets, personal data or anything about a user's videos here.
