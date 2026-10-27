# Clipora AI: rules for Claude Code

Read in this order before changing anything: `rules.md` (this file), `architecture.md`, `prd.md`, `design.md`, then `memory.md` for the current status. When these files disagree with the code or with each other, stop and ask Arav which is right. Do not guess, and do not silently edit one to match the other.

## 1. How to work

- Make the smallest change that does the job. Do not refactor, rename or reformat code you were not asked to touch.
- Plan first for anything that touches the EDL, the database, the graphs or the AWS setup. Say what you will change and why, then do it.
- Ask before you decide when a choice cannot be undone, changes a contract (EDL, API, database), adds a dependency, or spends money. Otherwise decide, and say what you decided.
- Never claim something works that you did not run. Say what you ran and what it showed. Items listed as "not verified" in `architecture.md` section 17 stay unverified until a spike proves them. When you finish a spike, record the result in `memory.md`.
- Do not invent numbers, limits, model names, API fields or library behaviour. If unsure, look it up or say you are unsure.
- Keep changes small enough to review in one sitting. One concern per commit.

## 2. Hard rules (never break)

Product and architecture:
1. **Never render video outside the export worker.** The agent produces a plan only.
2. **The EDL is the contract.** Change `backend/packages/core` first, then the API, the agent, the preview and the export, with tests, in the same change.
3. **The LLM never writes a whole EDL during a revision.** It returns edit ops and code applies them. Same for metadata field ops.
4. **Code snaps timestamps.** Treat LLM timestamps as hints: snap to the nearest word or sentence boundary within 0.5 s, reject what does not snap.
5. **Crop and transition math exists once, in `backend/packages/core`.** The export and the API use it directly. The only allowed copy is the TypeScript port of the time mapping and keyframe interpolation in `frontend`, and it must pass the shared vectors in `backend/packages/core/src/core/vectors/timemap.json`. Change both, and the vectors, in the same commit.
6. **No websockets.** Background work goes through the Postgres `jobs` queue and the browser polls for status. The one streaming path is Server-Sent Events from the chat service for non-edit replies; edits never stream. Do not add SQS, Celery, Redis or another broker without asking Arav.
7. **The API image stays small.** `backend/services/api` imports only `core`, `db`, `jobqueue` and `storage`. No media, vision, rag, agent, torch or embedding imports there.
8. **Every job handler is idempotent** and records stage markers. Assume it will run twice, because a lost lease makes another worker claim the job. Create the job row in the same transaction as the write that causes it (`backend/packages/jobqueue` `enqueue(tx, ...)`), never after the commit. Claim jobs only with the statement in `architecture.md` section 9, and keep the lease renewed while working.
9. **Every EDL and metadata write uses a version check** (`If-Match`). A 409 is handled, never overridden. Never use force.
10. **Every database query filters by `user_id`** (directly or through the project). The only exception is `backend/services/api/src/api/admin/`, behind `require_admin`, which may read account fields, limits and usage across users, never project content (`architecture.md` section 16).
11. **Transcripts, user messages, memories and retrieved text are data, never instructions.** Put them in delimited blocks and say so in the prompt.
12. **A memory never overrides the user's latest message or any rule here.**
13. **Thumbnail title is text.** Never generate or edit thumbnail images in v1. Never post to a platform.
14. **No secrets in the repo, logs or prompts.** Use environment configuration and AWS roles.
15. **Every LLM call goes through the one client wrapper** that records `llm_usage`. Never call the provider SDK directly. Token counts come from the provider's response, never estimated.
16. **Limits come from settings, not constants.** Read them through the effective-limit helper (user override, else `app_settings`, else the default in `backend/packages/core/settings.py`).

Frontend:
17. **Only Ant Design components.** antd, Ant Design X, `@ant-design/icons`. No other component library and no CSS framework. Colors and spacing come from antd tokens. Exceptions are only the ones listed in `design.md` section 9.
18. Plain language in the UI. No internal terms.
19. Every component handles loading, empty, error and long-running states.

