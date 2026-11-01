#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests",
#     "beautifulsoup4",
#     "tqdm",
# ]
# ///

import requests
import re
import os
import json
from urllib.parse import urlparse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from tqdm import tqdm
import argparse

MAX_WORKERS = 10
TIMEOUT = 10
CACHE_FILE = "rsarchive_tafeln/ga_cache.json"
METADATA_CACHE_FILE = "rsarchive_tafeln/ga_metadata_cache.json"
BASE_DIR = "rsarchive_tafeln"
session = requests.Session()

MIN_IMAGE_SIZE = 100 * 1024  # 100 KB - skip tiny thumbnails

def format_book_title(raw_title, ga):
    cleaned = re.sub(r'\bGA[-\s]?\d{1,3}\b', '', raw_title, flags=re.IGNORECASE)
    cleaned = re.sub(r'^[\s:,\-–—]+|[\s:,\-–—]+$', '', cleaned)
    if not cleaned:
        return f"(GA-{ga})"
    return f"{cleaned} (GA-{ga})"

# ====================== GA NUMBER CACHE ======================
def load_or_fetch_ga_cache(force_refresh=False):
    os.makedirs(BASE_DIR, exist_ok=True)
    if os.path.exists(CACHE_FILE) and not force_refresh:
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if datetime.now() - datetime.fromisoformat(cache["timestamp"]) > timedelta(days=30):
                if input("GA number cache is old. Update now? (y/n): ").strip().lower() == 'y':
                    return fetch_ga_list()
            return cache["ga_list"]
        except:
            pass
    return fetch_ga_list()

def fetch_ga_list():
    print("Fetching GA list from Volumes.html...")
    try:
        r = session.get("https://rsarchive.org/Volumes.html", timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        ga_numbers = set()
        for text in soup.find_all(string=True):
            for m in re.findall(r'\b(?:GA)?\s*(\d{1,3})(?:[a-z])?\b', text, re.IGNORECASE):
                try:
                    ga = int(m)
                    if 1 <= ga <= 400:
                        ga_numbers.add(ga)
                except:
                    pass
        valid = sorted(ga_numbers)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"timestamp": datetime.now().isoformat(), "ga_list": valid}, f, indent=2)
        print(f"✅ Cached {len(valid)} GA numbers.")
        return valid
    except Exception as e:
        print(f"Failed to fetch GA list: {e}")
        return None

