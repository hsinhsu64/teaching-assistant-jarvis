"""分講者（sherpa-onnx，CPU）：pyannote segmentation 3.0 分段＋CAM++ 聲紋，自由分群。

轉寫工作站與筆電退路共用。回傳片段與每群的平均聲紋（給 transcript_build.merge_small_clusters 歸併小群）。
實測（2026-11-22）：固定人數會把兩位男性併成一人；自由分群門檻 0.7 準，但會分出幾十群，所以要歸併。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

RATE = 16000
SEGMENTATION_DIR = "sherpa-onnx-pyannote-segmentation-3-0"
EMBEDDING_FILE = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
EMBED_SEGMENTS = 8        # 每群取最長的幾段算平均聲紋
EMBED_MAX_SECONDS = 10.0  # 每段最多取幾秒


def model_files(models_dir: Path) -> dict[str, Path]:
    models_dir = Path(models_dir)
    return {"segmentation": models_dir / SEGMENTATION_DIR / "model.onnx",
            "embedding": models_dir / EMBEDDING_FILE}


def load_audio(path: Path) -> np.ndarray:
    """讀成 16 kHz 單聲道 float32（錄音器存的就是這個格式）。"""
    import soundfile as sf

    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if rate != RATE:
        from .audio_io import resample

        mono = resample(mono, rate, RATE)
    return np.ascontiguousarray(mono, dtype=np.float32)


def diarize(audio: np.ndarray, models_dir: Path, threshold: float = 0.7,
            threads: int = 4) -> tuple[list[dict], dict[int, list[float]]]:
    import sherpa_onnx

    files = model_files(models_dir)
    missing = [str(p) for p in files.values() if not p.exists()]
    if missing:
        raise FileNotFoundError("找不到分講者模型：" + "、".join(missing))
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(files["segmentation"])),
            num_threads=threads),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(files["embedding"]), num_threads=threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
        min_duration_on=0.3, min_duration_off=0.5)
    segs = sherpa_onnx.OfflineSpeakerDiarization(cfg).process(audio).sort_by_start_time()
    diar = [{"start": float(s.start), "end": float(s.end), "speaker": int(s.speaker)} for s in segs]
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(files["embedding"]), num_threads=threads))
    return diar, cluster_embeddings(audio, diar, extractor)


def cluster_embeddings(audio: np.ndarray, diar: list[dict], extractor) -> dict[int, list[float]]:
    by: dict[int, list[dict]] = {}
    for d in diar:
        by.setdefault(d["speaker"], []).append(d)
    out: dict[int, list[float]] = {}
    for spk, segs in by.items():
        vecs = []
        for d in sorted(segs, key=lambda d: d["start"] - d["end"])[:EMBED_SEGMENTS]:
            a = int(d["start"] * RATE)
            b = min(int(d["end"] * RATE), a + int(EMBED_MAX_SECONDS * RATE))
            if b - a < RATE // 2:
                continue
            stream = extractor.create_stream()
            stream.accept_waveform(RATE, audio[a:b])
            stream.input_finished()
            if extractor.is_ready(stream):
                vecs.append(np.asarray(extractor.compute(stream), dtype=np.float32))
        if vecs:
            out[spk] = np.mean(vecs, axis=0).tolist()
    return out
