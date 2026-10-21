"""
VMU dormitory vacancy check - runs ONCE per call (GitHub Actions calls it every ~10 min).
Login details and the ntfy topic come from GitHub Secrets, not from this file.
"""
import datetime
import os
import re
import sys
import time
import traceback

import requests
from playwright.sync_api import sync_playwright

USERNAME = os.environ["VDU_USERNAME"]
PASSWORD = os.environ["VDU_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

LOGIN_URL = "https://studentas.vdu.lt/dormitory/login"
NO_VACANCY_TEXT = "No vacancies were found"

# Only regions whose name contains one of these words are checked ("Kaunas" also covers "Kaunas raj.").
WANTED_REGIONS = ["Kaun"]

# Each scheduled run does one check. If it fails (slow site), it retries up to 3 times, 30 s apart.
CHECKS_PER_RUN = 1
GAP_SECONDS = 0
CHECK_ATTEMPTS = 5
RETRY_WAIT = 30


def notify(message, urgent=False):
    # urgent=True sends at ntfy's highest priority (loud, long vibration, can bypass Do Not Disturb)
    headers = {"Priority": "urgent", "Tags": "rotating_light"} if urgent else {}
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
                      headers=headers, timeout=15)
    except Exception as e:
        print("Could not send notification:", e)


def settle(page):
    """Wait for the page to calm down, but never fail just because it keeps loading."""
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1000)


def real_options(select):
    opts = select.locator("option").evaluate_all(
        "els => els.map(e => ({v: e.value, t: e.textContent.trim()}))"
    )
    return [o for o in opts if o["v"].strip() != ""]


def switch_to_english(page):
    for sel in ["a[href*='lang=en']", "a[href*='/en']", "img[src*='gb']",
                "img[src*='en.']", "img[alt*='ngl' i]", "img[alt='EN']"]:
        el = page.locator(sel)
        if el.count():
            el.first.click()
            settle(page)
            print("  switched to English")
            return
    print("  WARNING: could not find the English flag")


def login(page):
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
    page.locator("input[type=password]").first.wait_for(state="visible")
    page.wait_for_timeout(2000)
    switch_to_english(page)
    page.locator("input[type=password]").first.wait_for(state="visible")
    page.wait_for_timeout(2000)
    page.locator("input[type=password]").first.fill(PASSWORD)
    page.locator("input[type=text], input[type=email], input:not([type])").first.fill(USERNAME)
    page.locator("input[type=password]").first.press("Enter")
    settle(page)
    print("  after login, page is:", page.url)

    if page.locator("select").count() < 3:
        page.get_by_text(re.compile(r"Studentų kambarius|Student", re.I)).first.click()
        settle(page)
        page.wait_for_timeout(1500)
        print("  opened student rooms:", page.url)
    print("  dropdowns found:", page.locator("select").count())


def check_all_regions(page):
    found = []
    regions = real_options(page.locator("select").nth(0))
    print("  regions on site:", [r["t"] for r in regions])
    regions = [r for r in regions if any(w.lower() in r["t"].lower() for w in WANTED_REGIONS)]
    print("  regions checked:", [r["t"] for r in regions])
    if not regions:
        raise RuntimeError("no matching regions found on the page")

    for region in regions:
        page.locator("select").nth(0).select_option(value=region["v"])
        page.wait_for_timeout(1500)

        for i in (1, 2):
            sel = page.locator("select").nth(i)
            opts = real_options(sel)
            if opts:
                sel.select_option(value=opts[0]["v"])
                page.wait_for_timeout(800)

        page.get_by_role("button", name=re.compile(r"Search|Ieškoti", re.I)).click()
        settle(page)
        page.wait_for_timeout(2000)

        text = page.inner_text("body")
        if NO_VACANCY_TEXT in text:
            print(f"  {region['t']}: no vacancies")
        else:
            print(f"  {region['t']}: CHANGED!")
            print("  page text starts with:", " ".join(text.split())[:300])
            found.append(region["t"])
    return found


def run_session(num_checks):
    """Logs in, then checks several times, a few minutes apart.
    Returns (regions_found, successful_checks, last_error)."""
    notified = set()
    successes, last_error = 0, None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = page = None
        try:
            for i in range(num_checks):
                if i > 0:
                    time.sleep(GAP_SECONDS)
                for attempt in range(1, CHECK_ATTEMPTS + 1):
                    try:
                        if context is None:  # (re)login when needed
                            context = browser.new_context(locale="en-US")
                            page = context.new_page()
                            page.set_default_timeout(90000)
                            login(page)
                        found = check_all_regions(page)
                        successes += 1
                        for region in found:
                            if region not in notified:  # one alert per region per run
                                notified.add(region)
                                notify(f"ROOM POSSIBLY AVAILABLE in {region}! Go check studentas.vdu.lt/dormitory now!", urgent=True)
                        break
                    except Exception as e:
                        traceback.print_exc()
                        last_error = e
                        print(f"Attempt {attempt} of {CHECK_ATTEMPTS} failed: {e}")
                        try:
                            context.close()
                        except Exception:
                            pass
                        context = page = None  # login again on the next attempt
                        if attempt < CHECK_ATTEMPTS:
                            time.sleep(RETRY_WAIT)
        finally:
            browser.close()
    return notified, successes, last_error


def main():
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    num_checks = 1 if manual else CHECKS_PER_RUN  # (manual runs: same)

    notified, successes, error = run_session(num_checks)
    print(f"Done: {successes} of {num_checks} checks succeeded.")

    now = datetime.datetime.now(datetime.timezone.utc)
    if successes == 0:
        # Every check failed. Alert always on manual runs, otherwise about once a day.
        if manual or (now.hour == 9 and now.minute < 20):
            notify("Dorm check failed: " + str(error)[:150])
        sys.exit(1)  # red X on GitHub
    elif manual and not notified:
        notify("Test run OK: logged in, no vacancies right now. Cloud check works.", urgent=True)


if __name__ == "__main__":
    main()