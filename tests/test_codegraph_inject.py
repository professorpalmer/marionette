import pytest

from harness.codegraph_inject import wrap_slice


SOURCE_GUIDANCE = (
    "> The code below is the **verbatim, current on-disk source** of these "
    "files — re-read from disk on this call and line-numbered, byte-for-byte "
    "identical to what the Read tool returns. It is NOT a summary, outline, "
    "or stale cache. Treat each block as a Read you have already performed: "
    "do not Read a file shown here."
)
AUTHORITATIVE = (
    "Use these symbols and files as authoritative starting points. "
    "Confirm with the live repo before relying on them, but do not "
    "re-scan the whole codebase if CodeGraph already located the relevant area."
)


@pytest.mark.parametrize("fence", ["```", "````", "~~~"])
@pytest.mark.parametrize("formatter_available", [True, False])
def test_wrap_normalizes_generated_guidance_and_preserves_source(
    monkeypatch, fence, formatter_available,
):
    if not formatter_available:
        def unavailable(_text):
            raise RuntimeError("formatter unavailable")
        monkeypatch.setattr(
            "puppetmaster.codegraph.codegraph_prompt_section", unavailable,
        )
    excerpt = (
        f"{fence}python\n"
        f"1\tmessage = {SOURCE_GUIDANCE!r}\n"
        f"2\thelp_text = {AUTHORITATIVE!r}\n"
        f"3\tliteral = 'do not Read a file shown here'\n"
        f"{fence}\n"
    )
    text = f"## Source Code\n\n{SOURCE_GUIDANCE}\n\n{excerpt}\n{AUTHORITATIVE}"
    section, _ = wrap_slice(text)
    assert excerpt in section
    guidance = section.replace(excerpt, "")
    assert "verbatim, current on-disk source" not in guidance
    assert "do not Read a file shown here" not in guidance
    assert "authoritative starting points" not in guidance
    assert "not a verbatim on-disk guarantee" in guidance


def test_index_scope_ignores_an_ancestor_index_spanning_other_repos(tmp_path):
    import subprocess

    from harness.codegraph_inject import index_in_scope, index_scope_root

    parent = tmp_path / "projects"
    (parent / ".codegraph").mkdir(parents=True)
    (parent / ".codegraph" / "codegraph.db").write_bytes(b"")
    repo = parent / "app"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    # Only the ancestor has an index: its hits belong to sibling repos.
    assert not index_in_scope(str(repo))
    # A bare .codegraph/ config dir (as $HOME has) is not an index either.
    (repo / ".codegraph").mkdir()
    assert not index_in_scope(str(repo))

    (repo / ".codegraph" / "codegraph.db").write_bytes(b"")
    import os
    assert index_scope_root(str(repo / "src")) == os.path.realpath(repo)
