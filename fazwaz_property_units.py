import argparse
import csv
import html
import logging
import re
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from fazwaz_project_units import (
    DETAIL_FIELDS,
    parse_unit_detail_page,
)
from main import (
    SITE_URL,
    clean_text,
    extract_unit_id,
    format_thb,
    format_thb_per_sqm,
    get_transaction_type,
    parse_embedded_unit_prices,
    request_with_retry,
    safe_filename,
    sleep_jitter,
)


DEFAULT_START_URL = "https://www.fazwaz.com/property-for-sale/thailand/phuket"
DEFAULT_LOG_FILE = "fazwaz_property_units.log"

logger = logging.getLogger("fazwaz_property_units")

LISTING_FIELDS = [
    "source_url",
    "page_number",
    "listing_index",
    "listing_url",
    "unit_id",
    "listing_transaction",
    "listing_title",
    "listing_project_name",
    "listing_project_url",
    "listing_location",
    "listing_price",
    "listing_price_per_sqm",
    "listing_beds",
    "listing_baths",
    "listing_size",
    "listing_property_type",
    "listing_updated",
    "listing_tags",
    "listing_features",
    "advertised_total",
    "page_result_range",
]


def setup_logging(log_file: str = DEFAULT_LOG_FILE) -> None:
    logging.basicConfig(
        filename=log_file,
        filemode="w",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def update_page_query(url: str, page: int) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if page <= 1:
        query.pop("page", None)
    else:
        query["page"] = str(page)

    return urlunparse(
        parsed._replace(query=urlencode(query))
    )


def normalize_lines(text: str) -> List[str]:
    lines = []

    for line in text.splitlines():
        cleaned = clean_text(line)

        if cleaned:
            lines.append(cleaned)

    return lines


def extract_total_count(soup: BeautifulSoup) -> Optional[str]:
    text = soup.get_text("\n", strip=True)
    patterns = [
        r"([\d,]+)\s+Properties available on FazWaz",
        r"\bof\s+([\d,]+)\s+Results\b",
        r"currently has\s+([\d,]+)\s+properties for sale",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return match.group(1).replace(",", "")

    return None


def extract_page_result_range(soup: BeautifulSoup) -> Optional[str]:
    text = clean_text(soup.get_text(" ", strip=True)) or ""
    match = re.search(r"\b\d+\s*-\s*\d+\s+of\s+[\d,]+\s+Results\b", text)
    return match.group(0) if match else None


def is_listing_href(href: Optional[str]) -> bool:
    if not href:
        return False

    return bool(
        re.search(r"/property-(?:sales|for-sale)/", href)
        and re.search(r"-u\d+(?:\D|$)", href)
    )


def find_listing_card(anchor) -> object:
    fallback = anchor

    for parent in anchor.parents:
        if not getattr(parent, "get_text", None):
            continue

        text = parent.get_text("\n", strip=True)

        if len(text) > 10000:
            break

        if "Details:" in text and (
            "Request Details" in text or "View Details" in text
        ):
            return parent

        fallback = parent

    return fallback


def first_listing_title(card, listing_url: str) -> Optional[str]:
    for anchor in card.select("a[href]"):
        href = urljoin(SITE_URL, anchor.get("href", "").strip())

        if href.split("?")[0] != listing_url.split("?")[0]:
            continue

        text = clean_text(anchor.get_text(" ", strip=True))

        if text and "for sale" in text.lower():
            return text

    return None


def parse_project_link(card) -> Tuple[Optional[str], Optional[str]]:
    for anchor in card.select('a[href*="/projects/"]'):
        name = clean_text(anchor.get_text(" ", strip=True))
        href = anchor.get("href")

        if name and href:
            return name, urljoin(SITE_URL, href.strip())

    return None, None


def parse_price_line(lines: Iterable[str]) -> Tuple[Optional[str], Optional[str]]:
    amount_pattern = r"(?:฿\s*[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?\s*[₴$€£])"
    price = None
    price_per_sqm = None

    for line in lines:
        if price is None:
            amount_match = re.search(amount_pattern, line)

            if amount_match:
                price = clean_text(amount_match.group(0))

        if "/SqM" in line:
            per_sqm_match = re.search(
                rf"\(?\s*({amount_pattern}\s*/SqM)\s*\)?",
                line,
            )

            if per_sqm_match:
                price_per_sqm = clean_text(per_sqm_match.group(1))

        if price and price_per_sqm:
            break

    return price, price_per_sqm


def parse_embedded_listing_prices(page_html: str) -> Dict[str, Dict[str, Optional[str]]]:
    prices = parse_embedded_unit_prices(page_html)
    decoded_html = html.unescape(page_html)
    pattern = re.compile(
        r'"_id":"(?P<unit_id>\d+)".{0,5000}?'
        r'"abbr":"THB".{0,5000}?'
        r'"current_price":"(?P<price>[\d.]+)".{0,5000}?'
        r'"indoor_area":(?P<area>[\d.]+|null)',
        re.DOTALL,
    )

    for match in pattern.finditer(decoded_html):
        unit_id = match.group("unit_id")

        if unit_id in prices:
            continue

        price = float(match.group("price"))
        area_value = match.group("area")
        area = None if area_value == "null" else float(area_value)
        prices[unit_id] = {
            "unit_price": format_thb(price),
            "unit_price_per_sqm": format_thb_per_sqm(price, area),
        }

    return prices


def parse_details(lines: List[str]) -> Dict[str, Optional[str]]:
    text = " ".join(lines)
    result: Dict[str, Optional[str]] = {}
    patterns = {
        "listing_beds": r"([\d.]+)\s+Bedroom\(s\)",
        "listing_baths": r"([\d.]+)\s+Bathroom\(s\)",
        "listing_size": r"([\d,.]+)\s+SqM",
    }

    for field, pattern in patterns.items():
        match = re.search(pattern, text)

        if match:
            value = match.group(1).strip()
            result[field] = f"{value} SqM" if field == "listing_size" else value

    for index, line in enumerate(lines):
        match = re.search(r"Property Type:\s*([^|]+)$", line)

        if match and clean_text(match.group(1)):
            result["listing_property_type"] = clean_text(match.group(1))
            break

        if line.rstrip(":").lower() == "property type" and index + 1 < len(lines):
            result["listing_property_type"] = clean_text(lines[index + 1])
            break

    return result


def parse_location(lines: List[str], price: Optional[str]) -> Optional[str]:
    if not price:
        return None

    price_index = next(
        (index for index, line in enumerate(lines) if line.startswith(price)),
        None,
    )

    if price_index is None:
        return None

    for line in reversed(lines[:price_index]):
        lowered = line.lower()

        if "updated" in lowered or "listed" in lowered:
            continue

        if "phuket" in lowered:
            return line

    return None


def parse_updated(lines: Iterable[str]) -> Optional[str]:
    for line in lines:
        match = re.search(r"\bUpdated:?\s*(.+)$", line, re.IGNORECASE)

        if match:
            return clean_text(match.group(1))

    return None


def parse_features(lines: List[str]) -> Tuple[Optional[str], Optional[str]]:
    tags = []
    features = []
    after_details = False
    skip_fragments = {
        "details:",
        "request details",
        "schedule viewing",
        "view details",
        "see all",
        "create alert",
    }

    for line in lines:
        lowered = line.lower()

        if "details:" in lowered:
            after_details = True
            continue

        if not after_details:
            continue

        if "request details" in lowered or "schedule viewing" in lowered:
            break

        if any(fragment in lowered for fragment in skip_fragments):
            continue

        if "verified listing" in lowered or "rent-to-own" in lowered:
            tags.append(line)
            continue

        if (
            " view" in lowered
            or "beach" in lowered
            or "quota" in lowered
            or "floor " in lowered
            or "year built" in lowered
            or "pets :" in lowered
            or "cam fee" in lowered
            or "sinking fund" in lowered
        ):
            features.append(line)

    unique_tags = list(dict.fromkeys(tags))
    unique_features = list(dict.fromkeys(features))

    return (
        " | ".join(unique_tags) if unique_tags else None,
        " | ".join(unique_features) if unique_features else None,
    )


def parse_listing_card(
    card,
    listing_url: str,
    page_number: int,
    listing_index: int,
    source_url: str,
    advertised_total: Optional[str],
    page_result_range: Optional[str],
    embedded_price: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Optional[str]]:
    text = card.get_text("\n", strip=True)
    lines = normalize_lines(text)
    price, price_per_sqm = parse_price_line(lines)
    thb_price = (embedded_price or {}).get("unit_price")
    thb_price_per_sqm = (embedded_price or {}).get("unit_price_per_sqm")
    project_name, project_url = parse_project_link(card)
    tags, features = parse_features(lines)

    row: Dict[str, Optional[str]] = {
        "source_url": source_url,
        "page_number": str(page_number),
        "listing_index": str(listing_index),
        "listing_url": listing_url,
        "unit_id": extract_unit_id(listing_url),
        "listing_transaction": get_transaction_type(listing_url),
        "listing_title": first_listing_title(card, listing_url),
        "listing_project_name": project_name,
        "listing_project_url": project_url,
        "listing_location": parse_location(lines, price),
        "listing_price": thb_price or price,
        "listing_price_per_sqm": thb_price_per_sqm or price_per_sqm,
        "listing_updated": parse_updated(lines),
        "listing_tags": tags,
        "listing_features": features,
        "advertised_total": advertised_total,
        "page_result_range": page_result_range,
    }
    row.update(parse_details(lines))

    return {key: value for key, value in row.items() if value}


def parse_listing_page(page_url: str, page_number: int) -> List[Dict[str, Optional[str]]]:
    logger.info("Parsing listing page | page=%s | url=%s", page_number, page_url)
    response = request_with_retry(page_url)

    if not response:
        logger.warning("Listing page request failed | page=%s | url=%s", page_number, page_url)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    embedded_prices = parse_embedded_listing_prices(response.text)
    advertised_total = extract_total_count(soup)
    page_result_range = extract_page_result_range(soup)
    seen_units: Set[str] = set()
    rows: List[Dict[str, Optional[str]]] = []

    for anchor in soup.select("a[href]"):
        href = anchor.get("href")

        if not is_listing_href(href):
            continue

        listing_url = urljoin(SITE_URL, href.strip()).split("?")[0]
        unit_id = extract_unit_id(listing_url)

        if not unit_id or unit_id in seen_units:
            continue

        seen_units.add(unit_id)
        card = find_listing_card(anchor)
        rows.append(
            parse_listing_card(
                card,
                listing_url,
                page_number,
                len(rows) + 1,
                page_url,
                advertised_total,
                page_result_range,
                embedded_prices.get(unit_id),
            )
        )

    logger.info(
        "Listing page parsed | page=%s | rows=%s | advertised_total=%s | result_range=%s",
        page_number,
        len(rows),
        advertised_total,
        page_result_range,
    )
    return rows


def normalize_listing_prices_from_detail(row: Dict[str, Optional[str]]) -> None:
    detail_price = row.get("unit_detail_price")
    detail_price_per_sqm = (
        row.get("unit_detail_price_per_sqm")
        or row.get("unit_basic_price_per_sqm")
    )

    if detail_price:
        row["listing_price"] = detail_price

    if detail_price_per_sqm:
        row["listing_price_per_sqm"] = detail_price_per_sqm


def enrich_with_unit_pages(
    rows: List[Dict[str, Optional[str]]],
    detail_limit: Optional[int] = None,
) -> List[Dict[str, Optional[str]]]:
    if detail_limit is not None:
        rows_to_enrich = rows[:detail_limit]
    else:
        rows_to_enrich = rows

    total = len(rows_to_enrich)

    for index, row in enumerate(rows_to_enrich, 1):
        listing_url = row.get("listing_url")
        print(f"   [{index}/{total}] Unit detail: {listing_url}")

        if not listing_url:
            continue

        try:
            listing_unit_id = row.get("unit_id")
            row.update(parse_unit_detail_page(listing_url))
            if listing_unit_id:
                row["unit_id"] = listing_unit_id
            normalize_listing_prices_from_detail(row)
        except Exception:
            logger.exception("Unit detail parsing crashed | unit_url=%s", listing_url)

        if index < total:
            sleep_jitter()

    return rows


def parse_property_search(
    start_url: str,
    page_limit: Optional[int] = None,
    listing_limit: Optional[int] = None,
    parse_details_enabled: bool = True,
    detail_limit: Optional[int] = None,
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
            break

        new_rows = []

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
                "Stopping because listing page contained no new unique units | page=%s | url=%s | total_unique=%s",
                page,
                page_url,
                len(rows),
            )
            break

        if listing_limit is not None and len(rows) >= listing_limit:
            rows = rows[:listing_limit]
            break

        page += 1
        sleep_jitter()

    if parse_details_enabled and rows:
        enrich_with_unit_pages(rows, detail_limit=detail_limit)

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
    slug = parsed.path.strip("/").replace("/", "_") or "fazwaz_property_units"
    return f"{safe_filename(slug)}.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Parse FazWaz property search pages and optionally open every unit "
            "detail page using the same unit detail parser as fazwaz_project_units."
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
        help="Output CSV file. Defaults to a name derived from the URL path.",
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
        "--no-details",
        action="store_true",
        help="Do not open unit detail pages; only save search-card data.",
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

    logger.info("Parser started | args=%s", vars(args))
    print(f"Log file: {args.log_file}")
    print(f"Start URL: {args.start_url}")

    rows = parse_property_search(
        args.start_url,
        page_limit=args.page_limit,
        listing_limit=args.listing_limit,
        parse_details_enabled=not args.no_details,
        detail_limit=args.detail_limit,
    )

    if not rows:
        print("No data parsed")
        logger.warning("Parser finished without parsed rows")
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
        "Parser finished | output_file=%s | rows=%s | advertised_total=%s | elapsed_sec=%s",
        output_file,
        len(rows),
        advertised_total,
        elapsed,
    )


if __name__ == "__main__":
    main()
