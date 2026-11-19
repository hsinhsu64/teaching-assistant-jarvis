import numpy as np
import pytest

import xiaoshen.wakeword as ww
from xiaoshen.config import Config
from xiaoshen.wakeword import (DEFAULT_WAKE, WakeWordDetector, WakeWordError, keyword_lines, model_files_ok,
                               validate_wake_word, wake_token_lines)

MODEL_FILES = [
    "tokens.txt",
    "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
    "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
    "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
]


class FakeStream:
    def __init__(self):
        self.fed = 0

    def accept_waveform(self, rate, samples):
        self.fed += len(samples)


class FakeSpotter:
    created = []

    def __init__(self, **kw):
        self.kw = kw
        self.results = ["小森"]
        self.resets = 0
        FakeSpotter.created.append(self)

    def create_stream(self):
        return FakeStream()

    def is_ready(self, s):
        return bool(self.results)

    def decode_stream(self, s):
        pass

    def get_result(self, s):
        return self.results.pop(0)

    def reset_stream(self, s):
        self.resets += 1


def write_tokens(model_dir, words=("小森", "賈維斯", "一號", "下一頁", "上一頁"), drop=()):
    toks = {t for w in words for line in keyword_lines(w) for t in line.split(" @")[0].split()
            if t[0] not in ":#"}
    toks -= set(drop)
    (model_dir / "tokens.txt").write_text("".join(f"{t} {i}\n" for i, t in enumerate(sorted(toks))), encoding="utf-8")


@pytest.fixture
def fake_model(tmp_path, monkeypatch):
    for f in MODEL_FILES:
        (tmp_path / f).write_text("x")
    write_tokens(tmp_path)
    monkeypatch.setattr(ww.sherpa_onnx, "KeywordSpotter", FakeSpotter)
    return tmp_path


def test_missing_model_raises(tmp_path):
    assert not model_files_ok(tmp_path)
    with pytest.raises(WakeWordError, match="找不到喚醒詞模型"):
        WakeWordDetector(tmp_path, 0.25, 1.0, "小森")


def test_keywords_file_written(fake_model):
    d = WakeWordDetector(fake_model, 0.3, 1.5, "小森")
    assert d.wake_label == "小森"
    assert (fake_model / "keywords_xs.txt").read_text(encoding="utf-8").splitlines() == keyword_lines("小森")
    assert keyword_lines("小森")[0] == f"x iǎo s ēn :{ww.WAKE_BOOST} #{ww.WAKE_THRESHOLD} @小森"
    kw = FakeSpotter.created[-1].kw
    assert kw["keywords_threshold"] == 0.3 and kw["keywords_score"] == 1.5
    assert kw["encoder"].endswith("int8.onnx") and kw["keywords_file"].endswith("keywords_xs.txt")


def test_feed_returns_labels_and_resets(fake_model):
    d = WakeWordDetector(fake_model, 0.25, 1.0, "小森")
    assert d.feed(np.zeros(1600, dtype=np.float32)) == ["小森"]
    assert FakeSpotter.created[-1].resets == 1
    assert d.feed(np.zeros(1600, dtype=np.float32)) == []


def test_real_model_smoke():
    model = Config().kws_model_dir
    if not model_files_ok(model):
        pytest.skip("喚醒詞模型未下載")
    d = WakeWordDetector(model, 0.25, 1.0, "小森")
    hits = []
    for _ in range(20):
        hits += d.feed(np.zeros(1600, dtype=np.float32))
    assert hits == []


def test_config_voice_defaults():
    c = Config()
    assert c.voice_enabled and c.live_model == "gemini-3.8-live"
    assert c.kws_model_dir.name == "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
    assert c.kws_threshold == 0.25 and c.kws_score == 1.0
    assert c.audio_device == "" and c.voice_idle_seconds == 8.0 and c.keys_wake == []
    assert c.wake_sensitivity == "medium"


