import io
import re
import time
import zipfile
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HK_DOMAIN = "hentaikun.com"
HK_BASE_URL = "https://hentaikun.com"
REQUEST_TIMEOUT = (8, 60)
MAX_IMAGE_BYTES = 80 * 1024 * 1024

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Referer": HK_BASE_URL + "/",
}
IMAGE_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif,image/*;q=0.8,*/*;q=0.5",
    "Referer": HK_BASE_URL + "/",
}

BLOCKED_TERMS = {
    "underage", "minor", "child", "children", "kid", "kids", "loli", "lolicon",
    "shota", "shotacon", "junior high", "middle school", "elementary",
    "schoolgirl", "schoolboy", "12 years old", "13 years old", "14 years old",
    "15 years old", "10yo", "11yo", "12yo", "13yo", "14yo", "15yo",
}

IMAGE_SELECTORS = [
    "img.image_rin",
    "img.image_show",
    "#con img",
    ".area_image img",
    ".image_show1 img",
    ".image_show2 img",
    "img.img-responsive",
]

IMAGE_ATTRS = ["src", "data-src", "data-original", "data-lazy-src", "data-url"]


@dataclass
class HentaiKunGallery:
    title: str
    url: str
    album: str
    images: List[str]


def _blocked(*values: str) -> bool:
    text = "\n".join(v or "" for v in values).lower()
    return any(term in text for term in BLOCKED_TERMS)


def _safe_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "-", name or "hentaikun")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:140] or "hentaikun"


def _clean_url(value: str) -> str:
    return re.sub(r"\s+", "", value or "").strip()


def extract_hentaikun_url(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None

    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None

    host = parsed.hostname.lower().removeprefix("www.")
    if host != HK_DOMAIN:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3:
        return None

    # Normalize album/chapter/page URLs back to the album URL:
    # /manga/<category>/<album>/chapter-1/2/ -> /manga/<category>/<album>/
    return f"https://{HK_DOMAIN}/" + "/".join(parts[:3]) + "/"


def _get(url: str) -> requests.Response:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host != HK_DOMAIN:
        raise ValueError(f"Blocked non-HentaiKun URL: {url}")
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response


def _absolute(url: str, base: str) -> str:
    return urljoin(base, _clean_url(url))


def _album_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    return parts[2] if len(parts) >= 3 else "hentaikun"


def _title_from_soup(soup: BeautifulSoup, fallback: str) -> str:
    node = soup.select_one("h1") or soup.select_one(".entry-title") or soup.select_one("title")
    title = node.get_text(" ", strip=True) if node else fallback
    title = re.sub(r"\s*-\s*HentaiKun.*$", "", title, flags=re.I).strip()
    title = re.sub(r"^Chapter\s+\d+\s*-\s*", "", title, flags=re.I).strip()
    return title or fallback


def _add_unique(target: List[str], seen: set[str], url: str, base: str) -> None:
    src = _absolute(url, base)
    if not src or src in seen:
        return
    parsed = urlparse(src)
    if parsed.scheme not in {"http", "https"}:
        return
    seen.add(src)
    target.append(src)


def _extract_page_image_urls(soup: BeautifulSoup, base_url: str) -> List[str]:
    urls: List[str] = []
    seen: set[str] = set()

    for selector in IMAGE_SELECTORS:
        for img in soup.select(selector):
            for attr in IMAGE_ATTRS:
                value = img.get(attr)
                if value:
                    _add_unique(urls, seen, value, base_url)
                    break

    # Current HentaiKun pages also expose upcoming pages inside preloadImages([...]).
    # Capture those URLs as a useful fallback and to avoid fetching every numbered page when possible.
    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=False) or ""
        if "preloadImages" not in text and "hendata.com" not in text:
            continue
        for match in re.findall(r"https?://[^'\"\s,]+\.(?:jpg|jpeg|png|webp|gif)", text, flags=re.I):
            _add_unique(urls, seen, match, base_url)

    return urls


def _extract_option_urls(soup: BeautifulSoup, base_url: str) -> List[str]:
    urls: List[str] = []
    seen: set[str] = set()
    for option in soup.select("select option, .form-control option"):
        value = _clean_url(option.get("value") or "")
        if value.startswith("http"):
            _add_unique(urls, seen, value, base_url)
    return urls


