from types import SimpleNamespace as NS

from xiaoshen.live_backend import LiveEvent, ToolCall, events_from_message, to_gemini_tools


def msg(server_content=None, tool_call=None):
    return NS(server_content=server_content, tool_call=tool_call)


def sc(**kw):
    base = dict(model_turn=None, input_transcription=None, output_transcription=None,
                turn_complete=None, interrupted=None)
    base.update(kw)
    return NS(**base)


def test_sdk_message_to_events():
    audio = msg(sc(model_turn=NS(parts=[NS(inline_data=NS(data=b"\x01\x02"), text=None)])))
    assert events_from_message(audio) == [LiveEvent("audio", data=b"\x01\x02")]
    heard = msg(sc(input_transcription=NS(text="小森，請問"), output_transcription=NS(text="我查一下")))
    assert events_from_message(heard) == [LiveEvent("input_text", text="小森，請問"),
                                          LiveEvent("output_text", text="我查一下")]
    call = msg(tool_call=NS(function_calls=[NS(id="c1", name="next_page", args=None)]))
    assert events_from_message(call) == [LiveEvent("tool_call", calls=[ToolCall("c1", "next_page", {})])]
    done = msg(sc(turn_complete=True, interrupted=True))
    assert [e.kind for e in events_from_message(done)] == ["interrupted", "turn_complete"]
    assert events_from_message(msg()) == []


def test_tool_declarations_converted():
    tools = [{"name": "next_page", "description": "翻頁", "parameters": {"type": "object", "properties": {}}},
             {"name": "ask", "description": "查詢", "parameters": {"type": "object", "properties": {
                 "question": {"type": "string"}}, "required": ["question"]}}]
    out = to_gemini_tools(tools)
    decls = out[0].function_declarations
    assert [d.name for d in decls] == ["next_page", "ask"]
    assert decls[1].parameters_json_schema["required"] == ["question"]


def test_live_config_waits_for_silence():
    """使用者回饋：講到一半就被打斷 → 至少 2 秒沒說話才算講完。"""
    from xiaoshen.live_backend import live_config

    cfg = live_config("sys", [], silence_ms=2000)
    aad = cfg.realtime_input_config.automatic_activity_detection
    assert aad.silence_duration_ms == 2000
    assert aad.end_of_speech_sensitivity.name == "END_SENSITIVITY_LOW"
    assert cfg.input_audio_transcription is not None and cfg.response_modalities == ["AUDIO"]


def test_config_voice_silence_default():
    from xiaoshen.config import Config

    assert Config().voice_end_silence_ms == 2000


def test_live_config_pins_voice():
    # 實機：沒指定聲音時每次叫醒（新連線）可能忽男忽女；固定用男聲
    from xiaoshen.live_backend import live_config

    cfg = live_config("sys", [], silence_ms=2000, voice="Charon")
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Charon"


def test_backend_passes_voice_to_config():
    from xiaoshen.live_backend import GeminiLiveBackend

    assert GeminiLiveBackend("m", 2000, voice="Orus").voice == "Orus"


def test_config_live_voice_is_male_default_and_overridable(tmp_path):
    from xiaoshen.config import Config, load_config

    assert Config().live_voice  # 有預設值：不指定就會忽男忽女
    p = tmp_path / "config.toml"
    p.write_text('live_voice = "Puck"\n', encoding="utf-8")
    assert load_config(p).live_voice == "Puck"
