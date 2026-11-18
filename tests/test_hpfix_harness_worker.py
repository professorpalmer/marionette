"""Regression tests for the cp/mv/install/rsync rule of _detect_escaped_writes."""

from __future__ import annotations

import os

from harness.worker import _detect_escaped_writes

WT = "/tmp/hpfix_wt"


def _found(cmd: str) -> list[str]:
    events = [{"kind": "action_start", "data": {"kind": "run_command", "goal": cmd}}]
    return _detect_escaped_writes(events, WT)


# Bug 8c8070c4-1: a '/' inside a relative token started an absolute match.
def test_relative_nested_destination_is_not_escaped():
    assert _found("cp a.txt sub/dir/b.txt") == []


def test_absolute_destination_still_escaped():
    assert _found("cp a.txt /var/tmp/hpfix/b.txt") == [os.path.abspath("/var/tmp/hpfix/b.txt")]


# Bug 8c8070c4-2: an absolute source was taken as the destination.
def test_absolute_source_relative_destination_is_not_escaped():
    assert _found("cp /etc/hosts ./hosts.txt") == []
    assert _found("mv /var/tmp/x hosts.txt") == []


def test_absolute_source_absolute_destination_reports_destination():
    assert _found("cp /etc/hosts /var/tmp/hosts.txt") == [os.path.abspath("/var/tmp/hosts.txt")]


def test_target_directory_option_is_destination():
    assert _found("cp -t /var/tmp/out a b") == [os.path.abspath("/var/tmp/out")]
    assert _found("cp -t out /etc/a /etc/b") == []


# Bug 8c8070c4-4: a path component named install matched the command word.
def test_install_path_component_is_not_a_command():
    assert _found("ls /opt/install/bin") == []
    assert _found("cat /usr/lib/cp/readme") == []


def test_cp_in_compound_shell_forms_still_escaped():
    cases = {
        "for f in a; do cp $f /var/tmp/z; done": "/var/tmp/z",
        "if x; then mv a /var/tmp/q; fi": "/var/tmp/q",
        "xargs -I{} cp {} /var/tmp/w": "/var/tmp/w",
        "nohup cp a /var/tmp/n": "/var/tmp/n",
        "time cp a /var/tmp/t": "/var/tmp/t",
        'bash -c "cp a /var/tmp/k"': "/var/tmp/k",
        "sh -c 'mv a /etc/x'": "/etc/x",
        "{ cp a /etc/y; }": "/etc/y",
    }
    for cmd, dest in cases.items():
        assert _found(cmd) == [os.path.abspath(dest)], cmd


def test_quoted_destination_with_space_is_kept_whole():
    assert _found('cp a "/var/tmp/x y"') == [os.path.abspath("/var/tmp/x y")]
    assert _found('cp "/etc/a b" "./c d"') == []


def test_cp_after_separator_still_escaped():
    assert _found("ls /opt && cp x /var/tmp/y") == [os.path.abspath("/var/tmp/y")]
    assert _found("sudo cp x /etc/passwd") == [os.path.abspath("/etc/passwd")]


# Bug 8c8070c4-5: install as an argument of a package manager only reads.
def test_package_manager_install_is_not_escaped():
    assert _found("pip install /tmp/pkg-1.0.whl") == []
    assert _found("npm install /tmp/pkg.tgz") == []
    assert _found("python -m pip install -U /tmp/pkg.whl") == []
    assert _found("sudo apt-get -y install /tmp/pkg.deb") == []


def test_install_command_word_still_escaped():
    assert _found("install -m 755 tool /usr/local/bin/tool") == [os.path.abspath("/usr/local/bin/tool")]
