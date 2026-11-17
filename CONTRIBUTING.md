# 參與貢獻

謝謝你願意幫忙改善教學助理賈維斯。遇到問題、想送修正，都很歡迎。
如果是你的 AI coding agent 要動手，請它先讀 [AGENTS.md](AGENTS.md)（專案結構、測試、診斷資料與硬性規則都寫在那裡）。

## 回報問題

開一個 Issue，寫下：作業系統與版本、做了什麼、看到什麼（錯誤訊息或截圖）。
需要診斷包時，維護者會另外跟你約定怎麼傳；**不要把診斷包、`config.toml`、金鑰、cookie 或任何個人資料貼到 Issue 或 PR**。

## 送出修改（Pull Request）

1. fork 這個 repo，從 `main` 開一個新 branch。
2. 修改，並加一個「修改前會失敗、修改後會通過」的測試。
3. 跑 `uv sync` 與 `uv run pytest -q`，全部通過。
4. 開 PR 到 `main`，照 PR 範本寫：(a) 什麼壞了、(b) 根本原因、(c) 修改內容、(d) 測試、
   (e) `uv run pytest -q` 的輸出、(f) 在真的 Mac 上驗證了什麼。
5. 一個 PR 只處理一個問題。

請保持行尾（`.bat` 是 CRLF，`.sh`／`.command` 是 LF），使用者看得到的文字用台灣繁體中文。

## PR 會怎麼處理

PR **不會直接在 GitHub 上合併**。維護者審查後，會把接受的修改移植到開發用的 repo，測試、審查，
再隨下一個版本發行，然後關閉 PR 並註明收錄的版本。PR 沒有「Merged」標記是正常流程，不是被拒絕。

## 授權與致謝

你送出的貢獻，以與本專案相同的 [PolyForm Noncommercial License 1.0.0](LICENSE) 授權。
接受的貢獻會在關閉 PR 時的留言與該版本的發行說明（release notes）中致謝。維護者移植到開發 repo 的 commit
會加一行 `Co-Authored-By` 註明你的 GitHub 帳號；不過公開 repo 每個版本只有一個整合後的「Release vX.Y.Z」commit，
在公開 repo 上看不到那一行。
