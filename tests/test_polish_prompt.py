"""Per-minute polish: runs, prompt, parsing and failure fallback (no API calls)."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")

from backend.polish import translator
from backend.polish.translator import (
    PolishRun, build_polish_messages, build_runs, choose_run_text,
    parse_polish_output, polish_runs,
)


def _seg(seq, start, end, text, zh="", lang="ko", target="zh", speaker=0):
    return SimpleNamespace(
        sequence=seq, timestamp_ms=start, start_ms=start, end_ms=end,
        ko_text=text, zh_text=zh, lang=lang, target_lang=target, speaker=speaker,
    )


def _run(source, speaker=0, lang="ko", target="zh", realtime_translation="实时译文"):
    return PolishRun(
        speaker=speaker, lang=lang, target_lang=target, start_ms=0, end_ms=5000,
        realtime_source=source, realtime_translation=realtime_translation, source=source,
    )


class RunTests(unittest.TestCase):
    def test_runs_group_by_speaker_language_and_pause(self):
        runs = build_runs([
            _seg(0, 0, 3000, "첫 문장", "第一句"),
            _seg(1, 3400, 6000, "둘째 문장", "第二句"),          # same speaker, short pause → merged
            _seg(2, 6200, 9000, "你把讲稿打开", "강의안을 여세요", lang="zh", target="ko", speaker=1),
            _seg(3, 12000, 14000, "셋째", "第三", speaker=0),
            _seg(4, 20000, 21000, "넷째", "第四", speaker=0),     # 6 s pause → new run
        ], "ko", "zh")
        self.assertEqual([(r.speaker, r.lang, r.segment_seqs) for r in runs],
                         [(0, "ko", [0, 1]), (1, "zh", [2]), (0, "ko", [3]), (0, "ko", [4])])
        self.assertEqual(runs[0].realtime_source, "첫 문장 둘째 문장")
        self.assertEqual(runs[0].realtime_translation, "第一句 第二句")
        self.assertEqual(runs[1].target_lang, "ko")

    def test_legacy_rows_without_new_fields_use_session_direction(self):
        legacy = SimpleNamespace(sequence=0, timestamp_ms=1000, ko_text="옛 문장", zh_text="旧句")
        (run,) = build_runs([legacy], "ko", "zh")
        self.assertEqual((run.lang, run.target_lang, run.speaker, run.start_ms), ("ko", "zh", None, 1000))

    def test_reasr_text_is_used_only_when_it_agrees_with_live_text(self):
        live = "지난주에는 두 명 발표했고 오늘은 다른 사람들 볼게요"
        better = "지난주에 우리 두 명 발표했고, 오늘은 다른 사람들 볼게요."
        self.assertEqual(choose_run_text(better, live), (better, "reasr"))
        self.assertEqual(choose_run_text("", live), (live, "realtime"))
        self.assertEqual(choose_run_text("감사합니다", live), (live, "realtime"))       # too short
        self.assertEqual(choose_run_text("전혀 다른 내용이 여기에 들어있습니다 정말로요", live), (live, "realtime"))



class SpeakerSplitTests(unittest.TestCase):
    def _run(self):
        return build_runs([
            _seg(0, 58000, 64000, "안녕하세요 석진씨 안녕하세요 경은누나", "你好硕珍 你好京恩姐", speaker=0),
            _seg(1, 63600, 69000, "왜요? 바빴잖아요", "为什么？你很忙吧", speaker=0),
        ], "ko", "zh", by_speaker=False)[0]

    def test_words_are_split_at_turns_and_blips_absorbed(self):
        from backend.polish.translator import speaker_subruns
        run = self._run()
        groups = [
            (4, [(58100, 58600, " 안녕하세요"), (58600, 59000, " 석진씨")]),
            (0, [(59200, 59700, " 안녕하세요"), (59700, 60300, " 경은누나")]),
            (4, [(60400, 60700, " 네")]),                       # 0.3 s blip inside speaker 0's turn
            (0, [(60800, 61500, " 앞으로는"), (61500, 62400, " 이야기")]),
        ]
        parts = speaker_subruns(run, [
            _seg(0, 58000, 64000, "안녕하세요 석진씨 안녕하세요 경은누나", "你好硕珍 你好京恩姐", speaker=0),
            _seg(1, 63600, 69000, "왜요? 바빴잖아요", "为什么？你很忙吧", speaker=0),
        ], groups)
        self.assertEqual([(p.speaker, p.source) for p in parts],
                         [(4, "안녕하세요 석진씨"), (0, "안녕하세요 경은누나 네 앞으로는 이야기")])
        # every live sentence (and its translation) lands in exactly one part
        self.assertEqual(sorted(seq for p in parts for seq in p.segment_seqs), [0, 1])

    def test_without_words_split_follows_relabelled_sentences(self):
        from backend.polish.translator import speaker_subruns
        segs = [_seg(0, 0, 3000, "첫 문장", "第一句", speaker=0), _seg(1, 3200, 6000, "둘째 문장", "第二句", speaker=2)]
        run = build_runs(segs, "ko", "zh", by_speaker=False)[0]
        parts = speaker_subruns(run, segs, None)
        self.assertEqual([(p.speaker, p.source, p.realtime_translation) for p in parts],
                         [(0, "첫 문장", "第一句"), (2, "둘째 문장", "第二句")])

    def test_adjacent_parts_of_one_speaker_are_merged(self):
        from backend.polish.translator import merge_adjacent_runs
        a, b = _run("근데 옛날에"), _run("똑같은 옷")
        b.start_ms, b.end_ms = 5500, 9000
        merged = merge_adjacent_runs([a, b, _run("네", speaker=1)])
        self.assertEqual([(m.speaker, m.source) for m in merged], [(0, "근데 옛날에 똑같은 옷"), (1, "네")])

class PromptTests(unittest.TestCase):
    def test_prompt_numbers_runs_with_speaker_and_direction(self):
        runs = [_run("오늘은 광합성을 설명합니다."), _run("讲稿在下面", speaker=1, lang="zh", target="ko")]
        messages = build_polish_messages(
            runs, {"0": "教授"}, previous_summary="- 上节课复习了细胞",
            previous_runs=[{"speaker": 0, "lang": "ko", "source": "앞에서는 세포", "translation": "前面讲了细胞"}],
            summary_lang="zh",
        )
        system, user = messages[0]["content"], messages[1]["content"]
        self.assertIn("本分钟要点", system)
        self.assertIn("用中文写", system)
        # Header and text on separate lines so the model does not copy the label.
        self.assertIn("[1] 译成中文｜说话人1（教授）\n오늘은 광합성을 설명합니다.", user)
        self.assertIn("[2] 译成韩语｜说话人2\n讲稿在下面", user)
        self.assertNotIn("无法理解的段落只输出", system)
        self.assertIn("必须用那种语言输出", system)
        self.assertLess(user.index("上一分钟"), user.index("本分钟逐段原文"))
        self.assertEqual(user.count("오늘은 광합성을 설명합니다."), 1)

    def test_glossary_matches_both_directions_and_material_is_bounded(self):
        runs = [_run("선물 시장입니다."), _run("期货的定义", speaker=1, lang="zh", target="ko")]
        user = build_polish_messages(
            runs, glossary=[("선물", "期货"), ("변압기", "变压器")],
            material_context="[Material: class.pdf]\n" + "x" * 1400,
        )[1]["content"]
        self.assertIn("선물 → 期货", user)
        self.assertNotIn("변압기", user)
        self.assertIn("class.pdf", user)
        self.assertNotIn("x" * 1300, user)
        self.assertLess(user.index("课程材料摘录"), user.index("本分钟逐段原文"))


class ParseTests(unittest.TestCase):
    def test_parse_translations_summary_and_drops_notes(self):
        output = (
            "[1] 今天讲光合作用。\n"
            "它发生在叶绿体里。\n"
            "（注：原文有重复，已省略）\n"
            "[2] 강의안은 아래에 있어요.\n"
            "[9] 越界编号被忽略\n"
            "要点：\n- 光合作用的场所\n- 讲稿位置\n- 第三条\n- 第四条超出\n"
        )
        translations, summary = parse_polish_output(output, 2)
        self.assertEqual(translations, {1: "今天讲光合作用。 它发生在叶绿体里。", 2: "강의안은 아래에 있어요."})
        self.assertEqual(summary, "- 光合作用的场所\n- 讲稿位置\n- 第三条")

    def test_no_content_marker_is_kept_verbatim(self):
        _translations, summary = parse_polish_output("[1] 好的。\n要点：（无实质内容）", 1)
        self.assertEqual(summary, "（无实质内容）")

    def test_copied_run_labels_are_stripped(self):
        # 10-02: 87 of 455 polished runs began with "说话人3 · 韩语→中文：".
        output = (
            "[1] 说话人4 · 中文→韩语：그리고 이 수업에서는\n"
            "[2] 译成中文｜说话人1（教授）\n今天讲光合作用。\n"
            "[3] 译文：强化学习\n"
            "[4] 说话人认为这样不对。\n"
        )
        translations, _summary = parse_polish_output(output, 4)
        self.assertEqual(translations, {
            1: "그리고 이 수업에서는", 2: "今天讲光合作用。", 3: "强化学习", 4: "说话人认为这样不对。",
        })


class _FakeStream:
    def __init__(self, text):
        self.text = text

    def __aiter__(self):
        async def gen():
            for index in range(0, len(self.text), 7):
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=self.text[index:index + 7]))])
        return gen()


class _FakeClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return _FakeStream(output)


class PolishCallTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.delay = patch.object(translator, "LLM_RETRY_DELAY_S", 0)
        self.delay.start()
        self.addCleanup(self.delay.stop)

    async def test_success_uses_llm_translations_and_bounded_request(self):
        client = _FakeClient(["[1] 今天讲光合作用。\n[2] 강의안은 아래에 있어요.\n要点：\n- 光合作用"])
        runs = [_run("오늘은 광합성을 설명합니다."), _run("讲稿在下面", speaker=1, lang="zh", target="ko")]
        with patch.object(translator, "get_client", return_value=client):
            result = await polish_runs(runs, summary_lang="zh")
        self.assertEqual(result.state, "ok")
        self.assertEqual([r.translation for r in result.runs], ["今天讲光合作用。", "강의안은 아래에 있어요."])
        self.assertEqual(result.summary, "- 光合作用")
        request = client.calls[0]
        self.assertTrue(request["stream"])
        self.assertLessEqual(request["max_tokens"], 3000)
        self.assertIn("timeout", request)

    async def test_timeouts_fall_back_to_live_translation_after_one_retry(self):
        client = _FakeClient([TimeoutError("slow"), TimeoutError("slow again")])
        runs = [_run("오늘은 광합성을 설명합니다.", realtime_translation="今天说明光合作用。")]
        with patch.object(translator, "get_client", return_value=client):
            result = await polish_runs(runs)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result.state, "fallback")
        self.assertEqual(result.summary, "")
        self.assertEqual(result.runs[0].translation, "今天说明光合作用。")

    async def test_runaway_repetition_is_cut_off_and_not_used(self):
        client = _FakeClient(["[1] " + "哈" * 800, "[1] " + "哈" * 800])
        runs = [_run("ㅋㅋ 재밌네요", realtime_translation="挺有意思")]
        with patch.object(translator, "get_client", return_value=client):
            result = await polish_runs(runs)
        self.assertEqual(result.state, "fallback")
        self.assertEqual(result.runs[0].translation, "挺有意思")

    async def test_run_left_in_the_spoken_language_uses_the_live_translation(self):
        # 10-02: Chinese→Korean runs often came back as rewritten Chinese.
        client = _FakeClient(["[1] 今天讲光合作用。\n[2] 讲稿就在下面这里。\n要点：\n- 光合作用"])
        runs = [
            _run("오늘은 광합성을 설명합니다."),
            _run("讲稿在下面", speaker=1, lang="zh", target="ko", realtime_translation="강의안은 아래에 있어요"),
        ]
        with patch.object(translator, "get_client", return_value=client):
            result = await polish_runs(runs, summary_lang="zh")
        self.assertEqual([r.translation for r in result.runs], ["今天讲光合作用。", "강의안은 아래에 있어요"])
        self.assertEqual(result.state, "ok")

    async def test_insane_length_translation_is_replaced_per_run(self):
        client = _FakeClient(["[1] 好。\n[2] " + "这是一段被模型凭空扩写出来的很长很长的内容，" * 12 + "\n要点：\n- 要点"])
        runs = [_run("네 좋아요", realtime_translation="好的"), _run("짧다", realtime_translation="短")]
        with patch.object(translator, "get_client", return_value=client):
            result = await polish_runs(runs)
        self.assertEqual([r.translation for r in result.runs], ["好。", "短"])
        self.assertEqual(result.state, "ok")


if __name__ == "__main__":
    unittest.main()
