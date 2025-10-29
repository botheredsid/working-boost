# app.py
import asyncio
import base64
import time
import traceback
import os
import shutil
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# try optional helper
_try_chromedriver_autoinstaller = True
try:
    import chromedriver_autoinstaller
except Exception:
    chromedriver_autoinstaller = None
    _try_chromedriver_autoinstaller = False

# ----- Configurable defaults -----
DEFAULT_WAIT_TIME = 25
DEFAULT_SCROLL_PAUSE = 1.0
DEFAULT_MAX_SCROLL_LOOPS = 60
DEFAULT_JS_POLL_INTERVAL = 1.0
DEFAULT_JS_POLL_TIMEOUT = 45
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


# ---------- utilities for locating chrome & chromedriver ----------

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
    """Return path to a chrome/chromium binary or None."""
    # 1) environment override
    env = os.environ.get("CHROME_BIN") or os.environ.get("GOOGLE_CHROME_SHIM")
    if env and os.path.isfile(env):
        return env

    # 2) which lookups
    for exe in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        p = shutil.which(exe)
        if p:
            return p

    # 3) common static paths
    for p in COMMON_CHROME_PATHS:
        if os.path.isfile(p):
            return p

    return None


def find_chromedriver_binary() -> Optional[str]:
    """Return path to chromedriver if present, otherwise try to auto-install."""
    # 1) environment override
    env = os.environ.get("CHROMEDRIVER_PATH") or os.environ.get("CHROMEDRIVER_BIN")
    if env and os.path.isfile(env):
        return env

    # 2) which lookups
    p = shutil.which("chromedriver")
    if p:
        return p

    # 3) common static paths
    for pth in COMMON_CHROMEDRIVER_PATHS:
        if os.path.isfile(pth):
            return pth

    # 4) try chromedriver_autoinstaller if available and chrome binary exists
    if _try_chromedriver_autoinstaller and chromedriver_autoinstaller is not None:
        chrome_bin = find_chrome_binary()
        if chrome_bin:
            try:
                # install returns path to the chromedriver file
                installed = chromedriver_autoinstaller.install(path="/tmp")
                if installed and os.path.isfile(installed):
                    return installed
            except Exception:
                # ignore and fallback to None
                pass

    # nothing found
    return None


# ---------- helper functions used inside worker ----------

def get_element_text_via_js(drv, el):
    try:
        txt = drv.execute_script(
            "return (arguments[0].innerText || arguments[0].textContent || '').trim();", el
        )
        return (txt or "").strip()
    except Exception:
        return ""


def find_address_for_button(drv, btn):
    """Best-effort: try listing--card ancestor, fallback to preceding address."""
    try:
        # 1) listing--card ancestor
        try:
            anc = btn.find_element(By.XPATH, "./ancestor::div[contains(@class,'listing--card')][1]")
            addr_el = anc.find_element(By.CSS_SELECTOR, "div.listing--property--address span, div.listing--property--address")
            addr = get_element_text_via_js(drv, addr_el)
            if addr:
                return addr
        except Exception:
            pass

        # 2) other ancestors
        for xp in [
            "./ancestor::div[contains(@class,'listing--item')][1]",
            "./ancestor::div[contains(@class,'listing--property--wrapper')][1]",
        ]:
            try:
                anc = btn.find_element(By.XPATH, xp)
                addr_el = anc.find_element(By.CSS_SELECTOR, "div.listing--property--address span, div.listing--property--address")
                addr = get_element_text_via_js(drv, addr_el)
                if addr:
                    return addr
            except Exception:
                pass

        # 3) preceding address in DOM
        try:
            addr_el = btn.find_element(By.XPATH, "preceding::div[contains(@class,'listing--property--address')][1]//span")
            addr = get_element_text_via_js(drv, addr_el)
            if addr:
                return addr
        except Exception:
            pass
    except Exception:
        pass
    return None


