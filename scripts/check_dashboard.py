"""Browser checks of the operations dashboard; records evidence unless --dry-run."""

import argparse
import json
import os
import re

from playwright.sync_api import expect, sync_playwright

from greyqueue.protocol import STATUSES
from scripts.harness import ROOT, Cluster


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true", help="run every check but write no evidence files"
    )
    record = not parser.parse_args().dry_run
    with Cluster() as cluster, sync_playwright() as playwright:
        cluster.worker("dashboard-worker")
        browser_args = {"headless": True}
        if os.environ.get("CHROMIUM_PATH"):
            browser_args["executable_path"] = os.environ["CHROMIUM_PATH"]
        browser = playwright.chromium.launch(**browser_args)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(cluster.base + "/dashboard")
        page.get_by_label("Client access token").fill(cluster.env["CLIENT_TOKEN"])
        page.get_by_role("button", name="Connect", exact=True).click()
        expect(page.locator("#connection")).to_contain_text("Connected", timeout=30000)
        page.locator("#submit").get_by_role("button", name="Submit job").click()
        expect(page.locator("#jobs")).to_contain_text("SUCCEEDED", timeout=30000)
        inspect = page.get_by_role("button", name=re.compile(r"^Inspect job ")).first
        inspect.click()
        expect(page.locator("#inspect")).to_contain_text("attempts")
        # Keyboard focus must survive the 2-second repaint of the rows.
        label = inspect.get_attribute("aria-label")
        inspect.focus()
        page.wait_for_timeout(2600)
        focused = page.evaluate("document.activeElement.getAttribute('aria-label')")
        assert focused == label, (focused, label)
        filters = page.locator("#filter option").all_inner_texts()[1:]  # after "All states"
        assert set(filters) == set(STATUSES), filters
        page.select_option("#filter", "CANCELLED")
        expect(page.locator("#jobs")).to_contain_text("No CANCELLED jobs")
        page.select_option("#filter", "")
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        assert cluster.env["CLIENT_TOKEN"] not in page.content()
        output = ROOT / "docs/results"
        output.mkdir(exist_ok=True)
        if record:
            page.screenshot(path=str(output / "dashboard-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if record:
            page.screenshot(path=str(output / "dashboard-mobile.png"), full_page=True)
        assert not errors, errors
        page.get_by_role("button", name="Disconnect", exact=True).click()
        assert page.locator("#connection").inner_text() == "Disconnected"
        page.wait_for_timeout(2500)  # an in-flight refresh must not flip the status back
        assert page.locator("#connection").inner_text() == "Disconnected"
        assert page.locator("#jobs tr").count() == 0 and page.locator("#depth").inner_text() == "—"
        # A rejected token disconnects with an explanation instead of polling forever.
        page.get_by_label("Client access token").fill("not-the-right-token-value")
        page.get_by_role("button", name="Connect", exact=True).click()
        expect(page.locator("#notice")).to_contain_text("rejected", timeout=10000)
        assert page.locator("#connection").inner_text() == "Disconnected"
        assert not errors, errors
        browser.close()
        if not record:
            print("PASS (dry run): all dashboard checks; no evidence written", flush=True)
            return
        (output / "dashboard-check.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "javascript_errors": errors,
                    "checks": [
                        "connect",
                        "submit",
                        "live completion",
                        "inspect attempts",
                        "focus kept across refresh",
                        "complete state filter",
                        "empty state",
                        "no persisted token",
                        "mobile overflow",
                        "disconnect clears panels",
                        "rejected token disconnects",
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print("PASS: browser dashboard interactions, desktop and mobile", flush=True)


if __name__ == "__main__":
    main()
