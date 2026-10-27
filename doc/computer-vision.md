# Computer vision: people, speakers and the browser overlay

How Clipora finds the people in a video, works out which one is talking, and shows that in the editor.
It covers YOLO (people), MediaPipe (mouths), the pure logic that joins them, and how the browser draws the
result on top of the playing video. For the rest of the pipeline see [how-it-works.md](how-it-works.md);
the design decisions are in [architecture.md](architecture.md) sections 12 to 12.2.

---

## 1. The big picture

Everything heavy runs **once per video, at ingest, in the worker**. The API and the browser only read the
saved result; nothing re-runs a model when you press play.

```
                          WORKER (ingest job, after transcribe)
 ┌─────────────────────────────────────────────────────────────────────────────────────────┐
 │                                                                                         │
 │  source.mp4 ──ffmpeg 3 fps, 640px──▶ YOLOv8n + ByteTrack ──▶ detections.json             │
 │                                       "where is each person,                             │
 │                                        same person as before?"     (stage "detect")      │
 │                                                │                                         │
 │                                                ▼ person boxes + track ids                 │
 │  source.mp4 ──ffmpeg 12 fps, 720px──▶ head crop per box ──▶ MediaPipe face landmarker    │
 │                                                              jawOpen 0..1 per person     │
 │  source.mp4 ──ffmpeg 16 kHz mono────▶ loudness (RMS) per sample                          │
 │                                                │                                         │
 │                                                ▼ LipFrame(t, loud, {track: mouth})        │
 │  transcript words ─────────────────▶ core.speakers.speaker_turns  (pure Python)          │
 │                                       "whose mouth moves with the sound?"                │
 │                                                │                                         │
 │                                                ▼                                         │
 │                                     detections-speakers.json   (stage "speakers")        │
 │                                     = detections + turns[]                               │
 └─────────────────────────────────────────────────────────────────────────────────────────┘
                                                  │ object storage (S3 / MinIO)
                    ┌─────────────────────────────┼─────────────────────────────┐
                    ▼                             ▼                             ▼
     AGENT / API: core.crop.crop_path    API: GET /projects/{id}/speaker   CHAT: "who is speaking?"
     9:16 crop keyframes in the EDL      boxes + turns + "Person N"        speaker_reply()
                    │                             │
                    ▼                             ▼
     EXPORT: ffmpeg crop follows         BROWSER: canvas overlay (green/red boxes)
     the same keyframes                  + Speakers lane on the timeline
```

| Step | Where | Model | Runs | Output |
|---|---|---|---|---|
| Find and track people | `backend/packages/vision/src/vision/__init__.py` | YOLOv8n + ByteTrack | 3 frames/s | `detections.json` |
| Read mouths | `backend/packages/vision/src/vision/lips.py` | MediaPipe face landmarker | 12 frames/s | `LipFrame` list (in memory) |
| Decide who talks | `backend/packages/core/src/core/speakers.py` | none (statistics) | once | `SpeakerTurn` list |
| Glue for the stage | `backend/services/worker/src/worker/speakers.py` | none | once | `detections-speakers.json` |
| Follow the talker | `backend/packages/core/src/core/crop.py` | none | each plan/edit | crop keyframes in the EDL |
| Serve to the UI | `backend/services/api/src/api/services/speaker.py` | none | each request | `SpeakerOut` JSON |
| Draw in the browser | `frontend/src/components/editor/player/SpeakerOverlay.tsx`, `timeline/SpeakerLane.tsx`, `lib/speakers.ts` | none | every animation frame | boxes and lane |

---

## 2. Why two models

They answer different questions, and the speaker feature needs both answers.

| | YOLO + ByteTrack | MediaPipe face landmarker |
|---|---|---|
| Question | *Where* is each person, and is this the *same* person as a moment ago? | Is *this* face's mouth open right now? |
| Sees | The whole frame | Only one enlarged head crop at a time |
| Gives | Body box + a stable `track_id` | 478 face landmarks + 52 blendshapes; we read `jawOpen` |
| Strong at | Small, turned or partly hidden people; identity over time | Fine detail of the mouth |
| Can't do | Anything about lips | Remember who a face belongs to between frames; find tiny faces in a big frame |
| Also used for | The 9:16 crop (where to point the frame) | Nothing else |

