#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "Pillow",
#     "reportlab",
#     "pytesseract",
#     "deep-translator",
# ]
# ///

import argparse
import json
import os
import sys
import io
from PIL import Image, ImageEnhance, ImageOps
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

BASE_DIR = "rsarchive_tafeln"


# ====================== DATA LOADING ======================

def _convert_old_format(entries, ga):
    """Convert old lecture-centric metadata to new image-centric format."""
    by_tafel = {}
    for e in entries:
        t = e.get("tafel_number", 0)
        if t not in by_tafel:
            by_tafel[t] = {
                "tafel_number": t,
                "image_url": e.get("image_url", ""),
                "ga": ga,
                "lectures": [],
            }
        appearance = {
            "lecture_title": e.get("lecture_title", "Unknown"),
            "book_title": e.get("book_title", "Unknown"),
            "date": e.get("date", "Unknown"),
            "location": e.get("location", "Unknown"),
        }
        if appearance not in by_tafel[t]["lectures"]:
            by_tafel[t]["lectures"].append(appearance)
    return sorted(by_tafel.values(), key=lambda x: x["tafel_number"])


def _resolve_image_path(ga, t_num, preferred_format=None):
    if preferred_format:
        ext = "jpg" if preferred_format == "jpeg" else preferred_format
        p = os.path.join(BASE_DIR, f"GA{ga}", f"{ga}-T{t_num:02d}.{ext}")
        if os.path.exists(p):
            return p
    for ext in ("webp", "jpg", "jpeg", "png"):
        p = os.path.join(BASE_DIR, f"GA{ga}", f"{ga}-T{t_num:02d}.{ext}")
        if os.path.exists(p):
            return p
    return os.path.join(BASE_DIR, f"GA{ga}", f"{ga}-T{t_num:02d}.webp")


def load_all_metadata(ga_numbers, preferred_format=None):
    all_entries = []
    for ga in ga_numbers:
        meta_path = os.path.join(BASE_DIR, f"GA{ga}", "metadata.json")
        if not os.path.exists(meta_path):
            print(f"  Warning: {meta_path} not found, skipping GA{ga}")
            continue
        with open(meta_path, "r", encoding="utf-8") as f:
            entries = json.load(f)

        if entries and "lectures" not in entries[0]:
            entries = _convert_old_format(entries, ga)

        for entry in entries:
            if "ga" not in entry:
                entry["ga"] = ga
            t_num = entry["tafel_number"]
            entry["local_path"] = _resolve_image_path(ga, t_num, preferred_format)
        all_entries.extend(entries)

    def sort_key(e):
        dates = [l.get("date", "9999-99-99") for l in e.get("lectures", [])]
        earliest = min(dates) if dates else "9999-99-99"
        return (earliest, e.get("ga", 0), e.get("tafel_number", 0))

    all_entries.sort(key=sort_key)
    return all_entries


# ====================== IMAGE ENHANCEMENT ======================

def upscale_image(img, print_width_inches, dpi):
    target_px = int(print_width_inches * dpi)
    orig_w, orig_h = img.size
    scale = target_px / orig_w
    new_h = int(orig_h * scale)
    return img.resize((target_px, new_h), Image.LANCZOS)


def is_bw(img):
    hsv = img.convert("HSV")
    sat_data = hsv.split()[1]
    max_sat = max(sat_data.tobytes())
    return max_sat <= 10


def enhance_image(img):
    if is_bw(img):
        img = ImageOps.autocontrast(img, cutoff=(5, 2))
        img = ImageEnhance.Contrast(img).enhance(1.5)
        img = ImageEnhance.Sharpness(img).enhance(1.2)
    else:
        img = ImageEnhance.Contrast(img).enhance(1.3)
        img = ImageEnhance.Color(img).enhance(1.4)
        img = ImageEnhance.Sharpness(img).enhance(1.2)
    return img


# ====================== OCR + TRANSLATION ======================

