"""讀取 題庫\\題庫.xlsx：依（簡報檔名, 頁碼）對應事先準備的題目。

欄位依序：簡報檔名、頁碼、題目、選項A–F、正解（A–F）、解析、出處。
壞列略過並回報「第 N 列：原因」，不中斷載入。
"""

from __future__ import annotations

from pathlib import Path

from .quiz import Question

LETTERS = "ABCDEF"


def _cell(value) -> str:
    return "" if value is None else str(value).strip()


def _deck_name(value) -> str:
    """簡報檔名容錯：去掉 .pdf 副檔名；Excel 把數字檔名存成 123.0 時還原成 123。"""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    name = _cell(value)
    return name[:-4] if name.lower().endswith(".pdf") else name


def _parse_row(row: tuple) -> tuple[tuple[str, int], Question]:
    cells = list(row) + [None] * (12 - len(row))
    deck, page, stem = _deck_name(cells[0]), cells[1], _cell(cells[2])
    options = [_cell(v) for v in cells[3:9]]
    answer, explanation, source = _cell(cells[9]).upper(), _cell(cells[10]), _cell(cells[11])

    if not deck:
        raise ValueError("缺簡報檔名")
    if isinstance(page, float) and page.is_integer():
        page = int(page)
    if isinstance(page, str) and page.strip().isdigit():
        page = int(page.strip())
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError(f"頁碼不是正整數：{_cell(cells[1])}")
    if not stem:
        raise ValueError("缺題目")
    # 選項必須從 A 連續填寫
    while options and not options[-1]:
        options.pop()
    if "" in options:
        raise ValueError("選項中間有空白")
    if len(options) < 2:
        raise ValueError("至少需要選項 A、B")
    if len(answer) != 1 or answer not in LETTERS[: len(options)]:
        raise ValueError(f"正解需為 {LETTERS[0]}–{LETTERS[len(options) - 1]}：{answer or '空白'}")

    question = Question(
        id=f"{deck}-p{page}",
        stem=stem,
        options=options,
        answer=LETTERS.index(answer),
        explanation=explanation,
        source=source,
    )
    return (deck, page), question


def load_bank(xlsx: Path) -> tuple[dict[tuple[str, int], Question], list[str]]:
    if not xlsx.exists():
        return {}, []
    try:
        from openpyxl import load_workbook

        wb = load_workbook(xlsx, read_only=True, data_only=True)
        rows = list(wb.worksheets[0].iter_rows(min_row=2, values_only=True))
        wb.close()
    except Exception as e:
        return {}, [f"題庫無法開啟：{e}"]

    bank: dict[tuple[str, int], Question] = {}
    warnings: list[str] = []
    for offset, row in enumerate(rows):
        excel_row = offset + 2
        if all(_cell(v) == "" for v in row):
            continue
        try:
            key, question = _parse_row(row)
        except ValueError as e:
            warnings.append(f"第 {excel_row} 列：{e}")
            continue
        if key in bank:
            warnings.append(f"第 {excel_row} 列：與前面重複（{key[0]} 第 {key[1]} 頁），已略過")
            continue
        bank[key] = question
    return bank, warnings
