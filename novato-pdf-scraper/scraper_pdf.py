#!/usr/bin/env python3
"""
Novato PDF Scraper
Extracts product info and images from 2026.pdf and uploads to GitHub.

PDF structure per product:
  - Product detail page: category header (top-right), nav tabs, doubled product name,
    tagline, section headings (POUŽITIE, VÝHODY, etc.), footer to skip.
  - Pricing page immediately after: contains "VÝROBOK POPIS SKLAD. Č. BALENIE POČET CENA"
  - Sidebar items appear at x0 > page.width (overflow) — filtered out by cropping.
"""

import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

import pdfplumber
import requests
from PIL import Image
from slugify import slugify

# ── Config ────────────────────────────────────────────────────────────────────
PDF_PATH = "/Users/kazukitanaka/Desktop/2026.pdf"
OUTPUT_JSON = "/Users/kazukitanaka/novato-pdf-scraper/products_pdf.json"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")  # set via: export GITHUB_TOKEN=your_token
GITHUB_OWNER = "grindmodeclub"
GITHUB_REPO  = "novato-web"
GITHUB_BRANCH = "main"
GITHUB_IMAGE_DIR = "images-pdf"

RAW_BASE = (
    f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}"
    f"/{GITHUB_BRANCH}/{GITHUB_IMAGE_DIR}"
)
API_BASE = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
    f"/contents/{GITHUB_IMAGE_DIR}"
)

GITHUB_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

# Slovak section headings to capture
KNOWN_HEADINGS = [
    "POUŽITIE",
    "VÝHODY",
    "NÁVOD NA POUŽITIE",
    "UPOZORNENIE",
    "TECHNICKÉ DÁTA",
    "TECHNICKÉ ÚDAJE",
    "OBSAH",
    "SKLADOVÉ ČÍSLO",
    "TECHNICKÉ DATA",
]

# Regex that matches any known heading (longest first to avoid partial matches)
_sorted_headings = sorted(KNOWN_HEADINGS, key=len, reverse=True)
HEADING_RE = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(h) for h in _sorted_headings) + r")(?!\w)"
)

FOOTER_SKIP_PREFIX = "Naše ústne"
PRICING_INDICATORS = ["VÝROBOK", "SKLAD", "BALENIE", "CENA"]
MIN_IMAGE_SIZE_BYTES = 5 * 1024  # 5 KB


# ── Text helpers ───────────────────────────────────────────────────────────────

def deduplicate_name(s: str) -> str:
    """
    Convert doubled (or quadrupled) character product names back to normal.
    e.g. 'AAIIRRSSOOLL®®'                       → 'AIRSOL®'
         'BBRRYYOOSSAANN®® PPRROOTTEECCTT'        → 'BRYOSAN® PROTECT'
         'TTTTEEEECCCCHHHHNNNNIIIICCCCKKKKÉÉÉÉ…'  → 'TECHNICKE É…'
    Consumes full runs of identical characters and emits one per run.
    Only collapses runs of 2 or more — single chars are kept as-is.
    """
    result = []
    i = 0
    while i < len(s):
        c = s[i]
        j = i + 1
        while j < len(s) and s[j] == c:
            j += 1
        result.append(c)  # one per run
        i = j
    return "".join(result)


def looks_doubled(s: str) -> bool:
    """Return True if the string looks like a doubled product name."""
    if len(s) < 4:
        return False
    # At least half the characters appear as adjacent pairs
    pairs = sum(1 for i in range(0, len(s) - 1, 2) if s[i] == s[i + 1])
    return pairs >= len(s) // 4


def is_nav_tabs_line(line: str) -> bool:
    """Detect nav-tab lines like 'A–N | N–S | S–Z'."""
    return "–" in line and "|" in line


def extract_main_text(page) -> str:
    """
    Extract text from main content area only.
    Sidebar overflow items live at x0 > page.width; we crop them out.
    We also skip the category header and nav tabs in the top-right corner
    by keeping only words with x0 < ~530 for lines in the top ~70px
    (the header/tabs zone), but including everything below that.
    """
    w = page.width  # typically ~793

    # Crop to the printable page — excludes sidebar at x > page.width
    cropped = page.crop((0, 0, w, page.height))
    return cropped.extract_text() or ""


