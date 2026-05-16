#!/usr/bin/env python3
import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

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


def wait(driver, seconds=30):
    return WebDriverWait(driver, seconds)


def make_driver(headless=True):
    options = Options()

    chromium_path = (
        os.getenv("CHROME_BINARY")
        or os.getenv("CHROMIUM_BINARY")
        or "/usr/bin/chromium-browser"
    )
    chromedriver_path = os.getenv("CHROMEDRIVER_PATH") or "/usr/bin/chromedriver"

    if Path(chromium_path).exists():
        options.binary_location = chromium_path

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
        service = Service(executable_path=chromedriver_path)
        return webdriver.Chrome(service=service, options=options)

    return webdriver.Chrome(options=options)


def click_login_button(driver):
    candidates = [
        (By.XPATH, "//button[contains(normalize-space(.), 'ورود به نیکس')]"),
        (By.XPATH, "//button[contains(normalize-space(.), 'ادامه')]"),
        (By.XPATH, "//button[@type='submit']"),
    ]

    for locator in candidates:
        try:
            btn = wait(driver, 8).until(EC.element_to_be_clickable(locator))
            btn.click()
            return
        except TimeoutException:
            pass

    raise RuntimeError("دکمه ورود/ادامه پیدا نشد.")


def is_logged_in(driver):
    try:
        url = driver.current_url
        if "/auth/" in url or "/login" in url:
            return False

        body = driver.find_element(By.TAG_NAME, "body").text
        markers = ["داشبورد", "فایل های من", "آپلود فایل", "کیف پول", "نیکس فایل"]
        return any(m in body for m in markers)
    except Exception:
        return False


def login(driver, username, password):
    driver.get(LOGIN_URL)

    username_input = wait(driver, 30).until(
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

    password_input = wait(driver, 30).until(
        EC.visibility_of_element_located((By.XPATH, "//input[@type='password']"))
    )
    password_input.clear()
    password_input.send_keys(password)

    click_login_button(driver)

    wait(driver, 60).until(lambda d: is_logged_in(d))


def go_to_files(driver):
    driver.get(MEDIA_URL)

    wait(driver, 40).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//input[@type='file'] | "
                "//*[self::button or self::a][contains(normalize-space(.), 'آپلود فایل')] | "
                "//*[contains(normalize-space(.), 'فایل های من')]",
            )
        )
    )


def find_file_input(driver):
    # NixFile current UI has:
    # <label for="uploader">آپلود فایل <input id="uploader" class="hidden" type="file"></label>
    selectors = [
        "input#uploader[type='file']",
        "#uploader",
        "label[for='uploader'] input[type='file']",
        "input[type='file']",
    ]

    last_error = None

    for selector in selectors:
        try:
            file_input = wait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )

            driver.execute_script("""
                const el = arguments[0];
                el.classList.remove('hidden');
                el.removeAttribute('hidden');
                el.style.display = 'block';
                el.style.visibility = 'visible';
                el.style.opacity = '1';
                el.style.position = 'fixed';
                el.style.zIndex = '999999';
                el.style.left = '20px';
                el.style.top = '20px';
                el.style.width = '400px';
                el.style.height = '40px';
            """, file_input)

            return file_input

        except Exception as exc:
            last_error = exc

    try:
        label = wait(driver, 20).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "label[for='uploader']"))
        )
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", label)
        driver.execute_script("arguments[0].click();", label)

        file_input = wait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input#uploader, input[type='file']"))
        )
        return file_input

    except Exception as exc:
        last_error = exc

    body = driver.find_element(By.TAG_NAME, "body").text[:2000]
    raise RuntimeError(
        "input آپلود فایل پیدا نشد. آخرین خطا: "
        + str(last_error)
        + "\\nمتن صفحه:\\n"
        + body
    )


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "concat('" + "',\"'\",'".join(value.split("'")) + "')"


def climb_to_card(driver, element):
    script = """
    let el = arguments[0];
    for (let i = 0; i < 12 && el && el.parentElement; i++) {
        el = el.parentElement;
        if (el.querySelector && el.querySelector('button[aria-haspopup="menu"]')) {
            return el;
        }
    }
    return null;
    """
    return driver.execute_script(script, element)


