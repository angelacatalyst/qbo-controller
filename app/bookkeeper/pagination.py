"""QBO query pagination.

Intuit returns at most 1,000 rows per query. Asking for more and treating a
full page as the last page drops the rest of the company file.
"""

QBO_MAX_PAGE = 1000
MAX_PAGES = 200


def walk_pages(fetch_page, page_size: int = QBO_MAX_PAGE) -> tuple[list, bool, int]:
    """Walk STARTPOSITION pages until a short page or the safety cap.

    fetch_page(start, page_size) returns one page of records.
    A short or empty page means the walk is complete. Hitting MAX_PAGES
    returns is_complete=False so callers cannot treat a truncated file as full.
    Duplicate Ids are kept once; completeness still uses the raw page length.
    """
    size = min(max(int(page_size or QBO_MAX_PAGE), 1), QBO_MAX_PAGE)
    records: list = []
    seen: set[str] = set()
    start = 1
    pages = 0

    while pages < MAX_PAGES:
        page = fetch_page(start, size) or []
        pages += 1
        if not page:
            return records, True, len(records)
        for row in page:
            row_id = ""
            if isinstance(row, dict) and row.get("Id") is not None:
                row_id = str(row.get("Id"))
            if row_id:
                if row_id in seen:
                    continue
                seen.add(row_id)
            records.append(row)
        if len(page) < size:
            return records, True, len(records)
        start += size

    return records, False, len(records)
