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

        cycle_title = f"GA {ga}"
        h2 = soup.find("h2")
        if h2 and h2.get_text(strip=True):
            cycle_title = h2.get_text(strip=True)

        lecture_list = []

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r'/\d{8}[a-z]?\d{2}\.html$', href):
                lecture_url = requests.compat.urljoin(base_url, href)
                lecture_title = a.get_text(strip=True)

                try:
                    lr = session.get(lecture_url, timeout=TIMEOUT)
                    lsoup = BeautifulSoup(lr.text, "html.parser")

                    german_div = lsoup.find("div", class_="German")
                    if german_div:
                        for img in german_div.find_all("img"):
                            img_src = img.get("src")
                            if not img_src:
                                continue
                            if img_src.startswith("../../"):
                                img_src = requests.compat.urljoin(lecture_url, img_src)
                            elif not img_src.startswith("http"):
                                img_src = requests.compat.urljoin(base_url, img_src)

                            # STRICT FILTER: Only accept rsarchive "tafel" URLs of the form:
                            # https://rsarchive.org/Lectures/GA208/German/images/208-T02.webp
                            tafeln_url_re = re.compile(
                                rf"^https://rsarchive\.org/Lectures/GA{ga}/German/images/{ga}-T(\d{{2}})\.webp$"
                            )
                            t_match = tafeln_url_re.match(img_src)
                            if not t_match:
                                continue
                            t_num = int(t_match.group(1))

                            date_match = re.search(r'/(\d{8})', href)
                            date_str = f"{date_match.group(1)[:4]}-{date_match.group(1)[4:6]}-{date_match.group(1)[6:8]}" if date_match else "Unknown"

                            location = "Unknown"
                            loc_match = re.search(r'\b(Dornach|Stuttgart|Berlin|Munich|Basel|Basle|Prague|London|Paris|New York)\b', lr.text, re.I)
                            if loc_match:
                                location = loc_match.group(1)

                            book_title = cycle_title
                            if "single translation" in lr.text.lower():
                                book_title = "Single Translation"

                            lecture_list.append({
                                "date": date_str,
                                "title": lecture_title,
                                "location": location,
                                "book_title": book_title,
                                "image_url": img_src,
                                "tafel_number": t_num
                            })
                except:
                    continue

        lecture_list.sort(key=lambda x: x["date"])

        data = {"lectures": lecture_list}
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
    filename = f"{ga}-T{t_num:02d}.webp" if t_num else os.path.basename(url)
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
    lectures = meta.get("lectures", [])

    if not lectures:
        print(f"GA{ga}: No lectures or images found.")
        return None

    results = []
    for lect in lectures:
        image_url = lect.get("image_url")
        t_num = lect.get("tafel_number")

        local_path = None
        if image_url and not metadata_only:
            local_path = download_image(image_url, ga, t_num)

        results.append({
            "ga": ga,
            "image_url": image_url,
            "local_path": local_path,
            "lecture_title": lect.get("title", "Unknown"),
            "book_title": lect.get("book_title", "Unknown"),
            "date": lect.get("date", "Unknown"),
            "location": lect.get("location", "Unknown"),
            "tafel_number": t_num
        })

    os.makedirs(ga_dir, exist_ok=True)
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"GA{ga}: Saved {len(results)} entries")
    return results

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
    print(f"{'GA':<6} {'Tafel':<6} {'Date':<12} {'Location':<12} {'Book Title':<35} Lecture Title")
    print("="*170)
    for item in all_results:
        t_num = item.get("tafel_number")
        t_str = f"T{t_num:02d}" if t_num else "N/A"
        print(f"{item['ga']:<6} {t_str:<6}   {item.get('date','Unknown'):<12} {item.get('location','Unknown'):<12} {item.get('book_title','Unknown')[:33]:<35} {item.get('lecture_title','')[:60]}")

    print(f"\n🎉 Done! Only images with '-T' in the filename are being saved.")

if __name__ == "__main__":
    main()