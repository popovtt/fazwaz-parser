import argparse
import csv
import logging
import time
from typing import Dict, List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fazwaz_project_units import (
    DETAIL_FIELDS,
    parse_unit_detail_page,
    setup_logging,
)
from fazwaz_property_units import (
    LISTING_FIELDS,
    normalize_listing_prices_from_detail,
    parse_listing_page,
)
from main import safe_filename, sleep_jitter


DEFAULT_START_URL = "https://www.fazwaz.com/property-for-sale/thailand/phuket"
DEFAULT_LOG_FILE = "fazwaz_search_project_units.log"

logger = logging.getLogger("fazwaz_project_units")


def update_page_query(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if page <= 1:
        query.pop("page", None)
    else:
        query["page"] = str(page)

    return urlunparse(parsed._replace(query=urlencode(query)))


def collect_search_unit_rows(
    start_url: str,
    page_limit: Optional[int] = None,
    listing_limit: Optional[int] = None,
) -> List[Dict[str, Optional[str]]]:
    rows: List[Dict[str, Optional[str]]] = []
    seen_units: Set[str] = set()
    page = 1

    while True:
        if page_limit is not None and page > page_limit:
            break

        page_url = update_page_query(start_url, page)
        print(f"Page {page}: {page_url}")
        page_rows = parse_listing_page(page_url, page)

        if not page_rows:
            print("   No listing rows found, stopping")
            logger.warning(
                "Stopping because search page contained no listing rows | page=%s | url=%s",
                page,
                page_url,
            )
            break

        new_rows: List[Dict[str, Optional[str]]] = []

        for row in page_rows:
            unit_id = row.get("unit_id")

            if not unit_id or unit_id in seen_units:
                continue

            seen_units.add(unit_id)
            new_rows.append(row)

            if listing_limit is not None and len(rows) + len(new_rows) >= listing_limit:
                break

        rows.extend(new_rows)
        print(f"   Added: {len(new_rows)} | Total unique: {len(rows)}")

        if not new_rows:
            logger.warning(
                "Stopping because search page contained no new unique units | page=%s | url=%s | total_unique=%s",
                page,
                page_url,
                len(rows),
            )
            break

        if listing_limit is not None and len(rows) >= listing_limit:
            return rows[:listing_limit]

        page += 1
        sleep_jitter()

    return rows


def enrich_rows_with_project_unit_parser(
    rows: List[Dict[str, Optional[str]]],
    detail_limit: Optional[int] = None,
) -> List[Dict[str, Optional[str]]]:
    rows_to_enrich = rows if detail_limit is None else rows[:detail_limit]
    total = len(rows_to_enrich)

    for index, row in enumerate(rows_to_enrich, 1):
        unit_url = row.get("listing_url")
        print(f"   [{index}/{total}] Unit detail: {unit_url or 'no url'}")

        if not unit_url:
            logger.warning("Unit skipped detail parsing because listing URL is missing")
            continue

        listing_unit_id = row.get("unit_id")

        try:
            detail = parse_unit_detail_page(unit_url)
        except Exception:
            logger.exception("Unit detail parsing crashed | unit_url=%s", unit_url)
            detail = {}

        if not detail:
            logger.warning("Unit detail data is empty | unit_url=%s", unit_url)
        else:
            row.update(detail)

        if listing_unit_id:
            row["unit_id"] = listing_unit_id

        normalize_listing_prices_from_detail(row)

        if index < total:
            sleep_jitter()

    return rows


def build_fieldnames(rows: List[Dict[str, Optional[str]]]) -> List[str]:
    fieldnames: Set[str] = set()

    for row in rows:
        fieldnames.update(row.keys())

    preferred_fields = LISTING_FIELDS + DETAIL_FIELDS
    dynamic_fields = sorted(
        field for field in fieldnames
        if field not in preferred_fields
    )

    ordered_fields = [
        field
        for field in preferred_fields + dynamic_fields
        if field in fieldnames
    ]
    return list(dict.fromkeys(ordered_fields))


def write_csv(rows: List[Dict[str, Optional[str]]], output_file: str) -> None:
    fieldnames = build_fieldnames(rows)

    with open(output_file, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def default_output_name(start_url: str) -> str:
    parsed = urlparse(start_url)
    slug = parsed.path.strip("/").replace("/", "_") or "fazwaz_search_project_units"

    if parsed.query:
        query_slug = parsed.query.replace("&", "_").replace("=", "-")
        slug = f"{slug}_{query_slug}"

    return f"{safe_filename(slug)}.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Parse FazWaz property search pages, then open every listing detail "
            "page using the price/detail parser from fazwaz_project_units.py."
        )
    )
    parser.add_argument(
        "start_url",
        nargs="?",
        default=DEFAULT_START_URL,
        help=f"FazWaz property search URL. Defaults to {DEFAULT_START_URL}.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output CSV file. Defaults to a name derived from the URL.",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        help="Read only the first N search result pages.",
    )
    parser.add_argument(
        "--listing-limit",
        type=int,
        help="Collect only the first N unique listing/unit URLs.",
    )
    parser.add_argument(
        "--detail-limit",
        type=int,
        help="Open detail pages only for the first N collected listings.",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE,
        help=f"Diagnostic log file. Defaults to {DEFAULT_LOG_FILE}.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(args.log_file)
    start_time = time.time()

    logger.info("Search parser started | args=%s", vars(args))
    print(f"Log file: {args.log_file}")
    print(f"Start URL: {args.start_url}")

    rows = collect_search_unit_rows(
        args.start_url,
        page_limit=args.page_limit,
        listing_limit=args.listing_limit,
    )

    if rows:
        enrich_rows_with_project_unit_parser(rows, detail_limit=args.detail_limit)

    if not rows:
        print("No data parsed")
        logger.warning("Search parser finished without parsed rows")
        return

    output_file = args.output or default_output_name(args.start_url)
    write_csv(rows, output_file)

    advertised_total = next(
        (row.get("advertised_total") for row in rows if row.get("advertised_total")),
        None,
    )
    elapsed = round(time.time() - start_time, 1)

    print("\nDone")
    print(f"File: {output_file}")
    print(f"Rows: {len(rows)}")
    print(f"Advertised total: {advertised_total or 'unknown'}")
    print(f"Time: {elapsed} sec")
    logger.info(
        "Search parser finished | output_file=%s | rows=%s | advertised_total=%s | elapsed_sec=%s",
        output_file,
        len(rows),
        advertised_total,
        elapsed,
    )


if __name__ == "__main__":
    main()