def test_sensitivity_levels():
    """喚醒靈敏度三檔（:加權 #門檻）。低＝修改前的設定；中＝預設；高＝合成語音在小聲＋雜訊下叫醒最多、
    40 段易混淆句仍沒有誤叫醒（2026-11-18 比較：低 36/80、中 38/80、高 49/80）。"""
    assert ww.SENSITIVITY == {"low": (1.0, 0.25), "medium": (1.5, 0.2), "high": (3.0, 0.05)}
    assert (ww.WAKE_BOOST, ww.WAKE_THRESHOLD) == ww.SENSITIVITY["medium"]
    assert keyword_lines("小森", "high")[0] == "x iǎo s ēn :3.0 #0.05 @小森"
    assert keyword_lines("小森", "low")[0] == "x iǎo s ēn :1.0 #0.25 @小森"
    assert keyword_lines("小森", "亂填") == keyword_lines("小森")  # 不認得的值＝中
    assert keyword_lines("小森", "high")[-4:] == keyword_lines("小森")[-4:]  # 翻頁指令不受影響


def test_detector_and_build_detector_use_sensitivity(fake_model):
    from xiaoshen.launcher import build_detector

    WakeWordDetector(fake_model, 0.25, 1.0, "小森", sensitivity="high")
    first = (fake_model / "keywords_xs.txt").read_text(encoding="utf-8").splitlines()[0]
    assert first == "x iǎo s ēn :3.0 #0.05 @小森"
    cfg = Config(wake_word="小森", wake_sensitivity="low")
    cfg.kws_model_dir = fake_model
    build_detector(cfg)
    assert (fake_model / "keywords_xs.txt").read_text(encoding="utf-8").splitlines()[0] == "x iǎo s ēn :1.0 #0.25 @小森"


def test_prev_page_keyword_lines():
    from xiaoshen.wakeword import PREV

    assert PREV == "上一頁"
    lines = keyword_lines("小森")
    assert "sh àng y ī y è @上一頁" in lines and "sh àng y í y è @上一頁" in lines


def test_keyword_lines_for_two_char_word():
    """喚醒詞那幾行加上較高的加權與較低的門檻（較容易叫醒）；「下一頁」「上一頁」維持全域設定（2026-11-18）。"""
    boost = f" :{ww.WAKE_BOOST} #{ww.WAKE_THRESHOLD} @小森"
    assert keyword_lines("小森") == [l.replace(" @小森", boost) for l in wake_token_lines("小森")] + [
        "x ià y ī y è @下一頁", "x ià y í y è @下一頁", "sh àng y ī y è @上一頁", "sh àng y í y è @上一頁"]
    assert ww.WAKE_BOOST > 1.0 and ww.WAKE_THRESHOLD < Config().kws_threshold


def test_tokens_for_two_char_word_are_initial_plus_toned_final():
    """每個字拆成聲母＋帶調韻母，與喚醒詞模型詞表的寫法相同（真模型的詞表見 test_real_model_knows_test_word）。"""
    assert wake_token_lines("小森")[0] == "x iǎo s ēn @小森"


def test_real_model_knows_test_word():
    """測試用的名字「小森」每個音都在真的喚醒詞模型詞表裡（假模型的 tokens.txt 是從同一套拼音產生的）。"""
    model = Config().kws_model_dir
    if not model_files_ok(model):
        pytest.skip("喚醒詞模型未下載")
    vocab = ww._vocab(model / "tokens.txt")
    for line in wake_token_lines("小森") + wake_token_lines("賈維斯"):
        assert all(t in vocab for t in line.split(" @")[0].split()), line


def test_tokens_for_jarvis():
    assert DEFAULT_WAKE == "賈維斯"
    # 第三聲在句中常唸成半上聲，模型常聽成別的聲調：第三聲的字四個聲調都收，預設讀音排第一
    assert wake_token_lines("賈維斯") == ["j iǎ w éi s ī @賈維斯", "j iā w éi s ī @賈維斯",
                                       "j iá w éi s ī @賈維斯", "j ià w éi s ī @賈維斯"]


def test_only_third_tone_chars_get_variants():
    assert wake_token_lines("維斯") == ["w éi s ī @維斯"]
    assert len(wake_token_lines("小美")) == 16  # 兩個第三聲：4 × 4
    assert len(wake_token_lines("小森")) == 4 and wake_token_lines("小森")[0] == "x iǎo s ēn @小森"


def test_yi_variants():
    lines = wake_token_lines("一號")
    assert any(l.startswith("y ī ") for l in lines) and any(l.startswith("y í ") for l in lines)
    assert any(l.startswith("y ì ") for l in lines)
    assert len(lines) == len(set(lines))


