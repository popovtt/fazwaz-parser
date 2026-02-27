import csv
import os
import random
import time
import requests
from bs4 import BeautifulSoup
from typing import List, Dict, Optional


# ================== CONFIG ==================
def safe_filename(name: str) -> str:
    return (
        name
        .replace("/", "_")
        .replace("\\", "_")
        .replace(" ", "_")
    )

BASE_URL = "https://www.fazwaz.com/project-directory/thailand"
PROPERTY_TYPES = "condo,apartment,penthouse"

AREA = "phuket"
SAFE_AREA = safe_filename(AREA)
OUTPUT_FILE = f"{SAFE_AREA}.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

MIN_DELAY = 0.8
MAX_DELAY = 1.7

MAX_RETRIES = 5
BACKOFF = 1.6

TIMEOUT = 15


# ================== HELPERS ==================

def sleep_jitter():
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


def request_with_retry(url: str) -> Optional[requests.Response]:

    delay = 1

    for attempt in range(1, MAX_RETRIES + 1):

        try:
            r = requests.get(
                url,
                headers=HEADERS,
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
                links.append(href.strip())
                page_links += 1

        print(f"   Найдено: {page_links}")

        page += 1

        sleep_jitter()

    print(f"📌 Всего ссылок: {len(links)}")

    return links


# ================== PROJECT PARSER ==================

def parse_project(url: str) -> Optional[Dict]:

    response = request_with_retry(url)

    if not response:
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    try:

        name_tag = soup.find("h1", class_="project-name")
        location_tag = soup.find("div", class_="project-location")

        if not name_tag:
            return None

        project_name = name_tag.text.strip()
        address = location_tag.text.strip() if location_tag else None

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
                key = parts[-1]
                value = parts[-2]
                info[key] = value

        row = {
            "project_name": project_name,
            "address": address,
            "address_link": address_link,
            "url": url,
            **info,
        }

        return row

    except Exception as e:

        print(f"[PARSE ERROR] {url}: {e}")
        return None


# ================== MAIN ==================

def main():

    start_time = time.time()

    # 1. Получаем ссылки
    links = get_all_links()

    if not links:
        print("❌ Ссылки не найдены")
        return

    # 2. Парсим проекты
    projects: List[Dict] = []

    total = len(links)

    print("\n🚀 Парсинг проектов...\n")

    for i, link in enumerate(links, 1):

        print(f"[{i}/{total}] {link}")

        data = parse_project(link)

        if data:
            projects.append(data)
        else:
            print("   ⚠️ Пропущено")

        sleep_jitter()

    if not projects:
        print("❌ Данные не получены")
        return

    # 3. Сохраняем CSV
    fieldnames = set()

    for row in projects:
        fieldnames.update(row.keys())

    fieldnames = list(fieldnames)

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
        writer.writerows(projects)

    elapsed = round(time.time() - start_time, 1)

    print("\n==============================")
    print("✅ Готово!")
    print(f"📁 Файл: {OUTPUT_FILE}")
    print(f"📊 Записей: {len(projects)}")
    print(f"⏱ Время: {elapsed} сек")
    print("==============================")


# ================== RUN ==================

if __name__ == "__main__":
    main()