"""
VMU dormitory vacancy check - runs ONCE per call (GitHub Actions calls it every ~10 min).
Login details and the ntfy topic come from GitHub Secrets, not from this file.
"""
import datetime
import os
import re
import sys
import traceback

import requests
from playwright.sync_api import sync_playwright

USERNAME = os.environ["VDU_USERNAME"]
PASSWORD = os.environ["VDU_PASSWORD"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]

LOGIN_URL = "https://studentas.vdu.lt/dormitory/login"
NO_VACANCY_TEXT = "No vacancies were found"
# Skip any region whose name contains this (e.g. "Vilnius"/"Vilniaus") - no notifications from it.
REGION_EXCLUDE = os.environ.get("REGION_EXCLUDE", "viln").lower()


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
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
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
    all_regions = real_options(page.locator("select").nth(0))
    print("  regions found:", [r["t"] for r in all_regions])
    if not all_regions:
        raise RuntimeError("no regions found on the page")

    regions = [r for r in all_regions if REGION_EXCLUDE not in r["t"].lower()]
    print("  checking only:", [r["t"] for r in regions])
    if not regions:
        raise RuntimeError(
            f"all regions match exclude filter '{REGION_EXCLUDE}'. Available: "
            + ", ".join(r["t"] for r in all_regions)
        )

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


def run_once():
    """One full attempt. Returns (regions_with_vacancies, error_or_None)."""
    found, error = [], None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="en-US")
        page = context.new_page()
        page.set_default_timeout(90000)
        try:
            login(page)
            found = check_all_regions(page)
        except Exception as e:
            traceback.print_exc()
            error = e
        finally:
            context.close()
            browser.close()
    return found, error


def main():
    # Try up to 2 times, in case the university site was just slow.
    found, error = [], None
    for attempt in (1, 2):
        found, error = run_once()
        if not error:
            break
        print(f"Attempt {attempt} failed: {error}")

    now = datetime.datetime.now(datetime.timezone.utc)

    # Send a notification on every check, whatever the outcome.
    if error:
        notify("Dorm check FAILED at " + now.strftime("%H:%M UTC") + ": " + str(error)[:150])
        sys.exit(1)  # makes the run show a red X on GitHub (and GitHub may email you)
    elif found:
        notify("POSSIBLE ROOM AVAILABLE in: " + ", ".join(found) + ". Go check studentas.vdu.lt/dormitory now!", urgent=True)
    else:
        notify("Dorm check OK at " + now.strftime("%H:%M UTC") + ": no vacancies right now.")


if __name__ == "__main__":
    main()