# ====================== METADATA CACHE ======================
def load_metadata_cache():
    if os.path.exists(METADATA_CACHE_FILE):
        try:
            with open(METADATA_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_metadata_cache(cache):
    with open(METADATA_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

# ====================== METADATA + IMAGE EXTRACTION ======================
def get_ga_metadata(ga, force_refresh=False):
    cache = load_metadata_cache()
    ga_str = str(ga)

    if not force_refresh and ga_str in cache:
        return cache[ga_str]

    print(f"  Scraping lectures for GA{ga}...")
    base_url = f"https://rsarchive.org/Lectures/GA{ga}/"
    try:
        r = session.get(base_url, timeout=TIMEOUT)
        if r.status_code != 200:
            data = {"lectures": []}
            cache[ga_str] = data
            save_metadata_cache(cache)
            return data

        soup = BeautifulSoup(r.text, "html.parser")

        cycle_title = f"(GA-{ga})"
        h2 = soup.find("h2")
        if h2 and h2.get_text(strip=True):
            cycle_title = format_book_title(h2.get_text(strip=True), ga)

        images_by_tafel = {}

        ga_table = soup.find("table", class_="gaTable")
        all_tds = ga_table.find_all("td") if ga_table else []
        rows = [all_tds[i:i+5] for i in range(0, len(all_tds), 5)]

        for cells in rows:
            if len(cells) < 5:
                continue

            link = cells[1].find("a", href=True)
            if not link:
                continue
            href = link["href"]
            if not re.search(r'/\d{8}[a-z]?\d{2}\.html$', href):
                continue

            lecture_url = requests.compat.urljoin(base_url, href)
            lecture_title = link.get_text(strip=True)

            raw_book = cells[2].get_text(strip=True)
            if raw_book.lower() == "single translation":
                book_title = "Single Translation"
            elif raw_book:
                book_title = format_book_title(raw_book, ga)
            else:
                book_title = cycle_title

            raw_location = cells[4].get_text(strip=True)

            date_match = re.search(r'/(\d{8})', href)
            date_str = f"{date_match.group(1)[:4]}-{date_match.group(1)[4:6]}-{date_match.group(1)[6:8]}" if date_match else "Unknown"

            appearance = {
                "lecture_title": lecture_title,
                "book_title": book_title,
                "date": date_str,
                "location": raw_location,
            }

            try:
                lr = session.get(lecture_url, timeout=TIMEOUT)
                lsoup = BeautifulSoup(lr.text, "html.parser")

                german_div = lsoup.find("div", class_="German")
                if german_div:
                    seen_in_page = set()
                    for img in german_div.find_all("img"):
                        img_src = img.get("src")
                        if not img_src:
                            continue
                        if not img_src.startswith("http"):
                            img_src = requests.compat.urljoin(lecture_url, img_src)

                        t_match = re.match(
                            rf"^https://rsarchive\.org/Lectures/GA{ga}/.+/{ga}-T(\d{{2}})\.webp$",
                            img_src
                        )
                        if not t_match:
                            continue
                        t_num = int(t_match.group(1))

                        if t_num in seen_in_page:
                            continue
                        seen_in_page.add(t_num)

                        if t_num not in images_by_tafel:
                            images_by_tafel[t_num] = {
                                "tafel_number": t_num,
                                "image_url": img_src,
                                "lectures": [],
                            }

                        existing = images_by_tafel[t_num]["lectures"]
                        if appearance not in existing:
                            existing.append(appearance)
            except:
                continue

        image_list = sorted(images_by_tafel.values(), key=lambda x: x["tafel_number"])

        data = {"images": image_list}
        cache[ga_str] = data
        save_metadata_cache(cache)
        return data

    except Exception as e:
        print(f"  Warning scraping GA{ga}: {e}")
        data = {"lectures": []}
        cache[ga_str] = data
        save_metadata_cache(cache)
        return data

# ====================== IMAGE DOWNLOAD WITH SIZE CHECK ======================
def download_image(url, ga, t_num):
    ga_dir = os.path.join(BASE_DIR, f"GA{ga}")
    os.makedirs(ga_dir, exist_ok=True)

    # Keep the extension the URL's path serves (rsarchive serves .webp today).
    ext = os.path.splitext(urlparse(url).path)[1] or ".webp"
    filename = f"{ga}-T{t_num:02d}{ext}" if t_num else os.path.basename(url)
    filepath = os.path.join(ga_dir, filename)

    if os.path.exists(filepath):
        return filepath

    print(f"    Downloading {filename} ...", end=" ")

    try:
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code == 200:
            with open(filepath, "wb") as f:
                f.write(r.content)
            print("✓")
            return filepath
        else:
            print("✗")
            return None
    except Exception:
        print("✗")
        return None

# ====================== PROCESS GA ======================
def process_ga(ga, overwrite=False, metadata_only=False, refresh_metadata=False):
    ga_dir = os.path.join(BASE_DIR, f"GA{ga}")
    metadata_path = os.path.join(ga_dir, "metadata.json")
    
    if os.path.exists(metadata_path) and not overwrite and not metadata_only:
        return None

    meta = get_ga_metadata(ga, force_refresh=refresh_metadata)
    images = meta.get("images", [])

    if not images:
        print(f"GA{ga}: No lectures or images found.")
        return None

    for img_entry in images:
        img_entry["ga"] = ga
        if not metadata_only:
            img_entry["local_path"] = download_image(img_entry["image_url"], ga, img_entry["tafel_number"])

    os.makedirs(ga_dir, exist_ok=True)
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(images, f, indent=2, ensure_ascii=False)

    print(f"GA{ga}: Saved {len(images)} images")
    return images

# ====================== MAIN ======================
def main():
    parser = argparse.ArgumentParser(description="Rudolf Steiner Tafeln Downloader - Strict -T Filter")
    parser.add_argument("startGA", type=int)
    parser.add_argument("endGA", type=int, nargs="?", default=None)
    parser.add_argument("-o", "--overwrite", action="store_true")
    parser.add_argument("-m", "--metadata-only", action="store_true")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--refresh-metadata", action="store_true", help="Force refresh metadata")
    args = parser.parse_args()

    start_ga = args.startGA
    end_ga = args.endGA if args.endGA is not None else start_ga

    valid_ga = load_or_fetch_ga_cache(force_refresh=args.refresh_cache)
    ga_to_process = [ga for ga in (valid_ga or range(start_ga, end_ga + 1)) if start_ga <= ga <= end_ga]

    print(f"Processing {len(ga_to_process)} GA numbers (strict -T image filter only)...")

    all_results = []
    for ga in tqdm(ga_to_process, desc="Overall Progress"):
        ga_results = process_ga(ga, args.overwrite, args.metadata_only, args.refresh_metadata)
        if ga_results:
            all_results.extend(ga_results)

    print("\n" + "="*170)
    print(f"{'GA':<6} {'Tafel':<6} {'Lectures':<10} Lecture / Book Titles")
    print("="*170)
    for item in all_results:
        t_num = item.get("tafel_number")
        t_str = f"T{t_num:02d}" if t_num else "N/A"
        lectures = item.get("lectures", [])
        titles = "; ".join(
            f"{l['lecture_title']} [{l['book_title']}]" for l in lectures
        )
        print(f"{item['ga']:<6} {t_str:<6}   {len(lectures):<10} {titles[:120]}")

    total_images = len(all_results)
    total_lectures = sum(len(item.get("lectures", [])) for item in all_results)
    print(f"\n🎉 Done! {total_images} unique images across {total_lectures} lecture appearances.")

if __name__ == "__main__":
    main()