from types import SimpleNamespace

import pytest

from harness import swarm_model_pin as pins


def test_codex_is_not_oauth_alias():
    assert pins._parse_pin_provider_model('codex/gpt-6-astra') == ('codex', 'gpt-6-astra')
    assert pins._parse_pin_provider_model('openai-codex:gpt-6-astra') == ('openai-codex', 'gpt-6-astra')
    assert pins.normalize_swarm_model_pin_request('codex/gpt-5.6-luna-pro')[0] == 'codex/gpt-5.6-luna-pro'


def test_native_exact_pin(monkeypatch):
    monkeypatch.setattr('harness.swarm_worker_allowlist.native_codex_available', lambda: True)
    out = pins.resolve_swarm_model_pin('codex/gpt-6-astra', allowed_adapters=['codex'])
    assert out['adapter'] == 'codex'
    assert out['pin_fields']['model'] == 'gpt-6-astra'
    assert out['pin_fields']['auto_route'] is False
    assert 'provider' not in out['pin_fields']


@pytest.mark.parametrize('available,allowed', [(False, ['codex']), (True, ['agentic']), (True, [])])
def test_native_pin_fails_closed(monkeypatch, available, allowed):
    monkeypatch.setattr('harness.swarm_worker_allowlist.native_codex_available', lambda: available)
    out = pins.resolve_swarm_model_pin('codex/gpt-6-astra', allowed_adapters=allowed)
    assert out['demoted']
    assert out['auto_route'] is False
    assert not out['pin_fields']


def test_codex_platform_and_cli_required(monkeypatch):
    from harness.swarm_worker_allowlist import native_codex_available
    monkeypatch.setattr('shutil.which', lambda command: '/bin/codex')
    monkeypatch.setattr('puppetmaster.platform_lock.is_adapter_enabled', lambda adapter: False)
    assert not native_codex_available()
    monkeypatch.setattr('puppetmaster.platform_lock.is_adapter_enabled', lambda adapter: True)
    monkeypatch.setattr('shutil.which', lambda command: None)
    assert not native_codex_available()


def test_implement_action_retains_native_low_pin(monkeypatch):
    from harness.send_loop_dispatch import _strict_agentic_dispatch
    monkeypatch.setattr('harness.swarm_worker_allowlist.native_codex_available', lambda: True)
    action = SimpleNamespace(model='codex/gpt-6-astra', adapter='codex', reasoning_effort='low')
    pin, strict, error = _strict_agentic_dispatch(action)
    assert not error
    assert strict and pin.adapter == 'codex'
    assert pin.reasoning_effort == 'low'
    action.adapter = 'agentic'
    assert _strict_agentic_dispatch(action)[2]


@pytest.mark.parametrize('expects_diff,sandbox', [(True, 'workspace-write'), (False, 'read-only')])
def test_native_implement_worker_spec(monkeypatch, expects_diff, sandbox):
    from test_edit_engines import _install_agentic_mocks, _cfg
    from harness.edit_engines import run_implement
    specs = []
    _install_agentic_mocks(monkeypatch, capture_specs=specs)
    monkeypatch.setattr('harness.swarm_worker_allowlist.native_codex_available', lambda: True)
    monkeypatch.setattr('harness.edit_engines.agentic_available', lambda: False)
    monkeypatch.setattr('harness.edit_engines.finalize_worktree_patch', lambda path: ('', []))
    pin, error = pins.resolve_worker_model_pin('codex/gpt-6-astra', 'low')
    assert not error
    result = run_implement(_cfg('/unused'), 'Inspect code', agentic_pin=pin, expects_diff=expects_diff)
    assert specs[0]['adapter'] == 'codex'
    payload = specs[0]['payload']
    assert payload['model'] == 'gpt-6-astra'
    assert payload['sandbox'] == sandbox
    assert payload['approval_policy'] == 'never'
    assert payload['extra_args'] == ['-c', 'model_reasoning_effort=low']
    assert payload['auto_route'] is False
    assert 'provider' not in payload
    assert result.adapter == 'codex'


