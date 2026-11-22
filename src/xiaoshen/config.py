"""設定載入：config.toml（本機、不進 git）覆蓋預設值。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .platform_support import app_data_dir, default_browser

FOLDERS = ("簡報", "參考資料", "圖庫", "題庫", "紀錄")

REPO_ROOT = Path(__file__).resolve().parents[2]
KWS_MODEL_NAME = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"


@dataclass
class Config:
    # 名字與喚醒詞（通用預設；個人化寫在 config.toml）
    display_name: str = "賈維斯"
    wake_word: str = ""            # 空字串＝和名字相同
    specialty: str = ""            # 專科，給 AI 回答與出題當背景
    meeting_type: str = "病例討論"
    meeting_type_other: str = ""   # meeting_type 選「其它」時的自訂名稱
    display_kiosk: bool = True     # 投影視窗用全螢幕模式
    auto_convert_pptx: bool = True
    data_root: Path | None = None  # None＝尚未設定（第一次啟動會請使用者選）
    control_port: int = 8765
    phone_port: int = 8766
    display_monitor: int = 1
    browser: Path | None = field(default_factory=lambda: default_browser())  # None＝找不到 Edge／Chrome，改用預設瀏覽器
    cloudflared: Path | None = None
    keys_next: list[str] = field(default_factory=lambda: ["PageDown", "ArrowRight", "ArrowDown", " "])
    keys_prev: list[str] = field(default_factory=lambda: ["PageUp", "ArrowLeft", "ArrowUp"])
    keys_drawer: list[str] = field(default_factory=lambda: ["b", "B", "."])
    # Phase 2：AI 回答與出題（金鑰只從環境變數 GEMINI_API_KEY 讀取）
    model_answer: str = "gemini-3.1-pro-preview"
    model_quiz: str = "gemini-3.1-pro-preview"
    # 主要模型配額用完、過載或不存在時依序改用
    ai_timeout_seconds: float = 150      # 文字模型每次回答／出題的總時限（解讀型問題曾超過 90 秒）
    model_ocr: str = "gemini-3.5-flash"  # 投影片圖片抄錄（只抄不解讀，用快模型）
    model_results: str = "gemini-3.5-flash"  # 問目前列出的文獻（年份、期刊）用的快模型
    model_fallbacks: list[str] = field(default_factory=lambda: ["gemini-3.5-flash", "gemini-3.8-flash"])
    oe_script: Path = Path.home() / ".claude" / "skills" / "openevidence" / "scripts" / "oe.py"
    # openevidence-mcp 資料夾（轉接程式＋附的 oe.py）；None＝找 ~/openevidence-mcp、~/.claude/mcp-servers/openevidence-mcp
    oe_mcp_dir: Path | None = None
    max_reference_chars: int = 400_000
    # 原文重點摘要簡報（§6.2b，用 Flash：較快、省費用）
    model_summary: str = "gemini-3.1-pro-preview"
    summary_thinking: str = "low"  # 摘要的思考量：low 較快（實測 41 秒 vs 預設 65 秒）；留空＝模型預設
    model_clarify: str = "gemini-3.5-flash"  # 送 OpenEvidence 前判斷要不要反問（§6.2c，用快模型）
    summary_timeout_seconds: float = 170   # 語音最多等 180 秒
    summary_max_chars: int = 600_000       # 超過就截斷並在第一頁註明讀到第幾頁
    summary_chunk_chars: int = 150_000     # 輸出被截斷時每段的大小
    # Phase 3：語音（喚醒詞在本機；叫醒後才用 Gemini Live）
    voice_enabled: bool = True
    live_model: str = "gemini-3.8-live"
    live_voice: str = "Charon"  # 固定聲音（男聲）；不指定時每次叫醒可能忽男忽女
    kws_model_dir: Path = field(default_factory=lambda: app_data_dir() / "models" / KWS_MODEL_NAME)
    kws_threshold: float = 0.25
    kws_score: float = 1.0
    wake_sensitivity: str = "medium"  # 喚醒靈敏度 low／medium／high（主持人畫面「設定」可改，重新啟動後生效）
    audio_device: str = ""        # 麥克風名稱的一部分；空字串 = 系統預設
    speaker_device: str = ""      # 喇叭名稱的一部分；空字串 = Windows 預設輸出（平常聽得到聲音的地方）
    voice_idle_seconds: float = 8.0         # 叫醒後幾秒內要開始問
    voice_followup_seconds: float = 6.0     # 答完後幾秒內可追問（一問一答＋追問）
    voice_end_silence_ms: int = 2000        # 停頓多久才算講完（使用者回饋：太短會被打斷）
    voice_max_listen_seconds: float = 60.0  # 每次叫醒最多送上雲端的收音秒數
    voice_speech_rms: float = 0.008         # 麥克風音量超過這個值算有人在說話（USB 麥克風安靜時實測約 0.001～0.003）
    keys_wake: list[str] = field(default_factory=list)  # 遙控器叫醒鍵
    # 會議錄音與會議記錄（錄音、逐字稿、會議記錄都放在「紀錄」底下的子資料夾）
    recording_max_minutes: int = 180       # 錄超過就自動停止並存檔
    recording_retention_days: int = 30     # 會議記錄做好後幾天刪除錄音與逐字稿（會議記錄本身保留）
    transcribe_fallback_minutes: int = 30  # 錄音送出後幾分鐘還沒有逐字稿，就提供「在這台電腦轉寫」
    minutes_model: str = ""                # 整理會議記錄的模型；空字串＝和 model_answer 相同
    worker_whisper_model: str = "large-v3"  # 轉寫工作站（有顯卡的電腦）用的 Whisper 模型
    worker_cluster_threshold: float = 0.7   # 分講者的分群門檻（越小分得越細）
    # 醫院的病房名稱：「病房＋床號」當病人識別資訊；也給轉寫當提示詞，減少錯聽
    wards: list[str] = field(default_factory=list)
    ward_misheard: list[str] = field(default_factory=list)  # 語音常把病房聽成的字（例：8B → 芭比）

    def __post_init__(self):
        from .phi import configure_wards

        configure_wards(self.wards, self.ward_misheard)

    def whisper_hotwords(self) -> str:
        """轉寫的提示詞：「8A病房 8B病房 NBC PI」（實測這樣寫，Whisper 不再把 8B 聽成芭比）。"""
        return " ".join(f"{w}病房" if any(ch.isdigit() for ch in w) else w
                        for w in (x.strip() for x in self.wards) if w)

    @property
    def effective_wake_word(self) -> str:
        return self.wake_word or self.display_name

    def folder(self, name: str) -> Path:
        if name not in FOLDERS:
            raise ValueError(f"未知的資料夾：{name}")
        if self.data_root is None:
            raise RuntimeError("尚未設定 data_root（資料根目錄）；請先完成第一次啟動的資料夾設定")
        return self.data_root / name


_PATH_FIELDS = {"data_root", "browser", "cloudflared", "oe_script", "kws_model_dir", "oe_mcp_dir"}


def load_config(path: Path | None = None) -> Config:
    path = path or REPO_ROOT / "config.toml"
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    known = Config.__dataclass_fields__
    values = {}
    for key, value in raw.items():
        if key not in known:
            continue
        if key in _PATH_FIELDS:
            if not value:
                continue  # 空字串＝用預設
            value = Path(value)
        values[key] = value
    return Config(**values)