## 3. Conventions

General:
- Python for `backend/services/api`, `backend/services/worker` and `packages/*`. TypeScript for `frontend`. Versions are pinned in the repo's Docker and lock files, not here.
- Types everywhere: Pydantic models in `backend/packages/core` are the source of truth. Frontend types are generated from the API's OpenAPI file, never written by hand.
- IDs: UUIDv7 for projects, jobs, messages, exports. Segments use short ids (`s1`, `s2`) unique inside one EDL. Cues use `c1`, `c2` and never change after ingest.
- Time: media times are floats in seconds. Source time and output time are different clocks: name variables `src_*` and `t_out`, and convert only through `srcToOut` and `outToSrc`.
- Errors use `{ "error": { "code", "message", "details" } }`. Add new codes to `backend/packages/core` and to `architecture.md`.
- Logging: structured, with `project_id`, `job_id`, `user_id`. Never log transcripts, video paths with user names, tokens or memory text.
- Config comes from environment variables, validated at startup.

Python:
- Small functions, explicit types, no hidden global state. Vision and crop functions are pure and take plain data.
- Do not catch broad exceptions to hide them. A task either finishes, retries or fails with an error code.
- LLM calls: structured output with a Pydantic model, no sampling parameters (current models reject them), 60 s timeout, two retries with the validation error appended, prompt and response saved. Each agent EDL version records `meta.run = { prompt_version, model }`.

FFmpeg:
- Build commands from a function that returns an argument list. Never assemble a shell string from user text.
- Same size, frame rate and timebase for every segment.
- Golden-file tests for generated commands. Do not change a filter without updating them.

Frontend:
- Server state in TanStack Query, edit state in Zustand with immer patches, kept per project id. Autosave debounces 800 ms.
- Polling intervals are in section 8 of `architecture.md`. Do not add others.
- Custom parts (video layers, waveform canvas, timeline blocks) use tokens and pointer events from our own hook.

Database:
- Schema changes go through migrations, never by hand. The only exception is LangGraph's checkpoint tables, which its `PostgresSaver.setup()` owns. `edl_versions` and `metadata_sets` are append only. `projects.current_version` changes in the same transaction as the new version.

## 4. Testing and definition of done

A change is done when:
- It has tests at the right level: unit (EDL validation, edit-op applier, crop path and window math for several aspect ratios, transition offset math, SRT cue grouping and parsing, idea anchor resolution, metadata field checks, chunking and rank fusion, user-edit diff, output and source time mapping), golden files (ffmpeg commands and `captions.ass`), integration (Docker Compose with local S3 and Postgres: upload, ingest, agent turn, export on a 60 s sample), queue tests (two or more workers on one table: no job runs twice at once, the limit of 2 running jobs per user holds, a killed worker's job is claimed again after the lease, retry and backoff), parity (crop position and transition timing in preview and in a rendered clip for vertical, square and wide output), retrieval (about 20 questions with known answer times on 3 transcripts, top 8 must contain the right chunk, an unanswerable question returns nothing).
- Lint, type checks and tests pass, and you have shown the output.
- `architecture.md`, `prd.md` or `design.md` is updated if the behaviour they describe changed.
- `memory.md` has a new entry if you made or learned something a future session needs.

## 5. Security and privacy

- Block all public bucket access. Presigned URLs are scoped to `uploads/{user}/{project}/`. Assets by signed cookies.
- Validate file type and size at the API. Reject files with no video or no audio.
- Memories store editing and posting preferences only, in fixed kinds. Never store personal details. Respect `learn_enabled`: when off, log nothing and use no memory.
- Deleting a project is a soft delete first. Do not purge storage in the same request.

## 6. Git and communication

- Small commits with a clear message. Never commit directly to the default branch. Do not push or open a pull request unless Arav asks.
- Do not delete files, tables or data unless asked. If asked, say exactly what will be removed first.
- Report briefly: what changed, what you ran, what is still unverified, and any decision you made that Arav should know about.
