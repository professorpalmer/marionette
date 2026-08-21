"""models.dev overlay: Hermes mapping, cache, and never-raise fallback."""
from harness import models_dev as md
from harness.opencode_zen import MODELS_DEV_PROVIDER, overlay_metadata


def test_hermes_maps_opencode_zen_to_opencode():
    assert md.models_dev_provider_id("opencode-zen") == "opencode"
    assert md.models_dev_provider_id("opencode-go") == "opencode-go"
    assert MODELS_DEV_PROVIDER == "opencode"


def test_lookup_returns_none_on_empty_or_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    md.clear_cache_for_tests()
    monkeypatch.setattr(md, "_registry", lambda **_kw: {})
    assert md.lookup_model("opencode-zen", "x-preview-f-free") is None
    assert overlay_metadata("x-preview-f-free") == {}


def test_lookup_reads_name_from_cached_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    md.clear_cache_for_tests()
    monkeypatch.setattr(md, "_registry", lambda **_kw: {
        "opencode": {
            "models": {
                "x-preview-f-free": {
                    "name": "Ox Alpha Free",
                    "tool_call": True,
                    "limit": {"context": 200000},
                },
            },
        },
    })
    row = md.lookup_model("opencode-zen", "x-preview-f-free", allow_network=False)
    assert row is not None
    assert row["name"] == "Ox Alpha Free"
    assert row["context_length"] == 200000
    assert row["tool_call"] is True


def test_lookup_reads_go_name_from_cached_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    md.clear_cache_for_tests()
    monkeypatch.setattr(md, "_registry", lambda **_kw: {
        "opencode-go": {
            "models": {
                "ox-alpha-free": {
                    "name": "Ox Alpha Free",
                    "tool_call": True,
                },
            },
        },
    })
    row = md.lookup_model("opencode-go", "ox-alpha-free", allow_network=False)
    assert row is not None
    assert row["name"] == "Ox Alpha Free"
    from harness.opencode_go import overlay_metadata as go_overlay

    assert go_overlay("ox-alpha-free", allow_network=False) == row


def test_lookup_never_raises(monkeypatch):
    monkeypatch.setattr(md, "_registry", lambda **_kw: (_ for _ in ()).throw(RuntimeError("boom")))
    assert md.lookup_model("opencode-zen", "x-preview-f-free") is None


def test_overlay_never_raises_on_hot_path(monkeypatch):
    monkeypatch.setattr(
        md, "lookup_model",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert overlay_metadata("x-preview-f-free") == {}
    assert overlay_metadata("x-preview-f-free", allow_network=True) == {}
