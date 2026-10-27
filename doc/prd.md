# Clipora AI: product requirements (v1)

Owner: Arav. Status: draft agreed in design chat, 2026-10-24. If a requirement here conflicts with `architecture.md`, raise it instead of picking one.

## 1. Problem

Turning a long video (a talk, a podcast, an interview, a lesson) into good short clips takes hours of scrubbing, cutting and reframing. Tools that auto-clip give no control, and they render before you can say "not that one". Creators want a draft plan quickly, the ability to correct it, and the text they need to post it.

## 2. Product in one paragraph

Clipora AI lets a user upload a video, chat with an AI agent about what they want, and get an **edit plan** shown in a timeline editor. The user changes the plan by hand or by chat. Only when they press Export does the system render the video. The agent also suggests ideas for each video, writes the posting metadata, and learns the user's preferences over time.

## 3. Users

- Creators, marketers and educators who have long videos with a main speaker and want shorts for social platforms.
- They are not video editors. They do not know what an EDL is, and the UI never says so.

## 4. Goals and non-goals

Goals for v1:
1. From upload to a first plan in minutes, with ideas waiting when the video opens.
2. The user stays in control: every AI choice can be changed, undone or replaced.
3. Any input aspect ratio and any output aspect ratio, with captions and transitions.
4. Posting text (title, thumbnail title, description, platform, hashtags, domain) in the same product.
5. Cheap to run on a free-tier-conscious AWS setup.

Non-goals for v1:
- Rendering before the user asks. Real-time collaboration. Mobile apps.
- Multiple speakers, active-speaker detection, zoom and punch-in effects (fields exist, behaviour is fixed).
- Generating thumbnail images, posting to platforms, scheduling.
- Training or fine-tuning models.
- Teams and billing. Roles are limited to admin and editor (FR-30 to FR-33).

## 5. Limits for v1

| Limit | Value |
| --- | --- |
| Source | Any aspect ratio, one main speaker. Size and length set by an admin (defaults: 1 GB per file, 60 minutes) |
| Videos per user | Set by an admin (defaults: 5 active videos, 5 GB total). An admin can override them for one user |
| Output | 30 to 90 s from the agent, 3 to 180 s when the user edits, any aspect ratio, 30 fps |
| Segments | 1 to 30 per plan |
| Transitions | 0.1 to 2.0 s, 12 types |
| Active memories | 50 per user |
| Concurrency | At most 2 running jobs per user, the rest wait |

## 6. User stories

1. As a creator I upload a video and see it appear in my history with its progress, and I can open any other video while it processes.
2. As a creator I open a processed video and see ideas made from its content, so I do not start from a blank chat.
3. As a creator I pick an idea or type "make a 1-minute short about pricing" and get a plan I can preview at once.
4. As a creator I drag clip edges, reorder, remove clips, and add rejected moments back from a list of candidates.
5. As a creator I click a line in the transcript and the video jumps there.
6. As a creator I choose any aspect ratio, and choose whether to crop to fill or fit the whole picture.
7. As a creator I choose a transition between any two clips, with a live preview, or one for all.
8. As a creator I see and correct the subtitles that will be burned into the short, and download them as SRT.
9. As a creator I tell the agent "that clip is boring" and it replaces it and does not repeat the mistake.
10. As a creator I ask "where do I talk about hiring?" and get timecodes I can click.
11. As a creator I export and download the video, once, when I am happy.
12. As a creator I ask for a title, thumbnail title, description, platform, hashtags and domain, and edit or regenerate any of them.
13. As a creator I can see, edit and delete what the system has learned about my style, and switch learning off.

## 7. Functional requirements

### Upload and history
- FR-1 Upload large files reliably with progress, retry of failed parts, and without stopping when the user switches video.
- FR-2 Optional: the user may supply an SRT; the system then skips transcription.
- FR-3 A left history list shows every video with thumbnail, title, last change and a live state chip. States: uploading, waiting, processing (with stage and percent), finding ideas, pick an idea, planning, needs your answer, exporting, ready, failed.
- FR-4 The user can rename and delete a video. A toast announces when a background video finishes.

### Understanding the video
- FR-5 Transcribe with an open model, with word timestamps, and group into subtitle cues with stable ids.
- FR-6 Detect and track people to find the main speaker.
- FR-7 Build a small proxy, thumbnails and an audio waveform for the editor.
- FR-8 Index the transcript for search by meaning and by keyword.

