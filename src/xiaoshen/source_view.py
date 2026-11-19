"""顯示出處原文（spec §6.2a）。

字卡出處可以叫出三種原文：文獻摘要（引用句用螢光標出）、簡報／參考資料的那一頁（PDF 頁面加螢光框）、
原文網頁（在投影幕另開瀏覽器視窗：PMC 開放全文 → DOI → PubMed）。主持人畫面與語音共用這裡。
不下載、不轉存付費全文；Gemini 一般知識的字卡沒有文獻出處。
"""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

from .answering import LITERATURE_ROUTES
from .deck import Deck, DeckError
from .platform_support import kill_profile_processes
from .fulltext import NeedsFallback, fetch_full_text
from .grounding import match_quote, normalize_keep
from .pubmed import PubMedError, _default_get, fetch_pmid, lookup_doi, pmc_fulltext, pmc_id
from .source_text import SourceDoc, SourceUnreadable, fetch_bytes, pdf_pages

DECK_LABEL = "今天的簡報"
PAGE_WIDTH = 1600
LOOKUP_TIMEOUT = 6  # 叫出原文時的網路查詢：語音那一輪不能卡太久（審查 #4）
MIN_WORDS = 3       # 螢光框每段至少幾個英文字：太短會在頁面上到處亂標（審查 #7）
_CJK = re.compile("[㐀-鿿]")
_HTTP = re.compile(r"https?://", re.I)
_PMC_URL = re.compile(r"pmc\.ncbi\.nlm\.nih\.gov/articles/(PMC\d+)", re.I)
ABSTRACT_ONLY = "只讀到摘要"


def _lookup_get(url: str) -> str:
    return _default_get(url, timeout=LOOKUP_TIMEOUT)


def _chunks(items: list, size: int, minimum: int) -> list[list]:
    """每 size 個一段；最後一段太短就併進前一段。"""
    out = [items[i:i + size] for i in range(0, len(items), size)]
    if len(out) > 1 and len(out[-1]) < minimum:
        tail = out.pop()
        out[-1] = out[-1] + tail
    return [c for c in out if len(c) >= minimum]


def highlight_phrases(quote: str) -> list[str]:
    """把引用句切成小段給 PDF 搜尋（PDF 的換行會讓整句搜不到）：英文每 4 個字，中文每 7 個字。
    尾巴太短就併進前一段；整句太短就不標（PDF 搜尋會比對到別的字裡面）。"""
    q = " ".join(normalize_keep(quote).split())
    if _CJK.search(q):
        return ["".join(c) for c in _chunks(list(q.replace(" ", "")), 7, 3)]
    return [" ".join(c) for c in _chunks(q.split(), 4, MIN_WORDS)]


class EdgeWindow:
    """投影幕上的原文網頁：獨立的瀏覽器視窗（自己的設定檔），放在投影螢幕、全螢幕；關閉時結束它。
    開與關都在同一把鎖裡做，連按兩次也不會多開一個（審查 #6）。"""

    def __init__(self, browser: Path, profile: Path, position: tuple[int, int],
                 popen: Callable = subprocess.Popen, kill_profile: Callable[[Path], None] = kill_profile_processes, kiosk: bool = True):
        self.kiosk = kiosk
        self.browser, self.profile, self.position = browser, Path(profile), position
        self.popen, self.kill_profile = popen, kill_profile
        self._lock = threading.Lock()

    def open(self, url: str) -> None:
        x, y = self.position
        with self._lock:
            self.kill_profile(self.profile)  # 先清掉舊的（含殘留），新開的才不會被交給舊行程
            size = ["--start-fullscreen"] if self.kiosk else ["--window-size=1280,720"]  # 視窗模式：由主持人自己拖去全螢幕
            self.popen([str(self.browser), f"--user-data-dir={self.profile}", f"--app={url}",
                        f"--window-position={x},{y}", *size, "--no-first-run",
                        "--hide-crash-restore-bubble"])

    def close(self) -> None:
        with self._lock:
            self.kill_profile(self.profile)


class _Stale(Exception):
    """查詢期間換了回答或已關閉：結果作廢。"""


