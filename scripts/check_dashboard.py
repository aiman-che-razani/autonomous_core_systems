import json
import os

from playwright.sync_api import expect, sync_playwright

from scripts.harness import ROOT, Cluster


def main():
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
        page.get_by_role("button", name="Inspect", exact=True).first.click()
        expect(page.locator("#inspect")).to_contain_text("attempts")
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        assert cluster.env["CLIENT_TOKEN"] not in page.content()
        output = ROOT / "docs/results"
        output.mkdir(exist_ok=True)
        page.screenshot(path=str(output / "dashboard-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(output / "dashboard-mobile.png"), full_page=True)
        assert not errors, errors
        page.get_by_role("button", name="Disconnect", exact=True).click()
        assert page.locator("#connection").inner_text() == "Disconnected"
        browser.close()
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
                        "no persisted token",
                        "mobile overflow",
                        "disconnect",
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
