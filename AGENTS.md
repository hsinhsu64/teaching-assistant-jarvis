# AGENTS.md：給 AI coding agent 的工作說明

你是在一位老師自己的電腦（多半是 Mac）上，幫忙排除「教學助理賈維斯」安裝或執行問題的 AI coding agent。
你可以在本機除錯、改程式、跑測試，再把修正以 Pull Request（PR）送到 `hsinhsu64/teaching-assistant-jarvis`。
動手前請讀完這份說明，尤其是「硬性規則」。

## 先知道兩件事

- **安裝資料夾不是 git repo。** 一行安裝指令把程式放在 `~/TeachingAssistantJarvis`（Windows：`%USERPROFILE%\TeachingAssistantJarvis`），
  重新執行安裝指令（更新）時，那裡的程式會整個換新，但會保留 `config.toml`、`tools/` 與 `.venv`。在那裡試改可以，但要送 PR，
  請另外把你的 fork clone 到別的資料夾，在那裡改、跑測試、commit。
- **PR 不會直接在 GitHub 上合併。** 見最後一節「PR 送出之後」。

## 專案結構

Python 套件在 `src/xiaoshen/`（套件名沿用 `xiaoshen`，請不要改名）。主要模組：

| 用途 | 模組 |
|---|---|
| 啟動、視窗、自我檢查 | `launcher.py`（啟動兩個 app、對外通道、開投影與主持人視窗）、`runner.py`、`first_run.py`、`selfcheck.py`、`__main__.py` |
| 平台層（所有只屬於某個作業系統的程式） | `platform_support.py` |
| 簡報與參考資料 | `deck.py`、`converter.py`（.pptx／.ppt 轉 PDF）、`render_proc.py`、`library.py`、`slide_ocr.py` |
| 回答與出處驗證 | `answering.py`、`grounding.py`、`card.py`、`jobs.py`、`llm.py`、`pubmed.py`、`source_view.py`、`source_text.py`、`summary.py`、`summary_deck.py` |
| OpenEvidence | `oe_client.py`、`oe_relay.py`、`oe_setup.py`、`oe_deck.py` |
| 語音 | `voice.py`、`voice_tools.py`、`live_backend.py`、`wakeword.py`、`audio_io.py`、`vocabulary.py`、`zh.py` |
| 題目與作答 | `quiz.py`、`quizbank.py`、`quizgen.py`、`records.py`、`state.py` |
| 伺服器與手機 | `server.py`、`hub.py`、`tunnel.py`、`phone_control.py`、`phone_mic.py`、`host_phone.py` |
| 設定、隱私、診斷 | `config.py`、`settings_store.py`、`persona.py`、`keysetup.py`、`phi.py`、`diagnostics.py`、`eventlog.py` |

其他：

- `web/`：投影畫面、主持人畫面、手機頁面（HTML／JS／CSS）。
- `tests/`：pytest 測試；`conftest.py`、`fakes.py`、`fake_live.py` 是共用的假物件。
- `scripts/get_wake_model.py`（下載喚醒詞模型）、`scripts/wake_test.py`（在現場麥克風測喚醒詞）。
- 根目錄：`install.sh`／`install.ps1`（一行安裝指令實際執行的腳本）、`versions.env`（安裝時下載的工具版本）、
  `start.command`／`set-key.command`（Mac）、`start.bat`／`set-key.bat`（Windows）、`config.example.toml`（設定範本）。

### macOS 專屬的地方

Mac 的問題多半出在這些檔案：

- `src/xiaoshen/platform_support.py`：`IS_MAC` 分支，例如應用程式資料夾（`~/Library/Application Support/xiaoshen`）、
  瀏覽器位置、螢幕座標（AppKit `NSScreen`，換算在純函式 `mac_rects_from_frames`）、金鑰寫進 `~/.zprofile`（`upsert_env_block`）。
- `src/xiaoshen/converter.py`：Mac 用 AppleScript（`osascript`）叫 PowerPoint 把簡報存成 PDF。
- `install.sh`：Mac 的安裝腳本（macOS 內建 bash 3.2，不能用 bash 4 語法）。
- `start.command`、`set-key.command`：Mac 雙擊啟動、設定金鑰。
- `src/xiaoshen/launcher.py`：投影與主持人視窗的位置（`pick_display_position`、`display_args`）。兩個平台預設都用
  kiosk（全螢幕）投影視窗；Mac 上視窗位置有問題時，可在 `config.toml` 設 `display_kiosk = false`，
  改開一般 app 視窗，由主持人拖到投影機並全螢幕。
- `src/xiaoshen/keysetup.py`、`src/xiaoshen/selfcheck.py` 裡的 `IS_MAC` 分支。

Mac 的部分盡量寫成純函式，讓 Windows 上也能測；AppKit 只在 Mac 分支裡才 import。

## 安裝與執行

