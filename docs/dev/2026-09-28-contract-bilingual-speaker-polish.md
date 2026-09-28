# Interface contract — bilingual detection, speakers, per-minute polish

2026-09-28 · shared spec for the backend core, speaker module and frontend work.

Field names `ko` / `zh` (WebSocket) and `ko_text` / `zh_text` (DB) are kept for
compatibility: they always mean **spoken (source) text** and **translated text**
of that item, whatever the actual languages are. The per-item language codes
below say which language each field holds.

## 1. Language model per session

- `session.source_lang` / `session.target_lang`: unchanged meaning (the pair chosen
  at creation, e.g. `ko` → `zh`). `target_lang` is also the **reader language**
  used for summaries.
- New `session.bidirectional: bool` (default `true`). When true, each utterance's
  spoken language is auto-detected among `{source_lang, target_lang}`:
  - spoken == source_lang → translated into target_lang
  - spoken == target_lang → translated into source_lang (e.g. Chinese speech → Korean)
- When false, behaviour is the old one: spoken = source_lang always.

## 2. WebSocket server → client messages

### Segment messages (`status` ∈ interim | translating | done | error | remove)

```jsonc
{
  "id": "uuid", "status": "done",
  "ko": "spoken text", "zh": "translation",
  "timestamp_ms": 123000,        // == start_ms (audio time since recording start)
  "start_ms": 123000, "end_ms": 127400,   // NEW, audio-sample based
  "lang": "zh",                  // NEW spoken language of this segment (may be null on early interim)
  "target_lang": "ko",           // NEW language of the `zh` field
  "speaker": 1                   // NEW 0-based speaker index, null if unknown (interim is always null)
}
```

### Speaker list — NEW

```json
{"status": "speakers", "speakers": [{"index": 0, "name": null}, {"index": 1, "name": "教授"}]}
```

Sent when a new speaker index first appears, and once right after a resumed
connection opens (all speakers known before the pause). `name: null` → client
shows the default label `说话人 {index+1}`; a `null` never erases a name the
client already knows.

### Per-minute polish — `status: "polish"` (extended)

```jsonc
{
  "status": "polish", "id": "uuid", "chunk_index": 3,
  "start_ms": 180000, "end_ms": 241500,          // NEW audio-time range of the window
  "start_segment_seq": 40, "end_segment_seq": 52,
  "state": "ok",                                  // NEW ok | fallback  (fallback = realtime text/translation reused)
  "summary": "- 要点一\n- 要点二",                   // NEW 1–3 bullet lines in session.target_lang; may be ""
  "runs": [                                       // NEW ordered utterance groups
    {"speaker": 0, "lang": "ko", "target_lang": "zh",
     "start_ms": 180200, "end_ms": 195000,
     "source": "…", "translation": "…", "text_origin": "reasr"}   // reasr | realtime
  ],
  "ko_combined": "joined source text",             // kept for old clients
  "zh_polished": "joined translation",             // kept for old clients
  "created_at": "iso"
}
```

### Running outline — NEW

```json
{"status": "outline", "outline": "- …\n- …", "through_ms": 300000, "updated_at": "iso"}
```

Refreshed every 5 polish windows, at every pause and once at the end of the session.

### Speaker corrections — NEW (2026-09-29)

```json
{"status": "relabel", "segments": [{"id": "uuid", "speaker": 2}]}
```

Sent before a `polish` message when per-minute diarization finds that a live sentence
was mostly spoken by someone else (≥ 50 % of the clip). The client updates `speaker` on
those sentences only (text/status unchanged); the DB is already updated. Polish `runs`
of a diarized window are split at speaker turns, word by word when re-recognition is used,
so one run never mixes two people even when a live clip did.

## 2a. Client → server control messages, pause / resume (2026-09-28)

Text frames on `/ws/{session_id}`; audio stays binary float32 PCM.

| Message | Server does | Class afterwards |
|---|---|---|
| `{"action": "stop"}` | tail work (below), sends the last `done` / `polish` / `outline`, closes 1000 | `ended` |
| `{"action": "pause"}` | same tail work and messages, closes 1000 | `paused` |
| socket drops (no message), or server closes it (1003 bad PCM, 1008 account disabled) | same tail work, sends nothing | `paused` |

