"""Language plan, tiered language ID, REST compatibility and export (contract 2026-10-05).

Probabilities are the real Whisper large-v3 values of the 10-02 class
(data/eval/2026-10-05/multilang/raw/langid.jsonl; design.md §8).
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session as DBSession, create_engine

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend.api import sessions as sessions_api
from backend.api.export import export_session
from backend.asr import langid
from backend.asr.langid import choose_language, choose_language_tiered
from backend.languages import (
    DEFAULT_PLAN, LanguagePlan, plan_from_legacy, primary_target, segment_translations, session_plan,
    translation_target, translation_targets, validate_plan,
)
from backend.storage.models import PolishedChunk, Segment, Session as SessionModel, User

FREQUENT = ("ko", "zh")
EN = ("en",)
EN_RU = ("en", "ru")


class TieredLanguageIdTests(unittest.TestCase):
    def decide(self, probs, extra=EN, duration=2.0, speech=2.0, previous=None):
        return choose_language_tiered(probs, FREQUENT, extra, duration, speech, previous).language

    def test_noise_with_some_english_probability_is_still_not_speech(self):
        # 09-28 08:33: noise that came out as "감사합니다." live.
        self.assertIsNone(self.decide({"ko": 0.01, "zh": 0.02, "en": 0.47}, duration=0.6, speech=0.5, previous="zh"))

    def test_clear_english_is_shown_as_english(self):
        # 101:50 "They think it will be a problem…": dropped as not-speech today.
        probs = {"en": 0.96094, "ru": 0.00438, "ko": 0.00204, "zh": 0.00029}
        self.assertEqual(self.decide(probs, duration=6.132, speech=5.28, previous="ko"), "en")
        self.assertIsNone(choose_language(probs, FREQUENT, 6.132, "ko").language)

    def test_an_english_turn_continues_on_a_plain_majority(self):
        # 155:15 "A heavy rain begin." inside the 4-minute English report.
        probs = {"en": 0.36646, "ru": 0.07159, "ko": 0.06573, "zh": 0.01324}
        self.assertEqual(self.decide(probs, duration=1.88, speech=1.55, previous="en"), "en")
        self.assertIsNone(self.decide(probs, duration=1.88, speech=1.55, previous=None))
        # Even then it needs p ≥ 0.2 and more than any frequent language.
        self.assertIsNone(self.decide({"en": 0.15, "ko": 0.064, "ru": 0.063, "zh": 0.019}, previous="en"))
        self.assertEqual(self.decide({"en": 0.30, "ko": 0.45, "zh": 0.01}, previous="en"), "ko")

    def test_chinese_is_not_diluted_by_the_english_probability(self):
        # 35:05: as a plain fourth candidate, English cut the Chinese share to 0.56 (< 0.60).
        probs = {"zh": 0.43237, "en": 0.23145, "ru": 0.06329, "ko": 0.03836}
        self.assertEqual(self.decide(probs, extra=EN_RU, duration=5.982, speech=5.95, previous="ko"), "zh")
        self.assertEqual(choose_language(probs, ("ko", "zh", "en", "ru"), 5.982, "ko").language, "ko")

    def test_short_unsure_or_outweighed_clips_do_not_switch(self):
        # 153:15 "Next.": 0.92 s of speech; 134:58 Russian-looking 0.81 s; 29:52 p(en) 0.15.
        self.assertIsNone(self.decide({"en": 0.78857, "ko": 0.01434, "ru": 0.00817, "zh": 0.00197},
                                      duration=1.88, speech=0.92, previous="ko"))
        self.assertIsNone(self.decide({"ru": 0.79736, "en": 0.0135, "ko": 0.00782, "zh": 0.00595},
                                      extra=EN_RU, duration=1.624, speech=0.81, previous="ko"))
        self.assertIsNone(self.decide({"en": 0.15002, "ko": 0.06403, "ru": 0.06305, "zh": 0.01938},
                                      duration=2.392, speech=2.39))
        # p(en) 0.55 is not twice the best frequent language (0.30).
        self.assertEqual(self.decide({"en": 0.55, "ko": 0.30, "zh": 0.05}, previous="ko"), "ko")

    def test_the_saved_english_sentence_at_30_05_is_corrected(self):
        probs = {"en": 0.62891, "ko": 0.08057, "zh": 0.03687, "ru": 0.00767}
        self.assertEqual(self.decide(probs, duration=6.032, speech=5.08, previous="ko"), "en")
        self.assertEqual(choose_language(probs, FREQUENT, 6.032, "ko").language, "ko")   # shown as Korean today

    def test_the_most_likely_occasional_language_is_chosen(self):
        probs = {"ru": 0.55664, "en": 0.04532, "ko": 0.01654, "zh": 0.00387}   # 115:44
        self.assertEqual(self.decide(probs, extra=EN_RU, duration=1.88, speech=1.42), "ru")
        self.assertIsNone(self.decide(probs, extra=EN, duration=1.88, speech=1.42))   # no Russian in this class

    def test_thresholds_come_from_settings(self):
        probs = {"en": 0.6, "ko": 0.05, "zh": 0.01}
        self.assertEqual(self.decide(probs), "en")
        with patch.object(langid.settings, "langid_extra_min_prob", 0.7):
            self.assertIsNone(self.decide(probs))

    def test_without_occasional_languages_detection_is_unchanged(self):
        audio = np.zeros(32000, dtype=np.float32)
        probs = {"ko": 0.30, "zh": 0.45, "en": 0.2}
        with patch.object(langid, "detect_language_probs", return_value=probs) as detect:
            self.assertEqual(langid.detect_language(audio, FREQUENT, "ko"), choose_language(probs, FREQUENT, 2.0, "ko"))
            self.assertEqual(langid.detect_language(audio, FREQUENT, "ko", extra=()), choose_language(probs, FREQUENT, 2.0, "ko"))
            self.assertEqual(langid.detect_language(audio, ("ko",), None).language, "ko")
        self.assertEqual(detect.call_count, 2)   # a one-language class skips Whisper language ID

    def test_speech_length_is_measured_after_trimming_silence(self):
        def tone(seconds):
            return (0.1 * np.sin(np.arange(int(16000 * seconds)) / 5)).astype(np.float32)

        silence = np.zeros(int(16000 * 0.6), dtype=np.float32)
        probs = {"en": 0.9, "ko": 0.01, "zh": 0.01}
        with patch.object(langid, "detect_language_probs", return_value=probs):
            short = langid.detect_language(np.concatenate([silence, tone(0.8), silence]), FREQUENT, "ko", extra=EN)
            long = langid.detect_language(np.concatenate([silence, tone(1.3), silence]), FREQUENT, "ko", extra=EN)
        self.assertIsNone(short.language)        # a 2 s clip with 0.8 s of speech
        self.assertEqual(long.language, "en")


class LanguagePlanTests(unittest.TestCase):
    def test_targets_follow_display_order_and_skip_the_spoken_language(self):
        plan = LanguagePlan(("ko", "zh"), ("en",), ("zh", "ko"))
        self.assertEqual((plan.targets("ko"), plan.targets("zh"), plan.targets("en")), (["zh"], ["ko"], ["zh", "ko"]))
        self.assertEqual(plan.primary_target("en"), "zh")
        self.assertEqual(LanguagePlan(("ko",), (), ("zh", "ko", "en", "ru")).targets("ko"), ["zh", "en", "ru"])
        self.assertEqual(translation_targets("zh", ("zh",)), [])
        self.assertIsNone(primary_target("zh", ("zh",)))
        self.assertEqual((translation_target("zh", "ko", "zh"), translation_target("en", "ko", "zh")), ("ko", "zh"))

    def test_the_default_plan_is_todays_bidirectional_class(self):
        self.assertEqual(DEFAULT_PLAN, plan_from_legacy("ko", "zh", True))
        self.assertEqual(DEFAULT_PLAN.as_dict(), {"languages": ["ko", "zh"], "extra_languages": [], "display_languages": ["zh", "ko"]})
        self.assertEqual(DEFAULT_PLAN.legacy_fields(), {"source_lang": "ko", "target_lang": "zh", "bidirectional": True})
        self.assertEqual(plan_from_legacy("ko", "zh", False), LanguagePlan(("ko",), (), ("zh",)))
        self.assertEqual(plan_from_legacy("zh", "ru", True).display, ("ru", "zh"))
        self.assertTrue(LanguagePlan(("ko",), ("en",), ("zh",)).legacy_fields()["bidirectional"])

    def test_validation(self):
        plan = validate_plan(["ko", "zh"], ["en", "ru"], ["zh", "ko", "en", "ru"])
        self.assertEqual(plan.spoken, ("ko", "zh", "en", "ru"))
        # Up to 3 occasional and 5 display languages (2026-10-15: en/ru/ja occasional).
        self.assertEqual(validate_plan(["ko"], ["en", "ru", "zh"], ["zh"]).extra, ("en", "ru", "zh"))
        for languages, extra, display in (
            ([], [], ["zh"]), (["ko", "zh", "en"], [], ["zh"]), (["ko"], ["en", "ru", "zh", "ja"], ["zh"]),
            (["ko"], [], ["zh", "ko", "en", "ru", "ja", "zh"]), (["fr"], [], ["zh"]), (["ko"], [], ["zh", "zh"]),
            (["ko", "zh"], ["zh"], ["zh"]), (["ko", "zh"], [], ["ko", "zh"]), ("ko", [], ["zh"]),
        ):
            with self.subTest(languages=languages, extra=extra, display=display):
                with self.assertRaises(ValueError):
                    validate_plan(languages, extra, display)

    def test_rows_saved_before_the_plan_are_derived(self):
        old = SimpleNamespace(source_lang="ko", target_lang="zh", bidirectional=True,
                              languages=None, extra_languages=None, display_languages=None)
        self.assertEqual(session_plan(old), DEFAULT_PLAN)
        self.assertEqual(session_plan(SimpleNamespace(source_lang="zh", target_lang="en", bidirectional=False)),
                         LanguagePlan(("zh",), (), ("en",)))
        new = SimpleNamespace(source_lang="ko", target_lang="zh", bidirectional=True, languages='["ko", "zh"]',
                              extra_languages='["en"]', display_languages='["zh", "ko"]')
        self.assertEqual(session_plan(new), LanguagePlan(("ko", "zh"), ("en",), ("zh", "ko")))
        broken = SimpleNamespace(source_lang="ko", target_lang="zh", bidirectional=False, languages='["xx"]',
                                 extra_languages=None, display_languages=None)
        self.assertEqual(session_plan(broken), LanguagePlan(("ko",), (), ("zh",)))

    def test_segment_translations_of_old_and_new_rows(self):
        def row(**values):
            return SimpleNamespace(**{"translations": None, "lang": "ko", "target_lang": "zh", "zh_text": None, **values})

        self.assertEqual(segment_translations(row(zh_text="你好")), {"zh": "你好"})
        self.assertEqual(segment_translations(row()), {"zh": None})                       # failed, old row
        self.assertEqual(segment_translations(row(translations='{"zh": "你好", "ko": null}')), {"zh": "你好", "ko": None})
        self.assertEqual(segment_translations(row(lang="zh", target_lang=None, translations="{}")), {})
        very_old = row(lang=None, target_lang=None, zh_text="旧句")
        self.assertEqual(segment_translations(very_old, DEFAULT_PLAN), {"zh": "旧句"})


class _ApiCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.db = DBSession(self.engine)
        self.addCleanup(self.db.close)
        self.user = User(username="t", password_hash="x")
        self.db.add(self.user)
        self.db.commit()

    def create(self, **body):
        return sessions_api.create_session(sessions_api.SessionCreate(name=body.pop("name", "L"), **body), self.db, self.user)

    def update(self, session_id, **body):
        return sessions_api.update_session(session_id, sessions_api.SessionUpdate(**body), self.db, self.user)


class PlanApiTests(_ApiCase):
    def test_create_with_a_plan_also_writes_the_legacy_fields(self):
        created = self.create(languages=["ko", "zh"], extra_languages=["en"], display_languages=["zh", "ko"])
        self.assertEqual((created["languages"], created["extra_languages"], created["display_languages"]),
                         (["ko", "zh"], ["en"], ["zh", "ko"]))
        self.assertEqual((created["source_lang"], created["target_lang"], created["bidirectional"]), ("ko", "zh", True))
        row = self.db.get(SessionModel, created["id"])
        self.assertEqual(json.loads(row.extra_languages), ["en"])
        # New fields win; the legacy ones are recomputed.
        mixed = self.create(source_lang="zh", target_lang="ru", bidirectional=False,
                            languages=["ko"], display_languages=["zh", "en"])
        self.assertEqual((mixed["source_lang"], mixed["target_lang"], mixed["bidirectional"]), ("ko", "zh", False))
        self.assertEqual(mixed["extra_languages"], [])
        # Missing plan fields come from the legacy fields (defaults: ko ⇄ zh).
        partial = self.create(extra_languages=["en"])
        self.assertEqual((partial["languages"], partial["display_languages"]), (["ko", "zh"], ["zh", "ko"]))

    def test_old_clients_and_old_rows_get_the_derived_plan(self):
        legacy = self.create(source_lang="zh", target_lang="ru")
        self.assertEqual((legacy["languages"], legacy["display_languages"]), (["zh", "ru"], ["ru", "zh"]))
        one_way = self.create(bidirectional=False)
        self.assertEqual((one_way["languages"], one_way["display_languages"]), (["ko"], ["zh"]))
        with self.assertRaises(HTTPException) as raised:
            self.create(source_lang="en", target_lang="ko")
        self.assertEqual(raised.exception.status_code, 422)

        old = SessionModel(name="old", user_id=self.user.id, source_lang="ko", target_lang="zh", bidirectional=True,
                           speaker_state='{"means": []}')
        self.db.add(old)
        self.db.commit()
        self.db.add_all([
            Segment(session_id=old.id, sequence=0, timestamp_ms=0, ko_text="你好", zh_text="안녕", lang="zh", target_lang="ko"),
            Segment(session_id=old.id, sequence=1, timestamp_ms=900, ko_text="옛 문장", zh_text="旧句"),
            Segment(session_id=old.id, sequence=2, timestamp_ms=1800, ko_text="실패", lang="ko", target_lang="zh"),
        ])
        self.db.add(PolishedChunk(session_id=old.id, chunk_index=0, start_segment_seq=0, end_segment_seq=1,
                                  ko_combined="", zh_polished="", runs_json=json.dumps([
                                      {"speaker": 0, "lang": "zh", "target_lang": "ko", "source": "你好", "translation": "안녕"},
                                  ], ensure_ascii=False)))
        self.db.commit()
        detail = jsonable_encoder(sessions_api.get_session(old.id, self.db, self.user))
        session = detail["session"]
        self.assertEqual((session["languages"], session["extra_languages"], session["display_languages"]),
                         (["ko", "zh"], [], ["zh", "ko"]))
        self.assertNotIn("speaker_state", session)
        self.assertEqual([s["translations"] for s in detail["segments"]], [{"ko": "안녕"}, {"zh": "旧句"}, {"zh": None}])
        self.assertEqual([s["targets"] for s in detail["segments"]], [["ko"], ["zh"], ["zh"]])
        run = detail["polished_chunks"][0]["runs"][0]
        self.assertEqual((run["targets"], run["translations"]), (["ko"], {"ko": "안녕"}))
        listed = jsonable_encoder(sessions_api.list_sessions(self.db, self.user))
        self.assertTrue(all(isinstance(item["display_languages"], list) for item in listed))
        starred = sessions_api.patch_segment(detail["segments"][0]["id"], sessions_api.SegmentPatch(is_starred=True),
                                             self.db, self.user)
        self.assertEqual((starred["is_starred"], starred["translations"]), (True, {"ko": "안녕"}))

    def test_invalid_plans_are_rejected(self):
        for body in ({"languages": ["ko", "zh", "en"]}, {"display_languages": ["ko", "zh"]},
                     {"languages": ["ko"], "extra_languages": ["ko"]}, {"display_languages": []},
                     {"languages": ["fr"]}, {"extra_languages": ["en", "ru", "ja", "fr"]}):
            with self.subTest(body=body):
                with self.assertRaises(HTTPException) as raised:
                    self.create(**body)
                self.assertEqual(raised.exception.status_code, 422)

    def test_the_plan_can_change_until_recording_starts(self):
        created = self.create(languages=["ko", "zh"], extra_languages=["en"], display_languages=["zh", "ko"])
        sid = created["id"]
        only_chinese = self.update(sid, display_languages=["zh"])
        self.assertEqual((only_chinese["display_languages"], only_chinese["extra_languages"]), (["zh"], ["en"]))
        # An old client re-saving the same direction keeps the occasional language…
        resaved = self.update(sid, name="N", source_lang="ko", target_lang="zh")
        self.assertEqual((resaved["name"], resaved["extra_languages"]), ("N", ["en"]))
        # …while a different legacy direction re-derives the plan.
        changed = self.update(sid, source_lang="zh", target_lang="ko")
        self.assertEqual((changed["languages"], changed["extra_languages"], changed["display_languages"]),
                         (["zh", "ko"], [], ["ko", "zh"]))
        with self.assertRaises(HTTPException) as raised:
            self.update(sid, languages=["zh", "ko"], display_languages=["zh"])   # reading = main language
        self.assertEqual(raised.exception.status_code, 422)

        row = self.db.get(SessionModel, sid)
        row.recording_state = "recording"
        self.db.commit()
        for body in ({"languages": ["zh"]}, {"extra_languages": ["en"]}, {"display_languages": ["ko", "zh", "en"]},
                     {"source_lang": "zh", "target_lang": "ko"}):
            with self.subTest(body=body):
                with self.assertRaises(HTTPException) as raised:
                    self.update(sid, **body)
                self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.update(sid, name="renamed")["display_languages"], ["ko", "zh"])


class MultilingualExportTests(_ApiCase):
    def _mixed_class(self):
        """A Korean class with one English sentence (two targets), one Chinese one and a polished minute."""
        created = self.create(name="Mixed", languages=["ko", "zh"], extra_languages=["en"], display_languages=["zh", "ko"])
        sid = created["id"]
        row = self.db.get(SessionModel, sid)
        row.recording_state, row.ended_at = "ended", datetime.now(timezone.utc)
        self.db.add_all([
            Segment(session_id=sid, sequence=0, timestamp_ms=1000, ko_text="안녕하세요", zh_text="你好",
                    lang="ko", target_lang="zh", translations='{"zh": "你好"}'),
            Segment(session_id=sid, sequence=1, timestamp_ms=2000, ko_text="Next.", zh_text="下一个。",
                    lang="en", target_lang="zh", translations='{"zh": "下一个。", "ko": "다음."}', speaker=1),
            Segment(session_id=sid, sequence=2, timestamp_ms=3000, ko_text="讲稿在下面", zh_text="강의안은 아래",
                    lang="zh", target_lang="ko", translations='{"ko": "강의안은 아래"}'),
            PolishedChunk(session_id=sid, chunk_index=0, start_segment_seq=0, end_segment_seq=2, start_ms=0,
                          end_ms=60_000, ko_combined="", zh_polished="", summary="- 要点", runs_json=json.dumps([
                              {"speaker": 1, "lang": "en", "target_lang": "zh", "source": "Next.", "translation": "下一个。",
                               "targets": ["zh", "ko"], "translations": {"zh": "下一个。", "ko": "다음."}},
                          ], ensure_ascii=False)),
        ])
        self.db.commit()
        return sid

    def _export(self, sid, fmt, **query):
        return export_session(sid, fmt, self.db, self.user, **query).body.decode("utf-8")

    def test_ui_lang_zh_and_the_default_are_todays_export(self):
        sid = self._mixed_class()
        for fmt in ("txt", "md"):
            with self.subTest(format=fmt):
                today = self._export(sid, fmt)
                self.assertEqual(self._export(sid, fmt, ui_lang="zh"), today)
                self.assertEqual(self._export(sid, fmt, ui_lang="fr"), today)   # unknown → default
                self.assertEqual(self._export(sid, fmt, ui_lang="zh-CN"), today)

    def test_ui_lang_sets_headers_and_language_names_but_never_the_content(self):
        sid = self._mixed_class()
        txt = self._export(sid, "txt", ui_lang="en")
        self.assertIn("# Korean → Chinese (class languages: Korean, Chinese; occasional: English; display: Chinese, Korean)", txt)
        self.assertIn("[00:02] Speaker 2: [English→Chinese/Korean] Next.\n    Chinese: 下一个。\n    Korean: 다음.", txt)
        self.assertIn("[00:03] [Chinese→Korean] 讲稿在下面 | 강의안은 아래", txt)
        self.assertIn("Speaker 2: Next.\n  → Chinese: 下一个。\n  → Korean: 다음.", txt)
        self.assertIn("- 要点", txt)                                       # key points are data

        md = self._export(sid, "md", ui_lang="en")
        self.assertIn("| Time | Original | Chinese | Korean |", md)
        self.assertIn("| 00:02 | **Speaker 2** [English→Chinese/Korean] Next. | 下一个。 | 다음. |", md)
        self.assertIn("- **Speaker 2**: 下一个。\n  <br>Korean: 다음.", md)
        self.assertIn("**Duration**: 0 min", md)
        self.assertNotIn("原文", md)
        self.assertNotIn("说话人", md)

        ko = self._export(sid, "md", ui_lang="ko")
        self.assertIn("| 시간 | 원문 | 중국어 | 한국어 |", ko)
        self.assertIn("**화자 2** [영어→중국어/한국어] Next.", ko)
        self.assertIn("**번역 방향**: 한국어 → 중국어 (수업 언어: 한국어, 중국어; 가끔: 영어; 표시: 중국어, 한국어)", ko)
        self.assertIn("## 원문 자막", ko)

        ru = self._export(sid, "txt", ui_lang="ru")
        self.assertIn("# Корейский → китайский (языки занятия: корейский, китайский; иногда: английский; "
                      "отображение: китайский, корейский)", ru)
        self.assertIn("[00:02] Спикер 2: [английский→китайский/корейский] Next.\n    Китайский: 下一个。\n    Корейский: 다음.", ru)
        self.assertIn("=== Чистовой перевод ===", ru)
        ru_md = self._export(sid, "md", ui_lang="ru")
        self.assertIn("| Время | Оригинал | Китайский | Корейский |", ru_md)

    def test_named_speakers_keep_their_names_in_every_language(self):
        sid = self._mixed_class()
        row = self.db.get(SessionModel, sid)
        row.speaker_names = json.dumps({"1": "教授"}, ensure_ascii=False)
        self.db.commit()
        self.assertIn("说话人2（教授）: [英语→中文/韩语] Next.", self._export(sid, "txt"))
        self.assertIn("Speaker 2 (教授): [English→Chinese/Korean] Next.", self._export(sid, "txt", ui_lang="en"))
        self.assertIn("**화자 2 (教授)**", self._export(sid, "md", ui_lang="ko"))

    def test_each_display_language_gets_its_own_line_and_column(self):
        sid = self._mixed_class()

        txt = export_session(sid, "txt", self.db, self.user).body.decode("utf-8")
        self.assertIn("# 韩语 → 中文（课堂语言：韩语、中文；偶尔：英语；显示：中文、韩语）", txt)
        self.assertIn("[00:01] 안녕하세요 | 你好", txt)                       # one target: unchanged format
        self.assertIn("[00:02] 说话人2: [英语→中文/韩语] Next.\n    中文: 下一个。\n    韩语: 다음.", txt)
        self.assertIn("[00:03] [中文→韩语] 讲稿在下面 | 강의안은 아래", txt)
        self.assertIn("说话人2: Next.\n  → 中文: 下一个。\n  → 韩语: 다음.", txt)

        md = export_session(sid, "md", self.db, self.user).body.decode("utf-8")
        self.assertIn("| Time | 原文 | 中文 | 韩语 |", md)
        self.assertIn("| 00:02 | **说话人2** [英语→中文/韩语] Next. | 下一个。 | 다음. |", md)
        self.assertIn("| 00:03 | [中文→韩语] 讲稿在下面 |  | 강의안은 아래 |", md)
        self.assertIn("- **说话人2**：下一个。\n  <br>韩语：다음.", md)


if __name__ == "__main__":
    unittest.main()