def ocr_image(img):
    try:
        import pytesseract
        data = pytesseract.image_to_data(
            img, lang="deu", output_type=pytesseract.Output.DICT
        )
    except Exception as e:
        print(f"    OCR error: {e}")
        return []

    words = []
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        if not text or len(text) < 2:
            continue
        try:
            conf = int(data["conf"][i])
        except (ValueError, TypeError):
            continue
        if conf < 40:
            continue
        words.append(
            {
                "text": text,
                "confidence": conf,
                "block_num": data["block_num"][i],
                "line_num": data["line_num"][i],
            }
        )
    return words


def translate_words(words):
    if not words:
        return words

    high_conf_texts = [w["text"] for w in words if w["confidence"] >= 70]

    translations = {}
    if high_conf_texts:
        try:
            from deep_translator import GoogleTranslator

            translator = GoogleTranslator(source="de", target="en")
            for text in set(high_conf_texts):
                try:
                    translations[text] = translator.translate(text)
                except Exception:
                    translations[text] = ""
        except Exception as e:
            print(f"    Translation error: {e}")

    for w in words:
        w["uncertain"] = w["confidence"] < 70
        if w["uncertain"]:
            w["translation"] = ""
        else:
            w["translation"] = translations.get(w["text"], "")

    return words


def group_ocr_lines(words):
    lines = {}
    for w in words:
        key = (w["block_num"], w["line_num"])
        lines.setdefault(key, []).append(w)

    result = []
    for key in sorted(lines.keys()):
        line_words = lines[key]
        german_parts = []
        english_parts = []
        has_uncertain = False

        for w in line_words:
            if w.get("uncertain"):
                german_parts.append(f"{w['text']} [?]")
                english_parts.append("________")
                has_uncertain = True
            else:
                german_parts.append(w["text"])
                english_parts.append(w.get("translation") or "")

        result.append(
            {
                "german": " ".join(german_parts),
                "english": " ".join(english_parts),
                "has_uncertain": has_uncertain,
            }
        )
    return result


# ====================== PDF GENERATION ======================

def compute_page_size(entries, print_width_inches):
    max_aspect = 0
    for entry in entries:
        p = entry.get("local_path", "")
        if not os.path.exists(p):
            continue
        with Image.open(p) as img:
            w, h = img.size
            aspect = h / w
            if aspect > max_aspect:
                max_aspect = aspect

    if max_aspect == 0:
        max_aspect = 640 / 960

    margin = 0.75 * inch
    img_w = print_width_inches * inch
    img_h = img_w * max_aspect
    text_area = 3.5 * inch

    page_w = img_w + 2 * margin
    page_h = img_h + text_area + 2 * margin
    return page_w, page_h, margin, img_w, img_h


def draw_page(c, entry, img, page_w, page_h, margin, img_w_pts, dpi, do_ocr):
    ga = entry.get("ga", "?")
    t_num = entry.get("tafel_number", 0)
    lectures = entry.get("lectures", [])

    actual_w, actual_h = img.size
    rendered_img_w = img_w_pts
    rendered_img_h = (actual_h / actual_w) * img_w_pts

    img_x = margin
    img_y = page_h - margin - rendered_img_h

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95, dpi=(dpi, dpi))
    buf.seek(0)

    c.drawImage(
        ImageReader(buf),
        img_x,
        img_y,
        width=rendered_img_w,
        height=rendered_img_h,
    )

    y = img_y - 20

    c.setFont("Helvetica-Bold", 14)
    c.drawString(margin, y, f"Tafel {t_num:02d} \u2014 GA {ga}")
    y -= 16

    if lectures:
        first = lectures[0]
        c.setFont("Helvetica", 10)
        date = first.get("date", "Unknown")
        loc = first.get("location", "Unknown")
        c.drawString(margin, y, f"Date: {date}     \u2022     Location: {loc}")
        y -= 16

        c.setFont("Helvetica-Bold", 10)
        c.drawString(margin, y, "Lectures:")
        y -= 14

        c.setFont("Helvetica", 9)
        for lect in lectures:
            title = lect.get("lecture_title", "Unknown")
            book = lect.get("book_title", "")
            line = f"  \u2022  {title}"
            if book:
                line += f"   [{book}]"
            c.drawString(margin, y, line)
            y -= 12

    if do_ocr:
        ocr_words = ocr_image(img)
        if ocr_words:
            ocr_words = translate_words(ocr_words)
            lines = group_ocr_lines(ocr_words)
            if lines:
                y -= 6
                col2_x = margin + 280
                c.setFont("Helvetica-Bold", 10)
                c.drawString(margin, y, "German Text")
                c.drawString(col2_x, y, "Translation")
                y -= 2
                c.setStrokeColorRGB(0.6, 0.6, 0.6)
                c.line(margin, y, page_w - margin, y)
                y -= 12

                c.setFont("Helvetica", 9)
                for line in lines:
                    if y < margin:
                        break
                    c.drawString(margin, y, line["german"])
                    c.drawString(col2_x, y, line["english"])
                    y -= 12