需要 [uv](https://docs.astral.sh/uv/)（一行安裝指令會裝在 `~/.local/bin/uv`）。在 repo 根目錄：

```
uv sync                                   # 建立 .venv、安裝相依套件
uv run python scripts/get_wake_model.py   # 下載喚醒詞模型（每台電腦一次，已存在就略過）
./start.command                           # Mac：會從 ~/.zprofile 的助理區塊載入金鑰再啟動
uv run python -m xiaoshen                 # 或直接啟動（環境變數裡要先有 GEMINI_API_KEY）
```

設定在 `config.toml`（本機檔案，已列在 `.gitignore`、不進 git；範本是 `config.example.toml`）。還沒設定資料夾位置（`data_root`）時，啟動會先跳出設定視窗。

## 跑測試

```
uv run pytest -q
```

全部要通過才送 PR。測試不需要金鑰、麥克風或 PowerPoint（Gemini、OpenEvidence 等外部服務都用假物件）；
有些測試會依環境自動略過（例如 PowerShell 解析只在 Windows 跑、喚醒詞模型沒下載時略過真模型測試）。
公開 repo 的 GitHub Actions 會在 Windows 與 macOS 各跑一次同樣的測試。

## 讀診斷資料

- **診斷包**：在主持人畫面的「設定」區按「產生診斷包」，桌面會出現 `診斷包-YYYYMMDD-HHMM.zip`。
  裡面只有：`about.json`（程式版本、系統、Python 版本）、`selfcheck.json`（自我檢查結果，資料夾路徑已去識別）、
  去掉金鑰類設定、資料夾路徑只留最後一層的 `config.toml`、最近 7 天的事件記錄 `events-*.jsonl`。不含金鑰、環境變數、簡報與參考資料。
- **事件記錄**：macOS 在 `~/Library/Application Support/xiaoshen/logs/events-YYYY-MM-DD.jsonl`
  （Windows：`%LOCALAPPDATA%\xiaoshen\logs`）。一行一筆 JSON，只記事件種類、工具名稱、狀態與頁碼，不記問題內容。
- **自我檢查**：主持人畫面上的紅燈／黃燈，會寫出問題與處理方式。
- 啟動 `start.command` 的終端機視窗也會印出錯誤訊息。

診斷包雖然已去識別，仍然**不要**把它附到 PR 或貼到公開的地方；只摘錄和問題有關、確認不含個人資料的幾行。

## 硬性規則

1. **絕不 commit 或貼出**：`config.toml`、任何 API 金鑰（`GEMINI_API_KEY` 只從環境變數讀取，不寫進任何檔案或程式碼）、
   cookie、診斷包，或任何個人資料（病人資訊、姓名、email、含使用者名稱的本機路徑）。log 與截圖要先檢查、遮掉。
2. **絕不削弱隱私掃描或任何隱私防護**：送雲端前的識別資訊檢查（`phi.py`）、診斷包的去識別（`diagnostics.py`）、
   待命時聲音只在本機辨識（`wakeword.py`）。維護者發行前會跑隱私掃描；不要改寫測試或程式去繞過這些檢查。
3. **保持行尾**：`.bat` 是 CRLF；`.sh` 與 `.command` 是 LF（而且要保留執行權限）。clone 前先
   `git config --global core.autocrlf false`，也別讓編輯器自動轉換。測試會檢查。
4. **使用者看得到的文字一律用台灣繁體中文**（畫面、語音、錯誤訊息、安裝腳本的提示）。技術名詞可保留英文。
5. **一個 PR 只處理一個問題。** 不要順手重構、改格式或改無關的檔案。
6. 不要輕易加相依套件；真的需要時用 `uv add` 更新 `pyproject.toml` 與 `uv.lock`，並在 PR 說明理由。

## 如何送出修改

1. 在 GitHub 上 fork `hsinhsu64/teaching-assistant-jarvis`，clone 你的 fork。
2. 從 `main` 開一個新 branch（例如 `fix/mac-display-window`）。
3. 先寫一個會失敗的測試重現問題，再修改程式讓它通過；跑 `uv run pytest -q`。
4. commit 後 push 到你的 fork，開 PR 到 `main`。PR 說明照範本寫齊：
   - **(a) 什麼壞了**：老師看到的現象、在哪一步、錯誤訊息（已去除個人資料）。
   - **(b) 根本原因**：為什麼會這樣，指出檔案與函式。
   - **(c) 修改內容**：改了什麼、為什麼這樣改。
   - **(d) 測試**：修改前會失敗、修改後會通過的測試（寫出測試名稱）。
   - **(e) `uv run pytest -q` 的輸出**（最後幾行即可）。
   - **(f) 在真的 Mac 上驗證了什麼**：macOS 版本、晶片（Apple silicon／Intel）、實際操作與結果；沒有實機驗證就寫「未驗證」。
5. 一個 PR 只處理一個問題；有好幾個問題就分開送。

## PR 送出之後

PR **不會直接在 GitHub 上合併**。這個公開 repo 的內容由維護者的開發 repo 發行而來：維護者會審查你的 PR，
把接受的修改移植到開發 repo、測試、審查，再隨下一個版本發行；發行時會用新的內容整個覆蓋公開 repo，
然後關閉你的 PR，並註明是哪一個版本收錄的。接受的貢獻會在關閉 PR 時的留言與該版本的發行說明（release notes）中致謝；
移植到開發 repo 的 commit 會加一行 `Co-Authored-By` 註明你，但公開 repo 每個版本只有一個整合後的
「Release vX.Y.Z」commit，看不到那一行。
所以 PR 被關閉而沒有「Merged」標記是正常的流程，不是被拒絕。之後要再送 PR，請先把你的 fork 同步到最新的 `main`。
