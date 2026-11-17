"""PubMed 搜尋（搜尋不限 OpenEvidence，可以查其他醫學資料庫；來源標期刊 citation）。

用 NCBI E-utilities（免金鑰）：esearch 找 PMID，efetch 取書目與摘要。只用有摘要的文章——
字卡的原文片段要逐字對得上摘要才算數。不送使用者 email（只帶 tool 名稱）。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TOOL = "xiaoshen"
TIMEOUT = 20


class PubMedError(Exception):
    pass


@dataclass
class PubMedRecord:
    pmid: str
    title: str
    authors: list[str]
    journal: str
    year: str
    volume: str
    issue: str
    pages: str
    doi: str
    abstract: str

    def citation(self) -> str:
        """例：Long B et al. J Emerg Med 2018;55(2):192-205. PMID 29731287"""
        who = (self.authors[0] + (" et al." if len(self.authors) > 1 else ".")) if self.authors else ""
        where = " ".join(x for x in (self.journal, self.year) if x)
        if self.volume:
            where += f";{self.volume}" + (f"({self.issue})" if self.issue else "")
        if self.pages:
            where += f":{self.pages}"
        return " ".join(x for x in (who, f"{where}." if where else "", f"PMID {self.pmid}") if x)

    def reference(self, index: int) -> dict:
        """字卡 PNG 的參考文獻欄位（與 OpenEvidence 文獻同格式）。"""
        return {"index": index, "title": self.title, "journal": self.journal, "year": self.year,
                "doi": self.doi, "first_author": self.authors[0] if self.authors else "",
                "authors": ", ".join(self.authors), "pmid": self.pmid,
                "abstract": self.abstract}  # 顯示出處原文用（§6.2a）


def _default_get(url: str, timeout: float = TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": f"{TOOL}/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def parse_efetch(xml: str) -> list[PubMedRecord]:
    root = ET.fromstring(xml)
    out = []
    for art in root.iter("PubmedArticle"):
        mc = art.find("MedlineCitation")
        a = mc.find("Article") if mc is not None else None
        if a is None:
            continue
        parts = []
        for t in a.findall("Abstract/AbstractText"):
            txt = _text(t)
            if txt:
                label = t.get("Label")
                parts.append(f"{label}: {txt}" if label else txt)
        if not parts:
            continue  # 沒有摘要就沒有原文可以驗證
        ji = a.find("Journal/JournalIssue")
        year = _text(ji.find("PubDate/Year")) if ji is not None else ""
        if not year and ji is not None:
            m = re.search(r"\d{4}", _text(ji.find("PubDate/MedlineDate")))
            year = m.group(0) if m else ""
        authors = []
        for au in a.findall("AuthorList/Author"):
            last, ini = _text(au.find("LastName")), _text(au.find("Initials"))
            name = " ".join(x for x in (last, ini) if x) or _text(au.find("CollectiveName"))
            if name:
                authors.append(name)
        doi = next((_text(e) for e in a.findall("ELocationID") if e.get("EIdType") == "doi"), "")
        out.append(PubMedRecord(
            pmid=_text(mc.find("PMID")), title=_text(a.find("ArticleTitle")), authors=authors,
            journal=_text(a.find("Journal/ISOAbbreviation")) or _text(a.find("Journal/Title")), year=year,
            volume=_text(ji.find("Volume")) if ji is not None else "",
            issue=_text(ji.find("Issue")) if ji is not None else "",
            pages=_text(a.find("Pagination/MedlinePgn")), doi=doi, abstract="\n".join(parts)))
    return out


def _efetch(ids: list[str], http_get: Callable[[str], str]) -> list[PubMedRecord]:
    f = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(ids), "retmode": "xml", "tool": TOOL})
    return parse_efetch(http_get(f"{EUTILS}/efetch.fcgi?{f}"))


def fetch_pmid(pmid: str, http_get: Callable[[str], str] = _default_get) -> PubMedRecord | None:
    """用 PMID 直接取那一篇（OpenEvidence 有給 PMID 時）；沒有摘要回 None。"""
    try:
        recs = _efetch([str(pmid)], http_get)
    except (OSError, ValueError, KeyError, ET.ParseError) as e:
        raise PubMedError("PubMed 暫時無法連線") from e
    return next((r for r in recs if r.pmid == str(pmid)), None)


EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_FIELD_TAG = re.compile(r"\[[^\]]*\]")
_DOI_TERM = re.compile(r'"([^"]+)"\[doi\]', re.I)
_HTML_TAG = re.compile(r"<[^>]+>")


def _europepmc_query(query: str) -> str:
    """PubMed 檢索式轉 Europe PMC：[doi] 改 DOI:"…"，其他欄位標記（[Mesh]、[tiab]…）拿掉，只找 PubMed 收錄的（SRC:MED）。"""
    q = _DOI_TERM.sub(lambda m: f'DOI:"{m.group(1)}"', query)
    q = re.sub(r"\s+", " ", _FIELD_TAG.sub("", q)).strip()
    return f"({q}) AND SRC:MED"


def _epmc_record(r: dict) -> PubMedRecord | None:
    abstract = r.get("abstractText") or ""
    # <h4>Background</h4>text → 「Background: text」，和 PubMed 的段落標籤格式一致
    abstract = re.sub(r"<h4>(.*?)</h4>", r"\n\1: ", abstract, flags=re.S)
    abstract = re.sub(r"[ \t]+", " ", _HTML_TAG.sub("", abstract)).strip()
    if not abstract or not r.get("pmid"):
        return None
    authors = [a.strip() for a in str(r.get("authorString") or "").rstrip(".").split(",") if a.strip()]
    ji = r.get("journalInfo") or {}
    journal = (ji.get("journal") or {}).get("isoabbreviation") or (ji.get("journal") or {}).get("title") or ""
    return PubMedRecord(pmid=str(r["pmid"]), title=str(r.get("title") or ""), authors=authors, journal=journal,
                        year=str(r.get("pubYear") or ""), volume=str(ji.get("volume") or ""),
                        issue=str(ji.get("issue") or ""), pages=str(r.get("pageInfo") or ""),
                        doi=str(r.get("doi") or ""), abstract=abstract)


def _search_europepmc(query: str, retmax: int, http_get: Callable[[str], str]) -> list[PubMedRecord]:
    """NCBI esearch 故障時（2026-11-05 實機兩度回 500）改用 Europe PMC 找 PubMed 文章（使用者同意）。
    找到 PMID 後仍到 NCBI efetch 取書目與摘要；efetch 也不行才用 Europe PMC 自己的摘要。"""
    try:
        q = urllib.parse.urlencode({"query": _europepmc_query(query), "format": "json", "resultType": "core",
                                    "pageSize": retmax})
        results = json.loads(http_get(f"{EUROPEPMC}?{q}"))["resultList"]["result"]
    except (OSError, ValueError, KeyError) as e:
        raise PubMedError("PubMed 暫時無法連線") from e
    ids = [str(r["pmid"]) for r in results if r.get("pmid")]
    if not ids:
        return []
    try:
        recs = _efetch(ids, http_get)
    except (OSError, ValueError, KeyError, ET.ParseError):
        recs = [rec for rec in (_epmc_record(r) for r in results) if rec is not None]
    order = {pmid: i for i, pmid in enumerate(ids)}
    return sorted(recs, key=lambda r: order.get(r.pmid, len(order)))


def search_pubmed(query: str, retmax: int = 8, http_get: Callable[[str], str] = _default_get) -> list[PubMedRecord]:
    """依相關性取前 retmax 篇，回傳有摘要的文章（保持 PubMed 排序）。NCBI 搜尋故障時改用 Europe PMC 找。"""
    try:
        q = urllib.parse.urlencode({"db": "pubmed", "term": query, "retmax": retmax, "sort": "relevance",
                                    "retmode": "json", "tool": TOOL})
        ids = json.loads(http_get(f"{EUTILS}/esearch.fcgi?{q}"))["esearchresult"]["idlist"]
    except (OSError, ValueError, KeyError):
        return _search_europepmc(query, retmax, http_get)
    if not ids:
        return []
    try:
        recs = _efetch(ids, http_get)
    except (OSError, ValueError, KeyError, ET.ParseError) as e:
        raise PubMedError("PubMed 暫時無法連線") from e
    order = {pmid: i for i, pmid in enumerate(ids)}
    return sorted(recs, key=lambda r: order.get(r.pmid, len(order)))


def lookup_doi(doi: str, http_get: Callable[[str], str] = _default_get) -> PubMedRecord | None:
    """用 DOI 找 PubMed 上同一篇（OpenEvidence 文獻顯示摘要用，§6.2a）。找不到回 None。"""
    # 加引號：DOI 裡的括號、分號會被當成查詢語法；取回後核對 DOI，不對就當沒找到（審查 #5：不能拿別篇的摘要當出處）
    recs = search_pubmed(f'"{doi}"[doi]', retmax=3, http_get=http_get)
    want = doi.strip().lower()
    return next((r for r in recs if r.doi.strip().lower() == want), None)


def pmc_id(pmid: str, http_get: Callable[[str], str] = _default_get) -> str:
    """PubMed 文章在 PMC 的開放全文編號（例 PMC12060654）；沒有就回空字串。"""
    try:
        q = urllib.parse.urlencode({"ids": pmid, "format": "json", "tool": TOOL})
        records = json.loads(http_get(f"https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/?{q}"))["records"]
    except (OSError, ValueError, KeyError) as e:
        raise PubMedError("PubMed 暫時無法連線") from e
    return next((str(r["pmcid"]) for r in records if r.get("pmcid")), "")


def _pmc_paragraphs(el) -> list[str]:
    """一節的標題與段落，依出現順序（子節遞迴）。"""
    out: list[str] = []
    for child in el:
        tag = child.tag
        if tag in ("title", "p"):
            txt = _text(child)
            if txt:
                out.append(txt)
        elif tag in ("sec", "list", "list-item", "boxed-text", "table-wrap", "caption"):
            out.extend(_pmc_paragraphs(child))
    return out


def pmc_fulltext(pmcid: str, http_get: Callable[[str], str] = _default_get) -> str:
    """PMC 開放全文的文字（標題、摘要、內文各節）。沒有內文（只有摘要）回空字串（原文重點摘要 §6.2b）。"""
    try:
        q = urllib.parse.urlencode({"db": "pmc", "id": pmcid.removeprefix("PMC"), "retmode": "xml", "tool": TOOL})
        root = ET.fromstring(http_get(f"{EUTILS}/efetch.fcgi?{q}"))
    except (OSError, ValueError, ET.ParseError) as e:
        raise PubMedError("PubMed 暫時無法連線") from e
    art = root.find(".//article")
    body = art.find("body") if art is not None else None
    if body is None:
        return ""
    parts = [_text(art.find(".//article-meta/title-group/article-title"))]
    abstract = art.find(".//article-meta/abstract")
    if abstract is not None:
        parts += _pmc_paragraphs(abstract)
    parts += _pmc_paragraphs(body)
    return "\n\n".join(p for p in parts if p)
