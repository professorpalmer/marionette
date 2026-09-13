"""Local models appear in the picker only while usable, at zero price."""
from __future__ import annotations

import json
import os

import pytest

from harness.local_model_manager import LocalModelError, LocalModelManager, reset_manager_for_tests
from harness.local_models import canonical_spec, local_secret_reach
from harness import model_visibility as mv
from harness import providers as prov


def test_local_provider_hidden_until_usable(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    assert prov.get_provider("local").name == "local"
    assert prov.get_provider("llama-cpp").name == "local"
    names = [p.name for p in prov.available_providers()]
    assert "local" not in names


def test_usable_local_spec_is_zero_price(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [{
            "id": "qwen-test",
            "name": "Qwen",
            "filename": "m.gguf",
            "url": "http://fixture/m.gguf",
            "revision": "x",
            "sha256": "b" * 64,
            "size": 1,
            "context_length": 1024,
            "min_ram_gb": 0,
            "recommended_ram_gb": 1,
            "min_disk_bytes": 1,
        }],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "name": "ollama",
        "vendor": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["llama3"],
        "selected_model": "llama3",
        "context_length": 4096,
        "has_key": False,
        "healthy": True,
    }]
    mgr._save(state)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    monkeypatch.setattr(mv, "_store_path", lambda: str(tmp_path / "models.json"))
    spec = canonical_spec("ollama-127-0-0-1-11434", "llama3")
    assert spec in mgr.usable_specs()
    rows = mv.catalog(available_only=True)
    local_rows = [row for row in rows if row["provider"] == "local"]
    assert local_rows
    assert local_rows[0]["price_in"] == 0
    assert local_rows[0]["price_out"] == 0
    assert local_rows[0]["spec"] == spec
    restarted = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    resolved = restarted.resolve_spec(spec)
    assert resolved["base_url"] == "http://127.0.0.1:11434/v1"
    assert resolved["secret_reach"].startswith("local-")


def test_unhealthy_external_cannot_resolve_or_build_pilot(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "name": "ollama",
        "vendor": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["llama3"],
        "selected_model": "llama3",
        "healthy": False,
    }]
    mgr._save(state)
    spec = canonical_spec("ollama-127-0-0-1-11434", "llama3")
    assert mgr.resolve_spec(spec) is None
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    try:
        prov.build_pilot(spec)
        raise AssertionError("unhealthy external must not build a driver")
    except (prov.ProviderError, LocalModelError):
        pass


def test_remote_key_survives_env_clear_into_driver(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HARNESS_KEY_ENV", "OPENROUTER_API_KEY")
    reset_manager_for_tests()
    from harness.keys import get_api_key_status, get_env_var_for_reach
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "1.2.3.4"),
    )

    def transport(url, **kwargs):
        return {"payload": {"data": [{"id": "qwen"}]}, "headers": {}, "status": 200}

    mgr = LocalModelManager(
        root=str(tmp_path / "local-models"),
        catalog=catalog,
        probe_transport=transport,
    )
    snap = mgr.save_external(
        "https://api.runpod.ai/v2/abc/openai/v1",
        accept_remote=True,
        model="qwen",
        api_key="sk-remote-secret-xyz",
    )
    row = snap["externals"][0]
    reach = local_secret_reach(row["id"])
    env_var = get_env_var_for_reach(reach)
    assert env_var != "OPENROUTER_API_KEY"
    assert os.environ.get(env_var) == "sk-remote-secret-xyz"
    os.environ.pop(env_var, None)
    assert not os.environ.get(env_var)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    driver = prov.build_pilot("local:%s/qwen" % row["id"])
    assert driver._key() == "sk-remote-secret-xyz"
    blob = json.dumps(mgr.snapshot())
    assert "sk-remote-secret-xyz" not in blob
    status = get_api_key_status(reach)
    assert status["has_key"] is True
    assert "sk-remote-secret-xyz" not in status["masked"]


