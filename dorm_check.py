
"""
VMU dormitory vacancy checker.
Runs once per GitHub Actions invocation.
Credentials are stored in GitHub Secrets.
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

WANTED_REGIONS = ["Kaun"]

CHECKS_PER_RUN = 1
CHECK_ATTEMPTS = 2
RETRY_WAIT = 10

NAVIGATION_TIMEOUT = 20000
ELEMENT_TIMEOUT = 10000


def notify(message, urgent=False):
    headers = (
        {"Priority": "urgent", "Tags": "rotating_light"}
        if urgent else {}
    )
    try:
        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=8,
        )
        print("Notification status:", response.status_code)
    except requests.RequestException as exc:
        print("Notification failed:", type(exc).__name__, str(exc)[:200])


def settle(page):
    """Wait briefly without requiring every network request to finish."""
    try:
        page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass
    page.wait_for_timeout(300)


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
                print("Switched to English")
                return
            except Exception as exc:
                print("Language switch skipped:", str(exc)[:120])

    print("English switch not found; continuing")


def login(page):
    # Diagnostic HTTP request: checks whether GitHub can reach the website.
    try:
        started = time.monotonic()
        response = requests.get(
            LOGIN_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=(3, 5),
            allow_redirects=True,
        )
        print(
            "HTTP probe:",
            "status =", response.status_code,
            "url =", response.url,
            "seconds =", round(time.monotonic() - started, 1),
        )
    except requests.RequestException as exc:
        print("HTTP probe failed:", type(exc).__name__, str(exc)[:200])

    # Wait for the first response, not DOMContentLoaded.
    try:
        response = page.goto(
            LOGIN_URL,
            wait_until="commit",
            timeout=NAVIGATION_TIMEOUT,
        )
        print(
            "Browser navigation:",
            "status =", response.status if response else "no response",
            "url =", page.url,
        )
    except Exception as exc:
        print("Navigation failed:", type(exc).__name__, str(exc)[:200])
        try:
            print("Current URL:", page.url)
            print("Page title:", page.title())
        except Exception:
            pass
        raise

    password_field = page.locator("input[type=password]").first
    password_field.wait_for(
        state="visible",
        timeout=ELEMENT_TIMEOUT,
    )
    print("Login form is visible")

    switch_to_english(page)

    password_field = page.locator("input[type=password]").first
    password_field.wait_for(
        state="visible",
        timeout=ELEMENT_TIMEOUT,
    )

    username_field = page.locator(
        "input[type=text], input[type=email], input:not([type])"
    ).first

    username_field.wait_for(
        state="visible",
        timeout=ELEMENT_TIMEOUT,
    )

    password_field.fill(PASSWORD)
    username_field.fill(USERNAME)
    password_field.press("Enter")

    settle(page)
    print("After login URL:", page.url)

    if page.locator("select").count() < 3:
        student_link = page.get_by_text(
            re.compile(r"Studentų kambarius|Student", re.I)
        ).first
        student_link.wait_for(state="visible", timeout=ELEMENT_TIMEOUT)
        student_link.click(timeout=ELEMENT_TIMEOUT)
        settle(page)

    print("Dropdowns found:", page.locator("select").count())


def check_all_regions(page):
    found = []

    selects = page.locator("select")
    if selects.count() < 3:
        raise RuntimeError(
            f"Expected at least 3 dropdowns; found {selects.count()}"
        )

    regions = real_options(selects.nth(0))
    print("Regions on site:", [r["t"] for r in regions])

    regions = [
        region for region in regions
        if any(word.lower() in region["t"].lower()
               for word in WANTED_REGIONS)
    ]
    print("Regions to check:", [r["t"] for r in regions])

    if not regions:
        raise RuntimeError("No matching Kaunas regions found")

    for region in regions:
        selects.nth(0).select_option(
            value=region["v"],
            timeout=ELEMENT_TIMEOUT,
        )
        page.wait_for_timeout(500)

        # Preserve the site's first available room/dorm choices.
        for index in (1, 2):
            select = page.locator("select").nth(index)
            options = real_options(select)
            if options:
                select.select_option(
                    value=options[0]["v"],
                    timeout=ELEMENT_TIMEOUT,
                )
                page.wait_for_timeout(300)

        page.get_by_role(
            "button",
            name=re.compile(r"Search|Ieškoti", re.I),
        ).click(timeout=ELEMENT_TIMEOUT)

        settle(page)
        page.wait_for_timeout(500)

        body_text = page.locator("body").inner_text(
            timeout=ELEMENT_TIMEOUT
        )

        if NO_VACANCY_TEXT in body_text:
            print(f"{region['t']}: no vacancies")
        else:
            # Do not automatically assume every changed page means vacancy.
            print(f"{region['t']}: RESULT NEEDS REVIEW")
            print("Page text:", " ".join(body_text.split())[:500])
            found.append(region["t"])

    return found


def run_session():
    notified = set()
    last_error = None
    successes = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = None

        try:
            for attempt in range(1, CHECK_ATTEMPTS + 1):
                try:
                    context = browser.new_context(locale="en-US")
                    page = context.new_page()
                    page.set_default_timeout(ELEMENT_TIMEOUT)

                    login(page)
                    found = check_all_regions(page)
                    successes = 1

                    for region in found:
                        if region not in notified:
                            notified.add(region)
                            notify(
                                "Possible dorm vacancy/result change in "
                                f"{region}. Check the official portal: "
                                "https://studentas.vdu.lt/dormitory/login",
                                urgent=True,
                            )
                    break

                except Exception as exc:
                    last_error = exc
                    print(
                        f"Attempt {attempt}/{CHECK_ATTEMPTS} failed:",
                        type(exc).__name__,
                        str(exc)[:250],
                    )
                    traceback.print_exc()

                    if attempt < CHECK_ATTEMPTS:
                        time.sleep(RETRY_WAIT)

                finally:
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            pass
                        context = None

        finally:
            browser.close()

    return notified, successes, last_error


def main():
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    notified, successes, error = run_session()
    print(f"Done: {successes}/{CHECKS_PER_RUN} checks succeeded.")

    if successes == 0:
        message = (
            "Dorm checker failed. "
            f"Last error: {str(error)[:250]}"
        )
        notify(message)
        sys.exit(1)

    if manual and not notified:
        notify(
            "Dorm checker test completed successfully. "
            "No confirmed vacancy alert was generated.",
            urgent=False,
        )


if __name__ == "__main__":
    main()