So YOLO says **"Person 2 is here"**, and MediaPipe, run inside Person 2's head region, says **"their mouth is
62 % open"**. Every mouth reading is tied to a YOLO track id, which is what lets us say *who* is talking.

Alternatives that were tried and dropped (see `doc/memory.md`): OpenCV YuNet mouth corners (no usable signal)
and matching the transcript alone (fooled by cutaways).

---

## 3. YOLO: finding and tracking people (stage `detect`)

File: `backend/packages/vision/src/vision/__init__.py` · called from `Ingest.detect` in
`backend/services/worker/src/worker/handlers/ingest.py`.

```
ffmpeg ─▶ raw BGR frames on stdout ─▶ numpy array ─▶ model.track(...) ─▶ boxes_from_result ─▶ DetectionFrame
 (3 fps, short side 640px)           (h, w, 3)       YOLOv8n + ByteTrack   class 0 only,        t, [Box...]
                                                      persist=True          coords as 0..1
```

1. **Frames.** `media.commands.detect_frames` builds an ffmpeg command that decodes the source at
   `DETECT_FPS = 3` frames per second, scaled so the short side is `DETECT_SHORT_SIDE = 640` px, and writes raw
   BGR pixels to stdout. The worker reads exactly `w * h * 3` bytes per frame: no images are written to disk.
2. **Detection.** `YOLOv8n` (the nano model, `YOLO_MODEL` env to change it) looks for class 0 (person) with
   confidence ≥ `CONFIDENCE = 0.35`.
3. **Tracking.** `model.track(..., persist=True, tracker="bytetrack.yaml")` runs ByteTrack, which matches each
   new box to the boxes of earlier frames (by position and motion) and keeps the same `track_id` for the same
   person. That id is the identity used everywhere else.
4. **Normalising.** `boxes_from_result` keeps person boxes only and stores coordinates as fractions of the frame
   (`x1, y1, x2, y2` in 0..1), so they work at any display size.
5. **Reset.** The model is cached per worker process; `_reset_tracker` clears ByteTrack's memory after each
   video so ids don't leak between videos.

Saved as `detections.json` (`core.crop.Detections`):

```json
{
  "fps": 3,
  "frames": [
    { "t": 12.333, "boxes": [
      { "track_id": 1, "x1": 0.12, "y1": 0.08, "x2": 0.41, "y2": 0.97, "conf": 0.91 },
      { "track_id": 4, "x1": 0.58, "y1": 0.15, "x2": 0.86, "y2": 0.99, "conf": 0.84 }
    ]}
  ],
  "turns": []
}
```

Cost: about 20 % of ingest time (`WEIGHTS["detect"] = 20`).

---

## 4. MediaPipe: reading mouths (stage `speakers`, part 1)

File: `backend/packages/vision/src/vision/lips.py`, function `measure_lips`.

```
for each frame at 12 fps (short side 720px):
    nearest YOLO frame (≤ 0.5 s away) ──▶ its person boxes
    for each box:
        head_region(box)            top part of the body box, a square about the head; skip if < 40 px
        crop + resize to 256 px     enlarged so small faces become readable
        MediaPipe FaceLandmarker    num_faces=1, blendshapes on
        jawOpen score               0 = closed .. 1 = wide open
    loudness(pcm, t + 0.08 s)       RMS of the audio around that moment
    ──▶ LipFrame(t, loud, {track_id: jawOpen, ...})
```

Details and why:

- **12 fps, 720 px.** Syllables come 4–6 times a second, so 12 samples a second see the mouth open and close.
  The frames are larger than YOLO's 640 px because lips need detail (`LIP_FPS`, `LIP_SHORT_SIDE`).
- **Boxes from YOLO.** YOLO only ran at 3 fps, so each lip frame borrows the boxes of the nearest detection
  frame, up to `MAX_BOX_GAP_S = 0.5` s away. That is how a mouth reading gets its `track_id`.
- **Head region.** `head_region` takes the top of the person box (about 55 % of its height at most) as a
  square, with a little margin. Heads smaller than `MIN_HEAD_PX = 40` px are skipped: their scores are noise.
