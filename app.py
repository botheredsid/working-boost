import asyncio
import base64
import time
import traceback
import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ----- Configurable defaults -----
DEFAULT_WAIT_TIME = 25
DEFAULT_SCROLL_PAUSE = 1.0
DEFAULT_MAX_SCROLL_LOOPS = 60
DEFAULT_JS_POLL_INTERVAL = 1.0
DEFAULT_JS_POLL_TIMEOUT = 45
DEFAULT_PER_LISTING_WAIT = 3
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

@app.get("/")
def root():
    return {"status": "Boost API running"}

# ---- Helper functions ----
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
        anc = btn.find_element(By.XPATH, "./ancestor::div[contains(@class,'listing--card')][1]")
        addr_el = anc.find_element(By.CSS_SELECTOR, "div.listing--property--address span, div.listing--property--address")
        return get_element_text_via_js(drv, addr_el)
    except Exception:
        return None


# ---- Main Selenium Worker ----
def selenium_boost_worker(email: str, password: str, num_buttons: int, headless: bool, wait_time: int = DEFAULT_WAIT_TIME) -> BoostResponse:
    logs: List[str] = []
    clicked_addresses: List[Optional[str]] = []
    screenshot_b64 = None
    driver = None

    try:
        logs.append("Starting Selenium worker")

        # Use Chrome path suitable for Render/Docker
        chrome_path = os.environ.get("CHROME_BIN", "/usr/bin/chromium")
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")

        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=en-US")

        service = ChromeService(executable_path=chromedriver_path)
        driver = webdriver.Chrome(service=service, options=options)
        wait = WebDriverWait(driver, wait_time)

        # --- LOGIN FLOW ---
        driver.get("https://www.affordablehousing.com/")
        logs.append("Opened affordablehousing.com")

        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "li.ah--signin--link"))).click()
        logs.append("Clicked homepage Sign In")

        email_input = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input#ah_user")))
        email_input.send_keys(email)
        logs.append("Entered email")

        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button#signin-button"))).click()
        logs.append("Clicked first Sign In button")

        password_input = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input#ah_pass")))
        password_input.send_keys(password)
        logs.append("Entered password")

        wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button#signin-with-password-button"))).click()
        logs.append("Clicked final Sign In button")

        wait.until(EC.url_contains("dashboard"))
        logs.append("Login confirmed (dashboard)")

        # Go to listing page
        driver.get("https://www.affordablehousing.com/v4/pages/Listing/Listing.aspx")
        logs.append("Navigated to listings")

        # Example: Wait for boost buttons
        buttons = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "button.usage-boost-button")))
        logs.append(f"Found {len(buttons)} boost buttons")

        clicked = 0
        for i, btn in enumerate(buttons[:num_buttons]):
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.6)
                driver.execute_script("arguments[0].click();", btn)
                addr = find_address_for_button(driver, btn)
                clicked_addresses.append(addr)
                clicked += 1
                logs.append(f"Clicked boost #{i+1} - {addr or 'no address'}")
                time.sleep(1)
            except Exception as ce:
                logs.append(f"Click error #{i+1}: {ce}")

        return BoostResponse(success=True, clicked_count=clicked, clicked_addresses=clicked_addresses, debug_logs=logs)

    except Exception as e:
        logs.append(f"Error: {str(e)}")
        logs.append(traceback.format_exc())
        if driver:
            try:
                screenshot_b64 = driver.get_screenshot_as_base64()
            except Exception:
                pass
        return BoostResponse(success=False, clicked_count=0, clicked_addresses=[], debug_logs=logs, error=str(e), screenshot_base64=screenshot_b64)

    finally:
        if driver:
            driver.quit()
            logs.append("Driver.quit() called")


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
            req.wait_time or DEFAULT_WAIT_TIME,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Server error: {exc}")

    return result
