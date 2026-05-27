#!/usr/bin/env python3
"""
test_canada_switch.py — Quick test for the Canada flag switch on myvipon.com.
Uses regular headed Chrome (no undetected_chromedriver).
Does NOT write to any sheet. Just logs in, clicks the Canada flag, and reports.

Run:
    python test_canada_switch.py
"""

import os, time, json
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ── Credentials ──────────────────────────────────────────────────────────────
def _load_accounts():
    path = os.path.expanduser("~/vipon_accounts.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return [{"username": "ayman1elmasry@yahoo.com", "password": "HighVoltage123*"}]

ACCOUNT = _load_accounts()[0]

# ─────────────────────────────────────────────────────────────────────────────
def log(msg): print(msg, flush=True)

def create_driver():
    opts = Options()
    opts.add_argument("--window-size=1280,900")
    opts.add_experimental_option("detach", True)   # keeps browser open after script ends
    return webdriver.Chrome(options=opts)

def login(driver, wait):
    driver.get("https://www.myvipon.com/login")
    time.sleep(3)

    # Find all visible email inputs and use the first interactable one
    inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='email'], input[name='email']")
    email_field = None
    for inp in inputs:
        if inp.is_displayed() and inp.is_enabled():
            email_field = inp
            break
    if not email_field:
        raise RuntimeError("Could not find visible email input on login page")

    driver.execute_script("arguments[0].scrollIntoView(true);", email_field)
    email_field.click()
    time.sleep(0.3)
    email_field.clear()
    email_field.send_keys(ACCOUNT["username"])

    pw_field = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
    pw_field.click()
    pw_field.send_keys(ACCOUNT["password"])

    driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']").click()
    time.sleep(4)
    log(f"Logged in as {ACCOUNT['username']}")
    log(f"Current URL: {driver.current_url}")

def switch_to_canada(driver):
    log("\nAttempting Canada flag switch...")
    try:
        dropdown = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "dropdownMenu2"))
        )
        # Read current flag
        try:
            current = driver.find_element(By.CSS_SELECTOR, "#dropdownMenu2 img.active_flag")
            log(f"  Current flag: {current.get_attribute('src')}")
        except Exception:
            log("  (could not read current flag)")

        dropdown.click()
        time.sleep(1)
        log("  Dropdown opened — looking for Canada /ca.svg option...")

        canada = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//li[@data-domain='www.amazon.ca']/a"
            ))
        )
        log(f"  Found Canada element: {canada.get_attribute('outerHTML')}")
        canada.click()
        time.sleep(3)

        # Verify
        try:
            new_flag = driver.find_element(By.CSS_SELECTOR, "#dropdownMenu2 img.active_flag")
            src = new_flag.get_attribute("src")
            log(f"  Flag after click: {src}")
            if "/ca.svg" in src:
                log("SUCCESS - Canada flag is now active")
                return True
            else:
                log(f"WARNING - Flag did not change (still: {src})")
                return False
        except Exception as e:
            log(f"  Could not verify flag after switch: {e}")
            return False

    except Exception as e:
        log(f"FAILED - {e}")

        # Print all dropdown menu items to help diagnose
        try:
            items = driver.find_elements(By.XPATH, "//ul[contains(@class,'dropdown-menu')]//a")
            log(f"  Dropdown items found: {len(items)}")
            for i, item in enumerate(items):
                log(f"    [{i}] href={item.get_attribute('href')} text={item.text.strip()}")
                imgs = item.find_elements(By.TAG_NAME, "img")
                for img in imgs:
                    log(f"         img src={img.get_attribute('src')}")
        except Exception as e2:
            log(f"  Could not inspect dropdown: {e2}")
        return False

def main():
    log("=== Canada Switch Test (headed Chrome) ===")
    driver = create_driver()
    wait   = WebDriverWait(driver, 15)

    driver.get("https://www.myvipon.com")
    time.sleep(4)
    log(f"Page loaded: {driver.current_url}")

    success = switch_to_canada(driver)

    if success:
        # Check page for amazon.ca links
        page_src = driver.page_source
        if "amazon.ca" in page_src:
            log("Page contains amazon.ca links - Canada products confirmed")
        else:
            log("WARNING - No amazon.ca links found in page source")
        log(f"Final URL: {driver.current_url}")
    else:
        log("\nSwitch failed. Chrome stays open so you can inspect manually.")
        log("Check the dropdown items printed above to find the correct selector.")

    log("\n=== Test done (Chrome stays open) ===")

if __name__ == "__main__":
    main()
