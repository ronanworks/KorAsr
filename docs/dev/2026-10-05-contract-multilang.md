# Interface contract — multi-language classes

2026-10-05 · extends `2026-09-28-contract-bilingual-speaker-polish.md` (still valid for
speakers, pause/resume, polish windows and outline). Design and measurements:
`data/eval/2026-10-05/multilang/design.md` (local, not in git).
2026-10-15 · Japanese (`ja`) added as a classroom, display and interface language; up to 3
occasional and 5 display languages. Measurements: `data/eval/2026-10-15/ja/report.html` (local).

## 一句话总结

A class has three language lists instead of one direction. Every sentence is
translated into every display language except its own; the first of those targets
is the **primary target**, which the old fields (`target_lang`, `zh_text`, `zh`,
`translation`) keep carrying, so old clients see exactly what they saw before.

## 1. Language plan per session

| Field | Meaning | Rule | Default |
|---|---|---|---|
| `languages` | frequent classroom languages; the first is the **main language** | 1–2 | `["ko","zh"]` |
| `extra_languages` | occasional classroom languages | 0–3, disjoint from `languages` | `[]` |
| `display_languages` | languages to read; the first is the **reading language** | 1–5 | `["zh","ko"]` |

- Codes: `ko`, `zh`, `en`, `ru`, `ja`. No duplicates. `display_languages[0] != languages[0]`
  (otherwise 422). The limits fit the user's class: ko/zh frequent, en/ru/ja occasional, all five shown.
- A display language need not be spoken in class (e.g. show `en` in a Korean class).
- Targets of a sentence spoken in `s`: `[d for d in display_languages if d != s]`, display order.
  Primary target = the first of them; none when `s` is the only display language.
- Legacy columns are always written from the plan:
  `source_lang = languages[0]`, `target_lang = display_languages[0]`,
  `bidirectional = len(languages) + len(extra_languages) > 1` (display only).
- Rows saved before the plan (NULL columns) mean
  `languages = bidirectional ? [source, target] : [source]`, `extra_languages = []`,
  `display_languages = bidirectional ? [target, source] : [target]`.
  The default plan is today's bidirectional Korean ⇄ Chinese class, sentence for sentence.

### Language ID (per final sentence)

- Frequent languages: exactly the previous rule (`choose_language`: shares, hysteresis,
  not-speech thresholds 0.10 / 0.20).
- Occasional language L (the most likely of `extra_languages`): chosen when
  `p(L) ≥ 0.5`, `p(L) ≥ 2 × max p(frequent)` and ≥ 1 s of speech after trimming silence;
  while the previous sentence was already L, `p(L) ≥ max p(frequent)` and `p(L) ≥ 0.2`
  suffice. Otherwise the frequent languages decide as before.
  Settings: `LANGID_EXTRA_MIN_PROB`, `LANGID_EXTRA_MIN_RATIO`, `LANGID_EXTRA_MIN_SPEECH_S`,
  `LANGID_EXTRA_STICKY_MIN_PROB`.
- Japanese as the occasional language L has its own, stricter values, because Whisper hears
  Korean as Japanese: `p(ja) ≥ 0.85` (`LANGID_JA_MIN_PROB`), `≥ 2 × max p(frequent)`
  (`LANGID_JA_MIN_RATIO`), ≥ 1 s of speech; while the previous sentence was Japanese,
  `p(ja) ≥ max p(frequent)` and `p(ja) ≥ 0.5` (`LANGID_JA_STICKY_MIN_PROB`).
- Japanese is recognised by Whisper (never SenseVoice) and gets none of the Chinese clean-up
  (Traditional → Simplified, filler removal): those run for `lang == "zh"` only.

## 2. WebSocket server → client

### Sentence messages (`status` ∈ interim | translating | done | error | remove)

```jsonc
{
  "id": "uuid", "status": "translating",
  "ko": "spoken text", "zh": "primary target's text (or a placeholder)",   // legacy, unchanged meaning
  "lang": "en", "target_lang": "zh",                                       // target_lang = primary target
  "targets": ["zh", "ko"],                                                 // NEW all targets, display order
  "translations": {"zh": "流式中的部分译文…", "ko": ""},                  // NEW full snapshot, every message
  "translation_state": {"zh": "translating", "ko": "translating"},        // NEW translating | done | error
  "timestamp_ms": 0, "start_ms": 0, "end_ms": 0, "speaker": 0
}
```

- One streaming translation call per target, all started together (each with its own
  `source → target` prompt; earlier sentences are shown in the same target when they
  have it). Progress is pushed at most every 100 ms, one message carrying all targets.
- `status` stays `translating` until every target has finished. Then `done` if at least
  one target succeeded, else `error`. A failed target is `null` in `translations` and
  `error` in `translation_state`. `zh` = the primary translation, or `翻译暂不可用`.
- No targets (spoken in the only display language): one `done` message with
  `targets: []`, `translations: {}`, `zh: ""`; no translation call.