def extract_header_text(page) -> str:
    """Return text from top-right area (category + nav tabs)."""
    w = page.width
    # Top strip: y=0..65, right half x=530..w
    top_right = page.crop((530, 0, w, 65))
    return (top_right.extract_text() or "").strip()


# ── Page classification ────────────────────────────────────────────────────────

def is_pricing_page(page) -> bool:
    """Pricing pages contain table keywords."""
    text = extract_main_text(page)
    hits = sum(1 for kw in PRICING_INDICATORS if kw in text)
    return hits >= 3


def is_section_divider(page) -> bool:
    """Pure category divider pages have very little text."""
    text = extract_main_text(page).strip()
    return len(text) < 80 and "\n" not in text.strip()


# ── Product page parser ────────────────────────────────────────────────────────

def parse_product_page(page) -> Optional[dict]:
    """
    Parse a product detail page.
    Returns dict with keys: name, category, tagline, sections.
    """
    # Category comes from the top-right header zone
    category_raw = extract_header_text(page)
    # First line of header is category
    category_lines = [l.strip() for l in category_raw.split("\n") if l.strip()]
    category = category_lines[0] if category_lines else ""

    # Main text (sidebar excluded)
    main_text = extract_main_text(page)
    lines = [l.strip() for l in main_text.split("\n") if l.strip()]

    # Find doubled product name line
    product_line_idx = None
    product_name_raw = ""
    for idx, line in enumerate(lines):
        if looks_doubled(line):
            product_line_idx = idx
            product_name_raw = line
            break

    if product_line_idx is None:
        return None

    product_name = deduplicate_name(product_name_raw)

    # Everything after the product name line is body text
    body_lines = lines[product_line_idx + 1:]

    # Skip nav-tabs lines and the category header if it leaked into main text
    body_lines = [
        l for l in body_lines
        if not is_nav_tabs_line(l)
        and l != category
    ]

    body_text = " ".join(body_lines)

    # Remove footer
    if FOOTER_SKIP_PREFIX in body_text:
        body_text = body_text[: body_text.index(FOOTER_SKIP_PREFIX)].strip()

    # Split body into tagline (before first heading) + sections
    first_match = HEADING_RE.search(body_text)
    if first_match:
        tagline = body_text[: first_match.start()].strip()
        sections_text = body_text[first_match.start():]
    else:
        tagline = body_text.strip()
        sections_text = ""

    sections = []
    if sections_text:
        matches = list(HEADING_RE.finditer(sections_text))
        for i, m in enumerate(matches):
            heading = m.group(1)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(sections_text)
            content = sections_text[start:end].strip().lstrip(":").strip()
            if content:
                sections.append({"title": heading, "content": content})

    return {
        "category": category,
        "name": product_name,
        "tagline": tagline,
        "sections": sections,
    }


# ── Image extraction ───────────────────────────────────────────────────────────

def extract_images_pdfimages(pdf_path: str, output_dir: str) -> Dict[int, str]:
    """
    Run pdfimages to extract images.
    Returns dict: page_number (1-based) → best image file path.
    Filters out small images (masks/backgrounds).
    """
    prefix = os.path.join(output_dir, "img")
    # Try brew path first, fall back to system pdfimages
    pdfimages_bin = "/opt/homebrew/bin/pdfimages"
    if not os.path.exists(pdfimages_bin):
        pdfimages_bin = "pdfimages"
    cmd = [pdfimages_bin, "-j", "-p", pdf_path, prefix]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [pdfimages] Warning: {result.stderr.strip()}")

    # pdfimages -p produces img-<page>-<idx>.jpg
    page_images: Dict[int, List] = {}

    for fname in sorted(os.listdir(output_dir)):
        fpath = os.path.join(output_dir, fname)
        if not os.path.isfile(fpath):
            continue
        if os.path.getsize(fpath) < MIN_IMAGE_SIZE_BYTES:
            continue
        m = re.match(r"img-(\d+)-(\d+)\.", fname)
        if not m:
            continue
        page_num = int(m.group(1))
        img_idx  = int(m.group(2))
        page_images.setdefault(page_num, []).append((img_idx, fpath))

    # Keep the largest image per page (most likely the product photo)
    best: Dict[int, str] = {}
    for page_num, imgs in page_images.items():
        imgs_sorted = sorted(imgs, key=lambda x: os.path.getsize(x[1]), reverse=True)
        best[page_num] = imgs_sorted[0][1]

    return best


