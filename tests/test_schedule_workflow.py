"""Disposable schedule workflow regressions; no providers or live jobs."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from harness.api import schedules as api
from harness.schedule_core import Schedule, CronExpr, due_fire_at, next_real_fire_after, validate_timezone
from harness.schedule_store import ScheduleStore
from harness.scheduler import resolve_schedule_repo


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv('HARNESS_STATE_DIR', str(tmp_path))
    return tmp_path


def spec(state, **kwargs):
    return dict(name='Daily', objective='Inspect', cron='30 9 * * *', repo=str(state), **kwargs)


def test_iana_gap_fold_and_month_jump():
    zone = ZoneInfo('America/New_York')
    assert validate_timezone(' America/New_York ') == 'America/New_York'
    cron = CronExpr.parse('30 2 * * *')
    assert next_real_fire_after(cron, datetime(2026, 3, 8, 1, 59, tzinfo=zone)) == datetime(2026, 3, 9, 2, 30, tzinfo=zone)
    s = Schedule(id='x', name='x', objective='x', cron='30 1 * * *', timezone=zone.key)
    first = due_fire_at(s, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
    assert first.timestamp() == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc).timestamp()
    s.last_fire_at = first.timestamp()
    assert due_fire_at(s, datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)) is None
    nxt = next_real_fire_after(CronExpr.parse('0 9 1 1 *'), datetime(2026, 9, 1, tzinfo=zone))
    assert nxt == datetime(2027, 1, 1, 9, tzinfo=zone)


def test_create_edit_timezone_revision_and_pause(state):
    code, added = api.post_schedules_add(spec(state, timezone='Asia/Kolkata'))
    assert code == 200
    assert added['timezone'] == 'Asia/Kolkata'
    assert added['next_fires'][0].endswith('+05:30')
    code, edited = api.post_schedules_update(dict(id=added['id'], revision=added['revision'], timezone='UTC'))
    assert code == 200
    assert edited['revision'] > added['revision']
    code, error = api.post_schedules_update(dict(id=added['id'], revision=added['revision'], name='Stale'))
    assert code == 409
    assert 'reload' in error['error'].lower()
    code, paused = api.post_schedules_disable(dict(id=added['id'], revision=edited['revision']))
    assert code == 200 and not paused['enabled']
    assert paused['next_fires'] == []


@pytest.mark.parametrize('patch', [dict(max_seconds='oops'), dict(max_tokens=-1), dict(max_swarms=1.5), dict(enabled='false'), dict(timezone='Fake/Zone'), dict(cron='0 0 30 2 *'), dict(objective=' '), dict(repo='relative')])
def test_invalid_create_is_actionable_and_atomic(state, patch):
    code, body = api.post_schedules_add({**spec(state), **patch})
    assert code == 400
    assert body['error']
    assert api.get_schedules()[1]['schedules'] == []


@pytest.mark.parametrize('repo', [
    pytest.param('', id='unscoped'),
    pytest.param('relative', id='relative_legacy_scope'),
])
def test_unscoped_schedule_does_not_follow_environment(monkeypatch, repo):
    monkeypatch.setenv('HARNESS_REPO', '/some/other/project')
    assert resolve_schedule_repo(Schedule(id='x', name='x', objective='x', cron='* * * * *', repo=repo)) == ''


def test_run_now_overlap_is_conflict_without_execution(state, monkeypatch):
    code, added = api.post_schedules_add(spec(state))
    store = ScheduleStore(str(state / 'schedules.sqlite'))
    store.try_claim(added['id'], 1, 'daemon')
    store.close()
    import harness.scheduler as scheduler
    monkeypatch.setattr(scheduler, '_default_session_factory', lambda _: pytest.fail('overlap executed'))
    code, result = api.post_schedules_run_now(dict(id=added['id']))
    assert code == 409
    assert result['ok'] is False
    assert result['run']['status'] == 'blocked'


def test_fall_back_preview_never_returns_an_elapsed_first_occurrence():
    zone = ZoneInfo('America/New_York')
    second_hour = datetime(2026, 11, 1, 1, 10, tzinfo=zone, fold=1)
    nxt = next_real_fire_after(CronExpr.parse('30 1 * * *'), second_hour)
    assert nxt == datetime(2026, 11, 2, 1, 30, tzinfo=zone)
    s = Schedule(id='x', name='x', objective='x', cron='30 1 * * *', timezone=zone.key,
                 created_at=second_hour.timestamp())
    assert due_fire_at(s, second_hour.replace(minute=30)) is None


def test_fall_back_missed_first_occurrence_catches_up_during_second_hour():
    zone = ZoneInfo('America/New_York')
    s = Schedule(id='x', name='x', objective='x', cron='59 1 * * *', timezone=zone.key,
                 last_fire_at=datetime(2026, 10, 31, 1, 59, tzinfo=zone).timestamp())
    fire = due_fire_at(s, datetime(2026, 11, 1, 1, 10, tzinfo=zone, fold=1))
    assert fire == datetime(2026, 11, 1, 1, 59, tzinfo=zone)


def test_claim_cannot_start_a_paused_daemon_snapshot(state):
    store = ScheduleStore(str(state / 'schedules.sqlite'))
    s = store.add(Schedule(id='', name='x', objective='x', cron='* * * * *'))
    store.set_enabled(s.id, False)
    assert store.try_claim(s.id, 1, 'daemon') is None
    assert store.list_runs(s.id) == []
    store.close()


def test_stale_writer_cannot_pause_or_execute_updated_row(state):
    path = str(state / 'schedules.sqlite')
    first, second = ScheduleStore(path), ScheduleStore(path)
    try:
        original = first.add(Schedule(id='', name='x', objective='x', cron='* * * * *'))
        updated = second.update_fields(original.id, expected_revision=original.revision, objective='New objective')
        assert first.try_claim(original.id, 1, 'old-daemon', expected_revision=original.revision) is None
        from harness.schedule_store import ScheduleConflict
        with pytest.raises(ScheduleConflict, match='reload'):
            first.set_enabled(original.id, False, expected_revision=original.revision)
        assert second.get(original.id).enabled
        assert not second.get(original.id).cancel_requested
        assert second.get(original.id).objective == 'New objective'
        assert updated.revision == original.revision + 1
    finally:
        first.close()
        second.close()


def test_invalid_update_leaves_existing_state_and_history_intact(state):
    _, added = api.post_schedules_add(spec(state))
    store = ScheduleStore(str(state / 'schedules.sqlite'))
    store.record_run(added['id'], 1, 2, 'ok')
    store.close()
    code, error = api.post_schedules_update(dict(id=added['id'], objective=' ', timezone='UTC'))
    assert code == 400
    assert 'objective' in error['error']
    current = api.get_schedules()[1]['schedules'][0]
    assert current['objective'] == 'Inspect'
    assert current['timezone'] == ''
    assert len(api.get_schedules_history(added['id'])[1]['runs']) == 1


def test_half_hour_dst_gap_and_all_policy_are_real_instants():
    zone = ZoneInfo('Australia/Lord_Howe')
    before = datetime(2026, 10, 4, 1, 59, tzinfo=zone)
    assert next_real_fire_after(CronExpr.parse('15 2 * * *'), before) == datetime(2026, 10, 5, 2, 15, tzinfo=zone)
    from harness.schedule_core import due_fire_slots
    zone = ZoneInfo('America/New_York')
    s = Schedule(id='x', name='x', objective='x', cron='30 1 * * *', timezone=zone.key,
                 missed_policy='all', last_fire_at=datetime(2026, 10, 30, 1, 30, tzinfo=zone).timestamp())
    fires = due_fire_slots(s, datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=1))
    assert [f.day for f in fires] == [31, 1]
    assert all(f.fold == 0 for f in fires)


def test_create_retry_is_idempotent_and_key_reuse_cannot_overwrite(state):
    import uuid
    body = spec(state, request_id=str(uuid.uuid4()))
    first = api.post_schedules_add(body)
    retry = api.post_schedules_add(body)
    assert first[0] == retry[0] == 200
    assert first[1]['id'] == retry[1]['id']
    assert len(api.get_schedules()[1]['schedules']) == 1
    code, error = api.post_schedules_add({**body, 'objective': 'Different objective'})
    assert code == 409
    assert 'already' in error['error'].lower()


def test_manual_run_rejects_stale_spec_without_executing(state, monkeypatch):
    _, added = api.post_schedules_add(spec(state))
    api.post_schedules_update(dict(id=added['id'], objective='Changed'))
    import harness.scheduler as scheduler
    monkeypatch.setattr(scheduler, '_default_session_factory', lambda _: pytest.fail('stale manual run executed'))
    code, result = api.post_schedules_run_now(dict(id=added['id'], revision=added['revision']))
    assert code == 409
    assert result['run']['status'] == 'blocked'
    assert not api.get_schedules_history(added['id'])[1]['runs']


def test_impossible_cron_is_rejected_at_store_boundary(state):
    store = ScheduleStore(str(state / 'schedules.sqlite'))
    try:
        with pytest.raises(ValueError, match='calendar date'):
            store.add(Schedule(id='', name='x', objective='x', cron='0 0 30 2 *'))
        assert store.list() == []
        # Restricted day-of-week OR day-of-month is still possible.
        assert CronExpr.parse('0 0 30 2 1').next_after(datetime(2026, 2, 1)).day == 2
    finally:
        store.close()


def test_concurrent_create_retries_insert_one_row(state):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import uuid
    # Initialize the disposable database before racing independent HTTP writers.
    api.get_schedules()
    barrier = Barrier(2)
    body = spec(state, request_id=str(uuid.uuid4()))
    def create():
        barrier.wait()
        return api.post_schedules_add(body)
    with ThreadPoolExecutor(max_workers=2) as workers:
        one, two = workers.submit(create), workers.submit(create)
        first, second = one.result(), two.result()
    assert first[0] == second[0] == 200
    assert first[1]['id'] == second[1]['id']
    assert len(api.get_schedules()[1]['schedules']) == 1
