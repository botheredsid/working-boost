# app.py
import asyncio
import base64
import time
import traceback
import os
import shutil
from typing import List, Optional

from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ----- Optional chromedriver autoinstaller support (if installed) -----
_try_chromedriver_autoinstaller = True
try:
    import chromedriver_autoinstaller
except Exception:
    chromedriver_autoinstaller = None
    _try_chromedriver_autoinstaller = False

# ----- Configurable defaults -----
DEFAULT_WAIT_TIME = 25
DEFAULT_JS_POLL_TIMEOUT = 45
DEFAULT_JS_POLL_INTERVAL = 1.0
DEFAULT_MAX_SCROLL_LOOPS = 60
DEFAULT_SCROLL_PAUSE = 1.0
# ---------------------------------

app = FastAPI(title="AffordableHousing Boost API", version="1.0")


class BoostRequest(BaseModel):
    email: str
    password: str
    num_buttons: int = Field(1, ge=1)
    headless: bool = True
    wait_time: Optional[int] = DEFAULT_WAIT_TIME


class BoostResponse(BaseModel):
    success: bool
    clicked_count: int
    clicked_addresses: List[Optional[str]]
    debug_logs: List[str]
    error: Optional[str] = None
    screenshot_base64: Optional[str] = None


# ---------- helper: locate chrome & chromedriver ----------
COMMON_CHROME_PATHS = [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/local/bin/chromium",
    "/snap/bin/chromium",
]

COMMON_CHROMEDRIVER_PATHS = [
    "/usr/bin/chromedriver",
    "/usr/bin/chromium-driver",
    "/usr/local/bin/chromedriver",
    "/opt/chromedriver",
]


def find_chrome_binary() -> Optional[str]:
    env = os.environ.get("CHROME_BIN") or os.environ.get("GOOGLE_CHROME_SHIM")
    if env and os.path.isfile(env):
        return env
    for exe in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        p = shutil.which(exe)
        if p:
            return p
    for p in COMMON_CHROME_PATHS:
        if os.path.isfile(p):
            return p
    return None


def find_chromedriver_binary() -> Optional[str]:
    env = os.environ.get("CHROMEDRIVER_PATH") or os.environ.get("CHROMEDRIVER_BIN")
    if env and os.path.isfile(env):
        return env
    p = shutil.which("chromedriver")
    if p:
        return p
    for pth in COMMON_CHROMEDRIVER_PATHS:
        if os.path.isfile(pth):
            return pth
    if _try_chromedriver_autoinstaller and chromedriver_autoinstaller is not None:
        chrome_bin = find_chrome_binary()
        if chrome_bin:
            try:
                installed = chromedriver_autoinstaller.install(path="/tmp")
                if installed and os.path.isfile(installed):
                    return installed
            except Exception:
                pass
    return None


# ---------- small DOM helpers ----------
def get_element_text_via_js(drv, el):
    try:
        txt = drv.execute_script(
            "return (arguments[0].innerText || arguments[0].textContent || '').trim();", el
        )
        return (txt or "").strip()
    except Exception:
        return ""


def find_address_for_button(drv, btn):
    try:
        for xp in [
            "./ancestor::div[contains(@class,'listing--card')][1]",
            "./ancestor::div[contains(@class,'listing--item')][1]",
            "./ancestor::div[contains(@class,'listing--property--wrapper')][1]",
        ]:
            try:
                anc = btn.find_element(By.XPATH, xp)
                addr_el = anc.find_element(By.CSS_SELECTOR,
                                           "div.listing--property--address span, div.listing--property--address")
                addr = get_element_text_via_js(drv, addr_el)
                if addr:
                    return addr
            except Exception:
                continue
        try:
            addr_el = btn.find_element(By.XPATH,
                                       "preceding::div[contains(@class,'listing--property--address')][1]//span")
            addr = get_element_text_via_js(drv, addr_el)
            if addr:
                return addr
        except Exception:
            pass
    except Exception:
        pass
    return None


