"""Marionette-owned Chrome reaper: match own profiles, never live Chrome."""
from __future__ import annotations

from harness.browser_reap import (
    cmdline_is_marionette_browser,
    parse_posix_ps,
    parse_wmic_list,
    profile_ownership,
    reap_targets,
    user_data_dir,
)

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MAC = {"home": "/Users/t", "platform": "darwin"}


def test_matcher_accepts_this_homes_profiles():
    for path in (
        "/Users/t/.pmharness/browser-profile",
        "/Users/t/.pmharness/browser-profile-real/chrome",
        "/Users/t/.puppetmaster/browser-profiles/abcd",
        "/Users/T/.PMHARNESS/browser-profile",
    ):
        assert cmdline_is_marionette_browser(
            CHROME + " --user-data-dir=" + path + " --remote-debugging-port=9333", **MAC
        ), path


def test_matcher_rejects_user_chrome_and_unrelated():
    for cmd in (
        CHROME + " --user-data-dir=/Users/t/Library/Application Support/Google/Chrome",
        CHROME + " --remote-debugging-port=9333",
        "python -m harness.cli gui --port 61156",
        CHROME + " --user-data-dir=/tmp/proof-audit/chrome-a --remote-debugging-port=9333",
    ):
        assert not cmdline_is_marionette_browser(cmd, **MAC), cmd


def test_sibling_install_under_another_home_is_not_ours():
    for path in (
        "/Users/t/.pse-harness/home/.pmharness/browser-profile",
        "/tmp/fakehome/.pmharness/browser-profile",
        "/Users/t/.pmharness/browser-profile-other",
    ):
        assert profile_ownership(CHROME + " --user-data-dir=" + path + " --no-first-run", **MAC) is None


def test_user_data_dir_keeps_spaces_and_ends_at_next_flag():
    cmd = CHROME + " --user-data-dir=/Users/t My/.pmharness/browser-profile --remote-debugging-port=9"
    assert user_data_dir(cmd) == "/Users/t My/.pmharness/browser-profile"
    assert profile_ownership(cmd, home="/Users/t My", platform="darwin") == "home"


def test_windows_paths_ignore_case_and_slash_direction():
    win = {"home": "C:\\Users\\T", "platform": "win32"}
    assert profile_ownership(
        'chrome.exe --user-data-dir="c:\\users\\t\\.pmharness\\browser-profile" --no-first-run', **win
    ) == "home"
    assert profile_ownership(
        'chrome.exe --user-data-dir="C:\\Users\\other\\.pmharness\\browser-profile"', **win
    ) is None


def test_reap_targets_own_and_orphaned_never_siblings_live_browsers():
    rows = parse_posix_ps("\n".join([
        "  11  1 " + CHROME + " --user-data-dir=/Users/t/Library/Application Support/Google/Chrome",
        "  22  1 " + CHROME + " --user-data-dir=/Users/t/.pmharness/browser-profile --no-first-run",
        "  33  1 " + CHROME + " --user-data-dir=/Users/t/.pse-harness/home/.pmharness/browser-profile --x",
        "  40  1 /Applications/Marionette.app/Contents/MacOS/Marionette",
        "  41 40 python -m harness.cli gui --port 5",
        "  42 41 " + CHROME + " --headless=new --user-data-dir=/var/folders/x/T/pm-cdp-sib --no-first-run",
        "  50  1 python -m harness.cli gui --port 6",
        "  51 50 " + CHROME + " --headless=new --user-data-dir=/var/folders/x/T/pm-cdp-mine --no-first-run",
        "  61  1 " + CHROME + " --headless=new --user-data-dir=/var/folders/x/T/pm-cdp-orphan --x",
        "  71 999 " + CHROME + " --headless=new --user-data-dir=/var/folders/x/T/pm-cdp-gone --x",
    ]))
    assert reap_targets(rows, self_pid=50, **MAC) == [22, 51, 61, 71]


def test_parse_wmic_list_reads_parent_ids():
    text = "\r\n".join([
        'CommandLine=chrome.exe --user-data-dir="C:\\Users\\t\\.pmharness\\browser-profile"',
        "ParentProcessId=4",
        "ProcessId=22",
        "",
        "junk",
        "ProcessId=0",
    ])
    assert parse_wmic_list(text) == [
        (22, 4, 'chrome.exe --user-data-dir="C:\\Users\\t\\.pmharness\\browser-profile"'),
    ]