def test_disconnected_local_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    from harness.keys import mark_disconnected, unmark_disconnected
    reset_manager_for_tests()
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "name": "ollama",
        "vendor": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["llama3"],
        "selected_model": "llama3",
        "healthy": True,
        "kind": "loopback",
        "requires_key": False,
    }]
    mgr._save(state)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    try:
        mark_disconnected("local")
        assert prov.get_provider("local").available is False
        try:
            prov.build_pilot("local:ollama-127-0-0-1-11434/llama3")
            raise AssertionError("disconnected local must not build a driver")
        except prov.ProviderError:
            pass
    finally:
        unmark_disconnected("local")


def test_activate_reconnects_disconnected_local(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    from harness.keys import mark_disconnected, unmark_disconnected
    reset_manager_for_tests()
    monkeypatch.setattr(mv, "_store_path", lambda: str(tmp_path / "models.json"))
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    spec = "local:ollama-127-0-0-1-11434/llama3"
    state = mgr._state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "name": "ollama",
        "vendor": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["llama3"],
        "selected_model": "llama3",
        "healthy": True,
        "kind": "loopback",
        "requires_key": False,
    }]
    mgr._save(state)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    mv.set_enabled(["openrouter:foo"])
    try:
        mark_disconnected("local")
        assert prov.get_provider("local").available is False
        mgr.activate(spec)
        assert prov.get_provider("local").available is True
        assert spec in mv.enabled_pilots()
    finally:
        unmark_disconnected("local")


def test_keyless_lan_builds_and_public_requires_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "lan-box",
        "vendor": "ollama",
        "base_url": "http://192.168.1.20:8080/v1",
        "models": ["qwen"],
        "selected_model": "qwen",
        "healthy": True,
        "has_key": False,
        "kind": "lan",
        "requires_key": False,
        "lan_accepted": True,
    }, {
        "id": "runpod-box",
        "vendor": "openai-compatible",
        "base_url": "https://proxy.runpod.net/v1",
        "models": ["qwen"],
        "selected_model": "qwen",
        "healthy": True,
        "has_key": False,
        "kind": "public",
        "requires_key": True,
        "remote_accepted": True,
    }]
    mgr._save(state)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    lan = prov.build_pilot("local:lan-box/qwen")
    assert lan.allow_keyless is True
    assert lan._key() == "local"
    try:
        prov.build_pilot("local:runpod-box/qwen")
        raise AssertionError("public HTTPS without a key must fail closed")
    except prov.ProviderError as exc:
        assert "requires an API key" in str(exc)


def test_loopback_llama_cpp_builds_keyless(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "llama-loop",
        "vendor": "llama.cpp",
        "base_url": "http://127.0.0.1:8080/v1",
        "models": ["qwen"],
        "selected_model": "qwen",
        "healthy": True,
        "has_key": False,
        "kind": "loopback",
        "requires_key": False,
    }]
    mgr._save(state)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    driver = prov.build_pilot("local:llama-loop/qwen")
    assert driver.allow_keyless is True
    assert driver._is_llama_cpp_host() is True
    assert driver._key() == "local"


def test_public_llama_cpp_stale_has_key_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "llama-public",
        "vendor": "llama.cpp",
        "base_url": "https://proxy.runpod.net/v1",
        "models": ["qwen"],
        "selected_model": "qwen",
        "healthy": True,
        "has_key": True,
        "kind": "public",
        "requires_key": True,
        "remote_accepted": True,
    }]
    mgr._save(state)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    from harness.local_models import local_secret_reach
    from harness.keys import get_env_var_for_reach
    monkeypatch.delenv(get_env_var_for_reach(local_secret_reach("llama-public")), raising=False)
    try:
        prov.build_pilot("local:llama-public/qwen")
        raise AssertionError("stale has_key must not open a public llama.cpp endpoint")
    except prov.ProviderError as exc:
        assert "requires an API key" in str(exc)


def test_first_activate_does_not_collapse_empty_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    monkeypatch.setattr(mv, "_store_path", lambda: str(tmp_path / "models.json"))
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "vendor": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "selected_model": "llama3",
        "healthy": True,
    }]
    mgr._save(state)
    assert mv.get_enabled() == []
    mgr.activate("local:ollama-127-0-0-1-11434/llama3")
    assert mv.get_enabled() == []
    mv.set_enabled(["openrouter:foo"])
    mgr.activate("local:ollama-127-0-0-1-11434/llama3")
    enabled = mv.get_enabled()
    assert "openrouter:foo" in enabled
    assert "local:ollama-127-0-0-1-11434/llama3" in enabled