def find_uploaded_card(driver, file_path: Path, timeout=600):
    filename = file_path.name
    stem = file_path.stem
    needles = [filename, stem]

    end = time.time() + timeout

    while time.time() < end:
        # اگر آپلود پنل پایین صفحه درصد پیشرفت دارد، کمی صبر کن تا صفحه کارت را بسازد.
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            if filename in body_text or stem in body_text:
                pass
        except Exception:
            body_text = ""

        # روش 1: پیدا کردن کارت بر اساس اسم فایل در کل DOM
        for needle in needles:
            if not needle:
                continue

            xpaths = [
                f"//*[contains(normalize-space(.), {xpath_literal(needle)})]",
                f"//p[contains(normalize-space(.), {xpath_literal(needle)})]",
                f"//span[contains(normalize-space(.), {xpath_literal(needle)})]",
                f"//div[contains(normalize-space(.), {xpath_literal(needle)})]",
            ]

            for xp in xpaths:
                elements = driver.find_elements(By.XPATH, xp)
                for el in elements:
                    card = climb_to_card(driver, el)
                    if card is not None:
                        return card

        # روش 2: اگر فایل تازه آپلود شده ولی اسمش در DOM فرق دارد،
        # اولین کارت دارای دکمه منوی سه‌نقطه را برگردان.
        # در UI فعلی نیکس‌فایل دکمه کارت این است:
        # button[aria-haspopup="menu"]
        try:
            menu_buttons = driver.find_elements(By.CSS_SELECTOR, "button[aria-haspopup='menu']")
            for btn in menu_buttons:
                card = climb_to_card(driver, btn)
                if card is not None:
                    return card
        except Exception:
            pass

        time.sleep(2)

    raise RuntimeError(f"کارت فایل آپلودشده پیدا نشد: {filename}")



def install_clipboard_hook(driver):
    script = """
    window.__nixCopiedLinks = window.__nixCopiedLinks || [];

    if (!window.__nixClipboardHooked) {
        window.__nixClipboardHooked = true;

        try {
            if (navigator.clipboard) {
                const origWrite = navigator.clipboard.writeText
                    ? navigator.clipboard.writeText.bind(navigator.clipboard)
                    : null;

                navigator.clipboard.writeText = function(text) {
                    try {
                        window.__nixCopiedLinks.push(String(text));
                    } catch (e) {}

                    if (origWrite) {
                        try {
                            return origWrite(text);
                        } catch (e) {
                            return Promise.resolve();
                        }
                    }

                    return Promise.resolve();
                };
            }
        } catch (e) {}

        try {
            const origExec = document.execCommand.bind(document);
            document.execCommand = function(cmd) {
                if (cmd === 'copy') {
                    try {
                        const sel = window.getSelection && window.getSelection().toString();
                        if (sel) window.__nixCopiedLinks.push(String(sel));

                        const active = document.activeElement;
                        if (active && active.value) {
                            window.__nixCopiedLinks.push(String(active.value));
                        }
                    } catch (e) {}
                }

                return origExec.apply(document, arguments);
            };
        } catch (e) {}
    }
    """
    driver.execute_script(script)


def read_hooked_link(driver, timeout=10):
    end = time.time() + timeout

    while time.time() < end:
        items = driver.execute_script("return window.__nixCopiedLinks || [];")
        if isinstance(items, list):
            for item in reversed(items):
                if isinstance(item, str) and item.strip().startswith("http"):
                    return item.strip()

        time.sleep(0.4)

    return ""


def is_valid_nixfile_download_link(value: str) -> bool:
    value = (value or "").strip()

    if not value.startswith("http"):
        return False

    bad = {
        "https://nixfile.com",
        "https://nixfile.com/",
        "https://panel.nixfile.com",
        "https://panel.nixfile.com/",
        "https://nixfile.com/auth/login",
        "https://panel.nixfile.com/auth/login",
    }

    if value.rstrip("/") in {item.rstrip("/") for item in bad}:
        return False

    if "nixfile.com" not in value:
        return False

    # لینک فایل معمولاً باید بعد از دامنه path واقعی داشته باشد.
    parsed = urlparse(value)
    if not parsed.path or parsed.path == "/":
        return False

    return True


