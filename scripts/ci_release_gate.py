#!/usr/bin/env python3
"""Release CI helpers: tree-green tests check, same-tree installer reuse.

Green CI Before Tag stays: a successful `tests` workflow must exist for this
git tree (commit or dest-PR merge with the same tree). release.yml must not
re-run pytest. See RELEASING.md.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Optional


REQUIRED_INSTALLER_NAMES = ("installer-mac", "installer-win", "installer-linux")


def matching_green_run(
    target_tree,  # type: str
    runs,  # type: Iterable[Dict[str, Any]]
    tree_for_sha,  # type: Callable[[str], Optional[str]]
):
    # type: (...) -> Optional[Dict[str, Any]]
    """Return the first successful tests run whose head commit tree matches."""
    if not target_tree:
        return None
    for run in runs:
        conclusion = run.get("conclusion")
        if conclusion not in (None, "", "success"):
            continue
        head = run.get("headSha") or run.get("head_sha") or ""
        if not head:
            continue
        tree = tree_for_sha(head)
        if tree and tree == target_tree:
            return run
    return None


def matching_installer_run(
    target_tree,  # type: str
    runs,  # type: Iterable[Dict[str, Any]]
    tree_for_sha,  # type: Callable[[str], Optional[str]]
    artifact_names_for_run,  # type: Callable[[Dict[str, Any]], Iterable[str]]
    platform,  # type: str
    skip_run_ids=None,  # type: Optional[Iterable[Any]]
):
    # type: (...) -> Optional[Dict[str, Any]]
    """Return a successful release run that already built this tree's installer."""
    skip = {str(x) for x in (skip_run_ids or ()) if x is not None}
    wanted = "installer-{}".format(platform)
    for run in runs:
        run_id = run.get("databaseId") or run.get("id")
        if run_id is not None and str(run_id) in skip:
            continue
        conclusion = run.get("conclusion")
        if conclusion not in (None, "", "success"):
            continue
        head = run.get("headSha") or run.get("head_sha") or ""
        if not head:
            continue
        tree = tree_for_sha(head)
        if not tree or tree != target_tree:
            continue
        names = set(artifact_names_for_run(run))
        if wanted in names:
            return run
    return None


def git_tree_sha(rev="HEAD"):
    # type: (str) -> str
    return subprocess.check_output(
        ["git", "rev-parse", "{}^{{tree}}".format(rev)],
        text=True,
    ).strip()


def git_commit_sha(rev="HEAD"):
    # type: (str) -> str
    return subprocess.check_output(
        ["git", "rev-parse", "{}^{{commit}}".format(rev)],
        text=True,
    ).strip()


def _gh_json(args, repo=None):
    # type: (List[str], Optional[str]) -> Any
    cmd = ["gh"] + args
    if repo and "--repo" not in args:
        cmd[1:1] = ["--repo", repo]
    return json.loads(subprocess.check_output(cmd, text=True))


def _detect_repo():
    # type: () -> str
    env = os.environ.get("GITHUB_REPOSITORY") or ""
    if env:
        return env
    data = json.loads(
        subprocess.check_output(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            text=True,
        )
    )
    return data["nameWithOwner"]