# ---------- selenium worker ----------
def selenium_boost_worker(email: str, password: str, num_buttons: int, headless: bool,
                          wait_time: int = DEFAULT_WAIT_TIME) -> BoostResponse:
    logs: List[str] = []
    clicked_addresses: List[Optional[str]] = []
    screenshot_b64 = None
    driver = None
    try:
        logs.append("Starting Selenium worker")

        # quick network pre-check (no external dependency)
        test_url = "https://www.affordablehousing.com/"
        logs.append(f"Connectivity quick-check to {test_url}")
        try:
            req = Request(test_url, method="HEAD")
            with urlopen(req, timeout=6) as resp:
                status = getattr(resp, "status", None)
                logs.append(f"HEAD status_code={status}")
        except Exception as e:
            logs.append(f"Pre-check failed: {e}")
            raise Exception(
                f"Network pre-check failed for {test_url}. Either outbound network is blocked or the site resets connections. "
                "Check network from container (curl) and/or site blocking. Full error: " + str(e)
            )

        chrome_bin = find_chrome_binary()
        chromedriver_bin = find_chromedriver_binary()
        logs.append(f"Detected chrome binary: {chrome_bin or '<none>'}")
        logs.append(f"Detected chromedriver binary: {chromedriver_bin or '<none>'}")

        if not chromedriver_bin:
            raise Exception("Chromedriver binary not found. Set CHROMEDRIVER_PATH or install chromium-driver in the image.")

        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=en-US")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-sync")
        options.add_argument("--metrics-recording-only")
        options.add_argument("--disable-default-apps")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--remote-debugging-port=0")
        options.add_argument("--disable-features=VizDisplayCompositor")
        options.add_argument("user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        if chrome_bin:
            options.binary_location = chrome_bin

        service = ChromeService(executable_path=chromedriver_bin)
        driver = webdriver.Chrome(service=service, options=options)
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
            )
        except Exception:
            pass

        wait = WebDriverWait(driver, wait_time)

        # robust get with retries/backoff
        tries = 3
        for attempt in range(1, tries + 1):
            try:
                driver.get("https://www.affordablehousing.com/")
                logs.append("Opened affordablehousing.com (via driver.get)")
                break
            except Exception as e:
                logs.append(f"driver.get attempt {attempt} failed: {e}")
                if attempt == tries:
                    raise
                time.sleep(1.5 * attempt)

        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "li.ah--signin--link"))).click()
        logs.append("Clicked homepage Sign In")

        email_input = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input#ah_user")))
        email_input.clear()
        email_input.send_keys(email)
        logs.append("Entered email")

        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button#signin-button"))).click()
        logs.append("Clicked first Sign In button")

        password_input = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input#ah_pass")))
        password_input.clear()
        password_input.send_keys(password)
        logs.append("Entered password")

        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button#signin-with-password-button"))).click()
        logs.append("Clicked final Sign In button")

        wait.until(EC.url_contains("dashboard"))
        logs.append("Login confirmed (dashboard)")

        listing_url = "https://www.affordablehousing.com/v4/pages/Listing/Listing.aspx"
        driver.get(listing_url)
        logs.append(f"Navigated to {listing_url}")

        loops = 0
        last_height = driver.execute_script("return document.body.scrollHeight")
        while loops < DEFAULT_MAX_SCROLL_LOOPS:
            driver.execute_script("window.scrollBy(0, window.innerHeight);")
            time.sleep(DEFAULT_SCROLL_PAUSE)
            loops += 1
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                time.sleep(0.5)
                break
            last_height = new_height
        logs.append(f"Finished incremental scrolling ({loops} loops)")

        start = time.time()
        found_count = 0
        found_context = None
        while time.time() - start < DEFAULT_JS_POLL_TIMEOUT:
            c_buttons = int(driver.execute_script(
                "return document.querySelectorAll('button.usage-boost-button, button.cmn--btn.usage-boost-button').length || 0;"))
            logs.append(f"[JS POLL] buttons={c_buttons}")
            if c_buttons > 0:
                found_context = ("main", None)
                found_count = c_buttons
                break
            time.sleep(DEFAULT_JS_POLL_INTERVAL)

        logs.append(f"[JS POLL RESULT] found_context={found_context} found_count={found_count}")
        if not found_context:
            try:
                screenshot_b64 = driver.get_screenshot_as_base64()
                logs.append("No cards/buttons found - saved screenshot")
            except Exception:
                logs.append("Screenshot failed")
            raise Exception("No listing cards/buttons found after JS polling")

        buttons = driver.find_elements(By.CSS_SELECTOR, "button.usage-boost-button, button.cmn--btn.usage-boost-button")
        logs.append(f"Collected {len(buttons)} button elements")

        boostable = []
        for idx, btn in enumerate(buttons, start=1):
            try:
                btn_text = get_element_text_via_js(driver, btn).lower()
                norm = " ".join(btn_text.split())
                classes = (btn.get_attribute("class") or "").lower()
                in_progress = ("usage-boost-inprogress" in classes) or ("progress" in norm) or ("inprogress" in norm)
                if ("boost" in norm) and not in_progress:
                    address = find_address_for_button(driver, btn)
                    boostable.append((address, btn, norm))
                    logs.append(f"[FOUND] btn#{idx} text='{norm}' addr='{address or '<none>'}'")
                else:
                    logs.append(f"[SKIP] btn#{idx} text='{norm[:60]}' in_progress={in_progress}")
            except Exception as e:
                logs.append(f"[WARN] error inspecting btn#{idx}: {e}")

        logs.append(f"Total boostable detected: {len(boostable)}")
        if not boostable:
            try:
                screenshot_b64 = driver.get_screenshot_as_base64()
                logs.append("No boostable buttons after filtering - saved screenshot")
            except Exception:
                logs.append("screenshot failed")
            raise Exception("No boostable buttons found to click")

        to_click = min(num_buttons, len(boostable))
        clicked = 0
        for i in range(to_click):
            address, btn, text = boostable[i]
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.6)
                driver.execute_script("arguments[0].click();", btn)
                clicked += 1
                clicked_addresses.append(address)
                logs.append(f"Clicked boost for: {address or '<address not found>'}")
                time.sleep(1.2)
            except Exception as ce:
                logs.append(f"Error clicking boost #{i+1}: {ce}")

        return BoostResponse(
            success=True,
            clicked_count=clicked,
            clicked_addresses=clicked_addresses,
            debug_logs=logs,
            error=None,
            screenshot_base64=screenshot_b64
        )

    except Exception as exc:
        tb = traceback.format_exc()
        logs.append(f"Unhandled exception: {str(exc)}")
        logs.append(tb)
        try:
            if driver:
                screenshot_b64 = driver.get_screenshot_as_base64()
                logs.append("Captured error screenshot (base64)")
        except Exception:
            pass

        return BoostResponse(
            success=False,
            clicked_count=0,
            clicked_addresses=[],
            debug_logs=logs,
            error=str(exc),
            screenshot_base64=screenshot_b64
        )

    finally:
        try:
            if driver:
                driver.quit()
                logs.append("Driver.quit() called")
        except Exception:
            pass


