# Hash-anchored edit foundation — acceptance notes

## Summary

Added stdlib-only, cross-platform hash-anchored edit support behind `HARNESS_HASH_EDIT=1`.
Existing `edit_file` / `write_file` tools are unchanged when the flag is off.

## Behavior

- **read_file**: when enabled, wraps content in `[@anchor ...]` / `[@/anchor]` tags with
  stable 12-char SHA-256 content hashes for full files or line ranges.
- **hash_edit**: accepts `path` + `ops[]` with `replace`, `insert`, and `delete`.
  Validates all anchors against current file content before writing; stale anchors
  reject the entire request with no partial writes.
- **Line endings**: hashes use LF-normalized text; writes preserve the file's dominant
  CRLF/LF style.
- **Checkpoints**: `hash_edit` takes a restore-point snapshot before applying, matching
  `edit_file` / `write_file`.

## Files touched

- `harness/hash_edit.py` — core format, anchor tags, validate/apply, atomic write
- `harness/tool_dispatch.py` — read_file anchor annotation, `_do_hash_edit`
- `harness/pilot.py` — `hash_edit` tool schema + parsing (flag-gated)
- `harness/tool_discovery.py` — include `hash_edit` in core toolset when enabled
- `harness/conversation.py` — dispatch branch with checkpoint integration
- `tests/test_hash_edit.py` — focused tests

## Verification

```bash
HARNESS_HASH_EDIT=1 python -m pytest tests/test_hash_edit.py -q
```

Expected: all tests pass, covering stale anchor rejection, CRLF normalization,
multi-hunk apply, no partial writes, read_file tags, schema/parsing, and checkpoint.

## Flag

- Enable: `HARNESS_HASH_EDIT=1` (or `true` / `yes`)
- Default: disabled — current tools remain compatible
