# Clipora AI: UI and UX design

Applies to everything in `frontend`. The one hard rule: **every visible component comes from Ant Design** (antd v6 at the time of writing, Ant Design X v2 for the AI parts, `@ant-design/icons` for icons). Confirm versions when installing, and check which antd version Ant Design X requires.

## 1. Rules

1. No other component library (Material UI, Radix, shadcn, Chakra, Headless UI and so on) and no CSS framework such as Tailwind.
2. Non-visual libraries are fine: TanStack Query, Zustand, immer.
3. Colors, spacing, radius and font sizes come from antd design tokens (`theme.useToken`). No hard-coded hex values in components.
4. If antd lacks something, build it from antd parts. If that is impossible, add it to the exceptions list in section 9 first, with the reason, then build it.
5. Keep antd screens in client components. Where a server component needs a dot-notation part such as `Select.Option`, import it from its own path, as antd's Next.js guide says.
6. One `XProvider` at the root holds the theme. **Dark theme only** (user decision 2026-10-25). Since 2026-10-27 (user decision) it looks like an IDE: VS Code's layout conventions (compact 13 px type, small radii, flat panels, Explorer-style section headers, a status bar) in GitHub's dark colours (Primer dark: `#0d1117` canvas, `#010409` side bar and status bar, `#1f6feb` accent). The agent has its own purple mark. No light mode and no theme switch. Seed and alias tokens, plus the few workbench colours antd has no token for (`ideColors`), live in `frontend/src/theme.ts`, the only place colour values are written.
7. Plain language in the UI. Never show "EDL", "SQS", "job" or "checkpoint" to a user. Say "plan", "waiting", "working on it".

## 2. Starting point

- Next.js App Router project from `create-next-app`.
- antd's `with-nextjs-app-router-inline-style` example for the style setup, and `@ant-design/nextjs-registry` in `app/layout.tsx` so the first screen is styled.
- The "full chat page" pattern from Ant Design X for the shell: `XProvider`, `Conversations`, `Welcome` with `Prompts`, `Bubble.List`, `Sender`. Also use X's agent components where they fit: `ThoughtChain` or `Think` for agent progress, `Attachments` for the optional SRT, `Suggestion` for slash commands in `Sender` (for example /shorter, /square), `Sources` for citations, `Actions` for thumbs and copy.
- Ant Design Pro is not used: it is an admin-console template (ProLayout, ProTable, ProForm). Check before ruling it out for anything else.

## 3. Screens

| Route | What it is |
| --- | --- |
| `/projects` | Empty state and the upload card for a new video. The video list is the sidebar. |
| `/projects/[id]` | The editor for one video |
| `/settings` | What Clipora has learned, and the learning switch |
| `/admin` | Admins only: limits, users and AI usage. Hidden from editors, and the API returns 403 if they open it |

Every page uses the same shell: a collapsible history sidebar on the left (a drawer on narrow screens) and the page on the right. Admins also see an Admin link at the bottom of the sidebar.

## 4. Editor layout

Use `Splitter` so panes can be resized.

```
+-----------+----------------------------------------------------------+
| History   |  Chat | Metadata (Tabs)     |  Preview                   |
| sidebar   |                             |  (output aspect ratio)     |
|           |                             +-----------------------------+
|           |                             |  Transcript panel           |
|           +-----------------------------+-----------------------------+
|           |  Timeline: ruler, waveform, edit lane, source lane       |
+-----------+----------------------------------------------------------+
```

The export action is a primary button in the editor header, opening a `Drawer`.

### Whole-video convert and YouTube import (2026-10-27)

- Before any plan exists, a bar above the player offers **Convert the whole video**: a shape (`Segmented`: 9:16, 4:5, 1:1) and **Convert** or **Convert & export** (opens the export drawer once the plan loads). The result is an edited video titled "Full video · 9:16" with the speaker-following crop.
- The projects page has an **Import from YouTube** card (`Input` with a link icon, `Button`) above the upload box. The server fetches the video; the first stage reads "Fetching from YouTube".

### Agent presence (2026-10-27)

- **Status bar** (`editor/StatusBar.tsx`) at the bottom of the editor: the agent's state on the left ("Clipora agent: Finding the right moments (2/6) · 25%", turns blue while it works; click opens the Agent tab), then plan version and clip count; output shape, save state and export state on the right.
- **Chat tab**: a header with the agent's mark and a status badge, avatars and a name on replies, the whole step plan while it works (done, running, pending), and a `Welcome` with starter tasks on an empty chat.
- One source of truth for the agent's state: `frontend/src/lib/agent.ts` (`agentStatus`, `AGENT_STEPS`).

## 5. Component map

