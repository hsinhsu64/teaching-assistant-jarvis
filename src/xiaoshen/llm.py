"""文字模型呼叫。流程模組只依賴 LLM 協定，測試用假物件替換。

金鑰只從環境變數 GEMINI_API_KEY 讀取，不寫進設定檔或 repo。
"""

from __future__ import annotations

import json
import os
import time
from typing import Protocol


class LLMError(Exception):
    pass


class LLM(Protocol):
    def generate_json(self, model: str, system: str, prompt: str, schema: dict,
                      timeout: float | None = None) -> dict: ...


# 配額用完、模型過載、模型名稱不存在時改用下一個備援模型；其他錯誤（例如請求格式錯）不重試
FALLBACK_CODES = {404, 429, 500, 503, 504}
MIN_CALL_SECONDS = 5  # 剩下的時間不到這麼多就不再換模型


class GeminiLLM:
    def __init__(self, fallbacks: list[str] | None = None, timeout: float = 150):
        self.fallbacks = list(fallbacks or [])
        self.timeout = timeout  # 呼叫端沒指定時的總時限（150 秒，見 config.ai_timeout_seconds）
        self.last_model = ""

    def _call(self, model: str, system: str, prompt: str, schema: dict, timeout: float) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                              http_options=types.HttpOptions(timeout=int(timeout * 1000)))
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_json_schema=schema,
            ),
        )
        return resp.text or ""

    def generate_json(self, model: str, system: str, prompt: str, schema: dict,
                      timeout: float | None = None) -> dict:
        if not os.environ.get("GEMINI_API_KEY"):
            raise LLMError("找不到 GEMINI_API_KEY")
        timeout = self.timeout if timeout is None else timeout
        models = [model] + [m for m in self.fallbacks if m != model]
        deadline = time.monotonic() + timeout  # 逾時是整次呼叫（含備援）的總時間
        last_err: Exception | None = None
        for m in models:
            remaining = deadline - time.monotonic()
            if remaining < MIN_CALL_SECONDS:
                raise LLMError(f"文字模型逾時（{timeout:.0f} 秒）") from last_err
            try:
                text = self._call(m, system, prompt, schema, remaining)
            except Exception as e:  # 網路、逾時、配額、模型名稱錯誤
                last_err = e
                if getattr(e, "code", None) in FALLBACK_CODES:
                    continue
                break
            self.last_model = m
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                raise LLMError("文字模型回傳的不是 JSON") from e
            if not isinstance(data, dict):
                raise LLMError("文字模型回傳格式錯誤")
            return data
        raise LLMError(f"文字模型呼叫失敗：{last_err.__class__.__name__}: {str(last_err)[:200]}") from last_err