def test_managed_driver_refreshes_and_releases_every_call(tmp_path, monkeypatch):
    import io
    import pytest
    from harness.local_models import MANAGED_ENDPOINT_ID
    from pmharness.drivers.openai_compat import OpenAICompatDriver
    monkeypatch.setenv('HARNESS_STATE_DIR', str(tmp_path))
    mgr = LocalModelManager(root=str(tmp_path / 'managed'), catalog={'models': []})
    state = mgr._state()
    state['managed']['runtime']['status'] = 'ready'
    state['managed']['model'].update(id='qwen', status='ready')
    state['managed']['process'] = dict(pid=444, port=12345, healthy=True)
    mgr._save(state)
    class Proc:
        pid = 444
        exited = False
        def poll(self):
            return 0 if self.exited else None
        def wait(self, timeout=None):
            assert self.exited
            return 0
    mgr._procs[444] = (Proc(), io.BytesIO())
    monkeypatch.setattr(mgr, '_probe_health', lambda *a, **k: True)
    monkeypatch.setattr('harness.local_model_manager.get_manager', lambda: mgr)
    driver = prov.build_pilot('local:%s/qwen' % MANAGED_ENDPOINT_ID)
    seen = []
    def call(self, *args, **kwargs):
        assert mgr.active_requests == 1
        seen.append(self.base_url)
        callback = kwargs.get('on_delta')
        if callback:
            callback('delta')
        return 'ok'
    for method in ('complete', 'chat', 'chat_stream'):
        monkeypatch.setattr(OpenAICompatDriver, method, call)
        kwargs = {'on_delta': lambda text: None} if method == 'chat_stream' else {}
        assert getattr(driver, method)('prompt', **kwargs) == 'ok'
        assert mgr.active_requests == 0
    with mgr.request_scope('local:managed/qwen') as before:
        old_generation = before.generation
    exe = tmp_path / 'llama-server'
    model_file = tmp_path / 'model.gguf'
    exe.write_text('fixture')
    model_file.write_text('fixture')
    mgr._set_component('runtime', status='ready', path=str(exe))
    mgr._set_component('model', status='ready', id='qwen', path=str(model_file))
    monkeypatch.setattr('harness.local_model_manager.find_free_port', lambda *a: 23456)
    monkeypatch.setattr('harness.local_model_manager.read_process_start_key', lambda *a: '')
    monkeypatch.setattr('harness.local_model_manager.stop_process_tree',
                        lambda pid, proc, **k: setattr(proc, 'exited', True))
    mgr.popen = lambda *a, **k: Proc()
    mgr.restart()
    with mgr.request_scope('local:managed/qwen') as after:
        assert after.generation > old_generation
    assert driver.complete('prompt') == 'ok'
    assert seen[-1] == 'http://127.0.0.1:23456/v1'
    def fail(*args):
        raise ValueError('callback')
    with pytest.raises(ValueError, match='callback'):
        driver.chat_stream([], on_delta=fail)
    assert mgr.active_requests == 0
    def fail_init(*args, **kwargs):
        assert mgr.active_requests == 1
        raise ValueError('construction')
    monkeypatch.setattr(OpenAICompatDriver, '__init__', fail_init)
    with pytest.raises(ValueError, match='construction'):
        driver.complete('prompt')
    assert mgr.active_requests == 0


