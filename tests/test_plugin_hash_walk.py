"""The plugin digest walk skips .git without descending and matches the reference digest."""
import hashlib
import os

import harness.agent_plugins as ap


def _reference(root):
    h = hashlib.sha256()
    paths = sorted((p for p in root.rglob('*') if p.is_file() and not p.is_symlink()),
                   key=lambda p: p.relative_to(root).as_posix())
    for p in paths:
        rel = p.relative_to(root).as_posix()
        if ap.STAMP_FILENAME in rel.split('/') or '.git' in rel.split('/'):
            continue
        h.update(rel.encode() + b'\0' + p.read_bytes() + b'\0')
    return h.hexdigest()


def test_digest_matches_reference_and_never_enters_git(tmp_path, monkeypatch):
    for rel in ['plugin.json', 'a/b/c.txt', 'a-b/x', 'a.b', 'sub/.git', ap.STAMP_FILENAME]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rel * 3)
    for i in range(50):
        p = tmp_path / '.git' / 'objects' / f'{i:02d}' / 'o'
        p.parent.mkdir(parents=True)
        p.write_text('x')
    os.symlink(tmp_path / 'plugin.json', tmp_path / 'link.json')
    os.symlink(tmp_path / 'a', tmp_path / 'linkdir')
    expected = _reference(tmp_path)
    scanned = []
    real = os.scandir
    monkeypatch.setattr(ap.os, 'scandir', lambda d: scanned.append(d) or real(d))
    assert ap.compute_plugin_content_sha256(tmp_path) == expected
    mine = [str(d) for d in scanned if str(d).startswith(str(tmp_path))]
    assert mine and not any('.git' in d for d in mine)
