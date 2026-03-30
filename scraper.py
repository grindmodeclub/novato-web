import os
import json
import time
import base64
import re
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

# ── Configuration ────────────────────────────────────────────────────────────
WEB_DIR = "/Users/kazukitanaka/Desktop/Novato_WEB SK STARÝ /WEB"
OUTPUT_FILE = "/Users/kazukitanaka/novato-scraper/products.json"

GITHUB_TOKEN = "YOUR_GITHUB_TOKEN_HERE"
GITHUB_OWNER = "grindmodeclub"
GITHUB_REPO = "novato-web"
GITHUB_BRANCH = "main"
GITHUB_API_BASE = "https://api.github.com"

GITHUB_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

# Paragraphs to skip (download buttons / nav labels)
SKIP_TEXTS = {
    "Katalogový list",
    "Katalógový list",
    "Karta bezpečnostných údajov",
    "Karta Bezpečnostných Údajov",
}

# Footer sentinel
FOOTER_SENTINEL = "Kontaktné informácie"

# Known top-level categories (first segment of breadcrumb)
KNOWN_CATEGORIES = {
    "čistenie a odmasťovanie",
    "lepenie a tmelenie",
    "mazanie",
    "špeciálne produkty",
    "elektroúdržba",
    "osobná hygiena",
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def is_section_heading(text: str) -> bool:
    """Heuristic: short paragraph that looks like a section title."""
    if not text or len(text) > 60:
        return False
    if text.startswith("•"):
        return False
    if text[0].isdigit():
        return False
    # Contains ":" in the middle → data value, not a heading
    if ":" in text and not text.endswith(":"):
        return False
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return False
    uppercase_ratio = sum(1 for c in alpha if c.isupper()) / len(alpha)
    # Heading if majority uppercase OR short enough with capital first letter
    return uppercase_ratio > 0.5 or (len(text) <= 30 and text[0].isupper())


def get_content_paragraphs(soup: BeautifulSoup):
    """
    Return all <p> elements in document order that are NOT inside
    nav/menu elements.
    """
    result = []
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        if not text:
            continue
        if p.find_parent("nav"):
            continue
        if p.find_parent(
            class_=lambda x: x and any("Menu" in c for c in x) if x else False
        ):
            continue
        result.append((text, p))
    return result


def find_breadcrumb_index(paragraphs):
    """
    Find the index of the first breadcrumb-like paragraph.
    Breadcrumb: first segment matches a known category, at least 2 segments,
    no colon.
    """
    for i, (text, _) in enumerate(paragraphs):
        if ":" in text:
            continue
        parts = [s.strip() for s in text.split("/") if s.strip()]
        if len(parts) >= 2:
            first_seg = parts[0].lower()
            if first_seg in KNOWN_CATEGORIES:
                return i
    return None


def extract_product(html_path: str):
    """
    Parse one HTML file and return (product_dict, local_image_path).
    Returns (None, None) if the page is not a product page.
    """
    product_id = os.path.splitext(os.path.basename(html_path))[0]

    try:
        with open(html_path, "r", encoding="utf-8", errors="replace") as f:
            soup = BeautifulSoup(f.read(), "lxml")
    except Exception as e:
        print(f"  [ERROR] Could not read {html_path}: {e}")
        return None, None

    paragraphs = get_content_paragraphs(soup)

    # ── Breadcrumb / category ─────────────────────────────────────────────
    bc_idx = find_breadcrumb_index(paragraphs)
    if bc_idx is None:
        return None, None  # Not a product page

    breadcrumb_text = paragraphs[bc_idx][0]
    category = breadcrumb_text.strip()

    # ── Product name: last segment of breadcrumb ──────────────────────────
    name = breadcrumb_text.split("/")[-1].strip()
    name_lower = name.lower()

    # ── Trim to product content (breadcrumb … footer) ─────────────────────
    footer_idx = None
    for i, (text, _) in enumerate(paragraphs):
        if FOOTER_SENTINEL in text:
            footer_idx = i
            break
    if footer_idx is not None:
        product_paras = paragraphs[bc_idx:footer_idx]
    else:
        product_paras = paragraphs[bc_idx:]

    # ── Tagline: first long paragraph after breadcrumb ────────────────────
    tagline = None
    section_start = 1  # default: start sections right after breadcrumb

    for i, (text, _) in enumerate(product_paras[1:], start=1):
        if text in SKIP_TEXTS:
            continue
        # Skip the product-name display paragraph (matches extracted name)
        if text.lower() == name_lower:
            continue
        if len(text) > 50 and not text.startswith("•"):
            tagline = text
            section_start = i + 1
            break
        if is_section_heading(text):
            # No tagline found; sections start here
            section_start = i
            break

    # ── Sections ──────────────────────────────────────────────────────────
    sections = []
    current_heading = None
    current_content_lines = []

    for text, _ in product_paras[section_start:]:
        if text in SKIP_TEXTS:
            continue
        # Skip product-name display lines (e.g. stylised heading at page bottom)
        if text.lower() == name_lower:
            continue
        if is_section_heading(text):
            if current_heading:
                content = "\n".join(current_content_lines).strip()
                if content:  # only keep sections that have actual content
                    sections.append({"title": current_heading, "content": content})
            current_heading = text
            current_content_lines = []
        else:
            if current_heading:
                current_content_lines.append(text)

    # Flush last section
    if current_heading:
        content = "\n".join(current_content_lines).strip()
        if content:
            sections.append({"title": current_heading, "content": content})

    # ── Product image ─────────────────────────────────────────────────────
    local_image_path = None
    html_dir = os.path.dirname(html_path)

    for img in soup.find_all("img"):
        src = img.get("data-orig-src") or img.get("src", "")
        if not src:
            continue
        if "blank" in src or ".svg" in src.lower() or "menu-close" in src:
            continue
        if "images/" not in src:
            continue
        # Strip query params (?crc=...) and URL-decode percent-encoding
        clean_src = unquote(src.split("?")[0])
        candidate = os.path.join(html_dir, clean_src)
        if os.path.isfile(candidate):
            local_image_path = candidate
            break

    product = {
        "id": product_id,
        "name": name,
        "category": category,
        "tagline": tagline,
        "sections": sections,
        "image_url": None,
    }
    return product, local_image_path


# ── GitHub upload ─────────────────────────────────────────────────────────────

def initialize_repo_if_empty():
    """
    If the repo has no commits, create an initial commit with a README
    so the Contents API works for subsequent uploads.
    """
    refs_url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/refs"
    resp = requests.get(refs_url, headers=GITHUB_HEADERS, timeout=30)
    if resp.status_code != 409:
        return  # Repo already has commits

    print("  [INFO] Repo is empty — attempting to initialize with first commit...")

    # 1. Create a blob for README
    blob_resp = requests.post(
        f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/blobs",
        headers=GITHUB_HEADERS,
        json={"content": base64.b64encode(b"# novato-web\n").decode(), "encoding": "base64"},
        timeout=30,
    )
    if blob_resp.status_code not in (200, 201):
        print(
            f"  [WARN] Could not initialize repo (token likely lacks 'repo' scope): "
            f"{blob_resp.status_code}. "
            f"Image uploads will fail; URLs are pre-populated in the JSON."
        )
        return

    blob_sha = blob_resp.json()["sha"]

    # 2. Create a tree
    tree_resp = requests.post(
        f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/trees",
        headers=GITHUB_HEADERS,
        json={"tree": [{"path": "README.md", "mode": "100644", "type": "blob", "sha": blob_sha}]},
        timeout=30,
    )
    tree_sha = tree_resp.json()["sha"]

    # 3. Create a commit
    commit_resp = requests.post(
        f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/commits",
        headers=GITHUB_HEADERS,
        json={"message": "Initial commit", "tree": tree_sha, "parents": []},
        timeout=30,
    )
    commit_sha = commit_resp.json()["sha"]

    # 4. Create the main branch ref
    ref_resp = requests.post(
        f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/git/refs",
        headers=GITHUB_HEADERS,
        json={"ref": f"refs/heads/{GITHUB_BRANCH}", "sha": commit_sha},
        timeout=30,
    )
    if ref_resp.status_code in (200, 201):
        print("  [INFO] Repo initialized successfully.")
    else:
        print(f"  [WARN] Could not finalize repo init: {ref_resp.status_code} {ref_resp.text[:100]}")
    time.sleep(1)


def get_file_sha(upload_path: str):
    """Return the SHA of an existing file in the repo, or None."""
    url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{upload_path}"
    resp = requests.get(url, headers=GITHUB_HEADERS, timeout=30)
    if resp.status_code == 200:
        return resp.json().get("sha")
    return None


def upload_image(local_path: str):
    """
    Upload image to GitHub.
    Returns (raw_url, status_str) where status_str is
    'uploaded', 'updated', 'already_exists', or 'error:<msg>'.
    """
    filename = os.path.basename(local_path)
    upload_path = f"images/{filename}"
    raw_url = (
        f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/{GITHUB_BRANCH}/{upload_path}"
    )

    try:
        with open(local_path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        return None, f"error:read:{e}"

    api_url = (
        f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/contents/{upload_path}"
    )
    payload = {
        "message": f"Add product image {filename}",
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }

    resp = requests.put(api_url, headers=GITHUB_HEADERS, json=payload, timeout=60)

    if resp.status_code in (200, 201):
        return raw_url, "uploaded"

    if resp.status_code in (409, 422):
        # File already exists — fetch SHA and update
        sha = get_file_sha(upload_path)
        if sha is None:
            return raw_url, "already_exists"
        payload["sha"] = sha
        payload["message"] = f"Update product image {filename}"
        resp2 = requests.put(
            api_url, headers=GITHUB_HEADERS, json=payload, timeout=60
        )
        if resp2.status_code in (200, 201):
            return raw_url, "updated"
        return raw_url, f"error:update:{resp2.status_code}"

    # Upload failed — return the expected URL so the JSON remains useful.
    # (Images can be pushed manually or by re-running with a token that has
    # the 'repo' or 'public_repo' OAuth scope.)
    return raw_url, f"error:{resp.status_code}:{resp.text[:120]}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    html_files = sorted(
        os.path.join(WEB_DIR, f)
        for f in os.listdir(WEB_DIR)
        if f.lower().endswith(".html")
    )

    products = []
    total = len(html_files)
    uploaded_count = 0
    already_exists_count = 0
    missing_count = 0
    skipped_count = 0
    upload_error_count = 0

    print(f"Found {total} HTML files in {WEB_DIR}\n")
    initialize_repo_if_empty()

    for idx, html_path in enumerate(html_files, start=1):
        fname = os.path.basename(html_path)
        print(f"[{idx:03d}/{total}] Processing: {fname}", end="")

        product, local_image_path = extract_product(html_path)

        if product is None:
            print("  → skipped (not a product page)")
            skipped_count += 1
            continue

        print(f"  → '{product['name']}'", end="")

        # ── Image upload ──────────────────────────────────────────────────
        if local_image_path:
            img_filename = os.path.basename(local_image_path)
            raw_url, status = upload_image(local_image_path)
            product["image_url"] = raw_url

            if "uploaded" in status or "updated" in status:
                print(f"  | image: {status} ({img_filename})")
                uploaded_count += 1
            elif "already_exists" in status:
                print(f"  | image: already exists ({img_filename})")
                already_exists_count += 1
                # Still set the raw URL even if not re-uploaded
                product["image_url"] = (
                    f"https://raw.githubusercontent.com/{GITHUB_OWNER}"
                    f"/{GITHUB_REPO}/{GITHUB_BRANCH}/images/{img_filename}"
                )
            else:
                print(f"  | image: UPLOAD ERROR — {status}")
                upload_error_count += 1

            time.sleep(0.5)
        else:
            print("  | image: NOT FOUND locally")
            missing_count += 1

        products.append(product)

    # ── Write output ──────────────────────────────────────────────────────
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)

    print(f"\n{'─' * 60}")
    print(f"Done.")
    print(f"  Total products scraped : {len(products)}")
    print(f"  Images uploaded        : {uploaded_count}")
    print(f"  Images already existed : {already_exists_count}")
    print(f"  Images upload errors   : {upload_error_count}")
    print(f"  Images missing locally : {missing_count}")
    print(f"  Pages skipped (no-product): {skipped_count}")
    print(f"  Output written to      : {OUTPUT_FILE}")
    if upload_error_count > 0:
        print(
            f"\n  ⚠  NOTE: {upload_error_count} image upload(s) failed.\n"
            f"     The expected raw GitHub URLs have been pre-populated in products.json\n"
            f"     so the data is ready for use once images are pushed.\n"
            f"     To fix: regenerate the GitHub token with 'repo' or 'public_repo' scope\n"
            f"     (classic PAT at https://github.com/settings/tokens) and re-run."
        )


if __name__ == "__main__":
    main()