class SourceViewer:
    def __init__(self, state, library, http_get: Callable[[str], str] = _lookup_get, web=None):
        self.state = state
        self.library = library
        self.http_get = http_get
        self.web = web
        if web is not None:
            state.web_closer = web.close  # 「關閉原文」與新回答都要連帶關掉投影上的網頁視窗
        self._decks: dict[str, Deck] = {}  # 參考資料 PDF、已被換掉的簡報（依檔案內容）
        self._page_deck: Deck | None = None  # 目前顯示中的那一頁屬於哪份 PDF
        self._seq = 0

    # ---- 對外 ----

    def show(self, card_no: int, kind: str = "auto") -> dict:
        """card_no：投影上字卡的編號（從 1 起）。kind：auto（文獻→摘要、簡報／參考資料→那一頁）、abstract、page、web。"""
        st = self.state
        with st.lock:
            ans, gen = st.answer, st.source_gen
        if ans is None or ans.status != "answered" or not ans.cards:
            return {"status": "no_answer", "message": "目前沒有回答"}
        try:
            n = int(card_no)
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= len(ans.cards):
            return {"status": "not_found", "message": f"沒有第 {card_no} 張字卡"}
        card = ans.cards[n - 1]
        try:
            if card.doc:
                note = "這張的出處是簡報或參考資料，已顯示那一頁" if kind == "web" else f"已顯示第 {n} 張的那一頁"
                return self._show_page(ans, gen, n, card, note)
            ref = ans.references.get(card.refs[0]) if card.refs and ans.route in LITERATURE_ROUTES else None
            if ref is None:
                return {"status": "no_source", "message": "這張沒有文獻出處"}
            if kind == "web":
                return self._show_web(ans, gen, n, card, ref)
            abstract, title = self.abstract_for(ref)
            if not abstract and self.web is not None:
                # 沒有摘要（例如指引 PDF）就直接開原文網頁
                res = self._show_web(ans, gen, n, card, ref)
                if res["status"] == "ok":
                    return {"status": "ok", "message": f"第 {n} 張沒有摘要，已打開原文網頁"}
                if res["status"] == "no_source":
                    return {"status": "no_abstract", "message": "這篇沒有摘要，也沒有可以開的原文網址"}
                return res
            return self._show_abstract(ans, gen, n, card, abstract, title)
        except _Stale:
            return {"status": "stale", "message": "已經有新的回答，請再說一次要看第幾張"}
        except PubMedError as e:
            return {"status": "error", "message": str(e)}
        except Exception:  # 審查 #10：任何意外都回一句話，不讓主持人畫面毫無反應
            return {"status": "error", "message": "無法顯示原文"}

    def close(self) -> dict:
        with self.state.lock:
            self.state.close_source_view()
        self.state._changed()
        return {"status": "ok", "message": "已關閉原文"}

    def page_png(self) -> bytes | None:
        """目前顯示中的那一頁（加螢光框）。"""
        v, deck = self.state.source_view, self._page_deck
        if not v or v.get("kind") != "page" or deck is None:
            return None
        try:
            return deck.render_png(v["page"] - 1, width=PAGE_WIDTH, phrases=tuple(highlight_phrases(v["quote"])))
        except (IndexError, DeckError):
            return None

    # ---- OE 簡報（§6.2c）：在投影幕點參考文獻 ----

    @staticmethod
    def resolve_ref(ans, ref: str) -> dict | None:
        """ref："n:<編號>"（能可靠對應的文獻）或 "o:<序號>"（其他引用文獻）。"""
        oe = getattr(ans, "oe", None)
        kind, _, key = str(ref).partition(":")
        if oe is None or not key.isdigit():
            return None
        if kind == "n":
            return next((c for c in oe.citations if str(c.get("index")) == key), None)
        if kind == "o" and int(key) < len(oe.other_citations):
            return oe.other_citations[int(key)]
        return None

    def open_reference(self, ref: str) -> dict:
        """在投影幕打開這篇文獻的網頁（順序同 §6.2a：PMC → DOI → PubMed → OpenEvidence 給的連結）。"""
        st = self.state
        with st.lock:
            ans, gen = st.answer, st.source_gen
            playing = st.summary_path if st.temp_kind in ("oe", "summary") else None
        if playing is not None:  # 投影上那份 OE 簡報／摘要簡報自己的文獻，不是目前的回答（審查 C2）
            from .oe_deck import deck_ref

            c = deck_ref(playing, ref)
        else:
            c = self.resolve_ref(ans, ref)
        if c is None:
            return {"status": "not_found", "message": "找不到這篇文獻"}
        try:
            url = self._web_url(c)
            if not url:
                return {"status": "no_source", "message": "這篇沒有可以開的原文網址"}
            if self.web is None:
                return {"status": "error", "message": "投影幕無法開網頁"}
            with st.lock:
                if not self._fresh(ans, gen):
                    raise _Stale
            self.web.open(url)
            label = c.get("title") or url
            try:
                self._commit(ans, gen, {"kind": "web", "ref": ref, "ref_detail": dict(c), "citation": label, "url": url})
            except _Stale:
                st.close_web_async()
                raise
        except _Stale:
            return {"status": "stale", "message": "已經有新的回答"}
        except Exception:
            return {"status": "error", "message": "無法打開原文網頁"}
        return {"status": "ok", "message": "已打開原文網頁"}

    # ---- 原文重點摘要簡報（§6.2b）：讀出投影上那份原文的全文 ----

    def read_full(self, get_bytes: Callable[[str], tuple[bytes, str]] = fetch_bytes) -> SourceDoc:
        st = self.state
        with st.lock:
            view, ans = st.source_view, st.answer
        if not view:
            raise SourceUnreadable("請先打開要整理的原文")
        n = int(view.get("card") or 0)
        card = ans.cards[n - 1] if ans is not None and 1 <= n <= len(ans.cards) else None
        citation = view.get("citation") or (card.source if card else "")
        if view.get("kind") == "page":
            deck = self._page_deck
            if deck is None:
                raise SourceUnreadable("找不到這份資料")
            pages = [deck.page_text(i) for i in range(deck.page_count)]
            if not any(p.strip() for p in pages):
                raise SourceUnreadable("這份是掃描檔，讀不到文字")
            return SourceDoc(deck.name, citation, pages, paged=True, full_text=True, origin="pdf")
        ref = self._ref_for(view, ans, card)
        if ref is None:
            raise SourceUnreadable("這張沒有文獻出處")
        title = ref.get("title") or citation
        if view.get("kind") == "web":
            url = str(view.get("url") or "")
            m = _PMC_URL.search(url)
            if m:
                text = self._pmc_text(m.group(1))
                if text:
                    return SourceDoc(title, citation, [text], paged=False, full_text=True, origin="pmc")
            else:
                try:
                    data, _ = get_bytes(url)
                except SourceUnreadable:
                    data = b""
                if data.startswith(b"%PDF"):
                    return SourceDoc(title, citation, pdf_pages(data), paged=True, full_text=True, origin="web")
        return self.full_doc(ref, citation, get_bytes)

    @staticmethod
    def _ref_for(view: dict, ans, card) -> dict | None:
        if view.get("ref"):  # 從 OE 簡報點開的文獻（§6.2c）：用打開當時記下的那篇
            return view.get("ref_detail") or SourceViewer.resolve_ref(ans, view["ref"])
        return ans.references.get(card.refs[0]) if card is not None and card.refs else None

    def current_ref(self) -> dict | None:
        """投影上打開的原文是哪一篇文獻；簡報／參考資料的頁面回 None。"""
        st = self.state
        with st.lock:
            view, ans = st.source_view, st.answer
        if not view or view.get("kind") == "page" or ans is None:
            return None
        n = int(view.get("card") or 0)
        card = ans.cards[n - 1] if 1 <= n <= len(ans.cards) else None
        return self._ref_for(view, ans, card)

    def full_doc(self, ref: dict, citation: str = "",
                 get_bytes: Callable[[str], tuple[bytes, str]] = fetch_bytes) -> SourceDoc:
        """全文：參考資料 PDF → PMC → Europe PMC → OpenAlex；都沒有丟 NeedsFallback（改請 OE 整理）。"""
        doc = fetch_full_text(ref, self.library, self.http_get, get_bytes, self._pmc_text)
        if doc is None:
            raise NeedsFallback(ref)
        if citation:
            doc.citation = citation
        return doc

    def abstract_doc(self, ref: dict, citation: str = "") -> SourceDoc:
        """最後退路：只讀摘要（封面照實標明）。"""
        abstract, abs_title = self.abstract_for(ref)
        if not abstract:
            raise SourceUnreadable("讀不到這篇的全文或摘要")
        return SourceDoc(abs_title or ref.get("title") or citation, citation, [abstract], paged=False,
                         full_text=False, note=ABSTRACT_ONLY, origin="abstract")

    def _pmc_text(self, pmcid: str) -> str:
        try:
            return pmc_fulltext(pmcid, http_get=self.http_get)
        except PubMedError:
            return ""

    # ---- 寫入投影：查詢期間換了回答或按了關閉就作廢（審查 #1）----

    def _fresh(self, ans, gen) -> bool:
        return self.state.answer is ans and self.state.source_gen == gen

    def _commit(self, ans, gen, view: dict, page_deck: Deck | None = None) -> None:
        st = self.state
        with st.lock:
            if not self._fresh(ans, gen):
                raise _Stale
            was_web = bool(st.source_view and st.source_view.get("kind") == "web")
            st.source_view = view
            if page_deck is not None:
                self._page_deck = page_deck
            if was_web and view.get("kind") != "web":
                st.close_web_async()  # 從網頁換成摘要／那一頁：關掉網頁視窗（不佔著鎖）
        st._changed()

    # ---- 三種原文 ----

    def _show_page(self, ans, gen, n: int, card, note: str) -> dict:
        deck = self._deck_for(card.doc, ans)
        if deck is None:
            if card.doc == DECK_LABEL:
                return {"status": "no_source", "message": "簡報已經換過，找不到回答時那份簡報"}
            return {"status": "no_source", "message": f"找不到「{card.doc}」這份資料"}
        if not 1 <= card.page <= deck.page_count:
            return {"status": "no_source", "message": f"「{card.doc}」沒有第 {card.page} 頁"}
        self._seq += 1
        self._commit(ans, gen, {"kind": "page", "card": n, "doc": card.doc, "page": card.page, "quote": card.quote,
                                "citation": card.source, "url": f"/source/page.png?v={self._seq}"}, page_deck=deck)
        return {"status": "ok", "message": note}

    def abstract_for(self, ref: dict) -> tuple[str, str]:
        """回傳（摘要, 標題）；OpenEvidence 文獻有 PMID 就直接取，否則用 DOI 到 PubMed 找。"""
        if ref.get("abstract"):
            return ref["abstract"], ref.get("title", "")
        if ref.get("pmid"):
            rec = fetch_pmid(ref["pmid"], http_get=self.http_get)
            if rec:
                ref.update(abstract=rec.abstract)
                return rec.abstract, rec.title or ref.get("title", "")
        elif ref.get("doi"):
            rec = lookup_doi(ref["doi"], http_get=self.http_get)
            if rec:
                ref.update(abstract=rec.abstract, pmid=rec.pmid)  # 記住，下次不用再查
                return rec.abstract, rec.title or ref.get("title", "")
        return "", ref.get("title", "")

    def _show_abstract(self, ans, gen, n: int, card, abstract: str, title: str) -> dict:
        text = normalize_keep(abstract) if abstract else ""
        m = match_quote(card.quote, abstract) if abstract and card.quote else None
        if not text:
            note = "PubMed 找不到這篇的摘要"
        elif m is None:  # 審查 #13：說明為什麼沒有螢光
            note = "摘要中沒有與引用句逐字相同的句子" + \
                   ("（OpenEvidence 的引用句是它整理過的文字）" if ans.route == "openevidence" else "")
        else:
            note = ""
        self._commit(ans, gen, {"kind": "abstract", "card": n, "title": title, "citation": card.source,
                                "text": text, "mark": [m.start, m.end] if m else None, "note": note})
        if not text:
            return {"status": "no_abstract", "message": "PubMed 找不到這篇的摘要，可以說「打開原文網頁」"}
        return {"status": "ok", "message": f"已顯示第 {n} 張的摘要"}

    def _web_url(self, ref: dict) -> str:
        """PMC 開放全文 → DOI → PubMed → OpenEvidence 給的連結（指引 PDF 等）。
        沒有 PMID 就直接開 DOI，不再查（審查 #4：省一輪網路查詢）。"""
        pmid, doi = str(ref.get("pmid") or ""), str(ref.get("doi") or "")
        if pmid:
            try:
                pmc = pmc_id(pmid, http_get=self.http_get)
            except PubMedError:
                pmc = ""
            if pmc:
                return f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc}/"
        if doi:
            return f"https://doi.org/{doi}"
        if pmid:
            return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        href = str(ref.get("href") or "").strip()
        return href if _HTTP.match(href) else ""

    def _show_web(self, ans, gen, n: int, card, ref: dict) -> dict:
        url = self._web_url(ref)
        if not url:
            return {"status": "no_source", "message": "這篇沒有可以開的原文網址"}
        if self.web is None:
            return {"status": "error", "message": "投影幕無法開網頁"}
        with self.state.lock:
            if not self._fresh(ans, gen):
                raise _Stale  # 查詢期間已按關閉或換了回答：不開視窗
        self.web.open(url)
        try:
            self._commit(ans, gen, {"kind": "web", "card": n, "citation": card.source, "url": url})
        except _Stale:
            self.state.close_web_async()  # 剛開的視窗也關掉
            raise
        return {"status": "ok", "message": f"已打開第 {n} 張的原文網頁"}

    def _deck_for(self, doc: str, ans) -> Deck | None:
        if doc == DECK_LABEL:
            cur = self.state.deck
            if not ans.deck_hash or (cur is not None and cur.content_hash == ans.deck_hash):
                return cur
            return self._open(Path(ans.deck_path), ans.deck_hash) if ans.deck_path else None
        lib = self.library
        ref = lib.docs.get(doc) if lib is not None else None
        return self._open(ref.path) if ref is not None else None

    def _open(self, path: Path, want_hash: str = "") -> Deck | None:
        key = f"{path}|{want_hash}"
        if key not in self._decks:
            try:
                deck = Deck(path)
            except DeckError:
                return None
            if want_hash and deck.content_hash != want_hash:
                return None  # 檔案內容已變：不是回答時那份
            if len(self._decks) >= 8:
                self._decks.pop(next(iter(self._decks)))
            self._decks[key] = deck
        return self._decks[key]