def test_native_implement_rechecks_availability_without_fallback(monkeypatch):
    from harness.edit_engines import run_implement
    from harness.config import HarnessConfig
    monkeypatch.setattr('harness.swarm_worker_allowlist.native_codex_available', lambda: True)
    pin, _ = pins.resolve_worker_model_pin('codex/gpt-6-astra', 'low')
    monkeypatch.setattr('harness.swarm_worker_allowlist.native_codex_available', lambda: False)
    def forbidden(*args, **kwargs):
        pytest.fail('unavailable pin must not dispatch a replacement')
    monkeypatch.setattr('harness.edit_engines.run_agentic_edit', forbidden)
    monkeypatch.setattr('harness.edit_engines.run_native_edit', forbidden)
    result = run_implement(HarnessConfig(), 'Change code', agentic_pin=pin)
    assert not result.ok
    assert result.adapter == 'codex'


def test_native_swarm_low_dispatch(monkeypatch, tmp_path):
    from test_bridge_agentic_swarm_routing import _CapturingWorkerSpec, _FakeOrchestrator
    from pmharness import bridge
    from pmharness.intent import DriverIntent
    monkeypatch.setattr('harness.swarm_worker_allowlist.native_codex_available', lambda: True)
    monkeypatch.setattr('puppetmaster.workers.WorkerSpec', _CapturingWorkerSpec)
    monkeypatch.setattr('puppetmaster.orchestrator.Orchestrator', _FakeOrchestrator)
    monkeypatch.setattr(bridge, '_warn_if_unindexed', lambda *args: None)
    _CapturingWorkerSpec._last_captured = []
    result = bridge.execute_intent(DriverIntent(
        action='run_swarm', goal='Inspect routing', roles=['explore'],
        model='codex/gpt-6-astra', reasoning_effort='low',
    ), cwd=str(tmp_path), state_dir=str(tmp_path / 'state'))
    assert result.adapter == 'codex'
    spec = _CapturingWorkerSpec._last_captured[0]
    assert spec.adapter == 'codex'
    assert spec.payload['model'] == 'gpt-6-astra'
    assert spec.payload['sandbox'] == 'read-only'
    assert spec.payload['approval_policy'] == 'never'
    assert spec.payload['extra_args'] == ['-c', 'model_reasoning_effort=low']
    assert spec.payload['allowed_adapters'] == ['codex']
    assert spec.payload['auto_route'] is False


def test_no_diff_terminal_diagnostics_survive_cleanup(monkeypatch):
    from test_edit_engines import _install_agentic_mocks, _cfg
    from harness.edit_engines import run_agentic_edit
    from harness.conversation_jobs import _enrich_worker_provenance
    artifact = SimpleNamespace(type='verification', payload={
        'stop_reason': 'submitted', 'turns': 9, 'total_tool_calls': 12,
        'last_tool_names': ['read_file', 'submit_report'],
        'mutating_tool_attempts': 1, 'mutating_tool_successes': 0,
        'progress_governor_fired': True,
        'tokens_in': 321, 'tokens_out': 45, 'failure': 'no_diff_produced',
    })
    pm_result = SimpleNamespace(job=SimpleNamespace(id='job1'), artifacts=[])
    _install_agentic_mocks(monkeypatch, orchestrator_result=pm_result)
    class Store:
        cleaned = False
        def list_tasks(self, job):
            assert not self.cleaned
            return []
        def list_artifacts(self, job):
            assert not self.cleaned
            return [artifact]
        def read_events(self, job):
            return []
    store = Store()
    monkeypatch.setattr('puppetmaster.store_factory.create_store', lambda *a, **kw: store)
    from harness import edit_engines
    cleanup = edit_engines._cleanup_store_dir
    def clean(path, errors):
        store.cleaned = True
        cleanup(path, errors)
    monkeypatch.setattr(edit_engines, '_cleanup_store_dir', clean)
    monkeypatch.setattr(edit_engines, 'finalize_worktree_patch', lambda path: ('', []))
    result = run_agentic_edit(_cfg('/unused'), 'Change code')
    assert store.cleaned and not result.ok
    assert (result.tokens_in, result.tokens_out) == (321, 45)
    assert result.terminal_diagnostics['stop_reason'] == 'submitted'
    assert result.terminal_diagnostics['turns'] == 9
    assert result.terminal_diagnostics['total_tool_calls'] == 12
    assert result.terminal_diagnostics['last_tool_names'] == ['read_file', 'submit_report']
    assert result.terminal_diagnostics['progress_governor_fired'] is True
    provenance = _enrich_worker_provenance({}, result)
    assert provenance['terminal_diagnostics'] == result.terminal_diagnostics


