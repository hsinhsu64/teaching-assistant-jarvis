# 教學助理賈維斯

在會議室使用的教學助理：投影簡報、主持人畫面、與會人員用手機掃碼作答、語音叫醒問答，回答一律根據簡報、參考資料或 OpenEvidence 的原文，並標明出處。

支援 Windows 與 Mac。Mac 版目前**測試中**（功能完整，但還在實機測試，遇到問題請見下方「Mac（測試中）」）。

## 安裝（一行指令）

不需要系統管理員權限，也不需要 git。安裝需要幾分鐘（視網路速度），最後會請你貼上 Gemini 金鑰。

**Windows**：按「開始」，輸入 `PowerShell` 打開它，貼上這一行後按 Enter：

```
irm https://raw.githubusercontent.com/hsinhsu64/teaching-assistant-jarvis/main/install.ps1 | iex
```

**Mac（測試中）**：打開「終端機」（Terminal），貼上這一行後按 Enter：

```
curl -fsSL https://raw.githubusercontent.com/hsinhsu64/teaching-assistant-jarvis/main/install.sh | bash
```

任何一步失敗時，畫面會寫出「第 N 步：…失敗，請…」；照著做之後重新執行同一行指令即可，已完成的步驟會自動略過。

## 會安裝哪些東西

全部放在你的使用者資料夾裡的 `TeachingAssistantJarvis`（Windows：`%USERPROFILE%\TeachingAssistantJarvis`；Mac：`~/TeachingAssistantJarvis`）：

| 項目 | 用途 | 位置 |
|---|---|---|
| uv | 安裝與執行 Python 程式的工具 | 使用者資料夾的 `.local/bin` |
| 程式本體與 Python 套件 | 教學助理本身 | `TeachingAssistantJarvis` |
| cloudflared | 讓與會人員的手機連進來作答 | `TeachingAssistantJarvis/tools` |
| Node.js（可攜版） | 執行 OpenEvidence 轉接程式 | `TeachingAssistantJarvis/tools/node` |
| openevidence-mcp | OpenEvidence 轉接程式與瀏覽器擴充功能 | `TeachingAssistantJarvis/tools/openevidence-mcp` |
| 喚醒詞模型（約 33 MB） | 在本機辨識喚醒詞，待命時聲音不會送出 | 本機的應用程式資料夾 |
| 桌面捷徑「教學助理賈維斯」 | 雙擊啟動 | 桌面 |

另外會把你的 Gemini 金鑰存成使用者環境變數 `GEMINI_API_KEY`（Mac 寫在 `~/.zprofile` 的一個標記區塊裡）。金鑰不會寫進教學助理的設定檔（`config.toml`）。

## Gemini 金鑰

安裝最後會請你設定（已經設定過就略過）：

1. 到 <https://aistudio.google.com/apikey> 用 Google 帳號登入，按「Create API key」，複製金鑰。
2. 回到安裝視窗貼上（畫面上不會顯示任何字），按 Enter。程式會先向 Google 確認金鑰可用才存。

之後要換金鑰：雙擊安裝資料夾裡的 `set-key.bat`（Mac：`set-key.command`）。

## OpenEvidence 擴充功能

用 OpenEvidence 查詢需要在瀏覽器手動載入一次擴充功能（安裝結束時會自動打開這一段說明）：

1. 用 **Chrome 或 Edge 其中一個**（只裝在一個瀏覽器）打開擴充功能頁：Chrome 輸入 `chrome://extensions`，Edge 輸入 `edge://extensions`。
2. 打開「開發人員模式」。
3. 按「載入未封裝項目」，選這個資料夾：
   - Windows：`%USERPROFILE%\TeachingAssistantJarvis\tools\openevidence-mcp\extension\dist`
   - Mac：`~/TeachingAssistantJarvis/tools/openevidence-mcp/extension/dist`（選資料夾時按 Cmd+Shift+G 可以直接貼上路徑）
4. 在同一個瀏覽器開一個分頁，登入 OpenEvidence；使用教學助理時不要關掉這個分頁。

沒裝擴充功能或沒登入時，問答會自動改查 PubMed，不會卡住。更新後如果擴充功能出錯，到擴充功能頁按它的「重新載入」。

## 第一次啟動

雙擊桌面上的「教學助理賈維斯」。第一次會跳出設定視窗：

- **資料夾位置**：會在你選的位置建立「教學助理資料」資料夾，裡面有 `簡報`、`參考資料`、`題庫`、`紀錄` 等子資料夾。預設放在「文件」裡；選雲端同步的資料夾，就能在兩台電腦共用。不能選在 `TeachingAssistantJarvis` 安裝資料夾裡面（更新時那裡會整個換新）。
- **助理名字**：預設「賈維斯」。
- **喚醒詞**：2 到 4 個中文字；留空就用名字。
- **科別**、**會議類型**（病例討論、Journal reading、病歷教學、Mortality & Morbidity、VS teaching、其它）。

這些之後都可以在主持人畫面的「設定」修改。啟動後先看主持人畫面的自我檢查，紅燈或黃燈會寫出要怎麼處理。

## 更新

重新執行同一行安裝指令即可（先結束教學助理）。你的設定（`config.toml`）、資料夾、金鑰都會保留，已是最新的項目會略過。

## Mac（測試中）

- 第一次收音時，macOS 會詢問「終端機」能否使用麥克風，請按允許；不小心拒絕了，到「系統設定 → 隱私權與安全性 → 麥克風」打開終端機。
- 啟動後終端機視窗要保持開著（可以縮小）。
- 遇到問題：在主持人畫面按「產生診斷包」，把桌面上的「診斷包」zip 傳給提供這套程式的人。診斷包不含金鑰、簡報與參考資料。

## 移除

刪除 `TeachingAssistantJarvis` 資料夾與桌面捷徑即可。你的「教學助理資料」資料夾在安裝資料夾以外，不會受影響。

## 回報問題與貢獻

歡迎回報問題或送 Pull Request，流程見 [CONTRIBUTING.md](CONTRIBUTING.md)。用 AI coding agent 在自己的電腦除錯、修改時，請它先讀 [AGENTS.md](AGENTS.md)。

## 授權

本程式採用 [PolyForm Noncommercial License 1.0.0](LICENSE)：可以免費使用、修改、分享，但**不能用於商業用途**。醫院、學校等非營利機構與個人教學、研究使用都在允許範圍內。

安裝時另外下載的 uv、cloudflared、Node.js、openevidence-mcp 都來自各自的官方來源，依各自的授權條款使用。
