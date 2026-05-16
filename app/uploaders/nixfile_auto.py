import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


LOGIN_URL = "https://panel.nixfile.com/auth/login"
PANEL_URL = "https://panel.nixfile.com"
MEDIA_URL = "https://panel.nixfile.com/media"

API_BASE = "https://api.nixfile.com"
UPLOAD_URL = f"{API_BASE}/v2/panel/file-manager/file"
LIST_URL = f"{API_BASE}/v2/panel/file-manager/file?page=1&per_page=18&self=true"

TOKEN_FILE = Path(os.getenv("NIXFILE_TOKEN_FILE", ".nixfile-token.json"))


def wait(driver, seconds=30):
    return WebDriverWait(driver, seconds)


def make_driver(headless=True):
    options = Options()

    chrome_binary = (
        os.getenv("CHROME_BINARY")
        or os.getenv("CHROMIUM_BINARY")
        or "/usr/bin/chromium-browser"
    )
    chromedriver_path = os.getenv("CHROMEDRIVER_PATH") or "/usr/bin/chromedriver"

    if Path(chrome_binary).exists():
        options.binary_location = chrome_binary

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1600,1000")
    options.add_argument("--lang=fa-IR")
    options.add_argument("--remote-debugging-port=0")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    if Path(chromedriver_path).exists():
        return webdriver.Chrome(
            service=Service(executable_path=chromedriver_path),
            options=options,
        )

    return webdriver.Chrome(options=options)


def click_login_button(driver):
    candidates = [
        (By.XPATH, "//button[contains(normalize-space(.), 'ورود به نیکس')]"),
        (By.XPATH, "//button[contains(normalize-space(.), 'ادامه')]"),
        (By.XPATH, "//button[@type='submit']"),
    ]

    for locator in candidates:
        try:
            btn = wait(driver, 10).until(EC.element_to_be_clickable(locator))
            btn.click()
            return
        except TimeoutException:
            pass

    raise RuntimeError("دکمه ورود/ادامه پیدا نشد.")


def is_logged_in(driver):
    try:
        current = driver.current_url
        if "/auth/" in current or "/login" in current:
            return False

        body = driver.find_element(By.TAG_NAME, "body").text
        markers = ["داشبورد", "فایل های من", "آپلود فایل", "کیف پول", "نیکس فایل"]
        return any(marker in body for marker in markers)
    except Exception:
        return False


def browser_login_and_get_token(username: str, password: str, headless=True) -> str:
    driver = make_driver(headless=headless)

    try:
        driver.set_page_load_timeout(90)
        driver.get(LOGIN_URL)

        username_input = wait(driver, 40).until(
            EC.visibility_of_element_located(
                (
                    By.XPATH,
                    "//input[@type='text' or @type='email' or @type='tel' "
                    "or contains(@placeholder,'موبایل') or contains(@placeholder,'ایمیل')]",
                )
            )
        )
        username_input.clear()
        username_input.send_keys(username)

        click_login_button(driver)

        password_input = wait(driver, 40).until(
            EC.visibility_of_element_located((By.XPATH, "//input[@type='password']"))
        )
        password_input.clear()
        password_input.send_keys(password)

        click_login_button(driver)

        wait(driver, 80).until(lambda d: is_logged_in(d))

        driver.get(MEDIA_URL)
        time.sleep(3)

        token = ""

        for cookie in driver.get_cookies():
            if cookie.get("name") == "accessToken" and cookie.get("value"):
                token = cookie["value"].strip()
                break

        if not token:
            token = driver.execute_script(
                """
                return (
                    localStorage.getItem('accessToken') ||
                    sessionStorage.getItem('accessToken') ||
                    localStorage.getItem('token') ||
                    sessionStorage.getItem('token') ||
                    ''
                );
                """
            )

        token = (token or "").strip()
        if token.lower().startswith("bearer "):
            token = token.split(" ", 1)[1].strip()

        if not token:
            raise RuntimeError("accessToken پیدا نشد.")

        save_token(token)
        return token

    finally:
        driver.quit()