def test_native_swarm_unavailable_does_not_dispatch(monkeypatch, tmp_path):
    from pmharness import bridge
    from pmharness.intent import DriverIntent
    monkeypatch.setattr('harness.swarm_worker_allowlist.native_codex_available', lambda: False)
    monkeypatch.setattr(bridge, '_warn_if_unindexed', lambda *args: None)
    def forbidden(*args, **kwargs):
        pytest.fail('unavailable native pin dispatched')
    monkeypatch.setattr('puppetmaster.orchestrator.Orchestrator.run', forbidden)
    with pytest.raises(ValueError, match='Native Codex pin'):
        bridge.execute_intent(DriverIntent(
            action='run_swarm', goal='Inspect routing', roles=['explore'],
            model='codex/gpt-6-astra', reasoning_effort='low',
        ), cwd=str(tmp_path), state_dir=str(tmp_path / 'state'))


def test_native_does_not_remap_model_family(monkeypatch):
    monkeypatch.setattr('harness.swarm_worker_allowlist.native_codex_available', lambda: True)
    out = pins.resolve_swarm_model_pin('codex/gpt-5.6-astra')
    assert out['pin_fields']['model'] == 'gpt-5.6-astra'


def test_openai_codex_stays_direct_agentic(monkeypatch):
    row = {
        'id': 'agentic/openai-codex/gpt-6-astra', 'adapter': 'agentic',
        'adapter_model_name': 'gpt-6-astra',
        'payload_defaults': {'provider': 'openai-codex', 'model': 'gpt-6-astra'},
    }
    monkeypatch.setattr(pins, '_usable_registry_rows', lambda **kwargs: [row])
    monkeypatch.setattr('harness.auto_registry.ensure_keyed_provider_registry_health', lambda: None)
    pin, error = pins.resolve_worker_model_pin('openai-codex:gpt-6-astra', 'low')
    assert not error
    assert pin.adapter == 'agentic' and pin.provider == 'openai-codex'
    assert pin.payload_fields()['allowed_adapters'] == ['agentic']


def test_codex_effort_reaches_cli_command():
    from puppetmaster.adapters.codex import build_codex_exec_command
    payload = pins.codex_worker_payload({'model': 'gpt-6-astra', 'reasoning_effort': 'low'}, expects_diff=False)
    command = build_codex_exec_command(
        executable='codex', model=payload['model'], cwd='/tmp',
        sandbox=payload['sandbox'], approval_policy=payload['approval_policy'],
        extra_args=payload['extra_args'],
    )
    assert command[0] == 'codex'
    assert 'gpt-6-astra' in command
    assert 'model_reasoning_effort=low' in command
    assert 'read-only' in command
    assert 'approval_policy="never"' in command


def test_native_reported_substitution_fails(monkeypatch):
    from test_edit_engines import _install_agentic_mocks, _cfg
    from harness.edit_engines import run_implement, AGENTIC_ROUTE_FAILED
    artifact = SimpleNamespace(type='verification', payload={'model': 'gpt-5.6-luna'})
    result = SimpleNamespace(job=SimpleNamespace(id='job1'), artifacts=[artifact])
    _install_agentic_mocks(monkeypatch, orchestrator_result=result)
    monkeypatch.setattr('harness.swarm_worker_allowlist.native_codex_available', lambda: True)
    store = SimpleNamespace(
        list_tasks=lambda job: [], list_artifacts=lambda job: [artifact],
        read_events=lambda job: [],
    )
    monkeypatch.setattr('puppetmaster.store_factory.create_store', lambda *args, **kwargs: store)
    monkeypatch.setattr('harness.edit_engines.finalize_worktree_patch', lambda path: ('patch', ['a.py']))
    pin, _ = pins.resolve_worker_model_pin('codex/gpt-6-astra', 'low')
    result = run_implement(_cfg('/unused'), 'Change code', agentic_pin=pin)
    assert not result.ok
    assert result.error == AGENTIC_ROUTE_FAILED
    assert result.model == 'gpt-5.6-luna'
    assert result.requested_model == 'codex/gpt-6-astra'
