"""本機喚醒詞偵測（sherpa-onnx 關鍵詞模型，CPU、離線）。

待命時麥克風音訊只進這裡，偵測到喚醒詞才開始把聲音送上雲端（隱私設計，spec §5.2）。
「下一頁」「上一頁」也在這裡辨識，任何人說都會翻頁（上一頁亦同）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import sherpa_onnx

DEFAULT_WAKE = "賈維斯"
NEXT = "下一頁"
PREV = "上一頁"
# 「下一頁」「上一頁」的拼音 token（聲母＋帶調韻母）；「一」在「頁」前常唸二聲，兩種都收
_PAGE_LINES = ["x ià y ī y è @下一頁", "x ià y í y è @下一頁",
               "sh àng y ī y è @上一頁", "sh àng y í y è @上一頁"]
# 變調字：除預設讀音外再收的聲調（「一」「不」依後字變調）
_TONE_VARIANTS = {"一": ["ī", "í", "ì"], "不": ["ù", "ú"]}
# 第三聲在句中常唸成半上聲，模型常聽成別的聲調：第三聲的字四個聲調都收（2026-11-18 實機「賈維斯」偶爾叫不醒）
_TONES = ("āáǎà", "ēéěè", "īíǐì", "ōóǒò", "ūúǔù", "ǖǘǚǜ")
# 喚醒靈敏度（sherpa-onnx 關鍵詞行的 :加權 #門檻），只套用在喚醒詞；「下一頁」「上一頁」維持全域設定。
# 低＝原本的全域設定；高＝合成語音小聲＋雜訊時叫醒最多，40 段易混淆句仍沒誤叫醒（2026-11-18 比較）
SENSITIVITY = {"low": (1.0, 0.25), "medium": (1.5, 0.2), "high": (3.0, 0.05)}
DEFAULT_SENSITIVITY = "medium"
WAKE_BOOST, WAKE_THRESHOLD = SENSITIVITY[DEFAULT_SENSITIVITY]

_TOKENS = "tokens.txt"
_ENCODER = "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx"
_DECODER = "decoder-epoch-13-avg-2-chunk-16-left-64.onnx"
_JOINER = "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx"


class WakeWordError(Exception):
    pass


class UnknownTokenError(WakeWordError):
    """喚醒詞含模型詞表沒有的音（可退回預設喚醒詞）。"""


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿"


def validate_wake_word(word: str) -> str | None:
    """合法回傳 None，否則回傳給人看的原因。"""
    if not 2 <= len(word) <= 4:
        return "喚醒詞要 2 到 4 個字"
    if not all(_is_cjk(c) for c in word):
        return "喚醒詞只能用中文字"
    if word in (NEXT, PREV):
        return f"「{word}」是翻頁指令，不能當喚醒詞"
    return None


def _third_tone_variants(final: str) -> list[str]:
    """第三聲的韻母回傳另外三個聲調的寫法；其他聲調回傳空清單。"""
    for tones in _TONES:
        if tones[2] in final:
            return [final.replace(tones[2], t) for t in tones if t != tones[2]]
    return []


def wake_token_lines(word: str) -> list[str]:
    """把中文喚醒詞轉成 sherpa-onnx 關鍵詞行（"<tokens> @<word>"）；預設讀音排第一，
    「一」「不」加變調版本，第三聲的字四個聲調都收。"""
    from itertools import product

    from pypinyin import Style, pinyin

    inits = pinyin(word, style=Style.INITIALS, strict=False)
    finals = pinyin(word, style=Style.FINALS_TONE, strict=False)
    per_char: list[list[list[str]]] = []
    for ch, i, f in zip(word, inits, finals):
        init, final = i[0], f[0]
        extra = _TONE_VARIANTS.get(ch) or _third_tone_variants(final)
        finals_opts = [final] + [v for v in extra if v != final]
        per_char.append([([init] if init else []) + [v] for v in finals_opts])
    lines: list[str] = []
    for combo in product(*per_char):
        line = " ".join(t for part in combo for t in part) + f" @{word}"
        if line not in lines:
            lines.append(line)
    return lines


def _boosted(line: str, sensitivity: str = DEFAULT_SENSITIVITY) -> str:
    boost, threshold = SENSITIVITY.get(sensitivity, SENSITIVITY[DEFAULT_SENSITIVITY])
    return line.replace(" @", f" :{boost} #{threshold} @", 1)


def keyword_lines(word: str, sensitivity: str = DEFAULT_SENSITIVITY) -> list[str]:
    """關鍵詞檔的內容：喚醒詞（依靈敏度加權、調門檻）＋翻頁指令。不認得的靈敏度＝中。"""
    return [_boosted(l, sensitivity) for l in wake_token_lines(word)] + _PAGE_LINES


def _vocab(tokens_file: Path) -> set[str]:
    return {ln.split()[0] for ln in tokens_file.read_text(encoding="utf-8").splitlines() if ln.split()}


def model_files_ok(model_dir: Path) -> bool:
    d = Path(model_dir)
    return all((d / f).is_file() for f in (_TOKENS, _ENCODER, _DECODER, _JOINER))


class WakeWordDetector:
    def __init__(self, model_dir: Path, threshold: float, score: float, wake_word: str,
                 sensitivity: str = DEFAULT_SENSITIVITY):
        d = Path(model_dir)
        if not model_files_ok(d):
            raise WakeWordError(f"找不到喚醒詞模型：{d}")
        reason = validate_wake_word(wake_word)
        if reason:
            raise WakeWordError(f"喚醒詞「{wake_word}」不合用：{reason}")
        self.wake_label = wake_word
        vocab = _vocab(d / _TOKENS)
        wake_lines = wake_token_lines(wake_word)
        unknown = [t for t in wake_lines[0].split(" @")[0].split() if t not in vocab]
        if unknown:
            raise UnknownTokenError(f"喚醒詞「{wake_word}」含模型不認得的音：{'、'.join(unknown)}")
        # 丟掉詞表沒有的變調版本（預設讀音一定要在）
        wake_lines = [ln for ln in wake_lines if all(t in vocab for t in ln.split(" @")[0].split())]
        keywords = d / "keywords_xs.txt"
        keywords.write_text("\n".join([_boosted(l, sensitivity) for l in wake_lines] + _PAGE_LINES) + "\n",
                            encoding="utf-8")
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(d / _TOKENS), encoder=str(d / _ENCODER), decoder=str(d / _DECODER),
            joiner=str(d / _JOINER), keywords_file=str(keywords),
            num_threads=2, provider="cpu", keywords_score=score, keywords_threshold=threshold,
        )
        self._stream = self._spotter.create_stream()

    def feed(self, samples: np.ndarray) -> list[str]:
        """餵 16 kHz float32 音訊，回傳這一塊命中的關鍵詞。"""
        self._stream.accept_waveform(16000, samples)
        hits = []
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            result = self._spotter.get_result(self._stream)
            if result:
                hits.append(result)
                self._spotter.reset_stream(self._stream)
        return hits

    def reset(self) -> None:
        self._stream = self._spotter.create_stream()
