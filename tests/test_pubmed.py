"""PubMed 搜尋（搜尋不限 OpenEvidence；來源要標期刊 citation）。"""

import json

import pytest

from xiaoshen.pubmed import PubMedError, PubMedRecord, parse_efetch, search_pubmed

XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle><MedlineCitation><PMID Version="1">29731287</PMID>
<DateCompleted><Year>2019</Year></DateCompleted>
<Article><Journal><JournalIssue><Volume>55</Volume><Issue>2</Issue><PubDate><Year>2018</Year><Month>Aug</Month></PubDate></JournalIssue>
<ISOAbbreviation>J Emerg Med</ISOAbbreviation></Journal>
<ArticleTitle>Controversies in Management of Hyperkalemia.</ArticleTitle>
<Pagination><MedlinePgn>192-205</MedlinePgn></Pagination>
<ELocationID EIdType="doi" ValidYN="Y">10.1016/j.jemermed.2018.04.004</ELocationID>
<Abstract><AbstractText Label="BACKGROUND">Hyperkalemia is a common electrolyte disorder.</AbstractText>
<AbstractText Label="CONCLUSIONS">Calcium should be given for <i>ECG changes</i> to stabilize the myocardium.</AbstractText></Abstract>
<AuthorList><Author><LastName>Long</LastName><Initials>B</Initials></Author>
<Author><LastName>Warix</LastName><Initials>JR</Initials></Author></AuthorList>
</Article></MedlineCitation></PubmedArticle>
<PubmedArticle><MedlineCitation><PMID Version="1">111</PMID>
<Article><Journal><JournalIssue><PubDate><MedlineDate>2020 Jan-Feb</MedlineDate></PubDate></JournalIssue>
<ISOAbbreviation>Kidney Int</ISOAbbreviation></Journal>
<ArticleTitle>No abstract here.</ArticleTitle></Article></MedlineCitation></PubmedArticle>
</PubmedArticleSet>"""


def test_parse_efetch_reads_citation_fields_and_abstract():
    recs = parse_efetch(XML)
    assert [r.pmid for r in recs] == ["29731287"]  # 沒有摘要的文章不用（沒有原文可以驗證）
    r = recs[0]
    assert r.journal == "J Emerg Med" and r.year == "2018" and r.volume == "55" and r.pages == "192-205"
    assert r.doi == "10.1016/j.jemermed.2018.04.004" and r.authors == ["Long B", "Warix JR"]
    assert "Calcium should be given for ECG changes to stabilize the myocardium." in r.abstract
    assert r.abstract.startswith("BACKGROUND: Hyperkalemia")


def test_citation_format():
    r = parse_efetch(XML)[0]
    assert r.citation() == "Long B et al. J Emerg Med 2018;55(2):192-205. PMID 29731287"
    solo = PubMedRecord("9", "T", ["Ho K"], "Lancet", "2021", "", "", "", "", "a")
    assert solo.citation() == "Ho K. Lancet 2021. PMID 9"


def test_search_calls_esearch_then_efetch():
    calls = []

    def get(url):
        calls.append(url)
        if "esearch" in url:
            return json.dumps({"esearchresult": {"idlist": ["29731287", "111"]}})
        return XML

    recs = search_pubmed("hyperkalemia AND calcium", retmax=5, http_get=get)
    assert [r.pmid for r in recs] == ["29731287"]
    assert "db=pubmed" in calls[0] and "retmax=5" in calls[0] and "sort=relevance" in calls[0]
    assert "hyperkalemia+AND+calcium" in calls[0] or "hyperkalemia%20AND%20calcium" in calls[0]
    assert "id=29731287,111" in calls[1] or "id=29731287%2C111" in calls[1]
    assert "@" not in "".join(calls)  # 不送使用者 email


def test_search_no_hits_skips_efetch():
    calls = []
    recs = search_pubmed("zzz", http_get=lambda u: calls.append(u) or json.dumps({"esearchresult": {"idlist": []}}))
    assert recs == [] and len(calls) == 1


def test_network_failure_raises_pubmed_error():
    def boom(url):
        raise OSError("down")

    with pytest.raises(PubMedError):
        search_pubmed("x", http_get=boom)


def test_fetch_pmid_uses_efetch_only():
    from xiaoshen.pubmed import fetch_pmid

    calls = []

    def get(url):
        calls.append(url)
        return EFETCH_ONE

    rec = fetch_pmid("29731287", http_get=get)
    assert rec.pmid == "29731287" and rec.abstract
    assert len(calls) == 1 and "efetch" in calls[0] and "id=29731287" in calls[0]


def test_fetch_pmid_without_abstract_returns_none():
    from xiaoshen.pubmed import fetch_pmid

    assert fetch_pmid("1", http_get=lambda u: "<PubmedArticleSet></PubmedArticleSet>") is None


EFETCH_ONE = """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>29731287</PMID><Article>
<Journal><JournalIssue><PubDate><Year>2018</Year></PubDate></JournalIssue><ISOAbbreviation>J Emerg Med</ISOAbbreviation></Journal>
<ArticleTitle>Controversies.</ArticleTitle><Abstract><AbstractText>Calcium first.</AbstractText></Abstract>
</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""