def test_managed_factory_cannot_downgrade_after_resolution(tmp_path, monkeypatch):
    import io
    import pytest
    from harness.managed_local_driver import ManagedLocalDriver
    monkeypatch.setenv('HARNESS_STATE_DIR', str(tmp_path))
    mgr = LocalModelManager(root=str(tmp_path / 'manager'), catalog={'models': []})
    state = mgr._state()
    state['managed']['runtime']['status'] = 'ready'
    state['managed']['model'].update(id='qwen', status='ready')
    state['managed']['process'] = dict(pid=444, port=12345, healthy=True)
    mgr._save(state)
    class Proc:
        exited = False
        def poll(self):
            return 0 if self.exited else None
        def wait(self, timeout=None):
            return 0
    proc = Proc()
    mgr._procs[444] = (proc, io.BytesIO())
    monkeypatch.setattr(mgr, '_probe_health', lambda *a, **k: True)
    monkeypatch.setattr('harness.local_model_manager.stop_process_tree',
                        lambda *a, **k: setattr(proc, 'exited', True))
    monkeypatch.setattr('harness.local_model_manager.get_manager', lambda: mgr)
    resolve = mgr.resolve_spec
    def resolve_then_stop(spec):
        endpoint = resolve(spec)
        mgr.stop()
        return endpoint
    monkeypatch.setattr(mgr, 'resolve_spec', resolve_then_stop)
    driver = prov.build_pilot('local:managed/qwen')
    assert isinstance(driver, ManagedLocalDriver)
    with pytest.raises(LocalModelError):
        driver.complete('must not contact the old port')


def test_adopted_and_external_llama_cpp_bypass_managed_driver(tmp_path, monkeypatch):
    from harness.managed_local_driver import ManagedLocalDriver
    from pmharness.drivers.openai_compat import OpenAICompatDriver
    monkeypatch.setenv('HARNESS_STATE_DIR', str(tmp_path))
    mgr = LocalModelManager(root=str(tmp_path / 'manager'), catalog={'models': []})
    state = mgr._state()
    state['managed']['runtime']['status'] = 'ready'
    state['managed']['model'].update(id='qwen', status='ready')
    state['managed']['process'] = dict(pid=444, port=12345, healthy=True)
    state['externals'] = [dict(id='external', base_url='http://127.0.0.1:5555/v1',
                               selected_model='qwen', healthy=True, vendor='llama.cpp')]
    mgr._save(state)
    monkeypatch.setattr(mgr, '_probe_health', lambda *a, **k: True)
    monkeypatch.setattr('harness.local_model_manager.process_matches_identity', lambda *a: True)
    monkeypatch.setattr('harness.local_model_manager.get_manager', lambda: mgr)
    drivers = [prov.build_pilot(spec) for spec in ('local:managed/qwen', 'local:external/qwen')]
    def forbidden(*args, **kwargs):
        pytest.fail('external or adopted request used managed lifecycle')
    for name in ('request_scope', 'start', 'stop', 'reconcile_process'):
        monkeypatch.setattr(mgr, name, forbidden)
    for driver in drivers:
        assert not isinstance(driver, ManagedLocalDriver)
        for method in ('complete', 'chat', 'chat_stream'):
            monkeypatch.setattr(OpenAICompatDriver, method, lambda *a, **k: 'ok')
            assert getattr(driver, method)('prompt') == 'ok'
    assert mgr.active_requests == 0


@pytest.mark.parametrize('method', ['complete', 'chat', 'chat_stream'])
@pytest.mark.parametrize('failure', ['transport', 'construction'])
def test_managed_entrypoint_releases_on_failure(tmp_path, monkeypatch, method, failure):
    import io
    from harness.managed_local_driver import ManagedLocalDriver
    from pmharness.drivers.openai_compat import OpenAICompatDriver
    mgr = LocalModelManager(root=str(tmp_path / 'managed'), catalog={'models': []})
    state = mgr._state()
    state['managed']['runtime']['status'] = 'ready'
    state['managed']['model'].update(id='qwen', status='ready')
    state['managed']['process'] = dict(pid=444, port=12345, healthy=True)
    mgr._save(state)
    class Proc:
        def poll(self):
            return None
    mgr._procs[444] = (Proc(), io.BytesIO())
    driver = ManagedLocalDriver(manager=mgr, spec='local:managed/qwen',
                                name='llama-cpp:qwen', model='qwen',
                                base_url='http://127.0.0.1:12345/v1',
                                api_key_env='LOCAL_MODEL_API_KEY', allow_keyless=True)
    def fail(*args, **kwargs):
        assert mgr.active_requests == 1
        raise RuntimeError(failure)
    monkeypatch.setattr(OpenAICompatDriver, '__init__' if failure == 'construction' else method, fail)
    kwargs = {'on_delta': lambda text: None} if method == 'chat_stream' else {}
    with pytest.raises(RuntimeError, match=failure):
        getattr(driver, method)('prompt', **kwargs)
    assert mgr.active_requests == 0