def find_link_in_dom(driver):
    candidates_xpath = (
        "//input[starts-with(@value,'http')] | "
        "//textarea[starts-with(normalize-space(text()),'http')] | "
        "//a[starts-with(@href,'http')]"
    )

    found = []

    for el in driver.find_elements(By.XPATH, candidates_xpath):
        value = (
            el.get_attribute("value")
            or el.get_attribute("href")
            or el.text
            or ""
        ).strip()

        if value.startswith("http"):
            found.append(value)

    body = driver.find_element(By.TAG_NAME, "body").text
    found.extend(re.findall(r"https?://\S+", body))

    for value in found:
        value = value.strip().strip(".,;)'\"")
        if is_valid_nixfile_download_link(value):
            return value

    return ""


def copy_link_from_card(driver, card):
    install_clipboard_hook(driver)
    driver.execute_script("window.__nixCopiedLinks = [];")

    # دکمه سه‌نقطه Headless UI:
    # <button aria-haspopup="menu" ...>
    menu_button = card.find_element(By.CSS_SELECTOR, "button[aria-haspopup='menu']")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", menu_button)
    time.sleep(0.5)

    try:
        menu_button.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", menu_button)

    # آیتم منو:
    # <button role="menuitem"> ... <span>کپی لینک</span></button>
    copy_item = wait(driver, 20).until(
        EC.element_to_be_clickable(
            (
                By.XPATH,
                "//*[@role='menuitem' and contains(normalize-space(.), 'کپی لینک')]"
                " | "
                "//*[contains(@role,'menuitem') and contains(normalize-space(.), 'کپی لینک')]"
                " | "
                "//button[contains(normalize-space(.), 'کپی لینک')]"
            )
        )
    )

    try:
        copy_item.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", copy_item)

    link = read_hooked_link(driver, timeout=15)
    if link and is_valid_nixfile_download_link(link):
        return link

    link = find_link_in_dom(driver)
    if link and is_valid_nixfile_download_link(link):
        return link

    raise RuntimeError("لینک کپی‌شده پیدا نشد یا لینک معتبر نبود.")



def dump_debug(driver):
    Path("debug").mkdir(exist_ok=True)
    driver.save_screenshot("debug/nixfile_error.png")
    Path("debug/nixfile_error.html").write_text(driver.page_source, encoding="utf-8")
    print("Debug saved: debug/nixfile_error.png و debug/nixfile_error.html", file=sys.stderr)


def upload_file(file_path: Path, username: str, password: str, headless=True, timeout=600):
    driver = make_driver(headless=headless)

    try:
        driver.set_page_load_timeout(90)

        print("[1/5] Login...")
        login(driver, username, password)

        print("[2/5] Opening media page...")
        go_to_files(driver)

        print("[3/5] Uploading file...")
        file_input = find_file_input(driver)
        file_input.send_keys(str(file_path.resolve()))

        print("[4/5] Waiting for uploaded file card...")
        card = find_uploaded_card(driver, file_path, timeout=timeout)

        print("[5/5] Copying download link...")
        return copy_link_from_card(driver, card)

    except Exception:
        try:
            dump_debug(driver)
        except Exception:
            pass
        raise

    finally:
        driver.quit()


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Upload any file to NixFile and print download link.")
    parser.add_argument("file", help="Path to file")
    parser.add_argument("--user", default=os.getenv("NIXFILE_USERNAME"))
    parser.add_argument("--password", default=os.getenv("NIXFILE_PASS"))
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)

    args = parser.parse_args()

    if not args.user or not args.password:
        print("NIXFILE_USERNAME و NIXFILE_PASS تنظیم نشده‌اند.", file=sys.stderr)
        sys.exit(1)

    file_path = Path(args.file)
    if not file_path.exists() or not file_path.is_file():
        print(f"فایل پیدا نشد: {file_path}", file=sys.stderr)
        sys.exit(1)

    link = upload_file(
        file_path=file_path,
        username=args.user,
        password=args.password,
        headless=not args.no_headless,
        timeout=args.timeout,
    )

    print(link)


if __name__ == "__main__":
    main()
