"""下載喚醒詞模型（每台電腦一次）：k2-fsa 官方 sherpa-onnx 中英關鍵詞模型，約 33 MB。

    uv run python scripts/get_wake_model.py

存到 config 的 kws_model_dir（預設：Windows 在 %LOCALAPPDATA%\\xiaoshen\\models\\...，
Mac 在 ~/Library/Application Support/xiaoshen/models/...），已存在就略過。
"""

from __future__ import annotations

import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from xiaoshen.config import load_config
from xiaoshen.wakeword import model_files_ok

URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
       "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2")


def main() -> int:
    target = load_config().kws_model_dir
    if model_files_ok(target):
        print(f"已有模型：{target}")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "kws.tar.bz2"
        print(f"下載 {URL}")
        urllib.request.urlretrieve(URL, archive)
        print(f"解壓到 {target.parent}")
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(target.parent, filter="data")
    if not model_files_ok(target):
        print(f"解壓後仍找不到模型檔：{target}", file=sys.stderr)
        return 1
    print(f"完成：{target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
