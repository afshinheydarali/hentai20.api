import io
import os
import re
import time
import zipfile
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

EHENTAI_BASE_URL = os.getenv("EHENTAI_BASE_URL", "https://e-hentai.org").rstrip("/")
EH_MAX_GALLERY_IMAGES = int(os.getenv("EH_MAX_GALLERY_IMAGES", "120") or "120")
EH_MAX_IMAGE_MB = int(os.getenv("EH_MAX_IMAGE_MB", "80") or "80")
EH_MAX_ZIP_MB = int(os.getenv("EH_MAX_ZIP_MB", "400") or "400")
EH_REQUEST_DELAY = float(os.getenv("EH_REQUEST_DELAY", "0.7") or "0.7")
EH_CONNECT_TIMEOUT = float(os.getenv("EH_CONNECT_TIMEOUT", "30") or "30")
EH_READ_TIMEOUT = float(os.getenv("EH_READ_TIMEOUT", "90") or "90")
EH_REQUEST_TIMEOUT = (EH_CONNECT_TIMEOUT, EH_READ_TIMEOUT)

EH_ALLOWED_HOSTS = {
    host.strip().lower()
    for host in os.getenv(
        "EH_ALLOWED_HOSTS",
        "e-hentai.org,exhentai.org,ehgt.org,hath.network",
    ).split(",")
    if host.strip()
}

IMAGE_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
}

GALLERY_RE = re.compile(r"/g/(\d+)/([a-zA-Z0-9]+)/?")


@dataclass
class GallerySearchResult:
    title: str
    url: str
    category: str = ""


