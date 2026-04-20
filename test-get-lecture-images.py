#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests", "pillow"]
# ///

import requests
from PIL import Image
import io

def get_image_info(url):
    try:
        # Get size
        head = requests.head(url, timeout=10, allow_redirects=True)
        size_bytes = int(head.headers.get('content-length', 0))
        size_str = f"{size_bytes / 1024:.1f} KB" if size_bytes < 1024*1024 else f"{size_bytes / (1024*1024):.1f} MB"

        # Get dimensions with range request (fast)
        headers = {"Range": "bytes=0-32768"}
        r = requests.get(url, headers=headers, timeout=10, stream=True)
        data = r.content

        with io.BytesIO(data) as f:
            img = Image.open(f)
            dimensions = f"{img.width} × {img.height}"

        print(f"URL: {url}")
        print(f"Size: {size_str} ({size_bytes:,} bytes)")
        print(f"Dimensions: {dimensions}")
        print("-" * 60)

    except Exception as e:
        print(f"Failed for {url}: {e}")

# Test with a known image from GA207
test_urls = [
    "https://rsarchive.org/Lectures/GA207/German/images/207-T01.webp",
    "https://rsarchive.org/Lectures/GA207/English/AP1985/images/207-T01.webp",
]

for url in test_urls:
    get_image_info(url)