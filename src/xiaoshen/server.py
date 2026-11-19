"""兩個 FastAPI app，共用同一個 AppState。

- 控制 app：投影畫面、主持人畫面、所有控制 API。只綁 127.0.0.1，絕不經 tunnel。
- 手機 app：只有作答相關路由，經 cloudflared 對外。不得掛任何控制或主持人路由。
"""

from __future__ import annotations

import base64
import io
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import qrcode
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import eventlog
from .config import FOLDERS, REPO_ROOT, load_config
from .converter import SOURCE_SUFFIXES
from . import first_run, persona, settings_store
from .host_phone import CODE_TTL
from .hub import Hub
from .quiz import InvalidAnswer, QuizClosed
from .state import AppState

if TYPE_CHECKING:
    from .jobs import JobRunner


WEB_DIR = REPO_ROOT / "web"
# 手機 app 只能提供這些靜態檔（主持人／投影畫面的檔案不得經 tunnel 外流）
PHONE_ASSETS = {"phone.js", "common.css"}


_ASSET = re.compile(r'(/static/([\w.-]+\.(?:js|css)))"')


def _versioned(m: re.Match) -> str:
    try:
        v = int((WEB_DIR / m.group(2)).stat().st_mtime)
    except OSError:
        return m.group(0)
    return f'{m.group(1)}?v={v}"'


def _page(name: str) -> Response:
    """頁面本身不快取；裡面引用的 JS／CSS 網址加上檔案版本：程式更新後瀏覽器一定抓新的（實機 2026-11-05）。"""
    html = _ASSET.sub(_versioned, (WEB_DIR / name).read_text(encoding="utf-8"))
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


class PhoneEnabledBody(BaseModel):
    enabled: bool


class TeacherBody(BaseModel):
    id: str


class PhoneTeacherBody(BaseModel):
    t: str
    cid: str = ""
    text: str


class PhoneAskBody(BaseModel):
    t: str
    cid: str = ""
    text: str
    route: str = "openevidence"


class PhoneNavBody(BaseModel):
    t: str
    action: str
    page: int | None = None


class PhonePlayBody(BaseModel):
    t: str
    kind: str
    name: str = ""


class RefOpenBody(BaseModel):
    ref: str


class SourceBody(BaseModel):
    card: int
    kind: Literal["auto", "abstract", "page", "web"] = "auto"


class NavBody(BaseModel):
    action: Literal["next", "prev", "goto"]
    page: int | None = None


class DeckBody(BaseModel):
    path: str


RouteName = Literal["openevidence", "pubmed", "references", "deck", "page"]


class AskBody(BaseModel):
    text: str
    route: RouteName
    refs: list[str] = []


class QuizGenBody(BaseModel):
    n: int
    route: RouteName
    refs: list[str] = []
    focus: str = ""


class DraftBody(BaseModel):
    id: str


class FigureBody(BaseModel):
    index: int | None = None


class ConfirmBody(BaseModel):
    question: str


class RefBody(BaseModel):
    name: str = ""


class TokenBody(BaseModel):
    t: str


class SubmitBody(BaseModel):
    t: str
    cid: str
    answers: dict[str, int]
    final: bool = False


class SettingsBody(BaseModel):
    display_name: str
    wake_word: str = ""
    specialty: str = ""
    meeting_type: str
    meeting_type_other: str = ""
    audio_device: str | None = None    # None＝這次沒送（不改）；""＝電腦預設
    speaker_device: str | None = None
    wake_sensitivity: str | None = None  # None＝這次沒送（不改）


SETTING_KEYS = ("display_name", "wake_word", "specialty", "meeting_type", "meeting_type_other")
RESTART_KEYS = (*settings_store.DEVICE_KEYS, "wake_sensitivity")  # 選填；啟動時才套用，改了要重新啟動


def system_device_names() -> dict[str, list[str]]:
    """這台電腦的麥克風與喇叭名稱（啟動時的清單；新插的裝置要重新啟動才會出現）。"""
    from .audio_io import list_device_names

    return {"input": list_device_names("input"), "output": list_device_names("output")}