def _headers(referer: Optional[str] = None) -> dict:
    headers = {
        "User-Agent": os.getenv(
            "EH_USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        ),
        "Accept-Language": "en-US,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    cookie = os.getenv("EHENTAI_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie
    return headers


def host_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return host in EH_ALLOWED_HOSTS or any(host.endswith(f".{allowed}") for allowed in EH_ALLOWED_HOSTS)


def extract_gallery_url(text: str) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None
    for candidate in re.findall(r"https?://[^\s]+", text):
        candidate = candidate.rstrip(").,]")
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if host in {"e-hentai.org", "exhentai.org"} or host.endswith(".e-hentai.org") or host.endswith(".exhentai.org"):
            match = GALLERY_RE.search(parsed.path)
            if match:
                return f"{parsed.scheme}://{host}/g/{match.group(1)}/{match.group(2)}/"
    return None


def gallery_url_from_parts(gallery_id: str, token: str, host: str = "e-hentai.org") -> str:
    return f"https://{host}/g/{gallery_id}/{token}/"


def gallery_parts_from_url(url: str) -> Optional[tuple[str, str, str]]:
    gallery_url = extract_gallery_url(url) or url
    parsed = urlparse(gallery_url)
    match = GALLERY_RE.search(parsed.path)
    if not match:
        return None
    host = (parsed.hostname or "e-hentai.org").lower()
    return match.group(1), match.group(2), host


def _fetch_soup(url: str, referer: Optional[str] = None) -> BeautifulSoup:
    if not host_allowed(url):
        raise ValueError("Blocked EHentai URL outside allowlist.")
    response = requests.get(url, headers=_headers(referer), timeout=EH_REQUEST_TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _safe_filename(value: str, default: str = "gallery") -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", value or default)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:120] or default).strip()


def _absolute_url(url: str, base: str = EHENTAI_BASE_URL) -> str:
    return url if url.startswith(("http://", "https://")) else urljoin(base + "/", url)


def _gallery_title_from_soup(soup: BeautifulSoup, fallback: str = "EHentai gallery") -> str:
    title = soup.select_one("h1#gn") or soup.select_one("h1#gj")
    if title:
        text = title.get_text(" ", strip=True)
        if text:
            return text
    title_tag = soup.find("title")
    if title_tag:
        text = title_tag.get_text(" ", strip=True).replace("- E-Hentai Galleries", "").strip()
        if text:
            return text
    return fallback


def get_gallery_title(gallery_url: str) -> str:
    gallery_url = extract_gallery_url(gallery_url) or gallery_url
    soup = _fetch_soup(gallery_url)
    return _gallery_title_from_soup(soup)


def search_galleries(query: str, limit: int = 10) -> List[GallerySearchResult]:
    query = (query or "").strip()
    if not query:
        return []
    search_url = f"{EHENTAI_BASE_URL}/?f_search={quote_plus(query)}"
    soup = _fetch_soup(search_url)
    results: List[GallerySearchResult] = []
    seen = set()

    for row in soup.select("tr.gtr0, tr.gtr1"):
        link = row.select_one(".glname a[href*='/g/']") or row.select_one("a[href*='/g/']")
        if not link:
            continue
        url = extract_gallery_url(link.get("href", ""))
        if not url or url in seen:
            continue
        title_el = row.select_one(".glink") or link
        title = title_el.get_text(" ", strip=True) or link.get("title") or url
        cat_el = row.select_one(".cn") or row.select_one(".cs")
        results.append(GallerySearchResult(title=title, url=url, category=cat_el.get_text(" ", strip=True) if cat_el else ""))
        seen.add(url)
        if len(results) >= limit:
            return results

    for link in soup.select("a[href*='/g/']"):
        url = extract_gallery_url(link.get("href", ""))
        if not url or url in seen:
            continue
        title = link.get("title") or link.get_text(" ", strip=True) or url
        if len(title) < 3:
            parent = link.find_parent()
            if parent:
                title = parent.get_text(" ", strip=True)[:160] or title
        results.append(GallerySearchResult(title=title, url=url))
        seen.add(url)
        if len(results) >= limit:
            break

    return results


def _gallery_page_urls(gallery_url: str, first_soup: BeautifulSoup) -> List[str]:
    gallery_url = extract_gallery_url(gallery_url) or gallery_url
    page_indexes = {0}
    for link in first_soup.select("a[href*='?p=']"):
        href = link.get("href") or ""
        parsed = urlparse(_absolute_url(href, gallery_url))
        if "/g/" not in parsed.path:
            continue
        page = parse_qs(parsed.query).get("p", [None])[0]
        if page and page.isdigit():
            page_indexes.add(int(page))
    max_page = max(page_indexes) if page_indexes else 0
    return [gallery_url if page == 0 else f"{gallery_url}?p={page}" for page in range(max_page + 1)]


def _image_page_urls(album_page_soup: BeautifulSoup, gallery_url: str) -> List[str]:
    urls: List[str] = []
    seen = set()
    for link in album_page_soup.select("a[href]"):
        href = link.get("href") or ""
        if "/s/" not in href:
            continue
        url = _absolute_url(href, gallery_url)
        if host_allowed(url) and url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def _generate_reloaded_page(picture_page: str, nl_value: str) -> str:
    parsed = urlparse(picture_page)
    query_params = parse_qs(parsed.query)
    query_params["nl"] = nl_value
    return parsed._replace(query=urlencode(query_params, doseq=True)).geturl()


def _image_url_from_page(image_page_url: str) -> str:
    soup = _fetch_soup(image_page_url, referer=EHENTAI_BASE_URL + "/")
    image = soup.find("img", {"id": "img", "src": True})
    if image and image.get("src"):
        return image["src"]

    loadfail = soup.find("a", {"id": "loadfail", "onclick": True})
    if loadfail:
        match = re.search(r"nl\('([^']+)'\)", loadfail.get("onclick", ""))
        if match:
            reloaded_url = _generate_reloaded_page(image_page_url, match.group(1))
            soup = _fetch_soup(reloaded_url, referer=image_page_url)
            image = soup.find("img", {"id": "img", "src": True})
            if image and image.get("src"):
                return image["src"]

    raise RuntimeError("Could not find image URL on EHentai page.")


def _download_image(image_url: str, referer: str) -> tuple[str, bytes]:
    if not host_allowed(image_url):
        raise ValueError("Blocked image URL outside allowlist.")
    response = requests.get(image_url, headers=_headers(referer), timeout=EH_REQUEST_TIMEOUT, stream=True)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type and content_type not in IMAGE_CONTENT_TYPES:
        response.close()
        raise ValueError(f"Blocked non-image response: {content_type}")

    max_image_bytes = EH_MAX_IMAGE_MB * 1024 * 1024
    length = response.headers.get("Content-Length")
    if length and length.isdigit() and int(length) > max_image_bytes:
        response.close()
        raise ValueError("Blocked oversized EHentai image.")

    data = io.BytesIO()
    total = 0
    for chunk in response.iter_content(chunk_size=1024 * 64):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_image_bytes:
            response.close()
            raise ValueError("Blocked oversized EHentai image.")
        data.write(chunk)
    response.close()

    path_name = urlparse(image_url).path.rsplit("/", 1)[-1].split("?", 1)[0] or "image"
    path_name = _safe_filename(path_name, "image")
    if "." not in path_name and content_type in IMAGE_CONTENT_TYPES:
        path_name += IMAGE_CONTENT_TYPES[content_type]
    return path_name, data.getvalue()


def build_gallery_zip(gallery_url: str) -> tuple[str, bytes, str]:
    gallery_url = extract_gallery_url(gallery_url) or gallery_url
    first_soup = _fetch_soup(gallery_url)
    title = _gallery_title_from_soup(first_soup)
    gallery_pages = _gallery_page_urls(gallery_url, first_soup)

    image_pages: List[str] = []
    for index, page_url in enumerate(gallery_pages):
        soup = first_soup if index == 0 else _fetch_soup(page_url, referer=gallery_url)
        image_pages.extend(_image_page_urls(soup, gallery_url))
        if len(image_pages) >= EH_MAX_GALLERY_IMAGES:
            image_pages = image_pages[:EH_MAX_GALLERY_IMAGES]
            break
        if EH_REQUEST_DELAY:
            time.sleep(EH_REQUEST_DELAY)

    if not image_pages:
        raise RuntimeError("No EHentai image pages found. The gallery may require login/cookies or may be unavailable.")

    max_zip_bytes = EH_MAX_ZIP_MB * 1024 * 1024
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, image_page_url in enumerate(image_pages, 1):
            image_url = _image_url_from_page(image_page_url)
            filename, image_bytes = _download_image(image_url, referer=image_page_url)
            archive_name = f"{index:03d}_{filename}"
            archive.writestr(archive_name, image_bytes)
            if zip_buffer.tell() > max_zip_bytes:
                raise ValueError("EHentai ZIP exceeded EH_MAX_ZIP_MB. Raise the limit or lower EH_MAX_GALLERY_IMAGES.")
            if EH_REQUEST_DELAY:
                time.sleep(EH_REQUEST_DELAY)

    filename = _safe_filename(title, "ehentai_gallery") + ".zip"
    return filename, zip_buffer.getvalue(), title