Tail work = commit the transcriber's open sentence (≥ 0.5 s of speech), wait for
every final to be recognised, translated and saved, `polish.finish()` so the
minute before the break is polished, fsync + close the PCM. The new state is
saved **before** the socket closes, so the client can resume at once. Only a
stop (or `POST /end`) ends a class.

**Recording states** (`session.recording_state`):
`created → recording ⇄ paused → ended`; `deleting` exists only inside a delete
transaction. `ended_at` stays `null` while `paused`. `duration_seconds` =
recorded audio seconds (PCM bytes / 64000), pauses excluded; updated at every
pause, disconnect and end.

**Resume** = a new WebSocket to a `paused` session (same URL). The claim moves
`created|paused → recording` atomically (a second socket gets close 4009; an
`ended` class gets 4009 before accept). The resumed connection:

- appends to the same PCM (never truncates; a torn last sample is zero-padded);
- continues the audio clock: new `start_ms` / `end_ms` / `timestamp_ms` start at
  the recorded length, so times stay unique and match positions in the PCM;
- continues `sequence` at max(sequence)+1, and the polish windows at the next
  `chunk_index` (`start_ms` of the first new window = recorded length, or the end
  of the last saved window if sentences after it were never polished, e.g. after
  a server crash); the previous minute's summary and last 2 runs remain context;
- keeps speaker numbers (tracker state saved at every pause/disconnect and once
  per polish window) and re-announces them with a `speakers` message.

A reconnect that arrives while the dropped socket is still doing its tail work
waits (≤ 45 s) instead of being refused. At server start, classes left
`recording` by a crash become `paused` (files untouched).

Client behaviour (frontend store): 暂停 sends `pause` and releases the microphone;
继续 opens a new socket without clearing shown segments / polish / speakers /
outline; an unexpected close retries after 2 s, 5 s and 10 s (re-acquiring the
microphone), then shows `paused` with “连接中断，已暂停，可点继续恢复”.

## 3. REST

- `POST /api/sessions` accepts `bidirectional: bool` (default `true`).
- `PATCH /api/sessions/{id}` accepts `bidirectional` only while `recording_state == "created"`.
- `GET /api/sessions/{id}` returns, in addition to the existing keys:
  - `session.bidirectional`, `session.live_outline`
  - each segment: `lang`, `target_lang`, `speaker`, `start_ms`, `end_ms` (nullable for old rows)
  - each polished chunk: `start_ms`, `end_ms`, `summary`, `status` (DB name of `state`), `runs` (parsed list, same shape as WS)
  - `speakers`: `[{"index": 0, "name": null, "segment_count": 12}, …]`
- `PUT /api/sessions/{id}/speakers/{index}` body `{"name": "教授"}` (or `null` / `""` to reset);
  name ≤ 24 chars, no control characters, index 0–15 → returns `{"speakers": [...]}`.
- `POST /api/sessions/{id}/end` — NEW. Ends a `paused` class, or a `created` one with no
  recording, without a WebSocket: `recording_state = "ended"`, `ended_at` set,
  `duration_seconds` = recorded audio seconds (0 without a PCM). Returns the session.
  Already `ended` → 200, unchanged. `recording` (or `created` with a PCM file) → 409.
  Owner only (others get 404). A resume racing the end: exactly one wins, the other gets 409/4009.
- `DELETE /api/sessions/{id}` now also accepts `paused` (same file staging as `ended`);
  `recording` still returns 409.
- `GET /api/sessions/{id}/recording`: `recording` is `false` while `paused` (the file is
  closed; downloads take a size snapshot).
- `session.speaker_state` (speaker tracker JSON for resume) is stored but never serialised.

## 4. Display conventions

- Speaker default label: `说话人 {index+1}`; palette by `index % 8`:
  `#2f6fde #d9480f #2b8a3e #9c36b5 #c2255c #0b7285 #e67700 #5c7cfa`.
- Direction badge per segment/run: `{languageLabel(lang)} → {languageLabel(target_lang)}`,
  shown only when it differs from the session default direction (keeps the feed quiet).
- `lang` attribute on the source text element = segment `lang`; on translation = `target_lang`.