| Need | Component |
| --- | --- |
| Page shell and panes | `Layout`, `Splitter`, `Tabs` (Chat, Metadata) |
| History sidebar | `Conversations` (date groups, New video action, menu for rename and delete). State chip is `Tag` plus `Progress`. |
| Chat thread and input | `Bubble.List`, `Sender` |
| Idea cards and quick replies | `Welcome`, `Prompts` |
| Agent progress in the thread | `ThoughtChain` |
| Thumbs, copy, retry on a reply | `Actions`, `Actions.Feedback`, `Actions.Copy` |
| Reason chips after a thumbs down | `Tag.CheckableTag` inside a `Popover` |
| Citations from the video | `Sources` under the reply, each source is a cue that seeks the video |
| Upload card | `Upload.Dragger` with a `customRequest` that calls the multipart upload manager |
| Aspect, framing, tone, platform | `Select`, `Segmented`, `InputNumber` |
| Zoom, transition duration | `Slider` |
| Effect picker | `Popover` holding a grid of `Card` |
| Timeline controls | `Tooltip` on trim handles and badges, `Dropdown` for the segment menu |
| Metadata form | `Form`, `Input`, `Input.TextArea`, `Select`, closable `Tag` for hashtags |
| Export panel, candidates drawer | `Drawer`, `Progress`, `Button` |
| Settings | `List`, `Switch` |
| Upload quota on the upload card | `Progress` (videos and storage), `Alert` when a limit is reached |
| Admin area | `Tabs` (Limits, Users, Usage), `Form` with `InputNumber`, `Table`, `Statistic`, `DatePicker.RangePicker`, `Drawer` for one user, `Popconfirm` for role changes |
| Toasts | `App` with `message` and `notification` |
| Confirmations | `Modal.confirm`, `Popconfirm` |
| Loading, empty, error | `Skeleton`, `Spin`, `Empty`, `Result`, `Alert` |
| Icons | `@ant-design/icons` |
| Transcript list | `Table` with the `virtual` prop, hidden header, fixed row height; `Input.Search`; `Typography`. Confirm virtual scroll with the real row height. |

## 6. Behaviour

### History sidebar
- Rows: thumbnail, title, relative time, state chip. Groups: Today, Yesterday, Previous 7 days, Older. The open video is highlighted. 30 per page, next page on scroll.
- State chip text:

| State | Chip |
| --- | --- |
| uploading | Uploading, with percent |
| queued | Waiting for a worker |
| ingest running | Stage and percent, such as Transcribing 40% |
| ideas running | Finding ideas |
| ready, ideas posted, no plan | Pick an idea (with a dot) |
| agent running | Planning your edit |
| waiting for the user | Needs your answer (with a dot) |
| export running | Exporting, with percent |
| ready with a plan | Ready, or Exported |
| failed | Failed, with Retry in the editor |

- Switching videos never stops work. Unsaved edits are saved first. An upload continues from the layout-level manager. Closing the tab stops an upload, and the row then says Upload interrupted with a Delete action.
- A toast with an Open button appears when a background video finishes.
- Menu: Rename, Delete (confirm). A filter box narrows the loaded rows by title.

### Editor tabs and speaker overlay
- Left tabs: Chat, Edited videos (n), Transcript, Title & hashtags. The right pane is only the player, at full height.
- Edited videos: one card per generated video (thumbnail, title, length, aspect, clips, edit count). The one being edited is highlighted and tagged Editing; click any other card (or its Edit this video button) to switch. Clips fold out; clicking a clip switches to that video if needed and jumps to the clip. Switching always stops playback first.
- Speaker button above the player toggles the YOLO boxes (green speaker, red others). Asking "who is speaking" in the chat turns it on. Never exported.
- Timeline fits the full width by default; zoom goes up to 20x with a thin horizontal scrollbar; Fit to width returns.

### Chat
- A pinned "Generated videos" block (Ant Design X `Think`) sits above the thread: one entry per generated video, with its clips (start to end in m:ss) as children. The one being edited is tagged Editing; Open in editor makes another one current (saved as a new version).
- Each AI plan reply is shown as the same `Think` block (video icon, title, length, aspect, clip count, clips as children), not as plain text.
- First thing on a new video: a card with the summary, topics and 4 to 6 idea cards (title, one-line reason, length, aspect badge). Hovering an idea outlines its moments on the source lane. "Make this" starts it. Below: a free-text box ("Or tell me what you want") and More ideas.
- After a plan exists, clicking an old idea asks "Replace the current plan?". The result can be undone from version history.
- Questions from the agent show tappable options. Free text is always allowed.
- Every agent reply that changes the plan says what changed and has Undo. Thumbs up and down sit under plan replies, idea cards and metadata sets. Thumbs down asks for an optional reason.
- Answers about the video show citations as chips. Clicking one seeks the video.