@pytest.mark.parametrize('fails', [False, True])
def test_managed_remembers_reasoning_rejection(monkeypatch, fails):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from harness.managed_local_driver import ManagedLocalDriver
    from pmharness.drivers.openai_compat import OpenAICompatDriver
    class Manager:
        @contextmanager
        def request_scope(self, spec):
            yield SimpleNamespace(model='qwen', base_url='http://localhost:1234/v1')
    driver = ManagedLocalDriver(manager=Manager(), spec='local:managed/qwen',
        name='local', model='qwen', base_url='http://localhost/v1', api_key_env='',
        enable_reasoning=True)
    seen = []
    def request(transport, *a, **k):
        seen.append(transport.enable_reasoning)
        transport.enable_reasoning = False
        if fails:
            raise ValueError('after rejection')
    monkeypatch.setattr(OpenAICompatDriver, 'complete', request)
    for _ in range(2):
        if fails:
            with pytest.raises(ValueError):
                driver.complete('hello')
        else:
            driver.complete('hello')
    assert seen == [True, False]


def test_managed_compaction_fork_keeps_lease_and_isolates_config(monkeypatch):
    from contextlib import ExitStack, contextmanager
    from types import SimpleNamespace
    from harness.managed_local_driver import ManagedLocalDriver
    from pmharness.drivers.compaction import compaction_driver
    from pmharness.drivers.openai_compat import OpenAICompatDriver
    leases = []
    class Manager:
        @contextmanager
        def request_scope(self, spec):
            leases.append(spec)
            yield SimpleNamespace(model='qwen', base_url='http://localhost:4321/v1')
    driver = ManagedLocalDriver(manager=Manager(), spec='local:managed/qwen',
        name='local', model='qwen', base_url='http://localhost/v1', api_key_env='',
        extra_body={'nested': {'value': 1}})
    driver._build_chat_body = lambda *a: pytest.fail('stale body')
    with ExitStack() as resources:
        fork = compaction_driver(driver, '', resources)
        assert fork is not driver
        fork.extra_body['nested']['value'] = 2
        assert driver.extra_body['nested']['value'] == 1
        assert '_build_chat_body' not in fork.__dict__
        monkeypatch.setattr(OpenAICompatDriver, 'complete', lambda self, *a, **k: self.base_url)
        assert fork.complete('summary') == 'http://localhost:4321/v1'
        assert leases == ['local:managed/qwen']
        with pytest.raises(ValueError):
            compaction_driver(driver, 'other-model', resources)


@pytest.mark.parametrize('method', ['complete', 'chat', 'chat_stream'])
def test_managed_transport_timeout_recovery_keeps_lease(monkeypatch, method):
    import io
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace
    from harness.managed_local_driver import ManagedLocalDriver
    active = []
    class Manager:
        @contextmanager
        def request_scope(self, spec):
            active.append(spec)
            try:
                yield SimpleNamespace(model='qwen', base_url='http://localhost:4321/v1')
            finally:
                active.pop()
    driver = ManagedLocalDriver(manager=Manager(), spec='local:managed/qwen',
        name='local', model='qwen', base_url='http://localhost:1234/v1',
        api_key_env='', allow_keyless=True)
    calls = []
    def transport(req, **kw):
        assert active == ['local:managed/qwen']
        assert req.full_url == 'http://localhost:4321/v1/chat/completions'
        body = json.loads(req.data)
        calls.append(body)
        if len(calls) == 1:
            raise TimeoutError('read operation timed out')
        if body.get('stream'):
            return io.BytesIO(b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        return io.BytesIO(b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}')
    monkeypatch.setattr('urllib.request.urlopen', transport)
    kwargs = {'on_delta': lambda text: None} if method == 'chat_stream' else {}
    response = getattr(driver, method)('hello' if method == 'complete' else
                                      [{'role': 'user', 'content': 'hello'}], **kwargs)
    assert response.text == 'ok' and not response.error
    assert len(calls) == 2
    assert response.meta['recovery_attempted']
    assert not active