def test_bu_variants():
    lines = wake_token_lines("不二")
    assert any(l.startswith("b ù ") for l in lines) and any(l.startswith("b ú ") for l in lines)


@pytest.mark.parametrize("word", ["小", "小森小森小", "Jarvis", "下一頁", "上一頁", "", "小 森", "小a"])
def test_invalid_wake_words(word):
    assert validate_wake_word(word) is not None


@pytest.mark.parametrize("word", ["小森", "賈維斯", "小森小森"])
def test_valid_wake_words(word):
    assert validate_wake_word(word) is None


def test_detector_uses_given_wake_word(fake_model):
    d = WakeWordDetector(fake_model, 0.25, 1.0, "賈維斯")
    assert d.wake_label == "賈維斯"
    assert (fake_model / "keywords_xs.txt").read_text(encoding="utf-8").splitlines() == keyword_lines("賈維斯")


def test_unknown_token_raises(fake_model):
    write_tokens(fake_model, drop=("éi",))
    with pytest.raises(WakeWordError, match="喚醒詞「賈維斯」含模型不認得的音：éi"):
        WakeWordDetector(fake_model, 0.25, 1.0, "賈維斯")


def test_unknown_variant_token_is_dropped_not_fatal(fake_model):
    write_tokens(fake_model, drop=("ì",))
    WakeWordDetector(fake_model, 0.25, 1.0, "一號")
    text = (fake_model / "keywords_xs.txt").read_text(encoding="utf-8")
    assert "y ī h ào :" in text and "ì" not in text.split("@下一頁")[0]


def test_build_detector_falls_back_on_invalid_word(fake_model):
    from xiaoshen.launcher import build_detector

    cfg = Config(wake_word="小")
    cfg.kws_model_dir = fake_model
    detector, warning = build_detector(cfg)
    assert detector.wake_label == DEFAULT_WAKE
    assert "小" in warning and "賈維斯" in warning


def test_build_detector_falls_back_on_unknown_token(fake_model):
    from xiaoshen.launcher import build_detector

    write_tokens(fake_model, words=("賈維斯", "下一頁", "上一頁"))
    cfg = Config(wake_word="小森")
    cfg.kws_model_dir = fake_model
    detector, warning = build_detector(cfg)
    assert detector.wake_label == DEFAULT_WAKE
    assert "小森" in warning and "不認得" in warning


def test_build_detector_ok_has_no_warning(fake_model):
    from xiaoshen.launcher import build_detector

    cfg = Config(wake_word="小森")
    cfg.kws_model_dir = fake_model
    detector, warning = build_detector(cfg)
    assert detector.wake_label == "小森" and warning is None


def test_build_detector_missing_model_does_not_fall_back(tmp_path):
    from xiaoshen.launcher import build_detector

    cfg = Config(wake_word="小森")
    cfg.kws_model_dir = tmp_path
    with pytest.raises(WakeWordError, match="找不到喚醒詞模型"):
        build_detector(cfg)


def test_build_detector_publishes_wake_word_in_use(fake_model):
    """畫面上的「喊…」要用實際生效的喚醒詞：退回預設時是賈維斯，不是設定的詞或名字（I-2）。"""
    from xiaoshen import persona
    from xiaoshen.launcher import build_detector

    cfg = Config(display_name="腎臟科教學助理", wake_word="小")
    cfg.kws_model_dir = fake_model
    persona.start_from_config(cfg)
    build_detector(cfg)
    assert persona.app_info()["wake"] == DEFAULT_WAKE
    cfg = Config(display_name="腎臟科教學助理", wake_word="小森")
    cfg.kws_model_dir = fake_model
    build_detector(cfg)
    assert persona.app_info()["wake"] == "小森" and persona.app_info()["name"] == "腎臟科教學助理"


def test_voice_wake_check_reports_fallback_label(fake_model, monkeypatch):
    from xiaoshen import launcher

    cfg = Config(wake_word="小")
    cfg.kws_model_dir = fake_model
    check = launcher.voice_check_functions(cfg)["wake_check"]
    label, warning = check()
    assert label == DEFAULT_WAKE and "小" in warning