# ---- endpoints ----
@app.get("/")
def health():
    return {"status": "Boost API running"}


@app.get("/browser")
def browser_test():
    logs = []
    chrome_bin = find_chrome_binary()
    chromedriver_bin = find_chromedriver_binary()
    logs.append(f"chrome: {chrome_bin or '<none>'}")
    logs.append(f"chromedriver: {chromedriver_bin or '<none>'}")

    if not chromedriver_bin:
        raise HTTPException(status_code=500, detail={"error": "chromedriver not found", "logs": logs})

    # Quick remote pre-check using standard library
    try:
        req = Request("https://www.google.com", method="HEAD")
        with urlopen(req, timeout=6) as r:
            logs.append(f"google HEAD ok status={getattr(r,'status',None)}")
    except Exception as e:
        logs.append(f"connectivity test failed: {e}")
        raise HTTPException(status_code=500, detail={"error": "connectivity test failed", "logs": logs})

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    if chrome_bin:
        options.binary_location = chrome_bin
    options.add_argument("--ignore-certificate-errors")
    service = ChromeService(executable_path=chromedriver_bin)
    driver = webdriver.Chrome(service=service, options=options)
    try:
        driver.get("https://www.google.com")
        title = driver.title
    finally:
        driver.quit()
    return {"page_title": title, "logs": logs}


@app.post("/boost", response_model=BoostResponse)
async def boost_endpoint(req: BoostRequest):
    loop = asyncio.get_event_loop()
    try:
        result: BoostResponse = await loop.run_in_executor(
            None,
            selenium_boost_worker,
            req.email,
            req.password,
            req.num_buttons,
            req.headless,
            req.wait_time or DEFAULT_WAIT_TIME
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Server error: {exc}")
    return result
