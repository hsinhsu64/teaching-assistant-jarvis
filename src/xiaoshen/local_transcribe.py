"""筆電退路：家裡電腦關機、斷網或同步卡住時，主持人按了才在這台電腦轉寫（不自動跑，以免拖慢晨會）。

Whisper turbo（sherpa-onnx int8，CPU）＋和工作站相同的分講者；沒有人名辨識（筆電不裝 torch），
人名由主持人在確認畫面自己補。實測約 1～1.5 小時／小時錄音。
模型第一次要用時才下載（約 600 MB，k2-fsa 官方 GitHub），主持人按確認才下載。
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

from . import diarization
from .platform_support import app_data_dir
from .zh import to_traditional

WHISPER_DIR = "sherpa-onnx-whisper-turbo"
WHISPER_FILES = ("turbo-encoder.int8.onnx", "turbo-decoder.int8.onnx", "turbo-tokens.txt")
REQUIRED_FILES = tuple(f"{WHISPER_DIR}/{f}" for f in WHISPER_FILES) + (
    f"{diarization.SEGMENTATION_DIR}/model.onnx", diarization.EMBEDDING_FILE)
RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
DOWNLOADS = [  # (網址, 種類, 大小（給進度用，約略）)
    (RELEASES + "asr-models/sherpa-onnx-whisper-turbo.tar.bz2", "tar", 563_790_207),
    (RELEASES + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2", "tar", 6_958_444),
    (RELEASES + "speaker-recongition-models/" + diarization.EMBEDDING_FILE, "file", 28_281_139),
]
DOWNLOAD_NOTE = "在這台電腦轉寫要先下載語音辨識模型（約 600 MB，來源 k2-fsa 官方 GitHub）。要下載嗎？"
CHUNK_SECONDS = 25.0
MIN_CHUNK_SECONDS = 0.5


def chunks(diar: list[dict], max_seconds: float = CHUNK_SECONDS,
           min_seconds: float = MIN_CHUNK_SECONDS) -> list[tuple[float, float]]:
    """依分講者的片段切成 Whisper 一次吃得下的長度（≤ 25 秒）；太短的略過。"""
    out = []
    for d in diar:
        t = d["start"]
        while t < d["end"]:
            end = min(d["end"], t + max_seconds)
            if end - t >= min_seconds:
                out.append((round(t, 3), round(end, 3)))
            t = end
    return out


class LocalEngines:
    def __init__(self, models_dir: Path, threshold: float = 0.7, threads: int | None = None):
        self.models_dir = Path(models_dir)
        self.threshold = threshold
        self.threads = threads or max(1, (os.cpu_count() or 2) // 2)  # 留一半給晨會的其他功能
        self._cache: tuple[str, list[dict], dict] | None = None
        self.models = {"asr": "sherpa-onnx whisper-turbo int8 (CPU, laptop fallback)",
                       "diarization": f"sherpa-onnx pyannote-3.0 + CAM++ (threshold {threshold})",
                       "names": "none (laptop fallback: rules only)"}

    def _diar(self, audio_path: Path):
        key = str(audio_path)
        if self._cache is None or self._cache[0] != key:
            audio = diarization.load_audio(audio_path)
            diar, emb = diarization.diarize(audio, self.models_dir, self.threshold, self.threads)
            self._cache = (key, diar, emb)
            self._audio = audio
        return self._cache[1], self._cache[2]

    def transcribe(self, audio: Path) -> list[dict]:
        import sherpa_onnx

        diar, _ = self._diar(audio)
        wd = self.models_dir / WHISPER_DIR
        rec = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=str(wd / WHISPER_FILES[0]), decoder=str(wd / WHISPER_FILES[1]), tokens=str(wd / WHISPER_FILES[2]),
            language="zh", task="transcribe", num_threads=self.threads)
        out = []
        rate = diarization.RATE
        for a, b in chunks(diar):
            stream = rec.create_stream()
            stream.accept_waveform(rate, self._audio[int(a * rate):int(b * rate)])
            rec.decode_stream(stream)
            text = to_traditional(stream.result.text.strip())
            if text:
                out.append({"start": a, "end": b, "text": text})
        return out

    def diarize(self, audio: Path) -> tuple[list[dict], dict]:
        return self._diar(audio)

    def person_names(self, text: str) -> list[str]:
        return []


class LocalTranscriber:
    def __init__(self, models_dir: Path | None = None, threshold: float = 0.7):
        self.models_dir = Path(models_dir) if models_dir is not None else app_data_dir() / "models"
        self.threshold = threshold

    def ready(self) -> bool:
        return all((self.models_dir / rel).exists() for rel in REQUIRED_FILES)

    def make_engines(self) -> LocalEngines:
        return LocalEngines(self.models_dir, self.threshold)

    def download(self, progress: Callable[[float], None]) -> None:
        total = sum(size for _, _, size in DOWNLOADS)
        done = 0
        self.models_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            for url, kind, size in DOWNLOADS:
                name = url.rsplit("/", 1)[1]
                target = Path(tmp) / name
                base = done

                def hook(blocks, block_size, _total, base=base, size=size):
                    progress(min(1.0, (base + min(size, blocks * block_size)) / total))

                urllib.request.urlretrieve(url, target, reporthook=hook)
                if kind == "tar":
                    with tarfile.open(target, "r:bz2") as tar:
                        # 只解開要用的檔（Whisper 的 tar 裡還有很大的 float32 版本）
                        wanted = [m for m in tar.getmembers() if m.isfile() and m.name.removeprefix("./") in REQUIRED_FILES]
                        tar.extractall(self.models_dir, members=wanted, filter="data")
                else:
                    shutil.copyfile(target, self.models_dir / name)
                done += size
                target.unlink(missing_ok=True)
        if not self.ready():
            raise RuntimeError("下載完成但找不到模型檔")
        progress(1.0)