- `interim` messages add `targets` once the language is known (reserve placeholders).
- Clients that ignore the new keys keep working: they read `zh` / `target_lang` as before.
- A translation with nothing recognisable is only a marker in its target language:
  `（听不清）` zh, `（잘 안 들림）` ko, `(inaudible)` en, `(неразборчиво)` ru, `（聞き取れず）` ja
  (live and polish; rows saved before may hold `（听不清）` for any target). It is ordinary text to display.

### Per-minute polish (`status: "polish"`)

`runs[i]` adds `targets` and `translations` (`{target: text}`); `translation` /
`target_lang` stay the primary target. `summary` (本分钟要点) and the outline are written
in the reading language only. A run of a sentence with no targets has
`targets: []`, `translations: {}`, `translation: ""`.

The polish request asks single-target runs for `[n] 译文` (as before) and multi-target runs
for one line per target, `[n|zh] …` / `[n|ko] …`; a line without a code belongs to the
primary target. Each target is checked on its own (missing, too long, still in the
spoken language → that target's live translation).

`speakers` / `relabel` / `outline` and all client → server messages are unchanged
(control messages `client_info` and `vad_gain`: 09-28 contract §2a).

## 3. REST

- `POST /api/sessions`, `PATCH /api/sessions/{id}` accept optional
  `languages`, `extra_languages`, `display_languages`.
  - None given: the legacy `source_lang` / `target_lang` / `bidirectional` are validated
    as before (`SUPPORTED_PAIRS`: ko/zh → zh/ko/en/ru, plus ko → ja, zh → ja, ja → ko, ja → zh)
    and the plan is derived from them. A PATCH that repeats the session's current legacy
    values keeps the plan (e.g. its occasional languages).
  - Any given: the plan wins (missing plan fields keep the current / legacy-derived
    value) and the legacy columns are recomputed. Invalid plan → 422.
  - Plan and legacy fields are locked once recording starts (409), as before.
- Every session in a response (`POST`, `PATCH`, `GET /api/sessions`, `GET /{id}`,
  `POST /{id}/end`) has the three arrays; old rows get the derived values.
  `speaker_state` is never serialised.
- `GET /api/sessions/{id}`: each segment has `translations` (parsed object,
  `{target_lang: zh_text}` for old rows) and `targets` (its keys, display order);
  `polished_chunks[].runs[]` as in the WS message (old runs get `targets: [target_lang]`).
  `PATCH /api/segments/{id}` returns the segment in the same shape.
- Summary (`POST /{id}/summary`): reading language only; each sentence carries its
  primary translation.
- Export:
  - header `韩语 → 中文（课堂语言：韩语、中文；偶尔：英语；显示：中文、韩语）`;
  - txt: a one-target sentence stays `原文 | 译文`; several targets → one indented line
    per target (`    中文: …`); sentences not in the main language are tagged `[英语→中文/韩语]`;
  - md: columns `| Time | 原文 | 中文 | 韩语 |` (one per display language).
  - Optional `ui_lang=zh|ko|en|ru|ja` (`GET …/export?format=md&ui_lang=en`; `ko-KR` → `ko`, unknown →
    `zh`): the interface language of the headers and labels — plan line, columns (`| Time | Original
    | Chinese | Korean |`), language names and tags, default speaker labels (`Speaker 2`), section
    titles. Transcripts, translations, key points, outline, notes and speaker names stay as saved.
    Without it (and with `zh`) the export is byte for byte the one above. The frontend sends its
    current interface language.

## 4. Storage

| Table | Column | Content |
|---|---|---|
| session | `languages`, `extra_languages`, `display_languages` | JSON arrays (TEXT); NULL = derive |
| segment | `translations` | JSON `{target: text \| null}` in display order (TEXT); NULL = `{target_lang: zh_text}`; `{}` = nothing to translate |
| polishedchunk | `runs_json` | each run adds `targets`, `translations` |

Additive migration (`_add_missing_columns`), no backfill. A new sentence row starts with
every target `null` and is updated once its translations finish.

## 5. Display conventions

- Badge only for items not in the main language: `中→韩`, `英→中·韩` (full text
  `英语 → 中文、韩语`); main-language sentences stay quiet.
- A sentence card lists its targets in display order (reading language first when it is a
  target); with several targets each line has a one-character language tag and its own
  state (生成中 / 翻译失败).
- Session label: `韩·中 → 中·韩`, `韩·中（偶尔英·俄）→ 中·韩`, `韩·中（偶尔英·俄·日）→ 中·韩·日`.
- Every source and translation line carries its language code as `lang` (`ja` / `zh` / `ko` / …):
  Han characters take that language's glyphs (`:lang(ja)` uses Yu Gothic UI / Meiryo /
  Hiragino Sans / Noto Sans JP), and the Korean `word-break: keep-all` stays on Korean text.
- Interface languages: 中文 / 한국어 / English / Русский / 日本語 (browser `ja*` → Japanese).
