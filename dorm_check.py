
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
REGION_EXCLUDE = os.environ.get("REGION_EXCLUDE", "viln").lower()

REQUEST_TIMEOUT = 15
PAGE_TIMEOUT = 30000
MAX_ATTEMPTS = 2


def notify(message, urgent=False):
    headers = {}
    if urgent:
        headers = {"Priority": "urgent", "Tags": "rotating_light"}

    try:
        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        print("Notification sent.")
    except Exception as exc:
        print("Could not send notification:", exc)


def settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(1000)


def real_options(select):
    options = select.locator("option").evaluate_all(
        "els => els.map(e => ({v: e.value, t: e.textContent.trim()}))"
    )
    return [o for o in options if o["v"].strip()]


def switch_to_english(page):
    selectors = [
        "a[href*='lang=en']",
        "a[href*='/en']",
        "img[src*='gb']",
        "img[src*='en.']",
        "img[alt*='ngl' i]",
        "img[alt='EN']",
    ]

    for selector in selectors:
        element = page.locator(selector)
        if element.count():
            try:
                element.first.click(timeout=3000)
                settle(page)
                print("Switched to English.")
                return
            except Exception:
                continue

    print("English switch not found; continuing.")


def login(page):
    page.goto(
        LOGIN_URL,
        wait_until="commit",
        timeout=90000,
    )

    password_input = page.locator("input[type='password']").first
    password_input.wait_for(state="visible", timeout=PAGE_TIMEOUT)

    switch_to_english(page)

    password_input = page.locator("input[type='password']").first
    password_input.wait_for(state="visible", timeout=PAGE_TIMEOUT)
    password_input.fill(PASSWORD)

    username_input = page.locator(
        "input[type='text'], input[type='email'], input:not([type])"
    ).first
    username_input.fill(USERNAME)
    password_input.press("Enter")
    settle(page)

    print("After login URL:", page.url)

    # If login did not lead to the dormitory search page,
    # try opening the student rooms section.
    if page.locator("select").count() < 3:
        student_link = page.get_by_text(
            re.compile(r"Studentų kambarius|Student", re.I)
        )
        if student_link.count():
            student_link.first.click()
            settle(page)

    if page.locator("select").count() < 3:
        raise RuntimeError(
            "Could not find the three dormitory search dropdowns. "
            "Login may have failed or the page structure changed."
        )

    print("Dormitory search page is ready.")


def check_all_regions(page):
    found = []
    all_regions = real_options(page.locator("select").nth(0))

    if not all_regions:
        raise RuntimeError("No regions found.")

    regions = [
        region for region in all_regions
        if REGION_EXCLUDE not in region["t"].lower()
    ]

    print("Regions to check:", [r["t"] for r in regions])

    if not regions:
        raise RuntimeError(
            f"No regions left after exclusion filter: {REGION_EXCLUDE}"
        )

    for region in regions:
        print("Checking region:", region["t"])

        selects = page.locator("select")
        selects.nth(0).select_option(value=region["v"])
        page.wait_for_timeout(1000)

        # Choose the first available option in each dependent dropdown.
        for index in (1, 2):
            select = page.locator("select").nth(index)
            options = real_options(select)

            if not options:
                raise RuntimeError(
                    f"No options in dropdown {index} for {region['t']}"
                )

            select.select_option(value=options[0]["v"])
            page.wait_for_timeout(500)

        search_button = page.get_by_role(
            "button",
            name=re.compile(r"Search|Ieškoti", re.I),
        )

        if not search_button.count():
            raise RuntimeError("Search button not found.")

        search_button.first.click()
        settle(page)
        page.wait_for_timeout(1000)

        text = " ".join(page.locator("body").inner_text().split())

        if not text:
            raise RuntimeError(
                f"Empty search response for region {region['t']}"
            )

        if NO_VACANCY_TEXT.lower() in text.lower():
            print(f"{region['t']}: no vacancies.")
            continue

        # Unknown results must not be treated as vacancies.
        # Confirm the actual vacancy-result selector/text from the
        # live site before enabling vacancy notifications.
        print(
            f"UNKNOWN RESULT for {region['t']}: "
            f"{text[:500]}"
        )

        raise RuntimeError(
            f"Could not verify vacancy status for {region['t']}. "
            "Check the search result page structure."
        )

    return found


def run_once():
    browser = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)

            context = browser.new_context(locale="en-US")
            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT)

            try:
                login(page)
                found = check_all_regions(page)
                return found, None
            finally:
                context.close()
                browser.close()
                browser = None

    except Exception as exc:
        traceback.print_exc()

        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

        return [], exc


def main():
    found = []
    error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"Attempt {attempt}/{MAX_ATTEMPTS}")

        found, error = run_once()

        if error is None:
            break

        print("Attempt failed:", error)

    now = datetime.datetime.now(datetime.timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")

    if error is not None:
        notify(
            f"Dorm check FAILED at {timestamp}: {str(error)[:250]}"
        )
        sys.exit(1)

    if found:
        notify(
            "POSSIBLE ROOM AVAILABLE in: "
            + ", ".join(found)
            + ". Check the official dormitory page now!",
            urgent=True,
        )
    else:
        notify(f"Dorm check completed at {timestamp}.")


if __name__ == "__main__":
    main()
