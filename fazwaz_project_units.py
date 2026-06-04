import argparse
import csv
import html
import json
import logging
import re
import time
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from bs4.element import NavigableString

from main import (
    BASE_URL,
    PROJECT_FIELDS,
    PROPERTY_TYPES,
    SITE_URL,
    UNIT_FIELDS,
    clean_text,
    extract_unit_id,
    format_thb,
    format_thb_per_sqm,
    get_transaction_type,
    parse_embedded_unit_prices,
    parse_project_info,
    parse_units,
    request_with_retry,
    safe_filename,
    sleep_jitter,
)


DETAIL_FIELD_PREFIX = "unit_detail_"
BASIC_INFORMATION_FIELD_PREFIX = "unit_basic_"
DEFAULT_LOG_FILE = "fazwaz_project_units.log"

logger = logging.getLogger("fazwaz_project_units")

DETAIL_FIELDS = [
    "unit_id",
    "unit_detail_title",
    "unit_detail_h1",
    "unit_detail_description",
    "unit_detail_canonical_url",
    "unit_detail_price",
    "unit_detail_price_currency",
    "unit_detail_breadcrumbs",
    "unit_detail_amenities",
    "unit_detail_images",
    "unit_basic_information",
]


def setup_logging(log_file: str = DEFAULT_LOG_FILE) -> None:
    logging.basicConfig(
        filename=log_file,
        filemode="w",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def log_missing_unit_fields(
    unit: Dict[str, Optional[str]],
    context: str,
    fields: Iterable[str],
    unit_url: Optional[str] = None,
    unit_id: Optional[str] = None,
) -> None:
    missing_fields = [field for field in fields if not unit.get(field)]

    if missing_fields:
        logged_unit_url = unit_url or unit.get("unit_url")
        logged_unit_id = (
            unit_id
            or unit.get("unit_id")
            or extract_unit_id(logged_unit_url)
        )
        logger.warning(
            "%s missing fields: %s | unit_url=%s | unit_id=%s",
            context,
            ", ".join(missing_fields),
            logged_unit_url,
            logged_unit_id,
        )


def log_project_unit_summary(
    project_url: str,
    soup: BeautifulSoup,
    units: List[Dict],
    embedded_prices: Dict[str, Dict[str, str]],
) -> None:
    available_unit_nodes = soup.select(".available-units-table-list")

    logger.info(
        "Project unit summary | project_url=%s | unit_nodes=%s | parsed_units=%s | embedded_prices=%s",
        project_url,
        len(available_unit_nodes),
        len(units),
        len(embedded_prices),
    )

    if available_unit_nodes and not units:
        logger.warning(
            "Unit nodes were found but no units were parsed | project_url=%s",
            project_url,
        )

    if not available_unit_nodes:
        possible_sections = soup.select(
            "[class*=available-unit], [class*=unit], [data-testid*=unit]"
        )
        logger.warning(
            "No unit nodes found with selector .available-units-table-list | project_url=%s | possible_unit_like_nodes=%s",
            project_url,
            len(possible_sections),
        )


def normalize_field_name(value: str, prefix: str = DETAIL_FIELD_PREFIX) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return f"{prefix}{normalized}" if normalized else prefix.rstrip("_")


def set_unique_value(
    values: Dict[str, Optional[str]],
    field_name: str,
    value: Optional[str],
) -> None:
    if not value:
        return

    if field_name not in values:
        values[field_name] = value
        return

    if values[field_name] == value:
        return

    index = 2

    while f"{field_name}_{index}" in values:
        if values[f"{field_name}_{index}"] == value:
            return

        index += 1

    values[f"{field_name}_{index}"] = value


def direct_text(tag) -> Optional[str]:
    text = " ".join(
        str(child).strip()
        for child in tag.children
        if isinstance(child, NavigableString)
    )

    return clean_text(text)


def first_meta_content(soup: BeautifulSoup, selectors: Iterable[str]) -> Optional[str]:
    for selector in selectors:
        tag = soup.select_one(selector)

        if tag and tag.has_attr("content"):
            value = clean_text(tag["content"])

            if value:
                return value

    return None


def parse_price_number(value: str) -> Optional[float]:
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)", value)

    if not match:
        return None

    return float(match.group(1).replace(",", ""))


def format_thb_text(value: float, suffix: str = "") -> str:
    return f"{format_thb(value)}{suffix}"