def _extract_next_url(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    node = soup.select_one("#pagination .next a[href], .next a[href], a[rel='next'][href]")
    if not node:
        return None
    href = _absolute(node.get("href") or "", base_url)
    return href or None


def get_hentaikun_gallery(url: str) -> HentaiKunGallery:
    normalized = extract_hentaikun_url(url)
    if not normalized:
        raise ValueError("Invalid HentaiKun album URL")

    album = _album_from_url(normalized)
    first = _get(normalized)
    soup = BeautifulSoup(first.text, "html.parser")
    title = _title_from_soup(soup, album)

    if _blocked(title, album, normalized):
        raise ValueError("Blocked: this gallery appears to involve minors or unsafe terms.")

    chapter_links: List[str] = []
    seen_chapters: set[str] = set()

    for link in soup.select("a.readchap[href]"):
        href = _absolute(link.get("href") or "", normalized)
        if href and href not in seen_chapters:
            seen_chapters.add(href)
            chapter_links.append(href)

    # If the album page uses a chapter dropdown instead of a.readchap, support it too.
    for href in _extract_option_urls(soup, normalized):
        if "/chapter-" in href and href not in seen_chapters:
            seen_chapters.add(href)
            chapter_links.append(href)

    chapter_links.reverse()
    if not chapter_links:
        chapter_links = [normalized]

    image_urls: List[str] = []
    seen_images: set[str] = set()

    for chapter_url in chapter_links:
        chapter_res = _get(chapter_url)
        chapter_soup = BeautifulSoup(chapter_res.text, "html.parser")

        page_urls: List[str] = []
        seen_pages: set[str] = set()
        for href in _extract_option_urls(chapter_soup, chapter_url):
            # Keep only page URLs for this chapter, not links to other chapters.
            if "/chapter-" not in href:
                continue
            if href not in seen_pages:
                seen_pages.add(href)
                page_urls.append(href)

        if not page_urls:
            page_urls = [chapter_url]

        # Fetch all page URLs from the dropdown. If missing, follow Next links as a fallback.
        for page_url in page_urls:
            page_res = _get(page_url)
            page_soup = BeautifulSoup(page_res.text, "html.parser")
            for image_url in _extract_page_image_urls(page_soup, page_url):
                _add_unique(image_urls, seen_images, image_url, page_url)
            time.sleep(0.1)

        if len(page_urls) == 1:
            current_url = page_urls[0]
            current_soup = chapter_soup
            for _ in range(80):
                next_url = _extract_next_url(current_soup, current_url)
                if not next_url or next_url in seen_pages:
                    break
                seen_pages.add(next_url)
                page_res = _get(next_url)
                current_soup = BeautifulSoup(page_res.text, "html.parser")
                for image_url in _extract_page_image_urls(current_soup, next_url):
                    _add_unique(image_urls, seen_images, image_url, next_url)
                current_url = next_url
                time.sleep(0.1)

    if not image_urls:
        raise RuntimeError("Could not find HentaiKun images.")

    return HentaiKunGallery(title=title, url=normalized, album=album, images=image_urls)


def _download_image(url: str, referer: str) -> tuple[str, bytes]:
    headers = dict(IMAGE_HEADERS)
    headers["Referer"] = referer
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if content_type and "image" not in content_type:
        response.close()
        raise RuntimeError(f"Unexpected non-image response: {url}")

    ext = urlparse(url).path.rsplit(".", 1)[-1].lower()
    if not re.fullmatch(r"[a-z0-9]{2,5}", ext):
        ext = "jpg"

    data = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        data.extend(chunk)
        if len(data) > MAX_IMAGE_BYTES:
            response.close()
            raise RuntimeError("Image is too large")
    response.close()

    if not data:
        raise RuntimeError(f"Empty image response: {url}")

    return ext, bytes(data)


def build_hentaikun_zip(url: str) -> tuple[str, bytes, str]:
    gallery = get_hentaikun_gallery(url)
    archive = io.BytesIO()
    safe_title = _safe_filename(gallery.title)

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.txt", f"Source: hentaikun\nTitle: {gallery.title}\nURL: {gallery.url}\nImages: {len(gallery.images)}\n")

        for index, image_url in enumerate(gallery.images, start=1):
            ext, data = _download_image(image_url, gallery.url)
            zf.writestr(f"{index:03}.{ext}", data)
            time.sleep(0.15)

    return f"hentaikun-{gallery.album}-{safe_title}.zip", archive.getvalue(), gallery.title