def _commit_tree_via_api(repo, sha):
    # type: (str, str) -> Optional[str]
    try:
        payload = json.loads(
            subprocess.check_output(
                ["gh", "api", "repos/{}/commits/{}".format(repo, sha)],
                text=True,
            )
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    commit = payload.get("commit") or {}
    tree = commit.get("tree") or {}
    sha_out = tree.get("sha")
    return sha_out if isinstance(sha_out, str) and sha_out else None


def _tree_resolver(repo):
    # type: (str) -> Callable[[str], Optional[str]]
    cache = {}  # type: Dict[str, Optional[str]]

    def tree_for(sha):
        # type: (str) -> Optional[str]
        if sha in cache:
            return cache[sha]
        tree = None  # type: Optional[str]
        try:
            tree = git_tree_sha(sha)
        except subprocess.CalledProcessError:
            tree = _commit_tree_via_api(repo, sha)
        cache[sha] = tree
        return tree

    return tree_for


def _list_successful_tests_runs(repo, limit):
    # type: (str, int) -> List[Dict[str, Any]]
    return _gh_json(
        [
            "run",
            "list",
            "--workflow",
            "tests",
            "--status",
            "success",
            "--limit",
            str(limit),
            "--json",
            "headSha,databaseId,url,event,displayTitle,conclusion",
        ],
        repo=repo,
    )


def _list_successful_release_runs(repo, limit):
    # type: (str, int) -> List[Dict[str, Any]]
    return _gh_json(
        [
            "run",
            "list",
            "--workflow",
            "release",
            "--status",
            "success",
            "--limit",
            str(limit),
            "--json",
            "headSha,databaseId,url,event,displayTitle,conclusion",
        ],
        repo=repo,
    )


def _artifact_names(repo, run):
    # type: (str, Dict[str, Any]) -> List[str]
    run_id = run.get("databaseId") or run.get("id")
    if run_id is None:
        return []
    try:
        payload = json.loads(
            subprocess.check_output(
                [
                    "gh",
                    "api",
                    "repos/{}/actions/runs/{}/artifacts".format(repo, run_id),
                ],
                text=True,
            )
        )
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return []
    artifacts = payload.get("artifacts") or []
    names = []
    for item in artifacts:
        name = item.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def cmd_require_green(args):
    # type: (argparse.Namespace) -> int
    repo = args.repo or _detect_repo()
    rev = args.sha
    target_tree = git_tree_sha(rev)
    target_commit = git_commit_sha(rev)
    runs = _list_successful_tests_runs(repo, args.limit)
    match = matching_green_run(target_tree, runs, _tree_resolver(repo))
    if match is None:
        sys.stderr.write(
            "No successful `tests` workflow for tree {} (commit {}).\n"
            "Wait for dest->main PR tests if merge^{{tree}} matches, or for "
            "main tests if a conflict resolution changed the tree.\n".format(
                target_tree, target_commit
            )
        )
        return 1
    url = match.get("url") or ""
    head = match.get("headSha") or match.get("head_sha") or ""
    sys.stdout.write(
        "tests workflow green for tree {} via {} {}\n".format(target_tree, head, url)
    )
    return 0


def cmd_adopt_installers(args):
    # type: (argparse.Namespace) -> int
    repo = args.repo or _detect_repo()
    platform = args.platform
    if platform not in ("mac", "win", "linux"):
        sys.stderr.write("platform must be mac, win, or linux\n")
        return 2
    target_tree = git_tree_sha(args.sha)
    skip_ids = []
    current = os.environ.get("GITHUB_RUN_ID")
    if current:
        skip_ids.append(current)
    runs = _list_successful_release_runs(repo, args.limit)
    resolver = _tree_resolver(repo)

    def names_for(run):
        # type: (Dict[str, Any]) -> List[str]
        return _artifact_names(repo, run)

    match = matching_installer_run(
        target_tree,
        runs,
        resolver,
        names_for,
        platform,
        skip_run_ids=skip_ids,
    )
    if match is None:
        sys.stderr.write(
            "No reusable installer-{} artifact for tree {}.\n".format(
                platform, target_tree
            )
        )
        return 1
    run_id = match.get("databaseId") or match.get("id")
    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="marionette-adopt-")
    try:
        subprocess.check_call(
            [
                "gh",
                "run",
                "download",
                str(run_id),
                "--repo",
                repo,
                "-n",
                "installer-{}".format(platform),
                "-D",
                tmp,
            ]
        )
        moved = _flatten_installer_files(tmp, out_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not moved:
        sys.stderr.write(
            "adopter downloaded installer-{} from run {} but found no assets\n".format(
                platform, run_id
            )
        )
        return 1
    sys.stdout.write(
        "adopted installer-{} from run {} ({}) for tree {}\n".format(
            platform, run_id, match.get("url") or "", target_tree
        )
    )
    return 0


def _flatten_installer_files(src_dir, dest_dir):
    # type: (str, str) -> List[str]
    """Copy installer/feed files to dest_dir regardless of artifact nesting."""
    wanted_ext = (".dmg", ".zip", ".exe", ".AppImage", ".blockmap")
    moved = []
    for root, _dirs, files in os.walk(src_dir):
        for name in files:
            lower = name.lower()
            keep = lower.endswith(wanted_ext) or (
                lower.startswith("latest") and lower.endswith(".yml")
            )
            if not keep:
                continue
            dest = os.path.join(dest_dir, name)
            shutil.copy2(os.path.join(root, name), dest)
            moved.append(dest)
    return moved


def build_parser():
    # type: () -> argparse.ArgumentParser
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    require = sub.add_parser(
        "require-green",
        help="fail unless a successful tests workflow exists for this tree",
    )
    require.add_argument("--sha", default="HEAD")
    require.add_argument("--repo", default="")
    require.add_argument("--limit", type=int, default=50)
    require.set_defaults(func=cmd_require_green)

    adopt = sub.add_parser(
        "adopt-installers",
        help="download same-tree installer artifacts from a prior release run",
    )
    adopt.add_argument("--platform", required=True, choices=("mac", "win", "linux"))
    adopt.add_argument("--out", required=True)
    adopt.add_argument("--sha", default="HEAD")
    adopt.add_argument("--repo", default="")
    adopt.add_argument("--limit", type=int, default=20)
    adopt.set_defaults(func=cmd_adopt_installers)
    return parser


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