def save_token(token: str) -> None:
    TOKEN_FILE.write_text(
        json.dumps(
            {
                "accessToken": token,
                "saved_at": int(time.time()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def load_cached_token() -> str:
    env_token = os.getenv("NIXFILE_ACCESS_TOKEN", "").strip()
    if env_token:
        if env_token.lower().startswith("bearer "):
            env_token = env_token.split(" ", 1)[1].strip()
        return env_token

    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            return str(data.get("accessToken") or "").strip()
        except Exception:
            return ""

    return ""


def get_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://panel.nixfile.com",
        "Referer": "https://panel.nixfile.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
    }


def token_works(token: str) -> bool:
    if not token:
        return False

    try:
        r = requests.get(
            LIST_URL,
            headers=get_headers(token),
            timeout=30,
        )
        return r.status_code == 200
    except Exception:
        return False


def get_token(force_login=False) -> str:
    load_dotenv()

    token = "" if force_login else load_cached_token()

    if token and token_works(token):
        return token

    username = os.getenv("NIXFILE_USERNAME", "").strip()
    password = os.getenv("NIXFILE_PASS", "").strip()
    headless = os.getenv("NIXFILE_HEADLESS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }

    if not username or not password:
        raise RuntimeError("NIXFILE_USERNAME و NIXFILE_PASS داخل .env تنظیم نشده‌اند.")

    print("[nixfile] logging in with browser to get fresh token...", file=sys.stderr)
    token = browser_login_and_get_token(username, password, headless=headless)

    if not token_works(token):
        raise RuntimeError("توکن جدید گرفته شد ولی API آن را قبول نکرد.")

    return token


def find_http_link(obj: Any) -> str:
    if isinstance(obj, dict):
        preferred = [
            "download_url",
            "downloadUrl",
            "public_url",
            "publicUrl",
            "link",
            "url",
            "short_link",
            "shortLink",
            "full_link",
            "fullLink",
        ]

        for key in preferred:
            value = obj.get(key)
            if isinstance(value, str) and is_good_link(value):
                return value

        for value in obj.values():
            found = find_http_link(value)
            if found:
                return found

    if isinstance(obj, list):
        for item in obj:
            found = find_http_link(item)
            if found:
                return found

    if isinstance(obj, str):
        for value in re.findall(r"https?://\\S+", obj):
            value = value.strip().strip('",}])')
            if is_good_link(value):
                return value

    return ""


def is_good_link(value: str) -> bool:
    value = (value or "").strip()

    if not value.startswith("http"):
        return False

    # خروجی نهایی نباید لینک private/storage API باشد
    if "api.nixfile.com" in value or "panel.nixfile.com" in value:
        return False

    if "nixfile.com" not in value:
        return False

    bad = {
        "https://nixfile.com",
        "https://nixfile.com/",
    }

    if value.rstrip("/") in {x.rstrip("/") for x in bad}:
        return False

    return True


def guess_link_from_file_item(item: dict) -> str:
    slug = str(item.get("slug") or "").strip("/")
    if slug:
        return f"https://nixfile.com/f/{slug}/"

    for key in ["hash", "token", "uuid", "uid", "code", "id"]:
        value = item.get(key)
        if value:
            value = str(value).strip("/")
            if value:
                return f"https://nixfile.com/f/{value}/"

    return ""


def find_uploaded_in_list(token: str, filename: str) -> str:
    r = requests.get(
        LIST_URL,
        headers=get_headers(token),
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()

    stack = [data]
    stem = Path(filename).stem

    while stack:
        cur = stack.pop()

        if isinstance(cur, dict):
            name = str(
                cur.get("name")
                or cur.get("file_name")
                or cur.get("filename")
                or cur.get("title")
                or ""
            )

            if filename in name or stem in name:
                link = guess_link_from_file_item(cur) or find_http_link(cur)
                if link:
                    return link

            stack.extend(cur.values())

        elif isinstance(cur, list):
            stack.extend(cur)

    return ""



def get_root_folder_id(token: str) -> str:
    r = requests.get(
        LIST_URL,
        headers=get_headers(token),
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    folder = (data.get("data") or {}).get("folder") or {}
    folder_id = str(folder.get("id") or "").strip()
    if not folder_id:
        raise RuntimeError("NixFile root folder_id not found.")
    return folder_id


def upload_with_token(file_path: Path, token: str) -> str:
    file_path = Path(file_path)

    if not file_path.exists() or not file_path.is_file():
        raise RuntimeError(f"File not found: {file_path}")

    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

    folder_id = os.getenv("NIXFILE_FOLDER_ID", "").strip()
    if not folder_id:
        folder_id = get_root_folder_id(token)

    with file_path.open("rb") as f:
        files = {
            "file": (file_path.name, f, mime),
        }

        r = requests.post(
            UPLOAD_URL,
            headers=get_headers(token),
            files=files,
            data={
                "folder_id": folder_id,
                "upload_type": "1",
            },
            timeout=300,
        )

    if r.status_code in {401, 403}:
        raise PermissionError("NixFile token is expired or rejected.")

    if r.status_code >= 400:
        raise RuntimeError(f"NixFile upload failed: HTTP {r.status_code}\n{r.text[:1500]}")

    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"NixFile returned non-JSON response:\n{r.text[:1500]}")

    # Prefer the public nixfile.com/<slug> link from file-manager list.
    link = find_uploaded_in_list(token, file_path.name)
    if link:
        return link

    link = find_http_link(data)
    if link:
        return link

    print(
        "[nixfile] upload response JSON:",
        json.dumps(data, ensure_ascii=False, indent=2),
        file=sys.stderr,
    )
    raise RuntimeError("آپلود موفق بود ولی لینک فایل از پاسخ API پیدا نشد.")



def upload_file_api(file_path: Path, force_login=False) -> str:
    token = get_token(force_login=force_login)

    try:
        return upload_with_token(file_path, token)
    except PermissionError:
        token = get_token(force_login=True)
        return upload_with_token(file_path, token)


def upload_file(file_path: Path, *args, **kwargs) -> str:
    return upload_file_api(Path(file_path))


def main():
    load_dotenv()

    if len(sys.argv) < 2:
        print("Usage: python app/uploaders/nixfile_auto.py /path/to/file", file=sys.stderr)
        sys.exit(1)

    file_path = Path(sys.argv[1])
    link = upload_file_api(file_path)
    print(link)


if __name__ == "__main__":
    main()
