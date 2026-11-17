import pytest

from xiaoshen import quizgen, summary, voice_tools
from xiaoshen.answering import derived_label, general_knowledge_system, interpret_system, note_label
from xiaoshen.config import Config
from xiaoshen.persona import MEETING_TYPES, Persona, context, current, meeting_label, set_persona


@pytest.mark.parametrize("spec,mt,other,expected", [
    ("兒科腎臟科", "病例討論", "", "兒科腎臟科病例討論"),
    ("兒科腎臟科", "Journal reading", "", "兒科腎臟科 Journal reading"),
    ("兒科腎臟科", "其它", "", "兒科腎臟科醫學教學"),
    ("兒科腎臟科", "其它", "科內讀書會", "兒科腎臟科科內讀書會"),
    ("", "病例討論", "", "病例討論"),
    ("", "其它", "", "醫學教學"),
])
def test_context(spec, mt, other, expected):
    assert context(Persona("x", spec, mt, other)) == expected


def test_meeting_label():
    for mt in MEETING_TYPES[:-1]:
        assert meeting_label(Persona(meeting_type=mt)) == mt
    assert meeting_label(Persona(meeting_type="其它", meeting_type_other="  讀書會 ")) == "讀書會"


def test_prompts_have_no_fragments():
    set_persona(Persona("賈維斯", "", "其它", ""))
    for text in (interpret_system(), general_knowledge_system(), quizgen.system_prompt(),
                 voice_tools.system_instruction()):
        assert "你是的" not in text and "在的" not in text and "住院醫師" not in text
        assert "晨會" not in text


def test_summary_prompt_audience():
    text = summary.system_prompt()
    assert "與會人員" in text and "住院醫師" not in text and "晨會" not in text


def test_prompts_use_context_and_name():
    set_persona(Persona("阿腎", "兒科腎臟科", "Journal reading"))
    assert "兒科腎臟科 Journal reading" in interpret_system()
    assert "你是兒科腎臟科 Journal reading的教學助理" in general_knowledge_system()
    assert "你是兒科腎臟科 Journal reading的出題助手" in quizgen.system_prompt()
    assert "給與會人員的簡報" in summary.system_prompt()
    assert "你是兒科腎臟科 Journal reading的助理阿腎" in voice_tools.system_instruction()
    assert "只叫你的名字或喚醒詞「賈維斯」、還沒說要求時（逐字稿可能只有這個詞，或被聽成發音相近的字）" in voice_tools.system_instruction()


def test_wake_word_shown_separately_from_name():
    """名字和喚醒詞不同時（名字超過 4 字、或喚醒詞退回預設），畫面要叫大家喊喚醒詞（I-2）。"""
    from xiaoshen.persona import app_info, start_from_config, wake_word

    start_from_config(Config(display_name="腎臟科教學助理", wake_word="小助", specialty="腎臟科"))
    assert current().display_name == "腎臟科教學助理" and current().specialty == "腎臟科"
    assert wake_word() == "小助"
    assert app_info() == {"name": "腎臟科教學助理", "wake": "小助", "audience": "與會人員"}
    start_from_config(Config(display_name="阿腎"))
    assert app_info()["wake"] == "阿腎"  # 喚醒詞留空＝同名字


def test_voice_rule_six_uses_wake_word():
    from xiaoshen.persona import set_wake_word

    set_persona(Persona("腎臟科教學助理"))
    set_wake_word("小助")
    text = voice_tools.system_instruction()
    assert "你是病例討論的助理腎臟科教學助理" in text  # 名字照舊用顯示名稱
    assert "只叫你的名字或喚醒詞「小助」" in text


def test_settings_save_keeps_running_wake_word():
    """設定存檔只換專科、會議類型；喚醒詞與名字一樣要重新啟動才換。"""
    from xiaoshen.persona import set_wake_word, wake_word

    set_wake_word("小助")
    set_persona(Persona("甲", "內科"))
    assert wake_word() == "小助"


def test_name_in_labels():
    set_persona(Persona("小森"))
    assert derived_label() == "小森推導（未經主治確認）："
    assert note_label() == "小森說明（未經主治確認）："


def test_set_persona_and_default():
    assert current() == Persona()  # conftest 每個測試前重設
    set_persona(Persona("甲"))
    assert current().display_name == "甲"


def test_unknown_meeting_type_falls_back():
    assert Persona.from_config(Config(meeting_type="亂打")).meeting_type == "病例討論"
    assert Persona.from_config(Config(display_name="甲", specialty="內科")).specialty == "內科"


def test_footer_regex_follows_name():
    set_persona(Persona("阿腎 (v2)"))
    rx = voice_tools._footer_line_re()
    assert rx.match("阿腎 (v2) · 字卡") and rx.match("3 / 10") and not rx.match("小森 · 字卡")