- **Enlarge to 256 px.** `HEAD_SIDE = 256`: the face landmarker works much better on a face that fills the image.
- **jawOpen.** The landmarker returns blendshapes (named face-muscle scores). `jawOpen` is the one that follows
  speech. If no face is found (back of the head, hand over the mouth) that person has no sample for that frame.
- **Loudness.** The audio is decoded once to 16 kHz mono (`worker/speakers.py:_pcm`). `loudness` is the RMS
  of the samples around `t + SOUND_LAG_S` (80 ms): the mouth opens slightly before the sound is heard.
- **Model file.** `face_landmarker.task` comes from `FACE_MODEL` or is downloaded once to
  `~/.cache/clipora/` and checked against a fixed sha256.

---

## 5. Deciding who talks (stage `speakers`, part 2)

File: `backend/packages/core/src/core/speakers.py`, function `speaker_turns`. Pure Python, no models, so it
is fast and fully unit-tested (`core/tests/test_speakers.py`).

**Idea:** while someone speaks, the talker's jaw opens and closes in time with the loudness of the voice. A
listener's jaw doesn't. So for each person we measure how well their mouth follows the sound.

```
transcript words ──speech_runs──▶ stretches of speech (words < 0.35 s apart join)
                                        │
                                  _windows: cut into ~3 s windows
                                        │
            for each window: for each person with ≥ 12 mouth samples in it
                correlation(mouth series, loudness series)   ← Pearson, -1..1
                                        │
                                     _judge
                                        │
          ┌─────────────────────────────┼──────────────────────────────┐
     nobody ≥ 0.3                one person ≥ 0.3 and           several ≥ 0.3
     → no speaker                ≥ 0.1 ahead of the next        → same person shown twice
       (voice-over, off-screen)  → that person                     (monitor, PiP): the largest
                                                                    box wins, confidence halved
                                        │
          fill one unclear window between two windows of the same person
                                        │
          join neighbouring windows of the same person (gap ≤ 1.5 s) into turns
                                        ▼
               SpeakerTurn(start, end, track_id, confidence)
```

| Constant | Value | Meaning |
|---|---|---|
| `WINDOW_S` | 3.0 | Speech is judged in windows of about 3 s |
| `MIN_SAMPLES` | 12 | A person needs a third of the window's samples to be judged |
| `SYNC_MIN` | 0.3 | Correlation that counts as talking (a listener is about 0) |
| `MARGIN` | 0.1 | How far ahead of the next person the talker must be |
| `WORD_GAP_S` | 0.35 | Words closer than this are one stretch of speech |
| `JOIN_GAP_S` | 1.5 | Turns of the same person closer than this merge |

The worker glue (`backend/services/worker/src/worker/speakers.py:with_turns`) skips the whole step when there
are no people, no words or no audio, and saves the detections plus `turns` under a **new** key,
`detections-speakers.json`. The old key is never overwritten, because the API and agent cache detections by
key. The ingest job's `stage_markers["speakers"].key` points at it, and every reader prefers that over
`stage_markers["detect"].key`.

Videos ingested before this stage existed can be updated with `python -m worker.backfill_speakers [ids]`.

---

## 6. Using the turns: the crop follows the talker

File: `backend/packages/core/src/core/crop.py`. Called by the agent when it plans
(`agent/planning.py`, `agent/crops.py`) and by the API when you edit (`api/services/plans.py`).

```
turns ──focus_spans──▶ [(start, end, who), ...] for the segment
                         · before any turn: the main person (largest box area × confidence)
                         · a turn shorter than CUT_MIN_S = 1.5 s doesn't move the frame (a quick "yeah")
                         · otherwise cut to the new talker and hold on them until the next one
         for each span:
           who's box centre per YOLO frame (lost < 1 s: hold; longer: centre)
           ─▶ median of 5 ─▶ EMA 0.2 ─▶ dead zone 0.04 ─▶ max speed 0.15 frame/s
           ─▶ CropKeyframe(t, cx, cy) every 0.5 s
```