def generate_pdf(entries, output_path, print_width_inches, dpi, do_enhance, do_ocr):
    if not entries:
        print("No entries to process.")
        return

    page_w, page_h, margin, img_w_pts, _ = compute_page_size(
        entries, print_width_inches
    )

    print(f"  Page size: {page_w/inch:.1f}\" x {page_h/inch:.1f}\" (landscape)")

    c = canvas.Canvas(output_path, pagesize=(page_w, page_h))
    c.setTitle("Rudolf Steiner Tafeln")
    c.setAuthor("rsarchive.org")

    total = len(entries)
    for idx, entry in enumerate(entries):
        local_path = entry.get("local_path", "")
        if not os.path.exists(local_path):
            print(f"  Skipping missing: {local_path}")
            continue

        ga = entry.get("ga", "?")
        t_num = entry.get("tafel_number", 0)
        print(f"  [{idx + 1}/{total}] GA{ga} T{t_num:02d}", end="")

        img = Image.open(local_path).convert("RGB")
        bw = is_bw(img)
        img = upscale_image(img, print_width_inches, dpi)

        if do_enhance:
            img = enhance_image(img)
            print(f" ({'B&W' if bw else 'color'} enhanced)", end="")

        print()

        draw_page(c, entry, img, page_w, page_h, margin, img_w_pts, dpi, do_ocr)
        c.showPage()

    c.save()
    print(f"\n\u2705 PDF saved: {output_path}")
    print(f"   {total} pages, {os.path.getsize(output_path) / 1024 / 1024:.1f} MB")


# ====================== MAIN ======================

def main():
    parser = argparse.ArgumentParser(
        description="Generate printable PDF from Rudolf Steiner Tafeln images"
    )
    parser.add_argument("ga_numbers", type=int, nargs="+", help="GA numbers to include")
    parser.add_argument("-o", "--output", type=str, default=None, help="Output PDF path")
    parser.add_argument("--dpi", type=int, default=300, help="Target print DPI (default: 300)")
    parser.add_argument(
        "--print-width",
        type=float,
        default=8.0,
        help="Image print width in inches (default: 8)",
    )
    parser.add_argument("--no-ocr", action="store_true", help="Skip OCR/translation")
    parser.add_argument(
        "--no-enhance", action="store_true", help="Skip color enhancement"
    )
    parser.add_argument(
        "--format", choices=["webp", "jpeg"], default=None,
        help="Preferred image format (default: auto-detect)"
    )
    args = parser.parse_args()

    if args.output is None:
        ga_str = "_GA".join(str(g) for g in sorted(args.ga_numbers))
        args.output = os.path.join(BASE_DIR, f"tafeln_GA{ga_str}.pdf")

    print(f"Loading metadata for GA {', '.join(str(g) for g in args.ga_numbers)}...")
    entries = load_all_metadata(args.ga_numbers, args.format)
    print(f"Found {len(entries)} images.")
    print(f"DPI: {args.dpi}   Print width: {args.print_width}\"")
    print(f"Enhancement: {'on' if not args.no_enhance else 'off'}")
    print(f"OCR/Translation: {'on' if not args.no_ocr else 'off'}")
    print()

    generate_pdf(
        entries,
        args.output,
        args.print_width,
        args.dpi,
        do_enhance=not args.no_enhance,
        do_ocr=not args.no_ocr,
    )


if __name__ == "__main__":
    main()
