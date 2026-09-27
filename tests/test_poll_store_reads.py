from collections import Counter

from harness.cli_job_merge import merge_running_cli_jobs_all_projects


def test_cross_project_poll_reads_each_store_once(monkeypatch):
    counts = Counter()

    class Store:
        busy_timeout_ms = 0

        def list_jobs(self):
            counts['jobs'] += 1
            return []

        def list_tasks_for_jobs(self, ids):
            return []

        def count_artifacts_for_jobs(self, ids):
            return {}

    def create(*args):
        counts['opens'] += 1
        return Store()

    monkeypatch.setattr('harness.state.create_store', create)
    monkeypatch.setattr('harness.cli_job_merge._foreign_state_dir_candidates',
                        lambda primary: ['/tmp/poll-a', '/tmp/poll-b', '/tmp/poll-c'])
    for _ in range(5):
        assert merge_running_cli_jobs_all_projects(seen_ids=set(), tasks_by_job={}) == []
    assert counts == {'opens': 15, 'jobs': 15}


def test_cross_project_running_scan_preserves_ownership_without_history(tmp_path, monkeypatch):
    import json
    import sqlite3
    from harness.state import DurableState
    from harness.cli_job_merge import merge_scoped_cli_jobs
    from harness.job_scoping import job_label_for_session

    durable = DurableState(str(tmp_path))
    durable.store.init()
    label = job_label_for_session('session-a')
    with sqlite3.connect(durable.store.db_path) as connection:
        for jid, status, stamp in (
            ('live', ' RUNNING ', label), ('old', 'complete', label),
            ('foreign', 'running', None),
        ):
            connection.execute('INSERT INTO jobs (id, data) VALUES (?, ?)', (
                jid, json.dumps({'id': jid, 'goal': 'fixture', 'status': status,
                                 'label': stamp}),
            ))
        # Model a corrupt legacy row predating the current projection triggers.
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='jobs'"
        ).fetchall()
        for (name,) in triggers:
            connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        connection.execute('INSERT INTO jobs (id, data) VALUES (?, ?)', ('corrupt', '{'))

    def forbidden(*args, **kwargs):
        raise AssertionError('running discovery must not hydrate historical jobs or artifacts')

    monkeypatch.setattr(durable, 'list_jobs', forbidden)
    monkeypatch.setattr(durable.store, 'list_artifacts_for_jobs', forbidden)
    monkeypatch.setattr('harness.cli_job_merge._foreign_state_dir_candidates',
                        lambda primary: [str(tmp_path)])
    monkeypatch.setattr('harness.cli_job_merge.open_cli_durable_at',
                        lambda *args, **kwargs: durable)
    monkeypatch.setattr('harness.cli_job_merge.open_cli_durable_state', lambda _: None)
    monkeypatch.setattr('harness.cli_job_merge.resolve_cli_state_dir', lambda _: None)
    monkeypatch.setenv('HARNESS_CLI_CROSS_PROJECT', '1')

    rows, _, _ = merge_scoped_cli_jobs([], harness_store=None,
                                     active_session_id='session-a',
                                     repo_root='', workspace_root='')

    assert [row['id'] for row in rows] == ['live']
    assert rows[0]['label'] == label
    assert rows[0]['session_id'] == 'session-a'