The keyframes are stored in the EDL. The preview (`frontend/src/lib/crop.ts:interpolateCrop`) and the export
(`backend/packages/media/src/media/export.py:crop_positions`, an ffmpeg crop with per-time commands)
interpolate the **same** keyframes, so what you see in the editor is what gets exported.

---

## 7. From the server to the browser

**API.** `GET /api/projects/{project_id}/speaker` (`api/routes/plans.py` →
`api/services/speaker.py:speaker_for`):

1. Finds the detections key from the ingest markers (speakers key first).
2. Loads the detections (cached by key).
3. Works out the main person for the current plan's segments.
4. Labels each talking track "Person 1", "Person 2"… in the order they first talk.

```json
{
  "available": true, "analysed": true, "fps": 3,
  "main_track_id": 1, "people": 2, "coverage": 0.93, "cut_min_s": 1.5,
  "turns":  [ { "start": 4.2, "end": 11.8, "track_id": 1, "label": "Person 1", "confidence": 0.71 }, ... ],
  "frames": [ { "t": 12.333, "boxes": [ { "track_id": 1, "x1": 0.12, "y1": 0.08, "x2": 0.41, "y2": 0.97 } ] }, ... ]
}
```

**Fetching.** `useSpeaker` in `frontend/src/api/hooks.ts` (TanStack Query):

- It only fetches while the **Speaker** button is on and the project is ready.
- The query key includes the plan version.
- `staleTime: Infinity`: detections never change, so it is fetched once per version.

---

## 8. How the browser identifies the speaker on the video

The browser does **no** vision. It has the whole timeline of boxes and turns in memory and, on every
animation frame, looks up "what is on screen right now" by time.

```
   requestAnimationFrame loop (SpeakerOverlay.tsx)
   ───────────────────────────────────────────────
   1. view()            ← Preview.tsx / SourcePlayer.tsx
        srcTime = <video>.currentTime      (source time, not edit time)
        window  = visible part of the source (the 9:16 crop at this moment), or null = whole frame
   2. nearestFrame(frames, srcTime)        binary search; nothing if > 0.5 s away (no stale boxes)
   3. focusTrack(speaker, srcTime)         who the crop follows now  (lib/speakers.ts)
      talkingAt(speaker, srcTime)          the turn covering now, if any
   4. map box (0..1 of source) → canvas pixels
        crop on:  x' = (x - window.left) / window.w × canvasWidth   (same for y)
        no crop:  letterbox fit of the whole frame inside the canvas
   5. draw each box
        track_id == focus  → green, thick  ("Person N · talking" if they are in a turn)
        everyone else      → red, thin     ("Person N")
```

Step by step:

1. **Which moment.** The overlay asks the player for a `SpeakerView` (`Preview.tsx:speakerView`). It reads
   `currentTime` straight from the `<video>` element that is showing. Because the editor plays the *source* file
   and jumps between segments, that time is already in source seconds, the same clock as the detections and
   turns. No conversion is needed.
2. **Which boxes.** `nearestFrame` binary-searches the detection frames (3 per second) for the closest one.
   More than 0.5 s away means none is close enough, and nothing is drawn rather than a box in the wrong place.
3. **Who is talking.**
   - `talkingAt` binary-searches the turns for the one covering `srcTime`: that person is **talking**.
   - `focusTrack` replays the same rule as `core.crop.focus_spans`: start with the main person, and switch to
     each turn's person once their turn is at least `cut_min_s` long. That person is who the **crop follows**.
   - The two usually agree; they differ during a short interjection (talking, but not long enough to cut to).
   - Because the browser uses the same rule and the same numbers as the backend, the green box is exactly what
     the exported 9:16 video follows.
