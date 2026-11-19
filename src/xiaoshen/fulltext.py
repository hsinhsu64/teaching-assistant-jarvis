"""找一篇文獻的全文：參考資料裡的 PDF → PubMed Central → Europe PMC → OpenAlex 開放取用 PDF。

都找不到回 None，由呼叫端改請 OpenEvidence 整理這一篇，再不行才只讀摘要。
任何對外請求都不帶使用者的 email 或個人資料（所以不用 Unpaywall）。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Callable

from .answering import citation_label
from .library import Library, RefDoc
from .pubmed import _pmc_paragraphs, _text, lookup_doi, pmc_id
from .source_text import SourceDoc, SourceUnreadable, pdf_pages

ORIGIN_LABEL = {
    "pdf": "使用者提供的 PDF",
    "pmc": "PubMed Central 全文",
    "europepmc": "Europe PMC 全文",
    "openalex": "開放取用 PDF",
    "web": "原文網頁 PDF",
    "abstract": "只讀到摘要",
    "oe": "OpenEvidence 整理",
}
MIN_TITLE_CHARS = 20  # 篇名太短（例如只有「Guideline」）容易誤認，不用篇名比對
TITLE_WINDOW = 200    # 篇名要出現在第一頁開頭這麼多字以內（內文引用舊版篇名不算）
_DOI_ANY = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)


class NeedsFallback(Exception):
    """找不到全文：改請 OpenEvidence 整理這一篇（再不行讀摘要）。"""

    def __init__(self, ref: dict):
        super().__init__("找不到全文")
        self.ref = ref


def norm_title(text: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿]", "", text.lower())


def _has_doi(text: str, doi: str) -> bool:
    """完整的 DOI（前後不能接著英數字：10.x/…05.021 不等於 …05.0215）。"""
    return re.search(r"(?<![0-9a-z])" + re.escape(doi) + r"(?![0-9a-z])", text.lower()) is not None


def find_library_pdf(ref: dict, library: Library | None) -> RefDoc | None:
    """參考資料裡的同一篇，寧可找不到也不要認錯：
    先在所有檔案的第一頁找完整 DOI；找不到才比篇名（就是檔名，或出現在第一頁開頭），
    而且這篇有 DOI 時，第一頁印著別的 DOI 的檔案不用篇名認（同系列不同版本）。"""
    if library is None:
        return None
    docs = [d for d in library.docs.values() if not d.scanned and d.pages]
    doi = str(ref.get("doi") or "").strip().lower()
    if doi:
        for doc in docs:
            if _has_doi(doc.pages[0], doi):
                return doc
    title = norm_title(str(ref.get("title") or ""))
    if len(title) < MIN_TITLE_CHARS:
        return None
    for doc in docs:
        if doi and _DOI_ANY.search(doc.pages[0]):
            continue  # 第一頁有 DOI、但不是這篇的
        if title == norm_title(doc.name) or title in norm_title(doc.pages[0])[:len(title) + TITLE_WINDOW]:
            return doc
    return None


def europepmc_fulltext(pmcid: str, http_get: Callable[[str], str]) -> str:
    """Europe PMC 的開放全文（JATS XML）；沒有內文回空字串。"""
    try:
        root = ET.fromstring(http_get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"))
    except (OSError, ValueError, ET.ParseError):
        return ""
    art = root if root.tag == "article" else root.find(".//article")
    body = art.find("body") if art is not None else None
    if body is None:
        return ""
    parts = [_text(art.find(".//article-meta/title-group/article-title"))]
    abstract = art.find(".//article-meta/abstract")
    if abstract is not None:
        parts += _pmc_paragraphs(abstract)
    parts += _pmc_paragraphs(body)
    return "\n\n".join(p for p in parts if p)


def openalex_pdf_url(doi: str, http_get: Callable[[str], str]) -> str:
    """OpenAlex 記錄的開放取用 PDF 網址；沒有回空字串。"""
    try:
        data = json.loads(http_get(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}"))
    except (OSError, ValueError):
        return ""
    loc = data.get("best_oa_location") or {}
    return str(loc.get("pdf_url") or "")


def _pmcid(ref: dict, http_get: Callable[[str], str]) -> str:
    pmid = str(ref.get("pmid") or "")
    try:
        if not pmid and ref.get("doi"):
            rec = lookup_doi(str(ref["doi"]), http_get=http_get)
            pmid = rec.pmid if rec else ""
        return pmc_id(pmid, http_get=http_get) if pmid else ""
    except Exception:
        return ""


def fetch_full_text(ref: dict, library: Library | None, http_get: Callable[[str], str],
                    get_bytes: Callable[[str], tuple[bytes, str]],
                    pmc_text: Callable[[str], str]) -> SourceDoc | None:
    title = str(ref.get("title") or "")
    citation = citation_label(ref) if ref.get("index") is not None else title

    def doc(pages: list[str], paged: bool, origin: str) -> SourceDoc:
        return SourceDoc(title, citation, pages, paged=paged, full_text=True, origin=origin)

    found = find_library_pdf(ref, library)
    if found is not None:
        return doc(list(found.pages), True, "pdf")
    pmcid = _pmcid(ref, http_get)
    if pmcid:
        try:
            text = pmc_text(pmcid)
        except Exception:
            text = ""
        if text:
            return doc([text], False, "pmc")
        text = europepmc_fulltext(pmcid, http_get)
        if text:
            return doc([text], False, "europepmc")
    if ref.get("doi"):
        url = openalex_pdf_url(str(ref["doi"]), http_get)
        if url:
            try:
                data, _ = get_bytes(url)
                if data.startswith(b"%PDF"):
                    return doc(pdf_pages(data), True, "openalex")
            except (SourceUnreadable, OSError):
                pass
    return None