def parse_embedded_unit_thb_data(
    page_html: str,
    unit_id: Optional[str],
) -> Dict[str, Optional[float]]:
    if not unit_id:
        return {}

    decoded_html = html.unescape(page_html)
    pattern = re.compile(
        r'"_id":"'
        + re.escape(unit_id)
        + r'".*?'
        r'"abbr":"THB".*?'
        r'"current_price":"(?P<price>[\d.]+)".*?'
        r'"indoor_area":(?P<area>[\d.]+|null)',
        re.DOTALL,
    )
    match = pattern.search(decoded_html)

    if not match:
        return {}

    area_value = match.group("area")

    return {
        "current_price": float(match.group("price")),
        "indoor_area": None if area_value == "null" else float(area_value),
    }


def get_page_currency_rate_to_thb(
    soup: BeautifulSoup,
    current_price_thb: Optional[float],
) -> Optional[float]:
    if not current_price_thb:
        return None

    title_tag = soup.find("title")
    candidates = []

    if title_tag:
        candidates.append(title_tag.get_text(" ", strip=True))

    candidates.extend(
        tag.get("content", "")
        for tag in soup.select(
            'meta[property="og:title"], meta[name="twitter:title"]'
        )
    )

    for candidate in candidates:
        match = re.search(r"for\s+([\d,]+(?:\.\d+)?)₴", candidate)

        if not match:
            continue

        displayed_price = parse_price_number(match.group(1))

        if displayed_price:
            return current_price_thb / displayed_price

    return None


def format_thb_monthly(value: float) -> str:
    return format_thb_text(value, "/mo")


def replace_title_price_with_thb(
    value: Optional[str],
    thb_price: Optional[str],
) -> Optional[str]:
    if not value or not thb_price:
        return value

    return re.sub(
        r"\bfor\s+\d[\d,]*(?:\.\d+)?\s*[^\w\s|]+",
        f"for {thb_price}",
        value,
        count=1,
    )


def remove_non_thb_money_values(
    values: Dict[str, Optional[str]],
) -> Dict[str, Optional[str]]:
    non_thb_symbols = ("₴", "$", "€", "£")
    money_field_fragments = (
        "price",
        "fee",
        "fund",
        "amount",
        "rent",
        "rental",
    )
    normalized: Dict[str, Optional[str]] = {}

    for key, value in values.items():
        if key == "unit_basic_information" and isinstance(value, str):
            parts = [
                part.strip()
                for part in value.split("|")
                if not any(symbol in part for symbol in non_thb_symbols)
            ]
            normalized[key] = " | ".join(parts) if parts else None
            continue

        if (
            isinstance(value, str)
            and any(symbol in value for symbol in non_thb_symbols)
            and any(fragment in key.lower() for fragment in money_field_fragments)
        ):
            normalized[key] = None
            continue

        normalized[key] = value

    return normalized


