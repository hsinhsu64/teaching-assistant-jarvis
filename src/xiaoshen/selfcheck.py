"""開機自我檢查：問題要在晨會開始前暴露。任何一項檢查出錯都不影響其他項。"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Callable, Literal

from .config import FOLDERS, Config
from . import persona
from .platform_support import IS_MAC
from .state import AppState

if TYPE_CHECKING:
    from .library import Library

Level = Literal["ok", "warn", "fail", "pending"]  # pending：還在背景確認（手機作答）


@dataclass
class CheckResult:
    name: str
    level: Level
    detail: str
    hint: str = ""  # 紅燈／黃燈時給使用者的「怎麼修」；綠燈留空

    def to_dict(self) -> dict:
        return asdict(self)


# ---- 「怎麼修」協助訊息（主持人畫面顯示在該列下方）----

RESTART = "按「結束程式」再重新啟動"


def _key_script() -> str:
    return "set-key.command" if IS_MAC else "set-key.bat"


def _network_hint() -> str:
    return (f"確認電腦已連上網路；醫院網路若擋連線，改接手機熱點後{RESTART}。"
            f"錯誤訊息若提到金鑰（API key），請雙擊安裝資料夾裡的 {_key_script()} 重新設定 Gemini 金鑰。")


def _mic_permission_hint() -> str:
    if IS_MAC:
        return "到「系統設定 → 隱私權與安全性 → 麥克風」允許「終端機」"
    return "到 Windows「設定 → 隱私權與安全性 → 麥克風」開啟「讓桌面應用程式存取您的麥克風」"


def _mic_hint(config: Config, error: Exception) -> str:
    if config.audio_device and "找不到名稱含" in str(error):
        return (f"插上那支麥克風後{RESTART}；或到下方「設定」的「麥克風」改選「電腦預設」或其他麥克風，"
                "儲存後重新啟動。")
    if IS_MAC:
        return f"確認麥克風已接上，並{_mic_permission_hint()}，然後重新啟動程式。"
    return (f"確認麥克風已接上，並在 Windows「設定 → 系統 → 音效 → 輸入」選一支麥克風；"
            f"再{_mic_permission_hint()}，然後重新啟動程式。")


PHONE_HINT = (f"醫院網路可能擋住手機作答的對外通道：改接手機熱點後{RESTART}。"
              "程式每 30 秒會再試一次，連上後自動變綠燈。")
HINT_CRASHED = "請在下方「設定」按「產生診斷包」，把桌面上的 zip 寄給開發者。"
HINT_DECK_EXPORT = "請從 PowerPoint 直接匯出 PDF（不要用掃描或列印成圖片），文字才讀得到。"


def _default_http_get(url: str) -> int:
    import httpx

    return httpx.get(url, timeout=8, follow_redirects=True).status_code


AUDIO_DEVICE_CHECKS = {"麥克風", "喇叭", "喚醒詞"}  # 用到音效裝置：自我檢查時一項一項來

PHONE_FIRST_CHECK_DELAY = 10.0  # 網址建立後先等幾秒才第一次查（太早查，有些網路會把「查不到」記一陣子）
PHONE_RETRY_SECONDS = 3.0       # 還連不上時每隔幾秒再試
PHONE_GIVE_UP_SECONDS = 120.0   # 網址建立後多久還連不上才亮紅燈


def _set_phone_row(state: AppState, level: Level, detail: str, hint: str) -> None:
    state.phone_pending = False
    state.phone_reachable = level == "ok"
    for check in state.checks:
        if check.get("name") == "手機作答":
            check.update(level=level, detail=detail, hint=hint)
    state._changed()


async def confirm_phone_link(state: AppState, http_get: Callable[[str], int], *, not_before: float,
                             give_up_at: float, interval: float = PHONE_RETRY_SECONDS,
                             clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep) -> bool:
    """開機時在背景確認手機作答網址，不拖住其他自我檢查。

    等到 not_before 才第一次查，之後每 interval 秒試一次：連上就變綠燈、投影出現 QR Code；
    到 give_up_at 還連不上才亮紅燈（之後交給每 30 秒的定期檢查）。"""
    if not state.public_base:
        return False
    if (wait := not_before - clock()) > 0:
        await sleep(wait)
    while True:
        try:
            status = await asyncio.to_thread(http_get, state.public_base + "/healthz")
            problem = "" if status == 200 else f"連線回應 {status}"
        except Exception as e:
            problem = f"連線失敗：{e}"
        if not problem:
            _set_phone_row(state, "ok", state.public_base, "")
            return True
        if clock() + interval > give_up_at:
            _set_phone_row(state, "fail", f"手機無法作答（{problem}）", PHONE_HINT)
            return False
        await sleep(interval)


def _root(config: Config, state: AppState) -> CheckResult:
    if config.data_root.is_dir():
        return CheckResult("資料夾", "ok", str(config.data_root))
    return CheckResult("資料夾", "fail", f"找不到資料根目錄：{config.data_root}",
                       "確認雲端硬碟（例如 Google Drive）已啟動並同步完成，資料夾也沒有被移走或改名。"
                       "要改用其他資料夾：結束程式，用文字編輯器把 config.toml 裡的 data_root 改成新位置，再重新啟動。")


def _subfolders(config: Config, state: AppState) -> CheckResult:
    missing = [n for n in FOLDERS if not config.folder(n).is_dir()]
    if missing:
        return CheckResult("子資料夾", "warn", "缺少：" + "、".join(missing),
                           f"在 {config.data_root} 裡建立缺少的資料夾（名稱要完全相同），再重新啟動程式。")
    return CheckResult("子資料夾", "ok", "、".join(FOLDERS))


def _deck(config: Config, state: AppState) -> CheckResult:
    entries = state.list_deck_files()
    if not entries:
        return CheckResult("簡報", "warn", "「簡報」資料夾內沒有簡報（PDF 或 pptx）",
                           f"把今天的簡報放進「簡報」資料夾（{config.folder('簡報')}），再重新啟動程式。")
    if state.deck is None and entries[0].suffix.lower() != ".pdf":  # 開機不轉 pptx（播放時才轉）
        return CheckResult("簡報", "ok", f"{entries[0].stem}（pptx：開機後在背景轉成 PDF，"
                                         "播放時還沒轉好會先提醒）")
    if state.deck is None:
        return CheckResult("簡報", "warn", "簡報無法開啟（見下方警告）",
                           "檔案可能損壞或還在同步：等雲端硬碟同步完成後重新啟動；仍打不開就重新匯出 PDF。")
    if state.deck.is_scanned():
        return CheckResult("簡報", "warn", f"{state.deck.name}：此檔為掃描檔，第一版無法讀取文字內容",
                           HINT_DECK_EXPORT)
    return CheckResult("簡報", "ok", f"{state.deck.name}（{state.deck.page_count} 頁）")


def references_check(library: "Library", warnings: list[str]) -> CheckResult:
    n = len(library.docs)
    if n == 0:
        # 沒有參考資料是正常用法（只用簡報或 OpenEvidence），不需要「怎麼修」
        return CheckResult("參考資料", "warn", "「參考資料」資料夾沒有 PDF；只能用簡報或 OpenEvidence 回答")
    if warnings:
        return CheckResult("參考資料", "warn", f"{n} 份；" + "；".join(warnings[:5]),
                           "依上面列出的原因處理那些檔案（例如掃描檔請換成能選取文字的 PDF），再重新放進「參考資料」資料夾。")
    return CheckResult("參考資料", "ok", f"{n} 份")


def openevidence_result(logged_in: bool, tab: bool | None) -> CheckResult:
    """OpenEvidence 那一列。tab：瀏覽器分頁的 relay 有沒有連上（None＝不檢查分頁）。
    開機與主持人按「重新檢查」共用。"""
    if tab is None:
        if logged_in:
            return CheckResult("OpenEvidence", "ok", "登入有效（提問若被驗證擋下會另外提示）")
        return CheckResult("OpenEvidence", "warn", "搜尋路線停用：OpenEvidence 未登入或 cookies 過期")
    open_tab = "請打開瀏覽器，並保持一個已登入的 OpenEvidence 分頁"
    if tab and logged_in:
        return CheckResult("OpenEvidence", "ok", "經瀏覽器的 OpenEvidence 分頁連線，登入有效")
    if tab:
        return CheckResult("OpenEvidence", "warn", "瀏覽器的 OpenEvidence 分頁沒有登入：請在分頁登入 OpenEvidence")
    if logged_in:
        return CheckResult("OpenEvidence", "warn",
                           f"沒有連到瀏覽器的 OpenEvidence 分頁：{open_tab}（目前暫用 cookies，提問可能被驗證擋下）")
    return CheckResult("OpenEvidence", "warn", f"搜尋路線停用：{open_tab}")


def run_checks(config: Config, state: AppState,
               bank_warnings: list[str] | None = None, llm_check: Callable[[], str] | None = None,
               oe_check: Callable[[], bool] | None = None, library: "Library | None" = None,
               oe_tab_check: Callable[[], bool] | None = None,
               library_warnings: list[str] | None = None,
               mic_check: Callable[[], float] | None = None, speaker_check: Callable[[], bool] | None = None,
               wake_check: Callable[[], bool] | None = None,
               live_check: Callable[[], float] | None = None) -> list[CheckResult]:
    """llm_check／oe_check／library 有傳才檢查（Phase 2 的 AI 項目）。"""
    bank_warnings = bank_warnings or []

    def bank(config: Config, state: AppState) -> CheckResult:
        problems = list(bank_warnings)
        known = {p.stem for p in state.list_deck_files()}
        unknown = sorted({name for name, _ in state.bank if name not in known})
        if unknown:
            problems.append("找不到這些簡報（檔名要與 PDF 相同）：" + "、".join(unknown))
        if problems:
            return CheckResult("題庫", "warn", "；".join(problems[:5]),
                               "依上面的說明修正題庫檔，再重新啟動程式；題目對應的簡報名稱要和 PDF 檔名完全相同（不含 .pdf）。")
        return CheckResult("題庫", "ok", f"{len(state.bank)} 題")

    def phone(config: Config, state: AppState) -> CheckResult:
        if not state.public_base:
            return CheckResult("手機作答", "fail", "對外通道未啟動，手機無法作答", PHONE_HINT)
        if state.phone_pending:  # 新網址要一段時間才查得到：由 confirm_phone_link 在背景確認
            return CheckResult("手機作答", "pending", "網址剛建立，正在確認手機連得上…")
        if state.phone_reachable:
            return CheckResult("手機作答", "ok", state.public_base)
        return CheckResult("手機作答", "fail", "對外通道中斷，手機暫時無法作答", PHONE_HINT)

    def conversion(config: Config, state: AppState) -> CheckResult:
        c = state.conversion_check
        hint = "" if c["level"] == "ok" else (
            "在 PowerPoint 打開這份簡報，另存成 PDF 放進「簡報」資料夾；"
            "若是轉檔失敗，先關掉 PowerPoint 裡開著的這份檔案，程式會自動重試。")
        return CheckResult(c["name"], c["level"], c["detail"], hint)

    def ai_model(config: Config, state: AppState) -> CheckResult:
        try:
            used = llm_check()
        except Exception as e:
            return CheckResult("AI 文字模型", "fail", f"{e}（無法回答與出題）", _network_hint())
        if used != config.model_answer:
            return CheckResult("AI 文字模型", "warn",
                               f"{config.model_answer} 無法使用，已改用備援 {used}（Pro 需付費方案）",
                               "不影響使用；要用 Pro 模型需要付費方案的 Gemini 金鑰。")
        return CheckResult("AI 文字模型", "ok", used)

    def openevidence(config: Config, state: AppState) -> CheckResult:
        return openevidence_result(oe_check(), oe_tab_check() if oe_tab_check is not None else None)

    def references(config: Config, state: AppState) -> CheckResult:
        return references_check(library, library_warnings or [])

    def mic(config: Config, state: AppState) -> CheckResult:
        try:
            rms = mic_check()
        except Exception as e:
            return CheckResult("麥克風", "fail", f"找不到麥克風，語音停用（{e}）", _mic_hint(config, e))
        if rms <= 0:
            hint = (f"檢查麥克風的靜音鍵與音量，並{_mic_permission_hint()}，然後重新啟動程式；"
                    "也可以到下方「設定」改選其他麥克風。")
            return CheckResult("麥克風", "warn", "麥克風沒有聲音（靜音或沒插好？）", hint)
        return CheckResult("麥克風", "ok", f"音量 {rms:.4f}")

    def speaker(config: Config, state: AppState) -> CheckResult:
        if speaker_check():
            return CheckResult("喇叭", "ok", "可播放")
        return CheckResult("喇叭", "warn", f"找不到喇叭，{persona.current().display_name}只能顯示、不能說話",
                           f"接上那個喇叭後{RESTART}；或到下方「設定」的「喇叭」改選「電腦預設」，儲存後重新啟動。")

    def wake(config: Config, state: AppState) -> CheckResult:
        res = wake_check()  # True/False，或 (實際使用的喚醒詞, 退回預設的警告或 None)
        if res:
            label, warning = res if isinstance(res, tuple) else (config.effective_wake_word, None)
            if warning:
                return CheckResult("喚醒詞", "warn", f"{warning}；請說「{label}」「下一頁」（本機辨識）",
                                   "到下方「設定」改用 2 到 4 個中文字的喚醒詞，儲存後重新啟動。")
            return CheckResult("喚醒詞", "ok", f"「{label}」「下一頁」本機辨識")
        return CheckResult("喚醒詞", "fail", "喚醒詞模型無法載入，語音停用（可用按鈕叫醒也一併停用）",
                           "重新執行一次安裝指令（設定與資料都會保留），再重新啟動程式；這段期間仍可打字提問。")

    def live(config: Config, state: AppState) -> CheckResult:
        try:
            seconds = live_check()
        except Exception as e:
            return CheckResult("語音模型", "fail", f"{config.live_model} 連不上，語音停用（簡報與作答照常）：{str(e)[:120]}",
                               _network_hint())
        return CheckResult("語音模型", "ok", f"{config.live_model}（{seconds:.1f} 秒）")

    checks = [("資料夾", _root), ("子資料夾", _subfolders), ("簡報", _deck), ("題庫", bank), ("手機作答", phone)]
    if state.conversion_check:
        checks.insert(3, ("簡報轉檔", conversion))
    if library is not None:
        checks.append(("參考資料", references))
    if llm_check is not None:
        checks.append(("AI 文字模型", ai_model))
    if oe_check is not None:
        checks.append(("OpenEvidence", openevidence))
    for name, fn, given in (("麥克風", mic, mic_check), ("喇叭", speaker, speaker_check),
                            ("喚醒詞", wake, wake_check), ("語音模型", live, live_check)):
        if given is not None:
            checks.append((name, fn))
    def run_one(name: str, check) -> CheckResult:
        try:
            return check(config, state)
        except Exception as e:
            return CheckResult(name, "fail", f"檢查失敗：{e}", HINT_CRASHED)

    # 同時檢查：總時間約等於最慢的那一項。用到音效裝置的幾項排在同一條線上依序跑，不互相搶裝置
    audio = [i for i, (name, _) in enumerate(checks) if name in AUDIO_DEVICE_CHECKS]
    lanes = [[i] for i in range(len(checks)) if i not in audio] + ([audio] if audio else [])
    results: list[CheckResult | None] = [None] * len(checks)

    def run_lane(lane: list[int]) -> None:
        for i in lane:
            results[i] = run_one(*checks[i])

    with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        for future in [pool.submit(run_lane, lane) for lane in lanes]:
            future.result()
    return results
