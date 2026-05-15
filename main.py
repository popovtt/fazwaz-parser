import csv
import html
import random
import re
import time
import requests
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
from urllib.parse import urljoin


# ================== CONFIG ==================
def safe_filename(name: str) -> str:
    return (
        name
        .replace("/", "_")
        .replace("\\", "_")
        .replace(" ", "_")
    )

BASE_URL = "https://www.fazwaz.com/project-directory/thailand"
PROPERTY_TYPES = "condo,apartment,penthouse,villa,house,townhouse"

AREA = "phuket"
SAFE_AREA = safe_filename(AREA)
OUTPUT_FILE = f"{SAFE_AREA}.csv"
SITE_URL = "https://www.fazwaz.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE_URL}/{AREA}",
}

COOKIES = {
    "currency": "THB",
}

MIN_DELAY = 0.8
MAX_DELAY = 1.7

MAX_RETRIES = 5
BACKOFF = 1.6

TIMEOUT = 15

PROJECT_FIELDS = [
    "project_name",
    "address",
    "address_link",
    "project_url",
]

UNIT_FIELDS = [
    "unit_index",
    "unit_url",
    "unit_transaction",
    "unit_price",
    "unit_price_per_sqm",
    "unit_beds",
    "unit_baths",
    "unit_size",
    "unit_floor",
    "unit_quota",
    "unit_furnishing",
    "unit_view",
    "unit_features",
    "unit_updated",
]


# ================== HELPERS ==================

def sleep_jitter():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def clean_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    text = " ".join(value.split())
    return text if text else None


def format_thb(value: float) -> str:
    return f"{round(value):,}฿"


def format_thb_per_sqm(price: float, area: Optional[float]) -> Optional[str]:
    if not area:
        return None

    return f"{round(price / area):,}฿/SqM"


def extract_unit_id(unit_url: Optional[str]) -> Optional[str]:
    if not unit_url:
        return None

    match = re.search(r"-u(\d+)(?:\D|$)", unit_url)
    return match.group(1) if match else None


def parse_embedded_unit_prices(page_html: str) -> Dict[str, Dict[str, str]]:
    decoded_html = html.unescape(page_html)
    prices: Dict[str, Dict[str, str]] = {}

    pattern = re.compile(
        r'"_index":"unit_index_v3".*?'
        r'"_id":"(?P<unit_id>\d+)".*?'
        r'"current_price":"(?P<price>[\d.]+)".*?'
        r'"indoor_area":(?P<area>[\d.]+|null)',
        re.DOTALL,
    )

    for match in pattern.finditer(decoded_html):
        price = float(match.group("price"))
        area_value = match.group("area")
        area = None if area_value == "null" else float(area_value)

        prices[match.group("unit_id")] = {
            "unit_price": format_thb(price),
            "unit_price_per_sqm": format_thb_per_sqm(price, area),
        }

    return prices


