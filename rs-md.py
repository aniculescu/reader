#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "tqdm",
# ]
# ///

import os
import json
from pathlib import Path
from tqdm import tqdm
import argparse

BASE_DIR = Path("rsarchive_tafeln")

def create_ga_markdown(ga_dir: Path):
    """Create a nicely formatted markdown file for one GA folder"""
    metadata_path = ga_dir / "metadata.json"
    if not metadata_path.exists():
        print(f"Skipping {ga_dir.name} — no metadata.json found")
        return False

    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error reading metadata for {ga_dir.name}: {e}")
        return False

    if not data:
        return False

    md_content = []
    ga_num = data[0]["ga"] if data else ga_dir.name.replace("GA", "")
    
    # Header
    cycle_title = data[0].get("cycle_title", f"GA {ga_num}")
    book_title = data[0].get("book_title", "")
    
    md_content.append(f"# GA {ga_num} — {cycle_title}")
    if book_title and book_title != "N/A":
        md_content.append(f"**Book:** {book_title}")
    md_content.append("")
    md_content.append(f"**Total Tafeln:** {len(data)}")
    md_content.append("\n---\n")

    for item in data:
        t_num = item.get("tafel_number", 0)
        image_url = item.get("image_url", "")
        local_path = item.get("local_path")
        
        # Determine actual image filename
        if local_path and os.path.exists(local_path):
            img_filename = Path(local_path).name
        else:
            # Fallback: look for the file in the folder
            possible_files = list(ga_dir.glob(f"*{ga_num}-T{t_num:02d}*"))
            img_filename = possible_files[0].name if possible_files else f"{ga_num}-T{t_num:02d}.webp"
        
        img_rel_path = img_filename
        
        lecture_title = item.get("lecture_title", "Unknown Lecture")
        date = item.get("date", "Unknown")
        cycle = item.get("cycle_title", "")
        
        md_content.append(f"## Tafel {t_num:02d}")
        md_content.append("")
        
        # Embed image (works great in Obsidian, Typora, MarkText, etc.)
        md_content.append(f"![Tafel {t_num:02d}]({img_rel_path})")
        md_content.append("")
        
        # Metadata block
        md_content.append("**Metadata**")
        md_content.append(f"- **GA Number**: {ga_num}")
        md_content.append(f"- **Date**: {date}")
        md_content.append(f"- **Lecture Title**: {lecture_title}")
        md_content.append(f"- **Cycle Title**: {cycle}")
        if book_title and book_title != "N/A":
            md_content.append(f"- **Book Title**: {book_title}")
        md_content.append("")
        
        md_content.append("---")
        md_content.append("")

    # Write the markdown file
    md_filename = ga_dir / f"GA{ga_num}.md"
    try:
        with open(md_filename, "w", encoding="utf-8") as f:
            f.write("\n".join(md_content))
        print(f"✅ Created {md_filename.name} with {len(data)} Tafeln")
        return True
    except Exception as e:
        print(f"❌ Failed to write markdown for GA{ga_num}: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Generate Markdown files for Steiner Tafeln")
    parser.add_argument("startGA", type=int, nargs="?", default=None, help="Start GA number (optional)")
    parser.add_argument("endGA", type=int, nargs="?", default=None, help="End GA number (optional)")
    parser.add_argument("--all", action="store_true", help="Process all GA folders found")
    args = parser.parse_args()

    if not BASE_DIR.exists():
        print(f"Error: {BASE_DIR} directory not found. Run the downloader first.")
        return

    # Find all GA folders
    ga_folders = sorted([d for d in BASE_DIR.iterdir() if d.is_dir() and d.name.startswith("GA")])
    
    if not ga_folders:
        print("No GA folders found in rsarchive_tafeln/")
        return

    to_process = []

    if args.all:
        to_process = ga_folders
    elif args.startGA is not None:
        start = args.startGA
        end = args.endGA if args.endGA is not None else start
        to_process = [d for d in ga_folders if start <= int(d.name.replace("GA", "")) <= end]
    else:
        # Interactive mode: ask user
        print("Found the following GA folders:")
        for d in ga_folders:
            print(f"  {d.name}")
        choice = input("\nProcess all folders? (y/n): ").strip().lower()
        if choice == 'y':
            to_process = ga_folders
        else:
            ga_num = input("Enter GA number to process: ").strip()
            folder = BASE_DIR / f"GA{ga_num}"
            if folder.exists():
                to_process = [folder]
            else:
                print("Folder not found.")
                return

    print(f"\nGenerating Markdown files for {len(to_process)} GA(s)...\n")

    success = 0
    for folder in tqdm(to_process, desc="Generating Markdown"):
        if create_ga_markdown(folder):
            success += 1

    print(f"\n🎉 Done! Successfully created {success} Markdown files.")
    print("You can now open them in Obsidian, Typora, or any Markdown viewer.")
    print("The images are embedded using relative paths, so keep the .md file inside the GA folder.")

if __name__ == "__main__":
    main()