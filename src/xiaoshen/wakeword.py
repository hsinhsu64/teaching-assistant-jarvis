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


def wake_token_lines(word: str) -> list[str]:
    """把中文喚醒詞轉成 sherpa-onnx 關鍵詞行（"<tokens> @<word>"）；「一」「不」加變調版本。"""
    from itertools import product

    from pypinyin import Style, pinyin

    inits = pinyin(word, style=Style.INITIALS, strict=False)
    finals = pinyin(word, style=Style.FINALS_TONE, strict=False)
    per_char: list[list[list[str]]] = []
    for ch, i, f in zip(word, inits, finals):
        init, final = i[0], f[0]
        finals_opts = [final] + [v for v in _TONE_VARIANTS.get(ch, []) if v != final]
        per_char.append([([init] if init else []) + [v] for v in finals_opts])
    lines: list[str] = []
    for combo in product(*per_char):
        line = " ".join(t for part in combo for t in part) + f" @{word}"
        if line not in lines:
            lines.append(line)
    return lines


def keyword_lines(word: str) -> list[str]:
    return wake_token_lines(word) + _PAGE_LINES


def _vocab(tokens_file: Path) -> set[str]:
    return {ln.split()[0] for ln in tokens_file.read_text(encoding="utf-8").splitlines() if ln.split()}


def model_files_ok(model_dir: Path) -> bool:
    d = Path(model_dir)
    return all((d / f).is_file() for f in (_TOKENS, _ENCODER, _DECODER, _JOINER))


class WakeWordDetector:
    def __init__(self, model_dir: Path, threshold: float, score: float, wake_word: str):
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
        keywords.write_text("\n".join(wake_lines + _PAGE_LINES) + "\n", encoding="utf-8")
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