def create_control_app(state: AppState, hub: Hub, jobs: "JobRunner | None" = None, viewer=None,
                       config_path: Path = first_run.CONFIG_PATH, diagnostics_dir: Path | None = None,
                       logs_dir: Path | None = None, device_names=system_device_names,
                       pick_folder=first_run.pick_folder_dialog) -> FastAPI:
    app = FastAPI(title="控制台", docs_url=None, redoc_url=None)
    state.on_change = lambda: hub.notify(state)
    app.mount("/static", StaticFiles(directory=WEB_DIR, check_dir=False), name="static")

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        # 沒有快取指示時瀏覽器會自己沿用舊的 JS：更新程式後投影／主持人畫面跑的還是舊版（實機 2026-11-05）
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"  # 每次都向伺服器確認（沒變就 304，不重傳）
        return response

    @app.get("/display")
    def display():
        return _page("display.html")

    @app.get("/host")
    def host():
        return _page("host.html")

    @app.get("/slides/{index}.png")
    def slide(index: int, w: int = 1920):
        if state.deck is None:
            raise HTTPException(404, "沒有簡報")
        try:
            png = state.deck.render_png(index, width=max(320, min(w, 3840)))
        except IndexError:
            raise HTTPException(404, "頁碼超出範圍")
        return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    # ---- 顯示出處原文（§6.2a）：主持人點出處的按鈕 ----

    @app.post("/api/source/show")
    def source_show(body: SourceBody):
        if viewer is None:
            raise HTTPException(503, "原文功能未啟用")
        res = viewer.show(body.card, body.kind)
        eventlog.log("source_show", source="host", card=body.card, kind=body.kind, status=res.get("status"))
        return res

    @app.post("/api/source/close")
    def source_close():
        if viewer is None:
            raise HTTPException(503, "原文功能未啟用")
        res = viewer.close()
        eventlog.log("source_close", source="host", status=res.get("status"))
        return res

    # ---- OE 簡報（§6.2c）：投影幕上的參考文獻可以點 ----

    @app.get("/deck/links/{index}")
    def deck_links_for(index: int):
        from .oe_deck import deck_links

        deck = state.deck
        if state.temp_kind not in ("oe", "summary") or deck is None:
            return []
        return deck_links(Path(deck.path), index)

    # ---- 手機（§6.3a）：主持人的兩個開關與「詢問老師」 ----

    @app.post("/api/check/openevidence")
    def recheck_openevidence():
        """黃燈那一列的「重新檢查」：重新確認瀏覽器分頁與登入（不花提問額度），更新自我檢查。"""
        if state.oe_recheck is None:
            raise HTTPException(503, "無法重新檢查")
        result = state.oe_recheck()
        with state.lock:
            state.set_check(result.name, result.level, result.detail)
        state._changed()
        eventlog.log("oe_recheck", level=result.level)
        return result.to_dict()

    @app.post("/api/phone/control-enabled")
    def phone_control_enabled(body: PhoneEnabledBody):
        with state.lock:
            state.phone_control_enabled = body.enabled
        state._changed()
        eventlog.log("phone_control_enabled", enabled=body.enabled)
        return {"enabled": state.phone_control_enabled}

    @app.post("/api/phone/search-enabled")
    def phone_search_enabled(body: PhoneEnabledBody):
        with state.lock:
            state.phone_search_enabled = body.enabled
        state._changed()
        eventlog.log("phone_search_enabled", enabled=body.enabled)
        return {"enabled": state.phone_search_enabled}

    @app.post("/api/teacher/show")
    def teacher_show(body: TeacherBody):
        return {"ok": state.show_teacher_question(body.id)}

    @app.post("/api/teacher/hide")
    def teacher_hide(body: TeacherBody):
        return {"ok": state.hide_teacher_question(body.id)}

    @app.post("/api/teacher/hide-all")
    def teacher_hide_all():
        state.hide_drawer()
        return {"ok": True}

    @app.post("/api/teacher/remove")
    def teacher_remove(body: TeacherBody):
        return {"ok": state.remove_teacher_question(body.id)}

    @app.post("/api/teacher/ask")
    def teacher_ask(body: TeacherBody):
        """「用（助理名）查這題」：照樣先做識別資訊檢查（JobRunner.ask 命中就不送）。"""
        q = state._teacher(body.id)
        if q is None:
            return {"ok": False, "message": "找不到這一題"}
        job = need_jobs().ask(q["text"], "openevidence", [], spoken=False)
        if not job:
            return {"ok": False, "message": "疑似含病人識別資訊，沒有送出"}
        return {"ok": True, "job": job}

    @app.post("/api/oe/play")
    def oe_play():
        if jobs is None:
            raise HTTPException(503, "AI 功能未啟用")
        return {"ok": jobs.play_answer_oe()}

    @app.post("/api/oe/stop")
    def oe_stop():
        """主持人畫面「收回 OE 簡報」：只收 OE 簡報，回到原本的畫面。"""
        return {"ok": state.stop_oe_deck()}

    @app.post("/api/oe/ref")
    def oe_ref(body: RefOpenBody):
        if viewer is None:
            raise HTTPException(503, "原文功能未啟用")
        res = viewer.open_reference(body.ref)
        eventlog.log("source_show", source="projector", ref=body.ref, status=res.get("status"))
        return res

    # ---- 原文重點摘要簡報（§6.2b）----

    @app.post("/api/summary/start")
    def summary_start():
        if jobs is None or viewer is None:
            raise HTTPException(503, "原文功能未啟用")
        if not state.source_view:
            raise HTTPException(409, "請先打開要整理的原文")
        job = jobs.summarize_source(viewer)
        eventlog.log("summary_request", source="host", started=bool(job))
        if not job:
            raise HTTPException(409, "正在查詢或整理中，請稍後再試")
        return {"job": job}

    @app.post("/api/summary/cancel")
    def summary_cancel():
        if jobs is None:
            raise HTTPException(503, "AI 功能未啟用")
        return {"ok": jobs.cancel_summary()}

    @app.get("/source/page.png")
    def source_page():
        png = viewer.page_png() if viewer is not None else None
        if png is None:
            raise HTTPException(404, "目前沒有顯示頁面")
        # 網址帶 ?v=（每次顯示都不同），可以快取：投影重繪時不必重抓（審查 #8）
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})

    @app.get("/qr.png")
    def qr():
        url = state.phone_url()
        if url is None:
            raise HTTPException(404, "手機作答網址尚未就緒")
        buf = io.BytesIO()
        qrcode.make(url, border=2).save(buf, format="PNG")
        return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})

    # ---- 設定：名字、喚醒詞、專科、會議類型（寫進 config.toml）----

    @app.get("/api/settings")
    def get_settings():
        """表單顯示已存檔的值（名字、喚醒詞存檔後要重新啟動才生效，但表單要看到自己剛存的）。"""
        p, c = persona.current(), state.config
        values = {"data_root": str(c.data_root or ""), "display_name": c.display_name, "wake_word": c.wake_word,
                  "specialty": p.specialty,
                  "meeting_type": p.meeting_type, "meeting_type_other": p.meeting_type_other,
                  "audio_device": c.audio_device, "speaker_device": c.speaker_device,
                  "wake_sensitivity": c.wake_sensitivity}
        if config_path.exists():
            saved = load_config(config_path)
            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
            values.update({k: getattr(saved, k) for k in (*SETTING_KEYS, *RESTART_KEYS) if k in raw})
            if raw.get("data_root"):
                values["data_root"] = str(saved.data_root)  # 剛換過、還沒重新啟動：表單顯示新的
        try:
            names = device_names()
        except Exception:  # 音訊系統出錯時選單只剩「電腦預設」與已存的值，設定頁照常可用
            names = {"input": [], "output": []}
        return {**values, "meeting_types": list(persona.MEETING_TYPES),
                "mic_options": settings_store.device_options(values["audio_device"], names.get("input", [])),
                "speaker_options": settings_store.device_options(values["speaker_device"], names.get("output", [])),
                "wake_sensitivity_options": settings_store.sensitivity_options()}

    @app.post("/api/settings")
    def post_settings(body: SettingsBody):
        values = {k: getattr(body, k).strip() for k in SETTING_KEYS}
        # 裝置與靈敏度欄位沒送＝不改（舊版畫面或其他用戶端不會把麥克風設定清掉）
        devices = {k: getattr(body, k).strip() for k in RESTART_KEYS if getattr(body, k) is not None}
        errors = settings_store.validate({**values, **devices})
        if errors:
            return JSONResponse({"errors": errors}, status_code=422)
        settings_store.upsert_keys(config_path, {**values, **devices})
        running = persona.current()
        # 麥克風、喇叭與喚醒靈敏度在啟動時套用，改了要重新啟動
        restart_needed = (values["display_name"] != state.config.display_name
                          or (values["wake_word"] or values["display_name"]) != state.config.effective_wake_word
                          or any(v != getattr(state.config, k) for k, v in devices.items()))
        # 名字要重新啟動才換（語音、喚醒詞都用啟動時的名字）；專科與會議類型立刻生效
        persona.set_persona(persona.Persona(running.display_name, values["specialty"], values["meeting_type"],
                                            values["meeting_type_other"]))
        eventlog.log("settings_saved", source="host", restart_needed=restart_needed)
        state._changed()
        return {"ok": True, "restart_needed": restart_needed}

    @app.post("/api/settings/folder")
    def change_folder():
        """跳出選資料夾視窗（選已有的，或在視窗裡新增）；建好子資料夾、記進 config.toml，重新啟動後生效。
        原本資料夾裡的檔案不搬、不刪。"""
        picked = pick_folder(state.config.data_root or first_run.picker_start_dir())
        if not picked:
            return {"ok": False, "cancelled": True}
        root = Path(picked)
        if first_run.inside_install_folder(root):
            raise HTTPException(422, f"資料夾不能放在安裝資料夾（{REPO_ROOT}）裡面：更新或移除程式時會被刪除。")
        try:
            for name in FOLDERS:
                (root / name).mkdir(parents=True, exist_ok=True)
            first_run.save_data_root(config_path, root)
        except OSError as e:
            raise HTTPException(500, f"無法建立資料夾：{e}") from e
        eventlog.log("data_folder_changed", source="host")
        return {"ok": True, "folder": str(root), "restart_needed": root != state.config.data_root}

    @app.post("/api/diagnostics")
    def post_diagnostics():
        """產生診斷包 zip 放到桌面，主持人畫面顯示路徑，請遠端測試者寄回。"""
        from datetime import datetime

        from . import diagnostics
        from .platform_support import app_data_dir, desktop_dir
        try:
            path = diagnostics.build_bundle(diagnostics_dir or desktop_dir(), list(state.checks), config_path,
                                            logs_dir or app_data_dir() / "logs", datetime.now(),
                                            data_root=state.config.data_root)
        except Exception as e:  # noqa: BLE001 - 任何失敗都要讓主持人看得到原因
            eventlog.log("diagnostics_failed", error=type(e).__name__)
            raise HTTPException(500, f"產生診斷包失敗：{e}") from e
        eventlog.log("diagnostics_created", source="host")
        return {"ok": True, "path": str(path)}

    @app.get("/api/state")
    def get_state(audience: Literal["display", "host"] = "display"):
        return state.snapshot(audience)

    @app.post("/api/nav")
    async def nav(body: NavBody):
        before = state.page + 1
        if body.action == "next":
            state.next()
        elif body.action == "prev":
            state.prev()
        else:
            if body.page is None:
                raise HTTPException(422, "goto 需要 page")
            state.goto(body.page)
        eventlog.log("page", source="host", action=body.action, before=before, after=state.page + 1)
        return {"page": state.page}

    @app.get("/api/decks")
    def decks():
        paths = state.list_deck_files()
        state.set_deck_paths(paths)
        return [{"name": p.stem, "path": str(p)} for p in paths]

    @app.post("/api/deck")
    async def open_deck(body: DeckBody):
        folder = state.config.folder("簡報").resolve()
        path = Path(body.path).resolve()
        if path.parent != folder or path.suffix.lower() not in (".pdf", *SOURCE_SUFFIXES) or not path.exists():
            raise HTTPException(400, "只能開啟「簡報」資料夾內的 PDF 或 pptx")
        if state.presenting:
            raise HTTPException(409, "播放中請先回到主畫面")
        result = state.request_deck(path, play=False)  # 還沒轉的 pptx：在背景轉，好了自動打開
        if result in ("busy", "error"):
            raise HTTPException(409, state.deck_request_reply(result)["message"])
        return {"deck": state.deck.name if state.deck else None,
                "converting": path.stem if result == "converting" else None}

    @app.post("/api/present")
    async def present():
        """主持人「播放簡報」：播放下拉選單選的那份（§6.0；2026-11-08 起語音在線也能用）。"""
        converting = state.play_after_conversion()
        if converting:  # 選的 pptx 還在轉：轉好直接播放
            return {"presenting": False, "converting": converting}
        if state.deck is None:
            raise HTTPException(409, "尚未選擇簡報")
        state.play(state.deck.path)
        return {"presenting": state.presenting}

    @app.post("/api/present/stop")
    async def present_stop():
        state.stop_presenting()
        return {"presenting": False}

    @app.post("/api/quiz/dispatch")
    async def dispatch():
        try:
            state.dispatch_current()
        except LookupError as e:
            raise HTTPException(404, str(e))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.post("/api/quiz/reveal")
    async def reveal():
        state.reveal()
        return {"ok": True}

    @app.post("/api/quiz/collect")
    async def collect():
        state.collect()
        return {"ok": True}

    @app.post("/api/drawer/hide")
    async def hide():
        state.hide_drawer()
        return {"ok": True}

    # ---- Phase 2：AI 回答與出題（只在控制 app） ----

    def need_jobs() -> "JobRunner":
        if jobs is None:
            raise HTTPException(503, "AI 功能未啟用")
        return jobs

    @app.post("/api/ask")
    def ask(body: AskBody):
        runner = need_jobs()
        if not body.text.strip():
            raise HTTPException(422, "請輸入問題")
        job = runner.ask(body.text.strip(), body.route, body.refs)
        return {"sent": True, "job": job} if job else {"sent": False, "phi": list(state.phi_hits)}

    @app.post("/api/quiz/generate")
    def quiz_generate(body: QuizGenBody):
        runner = need_jobs()
        try:
            job = runner.make_quiz(body.n, body.route, body.refs, body.focus.strip())
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"sent": True, "job": job} if job else {"sent": False, "phi": list(state.phi_hits)}

    @app.post("/api/drafts/publish")
    def drafts_publish():
        try:
            state.publish_drafts()
        except LookupError as e:
            raise HTTPException(404, str(e))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.post("/api/drafts/remove")
    def drafts_remove(body: DraftBody):
        state.remove_draft(body.id)
        return {"ok": True}

    @app.post("/api/drafts/regenerate")
    def drafts_regenerate(body: DraftBody):
        try:
            return {"job": need_jobs().regenerate(body.id)}
        except LookupError as e:
            raise HTTPException(404, str(e))

    @app.post("/api/answer/quotes")
    def answer_quotes():
        state.toggle_quotes()
        return {"show_quotes": state.show_quotes}

    @app.post("/api/answer/show")
    def answer_show():
        state.show_answer()
        return {"drawer": state.drawer}

    @app.get("/ai/figure/{answer_id}/{index}")
    def ai_figure(answer_id: str, index: int):
        """OpenEvidence 附圖：只給目前這個回答的圖，路徑來自伺服器自己的紀錄（不接受外部路徑）。"""
        a = state.answer
        if a is None or a.id != answer_id or not 0 <= index < len(a.figures):
            raise HTTPException(404)
        path = Path(a.figures[index].path)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    @app.post("/api/answer/figure")
    def answer_figure(body: FigureBody):
        a = state.answer
        if body.index is not None and (a is None or not 0 <= body.index < len(a.figures)):
            raise HTTPException(404, "沒有這張圖")
        state.figure_zoom = body.index
        state._changed()
        return {"figure_zoom": state.figure_zoom}

    @app.get("/api/references")
    def references():
        lib = need_jobs().library
        return [{"name": d.name, "pages": len(d.pages), "scanned": d.scanned} for d in lib.docs.values()]

    # ---- Phase 3：語音（只在控制 app） ----

    @app.post("/api/voice/wake")
    def voice_wake():
        if state.wake_handler is None:
            raise HTTPException(409, "語音未啟用")
        state.wake_handler()
        return {"ok": True}

    @app.post("/api/voice/stop")
    def voice_stop():
        if state.stop_handler is not None:
            state.stop_handler()
        return {"ok": True}

    # ---- 結束程式（主持人畫面按鈕，畫面上再確認一次才送出）----

    @app.post("/api/host-phone/pair")
    def host_phone_pair():
        """筆電按「連接主持人手機」：產生一次性 QR Code（§6.3b）。對外通道的 gateway 擋掉手機來的請求。"""
        if not state.public_base:
            raise HTTPException(409, "對外通道尚未就緒，手機連不上")
        buf = io.BytesIO()
        qrcode.make(f"{state.public_base}/h?c={state.host_pairing.new_code()}", border=2).save(buf, format="PNG")
        return {"qr": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), "expires_in": CODE_TTL}

    @app.post("/api/host-phone/revoke")
    def host_phone_revoke():
        state.host_pairing.revoke()
        eventlog.log("host_phone", action="revoked")
        state._changed()
        return {"ok": True}

    @app.post("/api/quit")
    def quit_app():
        if state.quit_handler is None:
            raise HTTPException(409, "無法從畫面結束，請在黑色視窗按 Ctrl+C")
        eventlog.log("quit", source="host")
        state.quit_handler()
        return {"ok": True}

    @app.post("/api/voice/ref")
    def voice_ref(body: RefBody):
        state.voice_ref = body.name or None
        state.ref_candidates = []
        state._changed()
        return {"voice_ref": state.voice_ref}

    @app.post("/api/voice/confirm")
    def voice_confirm(body: ConfirmBody):
        """主持人畫面確認（可改字）語音要查的問題，直接送出（§5.4）。"""
        runner = need_jobs()
        pending = state.active_voice_confirm()
        if pending is None:
            raise HTTPException(409, "沒有待確認的語音查詢")
        text = body.question.strip() or pending["question"]
        state.clear_voice_confirm()
        job = runner.ask(text, pending["route"], pending["refs"], confirmed=True)  # 確認過就不反問
        return {"sent": True, "job": job} if job else {"sent": False, "phi": list(state.phi_hits)}

    @app.post("/api/voice/confirm/cancel")
    def voice_confirm_cancel():
        state.clear_voice_confirm()
        return {"ok": True}

    @app.post("/api/voice/phi/clear")
    def voice_phi_clear():
        state.voice_phi_alert = False
        state._changed()
        return {"ok": True}

    @app.websocket("/ws/mic")
    async def mic_ws(ws: WebSocket):
        """主持人手機的麥克風（16 kHz int16 單聲道，§6.3b）。只有配對過的手機經 gateway 連得到。"""
        await ws.accept()
        mic = state.phone_mic
        gen = mic.start()
        eventlog.log("phone_mic", action="start")
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    if not mic.feed(gen, msg["bytes"]):
                        await ws.close()  # 這一輪結束或有新的連線：請手機停止收音
                        break
                elif msg.get("text") == "ping":
                    await ws.send_text("ok")
        finally:
            mic.stop(gen)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket, audience: Literal["display", "host"] = Query("display")):
        await hub.connect(ws, audience, state)
        try:
            while True:
                await ws.receive_text()  # 只用來偵測斷線
        except WebSocketDisconnect:
            hub.disconnect(ws)

    return app


