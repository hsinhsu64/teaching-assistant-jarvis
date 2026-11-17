"""即時語音模型介面。語音層可抽換（spec §5.1）：其他模組只依賴 LiveBackend／LiveSession／LiveEvent。

工具定義用與廠商無關的 dict：{"name", "description", "parameters"(JSON Schema)}。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Protocol

EventKind = Literal["audio", "input_text", "output_text", "tool_call", "turn_complete", "interrupted", "closed"]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LiveEvent:
    kind: EventKind
    data: bytes = b""
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)


class LiveSession(Protocol):
    async def send_audio(self, pcm16k: bytes) -> None: ...
    async def send_tool_results(self, results: list[tuple[ToolCall, dict]]) -> None: ...
    def events(self) -> AsyncIterator[LiveEvent]: ...


class LiveBackend(Protocol):
    def connect(self, system: str, tools: list[dict]): ...  # async context manager → LiveSession


# ---- Gemini Live ----

def to_gemini_tools(tools: list[dict]):
    from google.genai import types

    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=t["name"], description=t.get("description", ""),
                                  parameters_json_schema=t.get("parameters") or {"type": "object", "properties": {}})
        for t in tools])]


def events_from_message(msg) -> list[LiveEvent]:
    """把 google-genai 的 LiveServerMessage 轉成 LiveEvent。"""
    out: list[LiveEvent] = []
    if getattr(msg, "tool_call", None):
        out.append(LiveEvent("tool_call", calls=[ToolCall(fc.id, fc.name, dict(fc.args or {}))
                                                 for fc in msg.tool_call.function_calls]))
    sc = getattr(msg, "server_content", None)
    if not sc:
        return out
    if sc.model_turn:
        for part in sc.model_turn.parts or []:
            if part.inline_data and part.inline_data.data:
                out.append(LiveEvent("audio", data=part.inline_data.data))
    if sc.input_transcription and sc.input_transcription.text:
        out.append(LiveEvent("input_text", text=sc.input_transcription.text))
    if sc.output_transcription and sc.output_transcription.text:
        out.append(LiveEvent("output_text", text=sc.output_transcription.text))
    if sc.interrupted:
        out.append(LiveEvent("interrupted"))
    if sc.turn_complete:
        out.append(LiveEvent("turn_complete"))
    return out


class _GeminiSession:
    def __init__(self, session):
        self._s = session

    async def send_audio(self, pcm16k: bytes) -> None:
        from google.genai import types

        await self._s.send_realtime_input(audio=types.Blob(data=pcm16k, mime_type="audio/pcm;rate=16000"))

    async def send_tool_results(self, results: list[tuple[ToolCall, dict]]) -> None:
        from google.genai import types

        await self._s.send_tool_response(function_responses=[
            types.FunctionResponse(id=c.id, name=c.name, response=r) for c, r in results])

    async def events(self) -> AsyncIterator[LiveEvent]:
        while True:  # receive() 每個回合結束就停，要重複呼叫
            async for msg in self._s.receive():
                for ev in events_from_message(msg):
                    yield ev


def live_config(system: str, tools: list[dict], silence_ms: int = 2000, voice: str = ""):
    """silence_ms：使用者停頓多久才算講完（使用者回饋：預設太短，講到一半就被打斷）。
    voice：固定的內建聲音名稱；不指定時每次連線可能換聲音（實機忽男忽女）。"""
    from google.genai import types

    speech = (types.SpeechConfig(voice_config=types.VoiceConfig(
        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))) if voice else None)
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=speech,
        system_instruction=system,
        tools=to_gemini_tools(tools),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                silence_duration_ms=silence_ms,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
            )),
    )


class GeminiLiveBackend:
    def __init__(self, model: str, silence_ms: int = 2000, voice: str = ""):
        self.model = model
        self.silence_ms = silence_ms
        self.voice = voice

    @asynccontextmanager
    async def connect(self, system: str, tools: list[dict]):
        from google import genai

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        config = live_config(system, tools, self.silence_ms, self.voice)
        async with client.aio.live.connect(model=self.model, config=config) as session:
            yield _GeminiSession(session)