def request_with_retry(url: str) -> Optional[requests.Response]:

    delay = 1

    for attempt in range(1, MAX_RETRIES + 1):

        try:
            r = requests.get(
                url,
                headers=HEADERS,
                cookies=COOKIES,
                timeout=TIMEOUT
            )

            if r.status_code == 200:
                return r

            print(f"[WARN] {url} → {r.status_code}")

        except Exception as e:
            print(f"[ERROR] {url}: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(delay)
            delay *= BACKOFF

    print(f"[FAIL] {url}")
    return None


# ================== LINK PARSER ==================

def get_all_links() -> List[str]:

    print("🔍 Сбор ссылок...")

    links: List[str] = []

    page = 1

    while True:

        url = (
            f"{BASE_URL}/{AREA}"
            f"?type={PROPERTY_TYPES}&page={page}"
        )

        print(f"📄 Страница {page}")

        response = request_with_retry(url)

        if not response:
            break

        soup = BeautifulSoup(response.text, "html.parser")

        items = soup.find_all("a", class_="site-map-item-link")

        if not items:
            print("✅ Страницы закончились")
            break

        page_links = 0

        for a in items:
            href = a.get("href")

            if href:
                links.append(urljoin(SITE_URL, href.strip()))
                page_links += 1

        print(f"   Найдено: {page_links}")

        page += 1

        sleep_jitter()

    print(f"📌 Всего ссылок: {len(links)}")

    return links


# ================== PROJECT PARSER ==================

def parse_project_info(soup: BeautifulSoup, url: str) -> Optional[Dict]:
    name_tag = soup.find("h1", class_="project-name")
    location_tag = soup.find("div", class_="project-location")

    if not name_tag:
        return None

    project_name = clean_text(name_tag.get_text(" ", strip=True))
    address = (
        clean_text(location_tag.get_text(" ", strip=True))
        if location_tag
        else None
    )

    street_view = soup.find(
        "a",
        {"data-tk": "Project_Highlight_Street_View"}
    )

    address_link = (
        street_view["href"].strip()
        if street_view and street_view.has_attr("href")
        else None
    )

    info_blocks = soup.find_all(
        "div",
        class_="property-info-element"
    )

    info: Dict[str, str] = {}

    for block in info_blocks:

        parts = list(block.stripped_strings)

        if len(parts) >= 2:
            key = clean_text(parts[-1])
            value = clean_text(parts[-2])

            if key and value:
                info[key] = value

    return {
        "project_name": project_name,
        "address": address,
        "address_link": address_link,
        "project_url": url,
        **info,
    }


def get_transaction_type(unit_url: Optional[str]) -> Optional[str]:
    if not unit_url:
        return None

    lowered_url = unit_url.lower()

    if "property-sales" in lowered_url or "for-sale" in lowered_url:
        return "sale"

    if "property-rentals" in lowered_url or "for-rent" in lowered_url:
        return "rent"

    return None


def parse_unit_info_item(item) -> Dict[str, Optional[str]]:
    values: Dict[str, Optional[str]] = {}

    for block in item.select(".available-units-table-list__item"):
        parts = list(block.stripped_strings)

        if len(parts) < 2:
            continue

        label = parts[-1].lower()
        value = clean_text(" ".join(parts[:-1]))

        if label == "beds":
            values["unit_beds"] = value
        elif label == "baths":
            values["unit_baths"] = value
        elif label == "size":
            values["unit_size"] = value
        elif label == "floor":
            values["unit_floor"] = value

    return values


def parse_unit_features(item) -> Dict[str, Optional[str]]:
    features = [
        clean_text(feature.get_text(" ", strip=True))
        for feature in item.select(".available-units-feature__col")
    ]
    features = [feature for feature in features if feature]

    result: Dict[str, Optional[str]] = {
        "unit_features": " | ".join(features) if features else None,
    }

    views: List[str] = []

    for feature in features:
        lowered_feature = feature.lower()

        if "quota" in lowered_feature:
            result["unit_quota"] = feature
        elif "furnished" in lowered_feature:
            result["unit_furnishing"] = feature
        elif "view" in lowered_feature:
            views.append(feature)

    if views:
        result["unit_view"] = " | ".join(views)

    return result


def parse_units(
    soup: BeautifulSoup,
    embedded_prices: Dict[str, Dict[str, str]],
) -> List[Dict]:
    units: List[Dict] = []

    for index, item in enumerate(
        soup.select(".available-units-table-list"),
        1
    ):
        href = item.get("href")
        unit_url = urljoin(SITE_URL, href.strip()) if href else None
        unit_id = extract_unit_id(unit_url)
        embedded_price = embedded_prices.get(unit_id or "", {})
        price_tag = item.select_one(".resale-rental-full-price")
        price_per_sqm_tag = item.select_one(
            ".available-units-table-list__price"
        )
        updated_tag = item.select_one(".float-tag")

        unit: Dict[str, Optional[str]] = {
            "unit_index": str(index),
            "unit_url": unit_url,
            "unit_transaction": get_transaction_type(unit_url),
            "unit_price": (
                embedded_price.get("unit_price")
                or clean_text(price_tag.get_text(" ", strip=True))
                if price_tag
                else None
            ),
            "unit_price_per_sqm": (
                embedded_price.get("unit_price_per_sqm")
                or clean_text(price_per_sqm_tag.get_text(" ", strip=True))
                if price_per_sqm_tag
                else None
            ),
            "unit_updated": (
                clean_text(updated_tag.get_text(" ", strip=True))
                if updated_tag
                else None
            ),
        }

        unit.update(parse_unit_info_item(item))
        unit.update(parse_unit_features(item))
        units.append(unit)

    return units


def parse_project(url: str) -> List[Dict]:

    response = request_with_retry(url)

    if not response:
        return []

    soup = BeautifulSoup(response.text, "html.parser")

    try:
        project = parse_project_info(soup, url)

        if not project:
            return []

        embedded_prices = parse_embedded_unit_prices(response.text)
        units = parse_units(soup, embedded_prices)

        if not units:
            return [project]

        return [
            {
                **project,
                **unit,
            }
            for unit in units
        ]

    except Exception as e:

        print(f"[PARSE ERROR] {url}: {e}")
        return []


# ================== MAIN ==================

def main():

    start_time = time.time()

    # 1. Получаем ссылки
    links = get_all_links()

    if not links:
        print("❌ Ссылки не найдены")
        return

    # 2. Парсим проекты и юниты
    rows: List[Dict] = []

    total = len(links)

    print("\n🚀 Парсинг проектов и юнитов...\n")

    for i, link in enumerate(links, 1):

        print(f"[{i}/{total}] {link}")

        project_rows = parse_project(link)

        if project_rows:
            rows.extend(project_rows)
            print(f"   Юнитов/строк: {len(project_rows)}")
        else:
            print("   ⚠️ Пропущено")

        sleep_jitter()

    if not rows:
        print("❌ Данные не получены")
        return

    # 3. Сохраняем CSV
    fieldnames = set()

    for row in rows:
        fieldnames.update(row.keys())

    preferred_fields = PROJECT_FIELDS + UNIT_FIELDS
    dynamic_fields = sorted(
        field for field in fieldnames
        if field not in preferred_fields
    )
    fieldnames = preferred_fields + dynamic_fields

    with open(
        OUTPUT_FILE,
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames
        )

        writer.writeheader()
        writer.writerows(rows)

    elapsed = round(time.time() - start_time, 1)

    print("\n==============================")
    print("✅ Готово!")
    print(f"📁 Файл: {OUTPUT_FILE}")
    print(f"📊 Записей: {len(rows)}")
    print(f"⏱ Время: {elapsed} сек")
    print("==============================")


# ================== RUN ==================

if __name__ == "__main__":
    main()