def parse_direct_thb_cam_fee(soup: BeautifulSoup) -> Optional[str]:
    text = clean_text(soup.get_text(" ", strip=True)) or ""
    patterns = [
        r"common area maintenance fee is ฿[\d,]+(?:\.\d+)? per square meter.*?"
        r"and is ฿(?P<amount>[\d,]+(?:\.\d+)?) for this",
        r"CAM Fee.*?฿(?P<amount>[\d,]+(?:\.\d+)?)/mo",
        r"CAM Fee.*?฿(?P<amount>[\d,]+(?:\.\d+)?)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            amount = parse_price_number(match.group("amount"))

            if amount is not None:
                return format_thb_monthly(amount)

    return None


def normalize_detail_prices_to_thb(
    values: Dict[str, Optional[str]],
    thb_data: Dict[str, Optional[float]],
    direct_thb_values: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Optional[str]]:
    normalized: Dict[str, Optional[str]] = dict(values)
    direct_thb_values = direct_thb_values or {}

    current_price = thb_data.get("current_price")
    indoor_area = thb_data.get("indoor_area")

    if current_price:
        normalized["unit_detail_price"] = format_thb(current_price)
        normalized["unit_detail_price_currency"] = "THB"

    normalized["unit_detail_title"] = replace_title_price_with_thb(
        normalized.get("unit_detail_title"),
        normalized.get("unit_detail_price"),
    )

    price_per_sqm = format_thb_per_sqm(current_price, indoor_area)

    if price_per_sqm:
        normalized["unit_basic_price_per_sqm"] = price_per_sqm
        normalized["unit_detail_price_per_sqm"] = price_per_sqm

    basic_information = normalized.get("unit_basic_information")

    if basic_information and price_per_sqm:
        basic_information = re.sub(
            r"Price per SqM: [^|]+",
            f"Price per SqM: {price_per_sqm} ",
            basic_information,
        )
        normalized["unit_basic_information"] = basic_information

    cam_fee = direct_thb_values.get("unit_basic_cam_fee")

    if cam_fee:
        normalized["unit_basic_cam_fee"] = cam_fee
        basic_information = normalized.get("unit_basic_information")

        if basic_information:
            basic_information = re.sub(
                r"CAM Fee: [^|]+",
                f"CAM Fee: {cam_fee} ",
                basic_information,
            )
            normalized["unit_basic_information"] = basic_information

    normalized = remove_non_thb_money_values(normalized)

    return normalized


def parse_json_ld_blocks(soup: BeautifulSoup) -> List[Dict]:
    blocks: List[Dict] = []

    for script in soup.select('script[type="application/ld+json"]'):
        raw_json = script.string or script.get_text()

        if not raw_json:
            continue

        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON-LD block skipped")
            continue

        if isinstance(data, list):
            blocks.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            graph = data.get("@graph")

            if isinstance(graph, list):
                blocks.extend(item for item in graph if isinstance(item, dict))
            else:
                blocks.append(data)

    return blocks


def parse_json_ld_unit_data(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {}

    for block in parse_json_ld_blocks(soup):
        block_type = block.get("@type")

        if isinstance(block_type, list):
            type_values = {str(value).lower() for value in block_type}
        else:
            type_values = {str(block_type).lower()}

        if "breadcrumblist" in type_values:
            breadcrumbs = []

            for element in block.get("itemListElement", []):
                if isinstance(element, dict):
                    name = element.get("name")

                    if name:
                        breadcrumbs.append(str(name))

            if breadcrumbs:
                result["unit_detail_breadcrumbs"] = " > ".join(breadcrumbs)

            continue

        if not (
            {"product", "offer", "apartment", "house", "residence", "place"}
            & type_values
        ):
            continue

        name = block.get("name")
        description = block.get("description")
        image = block.get("image")
        offers = block.get("offers")

        if name and not result.get("unit_detail_title"):
            result["unit_detail_title"] = clean_text(str(name))

        if description and not result.get("unit_detail_description"):
            result["unit_detail_description"] = clean_text(str(description))

        if image and not result.get("unit_detail_images"):
            if isinstance(image, list):
                result["unit_detail_images"] = " | ".join(str(item) for item in image)
            else:
                result["unit_detail_images"] = str(image)

        if isinstance(offers, dict):
            price = offers.get("price")
            currency = offers.get("priceCurrency")

            if price and not result.get("unit_detail_price"):
                result["unit_detail_price"] = str(price)

            if currency and not result.get("unit_detail_price_currency"):
                result["unit_detail_price_currency"] = str(currency)

    return result


def parse_label_value_blocks(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    values: Dict[str, Optional[str]] = {}
    selectors = [
        ".property-info-element",
        ".property-detail-element",
        ".property-details-item",
        ".property-detail__item",
        ".detail-property-element",
    ]

    for block in soup.select(",".join(selectors)):
        parts = [clean_text(part) for part in block.stripped_strings]
        parts = [part for part in parts if part]

        if len(parts) < 2:
            continue

        label = parts[-1]
        value = " ".join(parts[:-1])
        field_name = normalize_field_name(label)

        if value and field_name not in values:
            values[field_name] = value

    return values


def parse_basic_information_items(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    values: Dict[str, Optional[str]] = {}
    raw_items: List[str] = []

    for index, item in enumerate(soup.select(".basic-information__item"), 1):
        parts = [clean_text(part) for part in item.stripped_strings]
        parts = [part for part in parts if part]

        if not parts:
            continue

        label_tag = item.select_one(".basic-information-topic")
        value_tag = item.select_one(".basic-information-info")

        if not label_tag:
            label_tag = item.select_one(
                (
                    "[class*=label], [class*=topic], [class*=title], "
                    "[class*=name], [class*=key], [class*=caption]"
                )
            )

        if not value_tag:
            value_tag = item.select_one(
                "[class*=info], [class*=value], [class*=amount], "
                "[class*=number], [class*=data]"
            )

        label = (
            direct_text(label_tag)
            or clean_text(label_tag.get_text(" ", strip=True))
            if label_tag
            else None
        )
        value = (
            clean_text(value_tag.get_text(" ", strip=True))
            if value_tag
            else None
        )

        if not label and len(parts) >= 2:
            label = parts[-1]

        if not value and len(parts) >= 2:
            value = " ".join(parts[:-1])

        if not label:
            label = f"item_{index}"

        if not value:
            value = " ".join(parts)

        field_name = normalize_field_name(
            label,
            prefix=BASIC_INFORMATION_FIELD_PREFIX,
        )
        raw_items.append(f"{label}: {value}")
        set_unique_value(values, field_name, value)

    if raw_items:
        values["unit_basic_information"] = " | ".join(raw_items)

    return values


def parse_amenities(soup: BeautifulSoup) -> Dict[str, Optional[str]]:
    amenities: List[str] = []
    selectors = [
        ".amenities__item",
        ".property-amenities__item",
        ".facilities-list li",
        ".features-list li",
        "[class*=amenit] li",
    ]

    for tag in soup.select(",".join(selectors)):
        value = clean_text(tag.get_text(" ", strip=True))

        if value and value not in amenities:
            amenities.append(value)

    return {
        "unit_detail_amenities": " | ".join(amenities) if amenities else None,
    }


def parse_unit_detail_page(unit_url: str) -> Dict[str, Optional[str]]:
    logger.info("Parsing unit detail page | unit_url=%s", unit_url)
    response = request_with_retry(unit_url)

    if not response:
        logger.warning("Unit detail request failed | unit_url=%s", unit_url)
        return {}

    soup = BeautifulSoup(response.text, "html.parser")
    unit_id = extract_unit_id(unit_url)

    if not unit_id:
        logger.warning("Unit id was not extracted from URL | unit_url=%s", unit_url)

    thb_data = parse_embedded_unit_thb_data(response.text, unit_id)

    if not thb_data:
        logger.warning(
            "Embedded THB data not found on unit detail page | unit_url=%s | unit_id=%s",
            unit_url,
            unit_id,
        )

    canonical_tag = soup.select_one('link[rel="canonical"]')
    h1_tag = soup.find("h1")
    direct_thb_values = {
        "unit_basic_cam_fee": parse_direct_thb_cam_fee(soup),
    }

    result: Dict[str, Optional[str]] = {
        "unit_id": unit_id,
        "unit_detail_h1": (
            clean_text(h1_tag.get_text(" ", strip=True))
            if h1_tag
            else None
        ),
        "unit_detail_title": first_meta_content(
            soup,
            [
                'meta[property="og:title"]',
                'meta[name="twitter:title"]',
                "title",
            ],
        ),
        "unit_detail_description": first_meta_content(
            soup,
            [
                'meta[property="og:description"]',
                'meta[name="description"]',
                'meta[name="twitter:description"]',
            ],
        ),
        "unit_detail_canonical_url": (
            canonical_tag["href"].strip()
            if canonical_tag and canonical_tag.has_attr("href")
            else unit_url
        ),
    }

    title_tag = soup.find("title")

    if not result.get("unit_detail_title") and title_tag:
        result["unit_detail_title"] = clean_text(title_tag.get_text(" ", strip=True))

    result.update(parse_json_ld_unit_data(soup))
    result.update(parse_label_value_blocks(soup))
    result.update(parse_basic_information_items(soup))
    result.update(parse_amenities(soup))
    result = normalize_detail_prices_to_thb(
        result,
        thb_data,
        direct_thb_values,
    )
    result = {key: value for key, value in result.items() if value}

    log_missing_unit_fields(
        result,
        "Unit detail parsed with incomplete data",
        [
            "unit_id",
            "unit_detail_title",
            "unit_detail_h1",
            "unit_detail_price",
            "unit_basic_information",
        ],
        unit_url=unit_url,
        unit_id=unit_id,
    )

    if len(result) <= 2:
        logger.warning(
            "Unit detail page produced very few fields | unit_url=%s | unit_id=%s | fields=%s",
            unit_url,
            unit_id,
            sorted(result.keys()),
        )

    return result


def parse_project_with_unit_pages(
    project_url: str,
    limit: Optional[int] = None,
) -> List[Dict]:
    response = request_with_retry(project_url)

    if not response:
        logger.warning("Project request failed | project_url=%s", project_url)
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    project = parse_project_info(soup, project_url)

    if not project:
        print(f"[PARSE ERROR] Project data not found: {project_url}")
        logger.warning("Project data not found | project_url=%s", project_url)
        return []

    embedded_prices = parse_embedded_unit_prices(response.text)
    units = parse_units(soup, embedded_prices)
    log_project_unit_summary(project_url, soup, units, embedded_prices)

    if not units:
        logger.warning("Project has no parsed units, writing project-only row | project_url=%s", project_url)
        return [project]

    rows: List[Dict] = []
    if limit is not None:
        units = units[:limit]

    total = len(units)

    for index, unit in enumerate(units, 1):
        unit_url = unit.get("unit_url")
        print(f"   [{index}/{total}] Unit: {unit_url or 'no url'}")
        unit_id = extract_unit_id(unit_url)

        logger.info(
            "Parsing project unit | project_url=%s | index=%s/%s | unit_url=%s | unit_id=%s",
            project_url,
            index,
            total,
            unit_url,
            unit_id,
        )

        if not unit_url:
            logger.warning(
                "Unit skipped detail parsing because unit URL is missing | project_url=%s | index=%s",
                project_url,
                index,
            )
        elif not unit_id:
            logger.warning(
                "Unit URL does not contain extractable unit id | project_url=%s | index=%s | unit_url=%s",
                project_url,
                index,
                unit_url,
            )

        if unit_id and unit_id not in embedded_prices:
            logger.warning(
                "Embedded listing price not found for unit | project_url=%s | index=%s | unit_url=%s | unit_id=%s",
                project_url,
                index,
                unit_url,
                unit_id,
            )

        log_missing_unit_fields(
            unit,
            "Project unit listing parsed with incomplete data",
            [
                "unit_url",
                "unit_transaction",
                "unit_price",
                "unit_price_per_sqm",
                "unit_beds",
                "unit_baths",
                "unit_size",
            ],
        )

        try:
            unit_detail = parse_unit_detail_page(unit_url) if unit_url else {}
        except Exception:
            logger.exception(
                "Unit detail parsing crashed | project_url=%s | index=%s | unit_url=%s",
                project_url,
                index,
                unit_url,
            )
            unit_detail = {}

        if unit_url and not unit_detail:
            logger.warning(
                "Unit detail data is empty | project_url=%s | index=%s | unit_url=%s",
                project_url,
                index,
                unit_url,
            )

        rows.append(
            {
                **project,
                **unit,
                **unit_detail,
            }
        )

        if index < total:
            sleep_jitter()

    return rows


def get_project_links(area: str, page_limit: Optional[int] = None) -> List[str]:
    print(f"Collecting project links for area: {area}")

    links: List[str] = []
    page = 1

    while True:
        if page_limit is not None and page > page_limit:
            break

        url = f"{BASE_URL}/{area}?type={PROPERTY_TYPES}&page={page}"
        print(f"   Directory page {page}: {url}")

        response = request_with_retry(url)

        if not response:
            logger.warning("Directory page request failed | area=%s | page=%s | url=%s", area, page, url)
            break

        soup = BeautifulSoup(response.text, "html.parser")
        items = soup.find_all("a", class_="site-map-item-link")

        if not items:
            logger.info("No project links found on directory page | area=%s | page=%s | url=%s", area, page, url)
            break

        page_links = []

        for item in items:
            href = item.get("href")

            if href:
                page_links.append(urljoin(SITE_URL, href.strip()))

        links.extend(page_links)
        print(f"      Found: {len(page_links)}")

        page += 1
        sleep_jitter()

    print(f"Project links collected: {len(links)}")
    return links


def parse_area_with_unit_pages(
    area: str,
    unit_limit: Optional[int] = None,
    project_limit: Optional[int] = None,
    page_limit: Optional[int] = None,
) -> List[Dict]:
    links = get_project_links(area, page_limit)

    if project_limit is not None:
        links = links[:project_limit]

    rows: List[Dict] = []
    total = len(links)

    for index, link in enumerate(links, 1):
        print(f"\n[{index}/{total}] Project: {link}")
        project_rows = parse_project_with_unit_pages(link, unit_limit)

        if project_rows:
            rows.extend(project_rows)
            print(f"   Rows added: {len(project_rows)}")
        else:
            print("   Skipped")
            logger.warning("Project skipped without rows | project_url=%s", link)

        if index < total:
            sleep_jitter()

    return rows


def is_property_search_url(url: Optional[str]) -> bool:
    if not url:
        return False

    path = urlparse(url).path
    return "/property-for-sale/" in path or "/property-sales/" in path


def parse_property_search_with_unit_pages(
    start_url: str,
    page_limit: Optional[int] = None,
    listing_limit: Optional[int] = None,
    detail_limit: Optional[int] = None,
) -> List[Dict]:
    from fazwaz_property_units import parse_property_search

    return parse_property_search(
        start_url,
        page_limit=page_limit,
        listing_limit=listing_limit,
        parse_details_enabled=True,
        detail_limit=detail_limit,
    )


def build_fieldnames(rows: List[Dict]) -> List[str]:
    fieldnames: Set[str] = set()

    for row in rows:
        fieldnames.update(row.keys())

    preferred_fields = PROJECT_FIELDS + UNIT_FIELDS + DETAIL_FIELDS

    if any(field.startswith("listing_") for field in fieldnames):
        from fazwaz_property_units import LISTING_FIELDS

        preferred_fields = PROJECT_FIELDS + LISTING_FIELDS + UNIT_FIELDS + DETAIL_FIELDS

    dynamic_fields = sorted(
        field for field in fieldnames
        if field not in preferred_fields
    )

    return [
        field
        for field in preferred_fields + dynamic_fields
        if field in fieldnames
    ]


def default_output_name(project_url: str, rows: List[Dict]) -> str:
    project_name = next(
        (
            row.get("project_name")
            for row in rows
            if row.get("project_name")
        ),
        None,
    )

    if project_name:
        base_name = project_name
    else:
        parsed_url = urlparse(project_url)
        base_name = parsed_url.path.rstrip("/").split("/")[-1] or "fazwaz_project"

    return f"{safe_filename(base_name)}_unit_pages.csv"


def default_area_output_name(area: str) -> str:
    return f"{safe_filename(area)}_unit_pages.csv"


def write_csv(rows: List[Dict], output_file: str) -> None:
    fieldnames = build_fieldnames(rows)

    with open(output_file, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Parse a FazWaz project page, open every unit page, "
            "and save combined project/unit details to CSV."
        )
    )
    parser.add_argument(
        "project_url",
        nargs="?",
        help="FazWaz project URL. Omit it when using --area.",
    )
    parser.add_argument(
        "--area",
        help="FazWaz project-directory area, for example: phuket.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output CSV file. Defaults to '<project>_unit_pages.csv'.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help=(
            "Parse only the first N units from a project page. For property "
            "search URLs, this also limits collected listings."
        ),
    )
    parser.add_argument(
        "--listing-limit",
        type=int,
        help="Collect only the first N listings from a property search URL.",
    )
    parser.add_argument(
        "--detail-limit",
        type=int,
        help="Open detail pages only for the first N listings from a property search URL.",
    )
    parser.add_argument(
        "--project-limit",
        type=int,
        help="Parse only the first N projects from the area directory.",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        help="Read only the first N project-directory pages for the area.",
    )
    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG_FILE,
        help=f"Diagnostic log file. Defaults to {DEFAULT_LOG_FILE}.",
    )

    args = parser.parse_args()

    if not args.project_url and not args.area:
        parser.error("provide either project_url or --area")

    if args.project_url and args.area:
        parser.error("provide project_url or --area, not both")

    return args


def main() -> None:
    args = parse_args()
    setup_logging(args.log_file)
    start_time = time.time()

    logger.info("Parser started | args=%s", vars(args))
    print(f"Log file: {args.log_file}")

    if args.area:
        print(f"Area: {args.area}")
        rows = parse_area_with_unit_pages(
            args.area,
            unit_limit=args.limit,
            project_limit=args.project_limit,
            page_limit=args.page_limit,
        )
        output_file = args.output or default_area_output_name(args.area)
    elif is_property_search_url(args.project_url):
        print(f"Property search: {args.project_url}")
        rows = parse_property_search_with_unit_pages(
            args.project_url,
            page_limit=args.page_limit,
            listing_limit=args.listing_limit or args.limit,
            detail_limit=args.detail_limit,
        )
        output_file = args.output or default_output_name(args.project_url, rows)
    else:
        print(f"Project: {args.project_url}")
        rows = parse_project_with_unit_pages(args.project_url, args.limit)
        output_file = args.output or default_output_name(args.project_url, rows)

    if not rows:
        print("No data parsed")
        logger.warning("Parser finished without parsed rows")
        return

    write_csv(rows, output_file)

    elapsed = round(time.time() - start_time, 1)

    print("\nDone")
    print(f"File: {output_file}")
    print(f"Rows: {len(rows)}")
    print(f"Time: {elapsed} sec")
    logger.info(
        "Parser finished | output_file=%s | rows=%s | elapsed_sec=%s",
        output_file,
        len(rows),
        elapsed,
    )


if __name__ == "__main__":
    main()