### Chat, ideas and the plan
- FR-9 After ingest, post 4 to 6 ideas with a short reason, length and aspect each, plus a summary. The user can ask for more.
- FR-10 The agent asks a question only when it needs one, and offers tappable answers.
- FR-11 The agent produces an edit plan (segments, crop per segment, transitions, captions) and explains it in chat.
- FR-12 The agent applies revisions as small edits ("shorter", "swap the second clip", "make it square"), says what it changed, and offers Undo.
- FR-13 Answers about the video cite the subtitle lines they came from, and say "I could not find it" instead of guessing.
- FR-14 User edits and agent edits never overwrite each other silently. A collision shows a clear choice.

### Editor
- FR-15 A preview plays the plan without rendering, in the chosen aspect ratio, with captions and transitions.
- FR-16 A timeline with thumbnails, an audio waveform, zoom, trim handles, reorder, a source lane with rejected candidates, and a plus button that adds them.
- FR-17 Trims snap to word boundaries (hold Shift to turn snapping off). Keyboard shortcuts: Space, S, Delete, undo, redo.
- FR-18 A transcript panel in SRT timecodes: click a line or a word to seek, edit the text, search, follow playback, add selected lines to the edit, download the SRT.
- FR-19 Aspect ratio picker with presets and custom size, and a Crop or Fit switch.
- FR-20 Effect picker per boundary and for all, with duration.
- FR-21 Autosave with undo and redo, and a version history.

### Export
- FR-22 Export renders the plan once with FFmpeg, shows progress, and offers the MP4 and an SRT for download.
- FR-23 A failed export can be retried, and leaves no partial files.

### Metadata
- FR-24 On request, produce: title (with 2 alternates), thumbnail title (2 to 5 words, text only, 2 alternates), description, recommended platform (with alternates and reasons), hashtags (3 to 8), domain.
- FR-25 Every field is editable, copyable and can be regenerated with an instruction. Changing the platform rewrites the platform-bound fields. History can be restored. A banner shows when the plan changed after the text was written.
- FR-26 The text must not claim anything that is not in the video.

### Learning
- FR-27 Capture thumbs (with reasons), edits to the AI's plan, exports, chat corrections and standing instructions.
- FR-28 Turn them into short preference statements, ask once before saving an inferred one, and use the matching ones in later prompts.
- FR-29 A settings page lists, edits, disables and deletes memories, and has a switch to stop learning.

### Admin, limits and usage
- FR-30 Two roles: admin and editor. Every user is an editor unless an admin makes them an admin. Only admins can open the admin area.
- FR-31 An admin sets the application limits: active videos per user, maximum file size, total storage per user and maximum video length. An admin can override any of them for a single user. Lowering a limit never removes existing videos.
- FR-32 An editor sees their own use before uploading (for example "3 of 5 videos, 2.1 of 5 GB") and gets a plain message when an upload would go over a limit. Deleting a video frees its slot and its storage.
- FR-33 An admin sees each editor's AI usage: input and output tokens and an estimated cost, by project, by feature (plan, ideas, metadata, answers, learning) and by day, for any date range. An admin never sees another user's videos, transcripts, chats or plans.

## 8. Non-functional requirements

- Interface built only from Ant Design components (see `design.md`).
- The API stays responsive: no heavy ML imports in its image, long work runs in workers.
- Every background task is safe to run twice.
- Only the owner can read a project. All queries filter by user.
- Proposal, confirm with Arav: the user's video, transcript and memories are not used to train models.
- Costs: one small RDS that also holds the job queue (no SQS, Redis or broker), no NAT gateway, open models for speech and embeddings, LLM cost per project kept low (one transcript pass per request, small calls elsewhere). Measure and set a monthly budget per project.

## 9. Success measures

- **Keep rate:** share of agent segments still in the exported plan.
- **Edit distance** between the agent's plan and the exported plan.
- **Time** from first plan to export.
- **Thumbs ratio** per prompt version.
- Share of videos that reach an export. Failed jobs per 100 videos.

Compare them before and after any prompt change on a fixed set of 10 transcripts.

## 10. Open questions

1. Maximum video length and size for v1 (the table above is the working answer), and which languages Whisper must handle.
2. LLM provider: Amazon Bedrock (decided 2026-10-25). Still open: the monthly budget per project.
3. Cognito sign-in methods: email and password, Google, or both.
4. How long to keep source videos and exports.
5. One caption style or several in v1.
6. Whether users may upload their own SRT (the design allows it).
7. The domain list and each platform's limits for titles, descriptions and hashtags.
8. Pricing and plans, not designed yet.
9. Whether storage limits should also count exports (today they count source videos only).

## 11. Release plan (proposal, not agreed)

1. Upload, ingest, history list.
2. Plan from a chat request, preview, timeline, autosave.
3. Export with captions, then aspect ratios and transitions.
4. Ideas, retrieval and transcript seeking.
5. Metadata graph.
6. Learning loop and settings.

Roles and upload limits ship with step 1. Token logging ships with the first LLM call (step 2), and the admin usage screen can follow.
