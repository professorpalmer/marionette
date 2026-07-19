from __future__ import annotations

from types import SimpleNamespace

from harness.api.files import FileServices, get_file_resolve


def _services(root) -> FileServices:
    return FileServices(
        cfg=SimpleNamespace(repo=str(root)),
        sessions=None,
        upload_dir=str(root / "uploads"),
    )


def test_file_resolve_prefers_exact_then_unique_suffix(tmp_path):
    nested = tmp_path / "harness" / "api"
    nested.mkdir(parents=True)
    target = nested / "routing_savings.py"
    target.write_text("value = 1\n", encoding="utf-8")
    svc = _services(tmp_path)

    status, exact = get_file_resolve("harness/api/routing_savings.py", svc)
    assert status == 200
    assert exact == {
        "ok": True,
        "path": "harness/api/routing_savings.py",
        "exact": True,
    }

    status, unique = get_file_resolve(r"API\ROUTING_SAVINGS.PY", svc)
    assert status == 200
    assert unique["path"] == "harness/api/routing_savings.py"
    assert unique["exact"] is False


def test_file_resolve_reports_ambiguity_and_skips_dependencies(tmp_path):
    for parent in ("one", "two", "node_modules/pkg"):
        folder = tmp_path / parent
        folder.mkdir(parents=True)
        (folder / "same.py").write_text(parent, encoding="utf-8")
    svc = _services(tmp_path)

    status, payload = get_file_resolve("same.py", svc)
    assert status == 409
    assert payload["candidates"] == ["one/same.py", "two/same.py"]


def test_file_resolve_never_fuzzes_traversal(tmp_path):
    (tmp_path / "inside.py").write_text("safe", encoding="utf-8")
    status, payload = get_file_resolve("../inside.py", _services(tmp_path))
    assert status in (400, 403, 404)
    assert payload.get("ok") is not True