# ---- NCBI esearch 故障時改用 Europe PMC 找 PMID（使用者同意 2026-11-05）----

import urllib.error  # noqa: E402

EPMC = {"resultList": {"result": [
    {"pmid": "29731287", "title": "Controversies in Management of Hyperkalemia.", "authorString": "Long B, Warix JR.",
     "pubYear": "2018", "doi": "10.1016/j.jemermed.2018.04.004", "pageInfo": "192-205",
     "journalInfo": {"volume": "55", "issue": "2", "journal": {"isoabbreviation": "J Emerg Med"}},
     "abstractText": "<h4>Background</h4>Hyperkalemia is a common electrolyte disorder."},
    {"pmid": "222", "title": "No abstract", "authorString": "X Y."},
]}}


def _router(esearch_fails=True, efetch_fails=False, calls=None):
    def get(url):
        if calls is not None:
            calls.append(url)
        if "esearch.fcgi" in url and esearch_fails:
            raise urllib.error.HTTPError(url, 500, "Internal Server Error", None, None)
        if "efetch.fcgi" in url:
            if efetch_fails:
                raise urllib.error.HTTPError(url, 500, "Internal Server Error", None, None)
            return XML
        if "europepmc" in url:
            return json.dumps(EPMC)
        raise AssertionError(url)
    return get


def test_esearch_down_uses_europepmc_then_ncbi_efetch():
    calls = []
    recs = search_pubmed('("Bartter Syndrome"[Mesh]) AND treatment', http_get=_router(calls=calls))
    assert [r.pmid for r in recs] == ["29731287"] and recs[0].journal == "J Emerg Med"
    epmc = next(u for u in calls if "europepmc" in u)
    assert "SRC%3AMED" in epmc and "Mesh" not in epmc  # PubMed 欄位標記拿掉、只找 PubMed 收錄的文章
    assert any("efetch.fcgi" in u and "29731287" in u for u in calls)


def test_esearch_and_efetch_down_uses_europepmc_abstracts():
    recs = search_pubmed("bartter", http_get=_router(efetch_fails=True))
    assert len(recs) == 1
    r = recs[0]
    assert (r.pmid, r.journal, r.year, r.volume, r.issue, r.pages) == ("29731287", "J Emerg Med", "2018", "55", "2", "192-205")
    assert r.authors == ["Long B", "Warix JR"] and r.abstract == "Background: Hyperkalemia is a common electrolyte disorder."
    assert r.citation() == "Long B et al. J Emerg Med 2018;55(2):192-205. PMID 29731287"


def test_everything_down_raises_pubmed_error():
    def get(url):
        raise urllib.error.URLError("down")

    with pytest.raises(PubMedError):
        search_pubmed("bartter", http_get=get)


def test_ncbi_ok_never_calls_europepmc():
    calls = []

    def get(url):
        calls.append(url)
        return json.dumps({"esearchresult": {"idlist": ["29731287"]}}) if "esearch" in url else XML

    search_pubmed("bartter", http_get=get)
    assert not any("europepmc" in u for u in calls)


def test_doi_lookup_falls_back_with_doi_field():
    from xiaoshen.pubmed import lookup_doi

    calls = []
    rec = lookup_doi("10.1016/j.jemermed.2018.04.004", http_get=_router(calls=calls))
    assert rec is not None and rec.pmid == "29731287"
    assert any("DOI%3A%2210.1016" in u for u in calls)
