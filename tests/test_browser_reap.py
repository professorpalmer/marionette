"""Marionette-owned Chrome reaper: match owned profiles, never live Chrome."""
from harness.browser_reap import cmdline_is_marionette_browser


def test_matcher_accepts_pmharness_and_puppetmaster_profiles():
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    assert cmdline_is_marionette_browser(
        chrome + " --user-data-dir=/Users/t/.pmharness/browser-profile --remote-debugging-port=9333"
    )
    assert cmdline_is_marionette_browser(
        chrome + " --user-data-dir=/Users/t/.pmharness/browser-profile-real/chrome"
    )
    assert cmdline_is_marionette_browser(
        "chromium --user-data-dir=/Users/t/.puppetmaster/browser-profiles/abcd --headless=new"
    )
    assert cmdline_is_marionette_browser(
        "chrome --user-data-dir=/var/folders/xx/pm-cdp-abc123"
    )


def test_matcher_rejects_user_chrome_and_unrelated():
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    assert not cmdline_is_marionette_browser(
        chrome + " --user-data-dir=/Users/t/Library/Application Support/Google/Chrome"
    )
    assert not cmdline_is_marionette_browser(
        chrome + " --remote-debugging-port=9333"
    )
    assert not cmdline_is_marionette_browser(
        "python -m harness.cli gui --port 61156"
    )
    assert not cmdline_is_marionette_browser(
        chrome + " --user-data-dir=/tmp/proof-audit/chrome-a --remote-debugging-port=9333"
    )