### Preview
- Two video layers inside a box with the output aspect ratio. Captions are drawn over the video with the export's style. Transitions animate during playback. Types the browser cannot match exactly are labelled "approximate preview".
- A "Playing the source" bar with Back to edit appears when the user seeks to a line outside the edit.

### Timeline
- Rows: ruler, waveform, edit lane (segment blocks with thumbnails), source lane (chosen ranges filled, rejected candidates dashed). The playhead spans all lanes.
- A small diamond on every boundary between segments is the transition badge. Click opens the effect picker. A cut shows as a thin line.
- Zoom 2 to 200 px per second (slider, or Ctrl and scroll). Trims snap to the nearest word boundary within 0.15 s, Shift turns snapping off.
- Plus button opens a drawer of rejected candidates, each with a preview. Dragging a dashed range into the edit lane adds it.
- Keyboard: Space play and pause, S split at the playhead, Delete remove the selected segment, Cmd or Ctrl plus Z and Shift plus Z undo and redo.

### Transcript panel
- Cues with SRT timecodes (`00:01:02,400 --> 00:01:05,100`). Cues outside the edit are dimmed. The active cue is highlighted and followed, and following pauses if the user scrolls, with a Follow button to resume.
- Click a cue to seek. Click a word to seek to that word (words show for the active cue and on hover). Up and Down move between cues when the list has focus.
- Double-click edits the text, saved as a caption edit. Select lines and press Add to edit to add that range as a segment. Search box with Enter to jump to the first hit. Download SRT.

### Aspect picker
- Above the preview: presets 9:16, 1:1, 4:5, 16:9, 21:9, custom width and height, and a Crop or Fit switch. Changing it updates the output and the server recomputes every crop.

### Metadata tab
- Before the first run: platform (let the AI choose, or pick), language, tone (neutral, casual, punchy, professional), notes. Button: Write title and hashtags.
- Fields: platform chips (recommended first, click to switch), title and thumbnail title with alternates as chips and character counters, description with a counter, hashtags as closable tags with an add box, domain select plus subtopic. Each field has refresh (with optional instruction) and copy. Copy all and Download .txt. A small chat at the bottom. A version menu restores earlier sets. A banner offers Update when the plan changed. The thumbnail title is shown as plain text only, never as an image.

### Export drawer
- Progress while rendering, then the MP4 and SRT download buttons. Offer Write title and hashtags when done.

### Settings
- "What Clipora has learned": a list of preference sentences with edit, disable, delete. A switch: Learn from my edits. A one-time toast asks before saving an inferred preference.

### Upload limits (editors)
- The upload card shows "3 of 5 videos" and "2.1 of 5 GB" with small progress bars, plus the size and length limit in its hint text.
- When a limit is reached the card is disabled, with a plain message such as "You have 5 videos. Delete one to upload another." A file that is too big or too long is rejected before upload when the browser can tell, with the limit in the message.
- A video rejected at processing because it is too long shows Failed with "This video is longer than 60 minutes" and a Delete action, no Retry.

### Admin area
- **Limits tab:** a form with the four limits (videos per user, file size, storage per user, video length), each with its unit and allowed range. Save shows what changed.
- **Users tab:** a table of users with email, role, videos, storage, tokens and estimated cost for the chosen date range (default: this month), sortable by any column, with a search box. Clicking a row opens a drawer with the user's limits (override or reset to the global value), role, and usage broken down by project, by feature and by day.
- **Usage tab:** totals for the date range as `Statistic` cards (tokens in, tokens out, estimated cost), and a table grouped by user, day or feature. Cost always says "estimated".
- Admins see project titles and numbers only. No link opens another user's editor.

## 7. States every component must handle

Loading (`Skeleton` or `Spin`), empty (`Empty` with the next action), error (`Alert` or `Result` with Retry), offline or polling failure (keep the last data, show a small notice), long-running (progress with the stage name).

## 8. Accessibility and theme

- Use antd's tokens so contrast is right on the dark theme. Check text and state chips against WCAG AA contrast.
- Everything reachable by keyboard. The custom timeline gets focusable segments, arrow keys to nudge by one word, and an `aria-label` on each segment with its start, end and reason.
- Color is never the only signal: state chips always have text.
- The preview video has captions drawn by us, not browser tracks, so keep a text transcript panel as the accessible alternative.

## 9. Exceptions (things antd cannot provide)

| Piece | Why custom | How |
| --- | --- | --- |
| Video layers and preview | antd has no video editing components | `video` elements in a clipping `div`, tokens for colors |
| Waveform | No audio component | `canvas`, tokens for colors |
| Timeline blocks, trim handles, playhead | No timeline component | Styled `div`s, pointer events in our own hook, no drag-and-drop library |

Add new exceptions here before building them.
