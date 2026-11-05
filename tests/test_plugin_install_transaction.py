"""Filesystem failure boundaries for portable plugin replacement."""
import json
from pathlib import Path

import pytest

from harness import plugin_registry as registry
from harness.agent_plugins import AgentPluginError
from tests.test_agent_plugins import _valid_package, plugins_home


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob('*') if p.is_file()}


def prepared(tmp_path):
    source = _valid_package(tmp_path / 'source')
    manifest = json.loads((source / 'plugin.json').read_text())
    manifest['extensions'] = {'marionette': {'requested_capabilities': ['fs']}}
    (source / 'plugin.json').write_text(json.dumps(manifest))
    old = registry.install_from_source(str(source))
    registry.consent_plugin_capabilities(old.id, ['fs'])
    registry.enable_plugin(old.id)
    manifest['version'] = '2.0.0'
    manifest['extensions']['marionette']['requested_capabilities'] = ['fs', 'shell']
    (source / 'plugin.json').write_text(json.dumps(manifest))
    return source, old


@pytest.mark.parametrize('failure', ['copy', 'manifest', 'stamp', 'enabled', 'consent', 'load'])
def test_failed_replacement_restores_package_and_state(plugins_home, tmp_path, monkeypatch, failure):
    source, old = prepared(tmp_path)
    before = snapshot(registry.plugins_dir())
    original_copy = registry.shutil.copytree
    original_stamp = registry.write_integrity_stamp
    original_load = registry.load_agent_plugin

    def copy(src, dst, *args, **kwargs):
        if Path(src) == source and failure == 'copy':
            Path(dst).mkdir()
            (Path(dst) / 'plugin.json').write_bytes((source / 'plugin.json').read_bytes())
            raise OSError('interrupted copy')
        result = original_copy(src, dst, *args, **kwargs)
        if Path(src) == source:
            if failure == 'manifest':
                (Path(dst) / 'plugin.json').write_text('{}')
        return result

    def stamp(path):
        result = original_stamp(path)
        if failure == 'stamp':
            registry.integrity_stamp_path(path).write_text('{}')
        return result

    def fail_write(*args, **kwargs):
        raise OSError('state write failed')

    def load(path, data):
        if failure == 'load' and Path(path) == Path(old.path):
            raise AgentPluginError('final load failed')
        return original_load(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(registry.shutil, 'copytree', copy)
        patch.setattr(registry, 'write_integrity_stamp', stamp)
        patch.setattr(registry, 'load_agent_plugin', load)
        if failure == 'enabled':
            patch.setattr(registry, '_write_enabled', fail_write)
        if failure == 'consent':
            patch.setattr(registry, '_write_capability_records', fail_write)
        with pytest.raises((OSError, AgentPluginError)):
            registry.install_from_source(str(source), force=True)
    assert snapshot(registry.plugins_dir()) == before
    assert [p.name for p in registry.plugins_dir().iterdir() if p.is_dir()] == [old.id]
    assert registry.verify_integrity_stamp(Path(old.path)) == old.sha256
    assert len(registry.list_enabled_plugin_skills()) == 1
    assert registry.discover_plugins()[0].version == '1.2.3'


def test_replacement_requires_explicit_enable_and_consent(plugins_home, tmp_path):
    source, old = prepared(tmp_path)
    new = registry.install_from_source(str(source), force=True)
    assert new.version == '2.0.0'
    assert new.path == old.path
    assert not new.enabled
    assert new.consented_capabilities == []
    assert registry.list_enabled_plugin_skills() == []
    assert len(registry.discover_plugins()) == 1
    with pytest.raises(AgentPluginError, match='consent'):
        registry.enable_plugin(new.id)
    registry.consent_plugin_capabilities(new.id, ['fs', 'shell'])
    registry.enable_plugin(new.id)
    registry.enable_plugin(new.id)
    assert len(registry.list_enabled_plugin_skills()) == 1
    servers = registry.list_enabled_mcp_servers()
    assert len(servers) == 1
    assert next(iter(servers.values()))['args'] == [str(Path(new.path) / 'server.py')]
    assert [p.name for p in registry.plugins_dir().iterdir() if p.is_dir()] == [old.id]


def test_enable_does_not_write_consent(plugins_home, tmp_path, monkeypatch):
    source = _valid_package(tmp_path / 'source')
    installed = registry.install_from_source(str(source))
    def fail(*args, **kwargs):
        raise AssertionError('enable must not publish consent records')
    monkeypatch.setattr(registry, '_write_capability_records', fail)
    assert registry.enable_plugin(installed.id).enabled


def test_discovery_waits_for_replacement(plugins_home, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    source, old = prepared(tmp_path)
    entered, release, reader_started = Event(), Event(), Event()
    original = registry._write_enabled

    def paused(enabled):
        entered.set()
        assert release.wait(5)
        original(enabled)

    def discover():
        reader_started.set()
        return registry.discover_plugins()

    monkeypatch.setattr(registry, '_write_enabled', paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(registry.install_from_source, str(source), force=True)
        try:
            assert entered.wait(5)
            reader = pool.submit(discover)
            assert reader_started.wait(5)
            assert not reader.done()
        finally:
            release.set()
        assert writer.result().version == '2.0.0'
        records = reader.result()
    assert [(r.id, r.version, r.enabled) for r in records] == [(old.id, '2.0.0', False)]


@pytest.mark.parametrize('existing', [False, True])
def test_failed_state_publication_restores_even_after_write(plugins_home, tmp_path, monkeypatch, existing):
    if existing:
        source, _ = prepared(tmp_path)
    else:
        source = _valid_package(tmp_path / 'source')
    root = registry.plugins_dir()
    before = snapshot(root)
    original = registry._write_capability_records
    def fail(records):
        original(records)
        raise OSError('failure after state publication')
    monkeypatch.setattr(registry, '_write_capability_records', fail)
    with pytest.raises(OSError):
        registry.install_from_source(str(source), force=True)
    assert snapshot(root) == before
    assert not list(root.glob('.install-*'))


def test_failed_directory_publication_restores_old(plugins_home, tmp_path, monkeypatch):
    source, old = prepared(tmp_path)
    before = snapshot(registry.plugins_dir())
    replace = registry.os.replace
    def fail(src, dst):
        if Path(src).name == 'new' and Path(dst) == Path(old.path):
            raise OSError('directory publication failed')
        return replace(src, dst)
    monkeypatch.setattr(registry.os, 'replace', fail)
    with pytest.raises(OSError, match='publication'):
        registry.install_from_source(str(source), force=True)
    assert snapshot(registry.plugins_dir()) == before
    assert not list(registry.plugins_dir().glob('.install-*'))


@pytest.mark.parametrize('readonly', [False, True])
def test_prepare_flushes_writable_staged_files_only(plugins_home, tmp_path, monkeypatch, readonly):
    import os
    import stat
    from harness import plugin_recovery
    source, old = prepared(tmp_path)
    payload = source / 'plugin.json'
    if readonly:
        payload.chmod(stat.S_IREAD)
    before_source = snapshot(source)
    source_mode = stat.S_IMODE(payload.stat().st_mode)
    real_open, real_fsync = Path.open, os.fsync
    descriptors = {}
    flushed = []

    def tracked(path, mode='r', *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if 'b' in mode:
            descriptors[handle.fileno()] = (path, handle)
        return handle

    def windows_fsync(fd):
        if fd in descriptors:
            path, handle = descriptors.pop(fd)
            if handle.closed:
                return real_fsync(fd)
            if not handle.writable():
                raise OSError(9, 'Windows fsync requires writable descriptor')
            assert '.install-' in str(path)
            assert path != payload and Path(old.path) not in path.parents
            flushed.append(path)
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', tracked)
        patch.setattr(plugin_recovery.os, 'fsync', windows_fsync)
        installed = registry.install_from_source(str(source), force=True)
    assert flushed
    assert snapshot(source) == before_source
    assert stat.S_IMODE(payload.stat().st_mode) == source_mode
    installed_payload = Path(installed.path) / 'plugin.json'
    assert installed_payload.read_bytes() == payload.read_bytes()
    assert stat.S_IMODE(installed_payload.stat().st_mode) == source_mode


@pytest.mark.parametrize('failure', ['open', 'fsync'])
def test_staged_flush_failure_restores_readonly_mode(tmp_path, monkeypatch, failure):
    import os
    import stat
    from harness import plugin_recovery
    path = tmp_path / 'staged'
    path.write_bytes(b'preserve staged bytes')
    path.chmod(0o555)
    mode = stat.S_IMODE(path.stat().st_mode)
    real_open = Path.open

    def fail(*args, **kwargs):
        raise OSError('staged flush failure')

    def fail_open(candidate, mode='r', *args, **kwargs):
        if candidate == path and mode == 'r+b':
            fail()
        return real_open(candidate, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        if failure == 'open':
            patch.setattr(Path, 'open', fail_open)
        else:
            patch.setattr(os, 'fsync', fail)
        with pytest.raises(OSError, match='staged flush failure'):
            plugin_recovery._sync_staged_file(path)
    assert path.read_bytes() == b'preserve staged bytes'
    assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize('kind', ['directory', 'symlink', 'hardlink'])
def test_staged_flush_rejects_non_private_regular_files(tmp_path, kind):
    import os
    from harness import plugin_recovery
    original = tmp_path / 'original'
    original.write_bytes(b'original')
    path = tmp_path / 'staged'
    if kind == 'directory':
        path.mkdir()
    elif kind == 'symlink':
        path.symlink_to(original)
    else:
        os.link(original, path)
    before = original.stat().st_mode
    with pytest.raises(AgentPluginError, match='non-private regular file'):
        plugin_recovery._sync_staged_file(path)
    assert original.read_bytes() == b'original'
    assert original.stat().st_mode == before


def test_staged_fsync_failure_keeps_old_plugin(plugins_home, tmp_path, monkeypatch):
    import os
    import stat
    source, old = prepared(tmp_path)
    before = snapshot(registry.plugins_dir())
    original_source = snapshot(source)
    real_fsync = os.fsync

    def fail(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('staged fsync failed')
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', fail)
        with pytest.raises(OSError, match='staged fsync failed'):
            registry.install_from_source(str(source), force=True)
    assert snapshot(registry.plugins_dir()) == before
    assert snapshot(source) == original_source
    assert registry.verify_integrity_stamp(Path(old.path)) == old.sha256
    assert registry.discover_plugins()[0].enabled


@pytest.fixture
def windows_readonly_delete(monkeypatch):
    """Emulate only Windows read-only unlink/rmdir denial, not Windows itself."""
    import os
    import stat
    for name in ('unlink', 'rmdir'):
        original = getattr(os, name)

        def checked(path, *args, _original=original, **kwargs):
            info = os.stat(path, dir_fd=kwargs.get('dir_fd'), follow_symlinks=False)
            if not info.st_mode & stat.S_IWUSR:
                raise PermissionError(13, 'emulated Windows read-only deletion', str(path))
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(os, name, checked)


@pytest.mark.parametrize('emulated', [False, True])
def test_repeat_readonly_replacement(plugins_home, tmp_path, request, emulated):
    import stat
    source = _valid_package(tmp_path / 'source')
    payload = source / 'plugin.json'
    payload.chmod(stat.S_IREAD)
    before = snapshot(source)
    mode = payload.stat().st_mode
    if emulated:
        request.getfixturevalue('windows_readonly_delete')
    for _ in range(3):
        installed = registry.install_from_source(str(source), force=True)
        target = Path(installed.path) / 'plugin.json'
        assert snapshot(source) == before
        assert payload.stat().st_mode == mode
        assert target.read_bytes() == payload.read_bytes()
        assert target.stat().st_mode == mode
        assert registry.verify_integrity_stamp(Path(installed.path)) == installed.sha256
        assert not list(registry.plugins_dir().glob('.retired-*'))


def interrupted_retirement(source, monkeypatch):
    from harness import plugin_recovery
    def fail(path):
        raise OSError('cleanup unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(plugin_recovery, '_remove_retired', fail)
        with pytest.raises(OSError, match='cleanup unavailable'):
            registry.install_from_source(str(source), force=True)
    retired, = registry.plugins_dir().glob('.retired-*')
    return retired


def test_retired_readonly_directories_retry_on_startup(plugins_home, tmp_path, monkeypatch,
                                                     windows_readonly_delete):
    import stat
    source, old = prepared(tmp_path)
    retired = interrupted_retirement(source, monkeypatch)
    before = snapshot(Path(old.path))
    for path in [retired, retired / 'old']:
        path.chmod(stat.S_IREAD | stat.S_IEXEC)
    for path in (retired / 'old').rglob('*'):
        path.chmod(stat.S_IREAD | (stat.S_IEXEC if path.is_dir() else 0))
    records = registry.discover_plugins()
    assert records[0].version == '2.0.0'
    assert snapshot(Path(old.path)) == before
    assert not retired.exists()
    assert registry.discover_plugins()[0].stamp_ok


def test_partial_retirement_failure_is_visible_and_retryable(plugins_home, tmp_path, monkeypatch,
                                                           windows_readonly_delete):
    from harness import plugin_recovery
    source, old = prepared(tmp_path)
    unlink = Path.unlink
    removed = []
    def fail(path, *args, **kwargs):
        if any(p.name.startswith('.retired-') for p in path.parents) and path.name != plugin_recovery.MARKER:
            if removed:
                raise OSError('unrelated deletion failure')
            removed.append(str(path))
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', fail)
        with pytest.raises(AgentPluginError, match='unrelated deletion failure'):
            registry.install_from_source(str(source), force=True)
    retired, = registry.plugins_dir().glob('.retired-*')
    assert removed
    assert (retired / plugin_recovery.MARKER).is_file()
    committed = snapshot(Path(old.path))
    assert registry.discover_plugins()[0].version == '2.0.0'
    assert snapshot(Path(old.path)) == committed
    assert not retired.exists()


@pytest.mark.parametrize('kind', ['unexpected', 'nested', 'symlink', 'hardlink', 'reparse'])
def test_retired_unexpected_entries_fail_closed(plugins_home, tmp_path, monkeypatch, kind):
    import os
    from types import SimpleNamespace
    from harness import plugin_recovery
    source, old = prepared(tmp_path)
    retired = interrupted_retirement(source, monkeypatch)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'keep external bytes and attributes')
    outside.chmod(0o444)
    mode = outside.stat().st_mode
    path = retired / 'old' / 'foreign'
    if kind == 'unexpected':
        path = retired / 'foreign'
        path.write_bytes(b'keep unexpected')
    elif kind == 'nested':
        path.write_bytes(b'keep unexpected')
    elif kind == 'symlink':
        path.symlink_to(outside)
    elif kind == 'hardlink':
        os.link(outside, path)
    else:
        path = retired / 'old'
    before = snapshot(retired)
    original_lstat = Path.lstat
    def reparse(candidate):
        result = original_lstat(candidate)
        if candidate == path:
            return SimpleNamespace(st_mode=result.st_mode, st_nlink=result.st_nlink,
                                   st_file_attributes=0x400)
        return result
    with monkeypatch.context() as patch:
        if kind == 'reparse':
            patch.setattr(Path, 'lstat', reparse)
        for _ in range(2):
            with pytest.raises(AgentPluginError, match='retirement blocked'):
                registry.discover_plugins()
    assert snapshot(retired) == before
    assert outside.read_bytes() == b'keep external bytes and attributes'
    assert outside.stat().st_mode == mode
    assert registry.verify_integrity_stamp(Path(old.path))


def test_readonly_rollback_preserves_active_modes(plugins_home, tmp_path, monkeypatch,
                                                windows_readonly_delete):
    source, old = prepared(tmp_path)
    payload = Path(old.path) / 'plugin.json'
    payload.chmod(0o444)
    (source / 'plugin.json').chmod(0o444)
    before, source_before = snapshot(Path(old.path)), snapshot(source)
    mode = payload.stat().st_mode
    def fail(*args):
        raise OSError('state publication failure')
    with monkeypatch.context() as patch:
        patch.setattr(registry, '_write_enabled', fail)
        with pytest.raises(OSError, match='state publication failure'):
            registry.install_from_source(str(source), force=True)
    assert snapshot(Path(old.path)) == before
    assert snapshot(source) == source_before
    assert payload.stat().st_mode == mode
    assert registry.discover_plugins()[0].enabled
    assert not list(registry.plugins_dir().glob('.retired-*'))


@pytest.mark.parametrize('damaged', [False, True])
def test_legacy_retired_requires_intact_evidence(plugins_home, tmp_path, monkeypatch, damaged,
                                               windows_readonly_delete):
    source, old = prepared(tmp_path)
    retired = interrupted_retirement(source, monkeypatch)
    marker_path = retired / 'transaction.json'
    marker = json.loads(marker_path.read_text())
    del marker['retirement']
    marker_path.write_text(json.dumps(marker))
    payload = retired / 'old' / 'plugin.json'
    if damaged:
        payload.unlink()
    else:
        payload.chmod(0o444)
        marker_path.chmod(0o444)
        retired.chmod(0o555)
    before = snapshot(retired)
    if damaged:
        with pytest.raises(AgentPluginError, match='retirement blocked'):
            registry.discover_plugins()
        assert snapshot(retired) == before
    else:
        assert registry.discover_plugins()[0].version == '2.0.0'
        assert not retired.exists()
    assert registry.verify_integrity_stamp(Path(old.path))


def test_cold_start_retries_retirement(plugins_home, tmp_path, monkeypatch):
    from tests.test_plugin_crash_recovery import COLD, run
    source, old = prepared(tmp_path)
    retired = interrupted_retirement(source, monkeypatch)
    (retired / 'old' / 'plugin.json').chmod(0o444)
    result = run(COLD, 'discover', source, old.id)
    assert result.returncode == 0, result.stderr
    record, = json.loads(result.stdout)
    assert record['version'] == '2.0.0' and record['stamp_ok']
    assert not retired.exists()


def test_retirement_preflight_preserves_external_hardlink(plugins_home, tmp_path, monkeypatch):
    import os
    from harness import plugin_recovery
    source, old = prepared(tmp_path)
    external = tmp_path / 'external'
    external.write_bytes(b'keep')
    external.chmod(0o444)
    mode = external.stat().st_mode
    commit = plugin_recovery.commit
    def linked(transaction):
        commit(transaction)
        os.link(external, transaction / 'old' / 'external')
    monkeypatch.setattr(plugin_recovery, 'commit', linked)
    with pytest.raises(AgentPluginError, match='non-private plain entry'):
        registry.install_from_source(str(source), force=True)
    assert not list(registry.plugins_dir().glob('.retired-*'))
    transaction, = registry.plugins_dir().glob('.install-*')
    assert (transaction / 'old' / 'external').read_bytes() == b'keep'
    assert external.stat().st_mode == mode
    assert registry.verify_integrity_stamp(Path(old.path))


def test_retired_empty_directory_retry(plugins_home, tmp_path, monkeypatch):
    from harness import plugin_recovery
    source, old = prepared(tmp_path)
    rmdir = Path.rmdir
    def fail(path):
        if path.name.startswith('.retired-'):
            raise OSError('final rmdir failure')
        return rmdir(path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'rmdir', fail)
        with pytest.raises(AgentPluginError, match='final rmdir failure'):
            registry.install_from_source(str(source), force=True)
    retired, = registry.plugins_dir().glob('.retired-*')
    assert list(retired.iterdir()) == []
    plugin_recovery.recover(registry.plugins_dir(), registry.verify_integrity_stamp)
    assert not retired.exists()
    assert registry.discover_plugins()[0].version == '2.0.0'
