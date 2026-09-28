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

Sent when a new speaker index first appears. `name: null` → client shows the
default label `说话人 {index+1}`.

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

Refreshed every 5 polish windows and once at the end of the session.

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

## 4. Display conventions

- Speaker default label: `说话人 {index+1}`; palette by `index % 8`:
  `#2f6fde #d9480f #2b8a3e #9c36b5 #c2255c #0b7285 #e67700 #5c7cfa`.
- Direction badge per segment/run: `{languageLabel(lang)} → {languageLabel(target_lang)}`,
  shown only when it differs from the session default direction (keeps the feed quiet).
- `lang` attribute on the source text element = segment `lang`; on translation = `target_lang`.