# ---------- Selenium worker that performs the full flow ----------
def selenium_boost_worker(email: str, password: str, num_buttons: int, headless: bool,
                          wait_time: int = DEFAULT_WAIT_TIME) -> BoostResponse:
    logs: List[str] = []
    clicked_addresses: List[Optional[str]] = []
    screenshot_b64 = None
    driver = None
    try:
        logs.append("Starting Selenium worker")

        # Detect chrome & chromedriver
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
        # set binary location if detected (helps selenium find correct browser)
        if chrome_bin:
            options.binary_location = chrome_bin

        # instantiate driver using discovered chromedriver
        service = ChromeService(executable_path=chromedriver_bin)
        driver = webdriver.Chrome(service=service, options=options)
        wait = WebDriverWait(driver, wait_time)

        # --- LOGIN FLOW ---
        driver.get("https://www.affordablehousing.com/")
        logs.append("Opened affordablehousing.com")

        # click homepage sign-in
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

        # wait for redirect (dashboard)
        wait.until(EC.url_contains("dashboard"))
        logs.append("Login confirmed (dashboard)")

        # --- NAVIGATE TO LISTING PAGE ---
        listing_url = "https://www.affordablehousing.com/v4/pages/Listing/Listing.aspx"
        driver.get(listing_url)
        logs.append(f"Navigated to {listing_url}")

        # incremental scrolling to load dynamic content
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

        # JS polling for presence of cards/buttons
        start = time.time()
        found_context = None
        found_count = 0
        while time.time() - start < DEFAULT_JS_POLL_TIMEOUT:
            c_buttons = int(driver.execute_script(
                "return document.querySelectorAll('button.usage-boost-button, button.cmn--btn.usage-boost-button').length || 0;"))
            c_cards = int(driver.execute_script(
                "return document.querySelectorAll('div.listing--card, div.listing--property--wrapper, div.listing--item').length || 0;"))
            c_addresses = int(driver.execute_script(
                "return document.querySelectorAll('div.listing--property--address, div.listing--property--address span').length || 0;"))
            logs.append(f"[JS POLL] buttons={c_buttons}, cards={c_cards}, addresses={c_addresses}")
            if c_buttons > 0 or c_cards > 0 or c_addresses > 0:
                found_context = ("main", None)
                found_count = max(c_buttons, c_cards, c_addresses)
                break
            time.sleep(DEFAULT_JS_POLL_INTERVAL)

        logs.append(f"[JS POLL RESULT] found_context={found_context} found_count={found_count}")
        if not found_context:
            try:
                screenshot_b64 = driver.get_screenshot_as_base64()
                logs.append("No cards/buttons found - saved screenshot (base64)")
            except Exception:
                logs.append("No cards/buttons found - screenshot failed")
            raise Exception("No listing cards or buttons found after JS polling")

        # collect buttons
        buttons = driver.find_elements(By.CSS_SELECTOR, "button.usage-boost-button, button.cmn--btn.usage-boost-button")
        logs.append(f"Collected {len(buttons)} button elements in main document (selenium)")

        # filter boostable: contains 'boost' and not in-progress
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
                    logs.append(f"[SKIP] btn#{idx} text='{norm[:60]}' class='{classes[:80]}' in_progress={in_progress}")
            except Exception as e:
                logs.append(f"[WARN] error inspecting btn#{idx}: {e}")
                continue

        logs.append(f"Total boostable detected: {len(boostable)}")
        if not boostable:
            try:
                screenshot_b64 = driver.get_screenshot_as_base64()
                logs.append("No boostable buttons after filtering - saved screenshot (base64)")
            except Exception:
                logs.append("No boostable buttons - screenshot failed")
            raise Exception("No boostable buttons found to click")

        # click up to requested number
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
                logs.append(f"Clicked boost for: {address or '<address not found>'} (text='{text}')")
                time.sleep(1.5)
            except Exception as ce:
                logs.append(f"Error clicking boost #{i+1} ({address or 'unknown'}): {ce}")
                continue

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


# ---- FastAPI endpoints ----

@app.get("/")
def health():
    return {"status": "Boost API running"}


@app.get("/browser")
def browser_test():
    """Simple endpoint to test chrome/chromedriver availability and return Google title."""
    logs = []
    chrome_bin = find_chrome_binary()
    chromedriver_bin = find_chromedriver_binary()
    logs.append(f"chrome: {chrome_bin or '<none>'}")
    logs.append(f"chromedriver: {chromedriver_bin or '<none>'}")

    if not chromedriver_bin:
        raise HTTPException(status_code=500, detail={"error": "chromedriver not found", "logs": logs})

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    if chrome_bin:
        options.binary_location = chrome_bin

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
    # run Selenium in a background thread to avoid blocking event loop
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