def create_phone_app(state: AppState, jobs: "JobRunner | None" = None) -> FastAPI:
    from .phone_control import PhoneControl

    app = FastAPI(title="作答", docs_url=None, redoc_url=None, openapi_url=None)
    control = PhoneControl(state, jobs)

    @app.get("/static/{name}")
    def phone_asset(name: str):
        if name not in PHONE_ASSETS or not (WEB_DIR / name).exists():
            raise HTTPException(404)
        return FileResponse(WEB_DIR / name, headers={"Cache-Control": "no-store"})

    def check_token(t: str) -> None:
        if t != state.token:
            raise HTTPException(403, "網址已失效，請重新掃描 QR Code")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/q")
    def phone_page(t: str = ""):
        check_token(t)
        return _page("phone.html")

    @app.get("/api/phone/state")
    def phone_state(t: str = "", cid: str = ""):
        check_token(t)
        if state.run is None:
            return {"run": None}
        return {"run": state.run.view_for_phone(cid)}

    @app.post("/api/phone/wake")
    def phone_wake(body: TokenBody):
        """只叫醒會議室麥克風，不從手機收音；冷卻 10 秒，對話中忽略。"""
        check_token(body.t)
        try:
            ignored = state.phone_wake()
        except LookupError as e:
            raise HTTPException(409, str(e))
        return {"ok": True, "ignored": ignored}

    # ---- 手機打字發問與投影控制（§6.3a）：只有翻頁、播放、結束；其餘指令不開放 ----

    @app.get("/api/phone/control")
    def phone_control_view(t: str = ""):
        check_token(t)
        return control.view()

    @app.post("/api/phone/teacher")
    def phone_teacher(body: PhoneTeacherBody):
        check_token(body.t)
        return control.teacher(body.cid, body.text)

    @app.post("/api/phone/ask")
    def phone_ask(body: PhoneAskBody):
        check_token(body.t)
        return control.ask(body.cid, body.text, body.route)

    @app.post("/api/phone/nav")
    def phone_nav(body: PhoneNavBody):
        check_token(body.t)
        return control.nav(body.action, body.page)

    @app.post("/api/phone/play")
    def phone_play(body: PhonePlayBody):
        check_token(body.t)
        return control.play(body.kind, body.name)

    @app.post("/api/phone/end")
    def phone_end(body: TokenBody):
        check_token(body.t)
        return control.end()

    @app.post("/api/phone/submit")
    async def submit(body: SubmitBody):
        check_token(body.t)
        try:
            state.submit(body.cid, body.answers, body.final)
        except QuizClosed as e:
            raise HTTPException(409, str(e))
        except InvalidAnswer as e:
            raise HTTPException(422, str(e))
        return {"ok": True}

    return app