4. **Where on screen.** Boxes are fractions of the *source* frame, but the preview may show only a 9:16 slice
   of it. The player gives the visible window (from `interpolateCrop` of the segment's keyframes at that time),
   and the overlay maps each box through it. Boxes outside the slice simply fall off the canvas. In source mode
   or "fit", the whole frame is letterboxed and boxes are mapped with the same contain fit.
5. **Drawing.**
   - A `<canvas>` sits on top of the video (`position: absolute`, `pointer-events: none`), sized for the
     device pixel ratio so lines stay sharp.
   - It is redrawn every animation frame, so the boxes move with playback and seeking without React re-renders.
   - It is display only: the export never draws it.

**Speakers lane** (`frontend/src/components/editor/timeline/SpeakerLane.tsx`):

- It shows the same turns as coloured bars under the timeline: one colour per person (`personIndex`), with
  opacity rising with confidence.
- Hover a bar for "Person N · 0:04–0:11"; click it to seek there.
- A red marker shows the current source time.
- It is shown in both the edit timeline and the source timeline while the Speaker button is on.

**Chat.** Questions like "who is speaking at 1:20?" match the `_SPEAKER` pattern (`agent/conversation.py`)
and are answered from the same turns by `speaker_reply` in `backend/services/chat/src/chat/turn.py`, with no
LLM guesswork about the video.

---

## 9. Time and coordinate conventions

| Thing | Unit | Notes |
|---|---|---|
| Detection `t`, turn `start/end`, crop keyframe `t` | Source seconds | Never edit/output time |
| Box `x1..y2`, crop `cx, cy` | Fraction 0..1 of the source frame | Resolution independent |
| Track id | Integer from ByteTrack | Per video; the UI turns it into "Person N" |
| Edit time ↔ source time | `frontend/src/lib/timemap.ts` | Used for seeking from the lane in the edit timeline |

---

## 10. Limits and how to tune

- **Id changes at camera cuts.** ByteTrack matches by position and motion, so after a hard cut the same person
  can get a new id and appear as a second "Person". A face-embedding re-identification step would fix this.
- **Monitor or photo copies.** A person shown on a screen behind the speaker moves their mouth in sync too. The
  largest box wins, but a big monitor can still be picked.
- **Small or turned faces.** Heads under 40 px or in profile give no reading; that speech has no turn, and the
  crop holds on the last talker.
- **Voice-over or off-screen speaker.** No mouth follows the sound, so there is no turn. This is intended.
- **Speed.** CPU only: YOLOv8n at 3 fps and the face landmarker at 12 fps × people. A GPU or a larger
  `DETECT_FPS` trade cost for accuracy.
- **Tuning.** All thresholds are module constants (sections 3–6). Change them, then re-run
  `python -m worker.backfill_speakers <project ids>` to recompute turns without a full re-ingest.

---

## 11. Code map

| Concern | File |
|---|---|
| ffmpeg frame decoding (3 fps / 12 fps) | `backend/packages/media/src/media/commands.py` (`detect_frames`) |
| YOLO + ByteTrack | `backend/packages/vision/src/vision/__init__.py` (`detect_people`) |
| Head crop + MediaPipe + loudness | `backend/packages/vision/src/vision/lips.py` (`measure_lips`) |
| Turns from mouths and sound | `backend/packages/core/src/core/speakers.py` (`speaker_turns`, `speaker_at`) |
| Ingest stages `detect`, `speakers` | `backend/services/worker/src/worker/handlers/ingest.py` |
| Stage glue, new storage key | `backend/services/worker/src/worker/speakers.py` (`with_turns`) |
| Backfill old videos | `backend/services/worker/src/worker/backfill_speakers.py` |
| Crop that follows the talker | `backend/packages/core/src/core/crop.py` (`focus_spans`, `crop_path`) |
| Crop in the export | `backend/packages/media/src/media/export.py` (`crop_positions`) |
| Speaker endpoint | `backend/services/api/src/api/services/speaker.py`, `api/routes/plans.py` |
| Chat answers | `backend/services/chat/src/chat/turn.py` (`speaker_reply`) |
| Fetch hook | `frontend/src/api/hooks.ts` (`useSpeaker`) |
| Time lookups | `frontend/src/lib/speakers.ts` (`talkingAt`, `focusTrack`, `personLabels`) |
| Crop interpolation in the preview | `frontend/src/lib/crop.ts` (`interpolateCrop`) |
| Boxes over the video | `frontend/src/components/editor/player/SpeakerOverlay.tsx` |
| Visible window for the overlay | `frontend/src/components/editor/player/Preview.tsx` (`speakerView`) |
| Speakers lane | `frontend/src/components/editor/timeline/SpeakerLane.tsx` |
