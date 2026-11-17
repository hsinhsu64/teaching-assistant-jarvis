"""喚醒詞實地測試：對著會議室麥克風念句子，看設定的喚醒詞與「下一頁」的命中與誤觸發。

    uv run python scripts/wake_test.py            # 自由測：即時顯示音量與每次命中
    uv run python scripts/wake_test.py --script   # 依序念 10 句，最後印出命中率與誤觸發數
    uv run python scripts/wake_test.py --script --threshold 0.2

只在本機辨識，不會把聲音送上雲端。
"""

from __future__ import annotations

import argparse
import queue
import sys
import time

import numpy as np
import sounddevice as sd

from xiaoshen.audio_io import BLOCK, MIC_RATE, find_device, to_float
from xiaoshen.config import load_config
from xiaoshen.wakeword import NEXT, WakeWordDetector


def build_script(wake: str) -> list[tuple[str, str | None]]:
    """念的句子與期待命中的詞；喚醒詞用設定的那個，其餘是容易誤觸發的近音句。"""
    return [
        (f"{wake}，請問 SRNS 的一線用藥是什麼？", wake),
        ("下一頁", NEXT),
        (f"{wake}，根據今天的簡報出五題", wake),
        ("好，我們看下一頁", NEXT),
        (wake, wake),
        ("腎臟科的小朋友今天有三位", None),
        ("小心，這位病人的腎功能在下降", None),
        ("下一位病人請報告", None),
        ("我們下週一再討論", None),
        ("小申你好，我是住院醫師", None),
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", action="store_true", help="依序念 10 句並統計")
    ap.add_argument("--threshold", type=float, default=None, help="喚醒詞門檻（預設用 config）")
    ap.add_argument("--seconds", type=float, default=6.0, help="每句的錄音秒數")
    args = ap.parse_args()

    config = load_config()
    threshold = args.threshold if args.threshold is not None else config.kws_threshold
    wake = config.effective_wake_word
    detector = WakeWordDetector(config.kws_model_dir, threshold, config.kws_score, wake)
    script = build_script(wake)
    device = find_device(config.audio_device, "input")
    print(f"喚醒詞：{wake}　麥克風：{sd.query_devices(device)['name'] if device is not None else '系統預設'}　門檻 {threshold}")

    blocks: queue.Queue[bytes] = queue.Queue()
    stream = sd.RawInputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=BLOCK, device=device,
                               callback=lambda indata, frames, t, status: blocks.put(bytes(indata)))
    stream.start()

    def listen(seconds: float, show_level: bool) -> list[str]:
        hits: list[str] = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                samples = to_float(blocks.get(timeout=0.5))
            except queue.Empty:
                continue
            for h in detector.feed(samples):
                hits.append(h)
                print(f"\n  ★ 命中：{h}", flush=True)
            if show_level:
                level = int(min(1.0, float(np.sqrt(np.mean(samples ** 2))) * 20) * 30)
                print("\r音量 " + "█" * level + " " * (30 - level), end="", flush=True)
        return hits

    try:
        if not args.script:
            print("自由測試中（Ctrl+C 結束）…")
            listen(float("inf"), True)
            return
        results = []
        for i, (sentence, expect) in enumerate(script, 1):
            input(f"\n[{i}/{len(script)}] 按 Enter 後念：「{sentence}」")
            detector.reset()
            while not blocks.empty():
                blocks.get_nowait()
            hits = listen(args.seconds, True)
            ok = (expect in hits) if expect else not hits
            results.append((sentence, expect, hits, ok))
            print(f"\n  → {'OK' if ok else '不符'}（期待：{expect or '不觸發'}，實際：{hits or '無'}）")
        should = [r for r in results if r[1]]
        should_not = [r for r in results if not r[1]]
        print("\n==== 結果 ====")
        print(f"應觸發命中：{sum(r[3] for r in should)} / {len(should)}")
        print(f"誤觸發：{sum(not r[3] for r in should_not)} / {len(should_not)}")
        for s, e, h, ok in results:
            print(f"  {'OK ' if ok else '不符'} {s}　→ {h or '無'}")
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()
        stream.close()


if __name__ == "__main__":
    sys.exit(main())
