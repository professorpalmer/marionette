"""The environment probe cache keeps only entries still inside their TTL."""
import harness.swarm_run_facts as srf


def test_expired_probe_entries_are_evicted(monkeypatch, tmp_path):
    clock = [1000.0]
    monkeypatch.setattr(srf.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(srf, '_probe_cache', {})
    monkeypatch.setattr('harness.environment_fingerprint.compute_environment_fingerprint',
                        lambda cwd, strict=False: ({'cwd': cwd}, None))
    for i in range(100):
        clock[0] += srf._PROBE_TTL_SECONDS  # every earlier worktree has expired
        srf._environment_payload(str(tmp_path / f'wt{i}'))
    assert len(srf._probe_cache) == 1
