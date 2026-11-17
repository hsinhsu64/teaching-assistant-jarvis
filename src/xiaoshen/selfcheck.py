"""開機自我檢查：問題要在晨會開始前暴露。任何一項檢查出錯都不影響其他項。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Callable, Literal

from .config import FOLDERS, Config
from . import persona
from .platform_support import IS_MAC
from .deck import list_decks
from .state import AppState

if TYPE_CHECKING:
    from .library import Library

Level = Literal["ok", "warn", "fail"]


@dataclass
class CheckResult:
    name: str
    level: Level
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _default_http_get(url: str) -> int:
    import httpx

    return httpx.get(url, timeout=8, follow_redirects=True).status_code


def _root(config: Config, state: AppState) -> CheckResult:
    if config.data_root.is_dir():
        return CheckResult("資料夾", "ok", str(config.data_root))
    return CheckResult("資料夾", "fail", f"找不到資料根目錄：{config.data_root}")


def _subfolders(config: Config, state: AppState) -> CheckResult:
    missing = [n for n in FOLDERS if not config.folder(n).is_dir()]
    if missing:
        return CheckResult("子資料夾", "warn", "缺少：" + "、".join(missing))
    return CheckResult("子資料夾", "ok", "、".join(FOLDERS))


def _deck(config: Config, state: AppState) -> CheckResult:
    if not list_decks(config.folder("簡報")):
        return CheckResult("簡報", "warn", "「簡報」資料夾內沒有 PDF")
    if state.deck is None:
        return CheckResult("簡報", "warn", "簡報無法開啟（見下方警告）")
    if state.deck.is_scanned():
        return CheckResult("簡報", "warn", f"{state.deck.name}：此檔為掃描檔，第一版無法讀取文字內容")
    return CheckResult("簡報", "ok", f"{state.deck.name}（{state.deck.page_count} 頁）")


def references_check(library: "Library", warnings: list[str]) -> CheckResult:
    n = len(library.docs)
    if n == 0:
        return CheckResult("參考資料", "warn", "「參考資料」資料夾沒有 PDF；只能用簡報或 OpenEvidence 回答")
    if warnings:
        return CheckResult("參考資料", "warn", f"{n} 份；" + "；".join(warnings[:5]))
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


def run_checks(config: Config, state: AppState, http_get: Callable[[str], int] | None = None,
               bank_warnings: list[str] | None = None, phone_retries: int = 1,
               retry_delay: float = 3.0, llm_check: Callable[[], str] | None = None,
               oe_check: Callable[[], bool] | None = None, library: "Library | None" = None,
               oe_tab_check: Callable[[], bool] | None = None,
               library_warnings: list[str] | None = None,
               mic_check: Callable[[], float] | None = None, speaker_check: Callable[[], bool] | None = None,
               wake_check: Callable[[], bool] | None = None,
               live_check: Callable[[], float] | None = None) -> list[CheckResult]:
    """llm_check／oe_check／library 有傳才檢查（Phase 2 的 AI 項目）。"""
    http_get = http_get or _default_http_get
    bank_warnings = bank_warnings or []

    def bank(config: Config, state: AppState) -> CheckResult:
        problems = list(bank_warnings)
        known = {p.stem for p in list_decks(config.folder("簡報"))}
        unknown = sorted({name for name, _ in state.bank if name not in known})
        if unknown:
            problems.append("找不到這些簡報（檔名要與 PDF 相同）：" + "、".join(unknown))
        if problems:
            return CheckResult("題庫", "warn", "；".join(problems[:5]))
        return CheckResult("題庫", "ok", f"{len(state.bank)} 題")

    def phone(config: Config, state: AppState) -> CheckResult:
        if not state.public_base:
            return CheckResult("手機作答", "fail", "對外通道未啟動，手機無法作答")
        # 新開的 trycloudflare 網址要幾秒 DNS 才生效，啟動時多試幾次
        problem = ""
        for attempt in range(max(phone_retries, 1)):
            if attempt:
                time.sleep(retry_delay)
            try:
                status = http_get(state.public_base + "/healthz")
            except Exception as e:
                problem = f"連線失敗：{e}"
                continue
            if status == 200:
                return CheckResult("手機作答", "ok", state.public_base)
            problem = f"連線回應 {status}"
        return CheckResult("手機作答", "fail", f"手機無法作答（{problem}）")

    def conversion(config: Config, state: AppState) -> CheckResult:
        c = state.conversion_check
        return CheckResult(c["name"], c["level"], c["detail"])

    def ai_model(config: Config, state: AppState) -> CheckResult:
        try:
            used = llm_check()
        except Exception as e:
            return CheckResult("AI 文字模型", "fail", f"{e}（無法回答與出題）")
        if used != config.model_answer:
            return CheckResult("AI 文字模型", "warn",
                               f"{config.model_answer} 無法使用，已改用備援 {used}（Pro 需付費方案）")
        return CheckResult("AI 文字模型", "ok", used)

    def openevidence(config: Config, state: AppState) -> CheckResult:
        return openevidence_result(oe_check(), oe_tab_check() if oe_tab_check is not None else None)

    def references(config: Config, state: AppState) -> CheckResult:
        return references_check(library, library_warnings or [])

    def mic(config: Config, state: AppState) -> CheckResult:
        try:
            rms = mic_check()
        except Exception as e:
            return CheckResult("麥克風", "fail", f"找不到麥克風，語音停用（{e}）")
        if rms <= 0:
            detail = "麥克風沒有聲音（靜音或沒插好？）"
            if IS_MAC:
                detail += "Mac 請到 系統設定 → 隱私權與安全性 → 麥克風，允許「終端機」"
            return CheckResult("麥克風", "warn", detail)
        return CheckResult("麥克風", "ok", f"音量 {rms:.4f}")

    def speaker(config: Config, state: AppState) -> CheckResult:
        if speaker_check():
            return CheckResult("喇叭", "ok", "可播放")
        return CheckResult("喇叭", "warn", f"找不到喇叭，{persona.current().display_name}只能顯示、不能說話")

    def wake(config: Config, state: AppState) -> CheckResult:
        res = wake_check()  # True/False，或 (實際使用的喚醒詞, 退回預設的警告或 None)
        if res:
            label, warning = res if isinstance(res, tuple) else (config.effective_wake_word, None)
            if warning:
                return CheckResult("喚醒詞", "warn", f"{warning}；請說「{label}」「下一頁」（本機辨識）")
            return CheckResult("喚醒詞", "ok", f"「{label}」「下一頁」本機辨識")
        return CheckResult("喚醒詞", "fail", "喚醒詞模型無法載入，語音停用（可用按鈕叫醒也一併停用）")

    def live(config: Config, state: AppState) -> CheckResult:
        try:
            seconds = live_check()
        except Exception as e:
            return CheckResult("語音模型", "fail", f"{config.live_model} 連不上，語音停用（簡報與作答照常）：{str(e)[:120]}")
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
    results = []
    for name, check in checks:
        try:
            results.append(check(config, state))
        except Exception as e:
            results.append(CheckResult(name, "fail", f"檢查失敗：{e}"))
    return results