# ── GitHub upload ──────────────────────────────────────────────────────────────

def github_upload_image(product_id: str, image_path: str) -> Optional[str]:
    """Upload JPEG to GitHub. Handles 422/409 (already exists) by updating."""
    with open(image_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()

    filename = f"{product_id}.jpg"
    api_url  = f"{API_BASE}/{filename}"

    payload = {
        "message": f"Add product image: {product_id}",
        "content": content_b64,
        "branch":  GITHUB_BRANCH,
    }

    resp = requests.put(api_url, headers=GITHUB_HEADERS, json=payload)

    if resp.status_code in (200, 201):
        print(f"  [GitHub] Uploaded {filename} ({resp.status_code})")
        return f"{RAW_BASE}/{filename}"

    if resp.status_code in (409, 422):
        print(f"  [GitHub] {filename} exists ({resp.status_code}), fetching SHA to update…")
        get_resp = requests.get(api_url, headers=GITHUB_HEADERS)
        if get_resp.status_code == 200:
            sha = get_resp.json().get("sha")
            payload["sha"] = sha
            payload["message"] = f"Update product image: {product_id}"
            put_resp = requests.put(api_url, headers=GITHUB_HEADERS, json=payload)
            if put_resp.status_code in (200, 201):
                print(f"  [GitHub] Updated {filename}")
                return f"{RAW_BASE}/{filename}"
            else:
                print(f"  [GitHub] Update failed ({put_resp.status_code}): {put_resp.text[:200]}")
        else:
            print(f"  [GitHub] Could not fetch SHA ({get_resp.status_code})")
    else:
        print(f"  [GitHub] Upload failed ({resp.status_code}): {resp.text[:200]}")

    return None


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"PDF: {PDF_PATH}")
    if not os.path.exists(PDF_PATH):
        print(f"ERROR: PDF not found at {PDF_PATH}")
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        print("Extracting images with pdfimages…")
        page_to_image = extract_images_pdfimages(PDF_PATH, tmpdir)
        print(f"  Found images on pages: {sorted(page_to_image.keys())[:20]}…")

        products = []

        with pdfplumber.open(PDF_PATH) as pdf:
            total = len(pdf.pages)
            print(f"PDF has {total} pages. Scanning…\n")

            for page_idx in range(total):
                page    = pdf.pages[page_idx]
                page_num = page_idx + 1

                if is_pricing_page(page):
                    print(f"  [{page_num}/{total}] Pricing table — skip")
                    continue

                if is_section_divider(page):
                    print(f"  [{page_num}/{total}] Section divider — skip")
                    continue

                product = parse_product_page(page)
                if not product or not product.get("name"):
                    print(f"  [{page_num}/{total}] No product detected — skip")
                    continue

                pname = product["name"]
                pid   = slugify(pname)
                print(f"\n[{page_num}/{total}] {pname}  (id={pid})")

                # ── Image ──────────────────────────────────────────────────────
                image_url = None
                img_src   = page_to_image.get(page_num)
                if img_src:
                    size_kb = os.path.getsize(img_src) // 1024
                    print(f"  Image: {os.path.basename(img_src)} ({size_kb} KB)")
                    jpeg_path = os.path.join(tmpdir, f"{pid}.jpg")
                    try:
                        with Image.open(img_src) as im:
                            im.convert("RGB").save(jpeg_path, "JPEG", quality=90)
                        image_url = github_upload_image(pid, jpeg_path)
                        time.sleep(0.5)
                    except Exception as e:
                        print(f"  [Image] Error: {e}")
                else:
                    print(f"  No image on page {page_num}")

                products.append({
                    "id":        pid,
                    "name":      pname,
                    "category":  product["category"],
                    "tagline":   product["tagline"],
                    "sections":  product["sections"],
                    "image_url": image_url or "",
                })

        # ── Write JSON ─────────────────────────────────────────────────────────
        print(f"\n{'='*60}")
        print(f"Writing {len(products)} products → {OUTPUT_JSON}")
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)

        with_img = sum(1 for p in products if p["image_url"])
        print(f"\nSummary")
        print(f"  Total products : {len(products)}")
        print(f"  With image_url : {with_img}")
        print(f"  Without        : {len(products) - with_img}")
        print("Done.")


if __name__ == "__main__":
    main()
