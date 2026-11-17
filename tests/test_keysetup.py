"""「設定金鑰」小程式：GEMINI_API_KEY 存成 Windows 使用者環境變數。"""

import pytest

from xiaoshen.keysetup import KeyError_, clean_key, setup_key

KEY = "AIza" + "SyA1234567890abcdefghijklmnopqrstu"  # 假金鑰；拼接寫法，測試檔本身不含完整的金鑰樣式


def test_clean_key_strips_and_rejects_bad_input():
    assert clean_key(f'  "{KEY}"\n') == KEY
    for bad in ["", "   ", "AIza abc", "金鑰"]:
        with pytest.raises(KeyError_):
            clean_key(bad)


def run(answers, verify="ok"):
    saved, said = {}, []
    code = setup_key(ask=lambda: answers.pop(0), verify=lambda k: verify,
                     save=lambda name, value: saved.update({name: value}), say=said.append)
    return code, saved, " ".join(said)


def test_valid_key_is_saved():
    code, saved, said = run([KEY])
    assert code == 0 and saved == {"GEMINI_API_KEY": KEY} and "已存好" in said
    assert KEY not in said  # 不把金鑰印出來


def test_rejected_key_is_not_saved():
    code, saved, said = run([KEY], verify="invalid")
    assert code == 1 and saved == {} and "無效" in said


def test_offline_still_saves_with_warning():
    code, saved, said = run([KEY], verify="unreachable")
    assert code == 0 and saved and "沒辦法確認" in said


def test_bad_format_asks_again():
    code, saved, said = run(["  ", KEY])
    assert code == 0 and saved == {"GEMINI_API_KEY": KEY}


def test_mac_message_says_close_terminal(monkeypatch):
    from xiaoshen import keysetup
    monkeypatch.setattr(keysetup, "IS_MAC", True)
    code, saved, said = run([KEY])
    assert code == 0 and "Terminal" in said and "~/.zprofile" in said
    monkeypatch.setattr(keysetup, "IS_MAC", False)
    _, _, said = run([KEY])
    assert "Terminal" not in said
