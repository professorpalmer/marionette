# Internal URI read surfaces — acceptance notes

Implemented OMP-inspired read/search resolution for Marionette/Puppetmaster
durable state. Agents use one filesystem-shaped interface (`read_file` /
`list_dir`) instead of bespoke store schemas.

## Schemes

| Scheme | Examples | Backing |
|--------|----------|---------|
| `job://` | `job://`, `job://{id}`, `job://{id}/artifacts` | Puppetmaster SwarmStore / DurableState |
| `artifact://` | `artifact://{job}/{art}`, `.../payload/claim` | Artifact JSON payloads |
| `agent://` | `agent://{job}/{run}/role` | Agent run records under `jobs/{id}/runs/` |
| `conflict://` | `conflict://`, `conflict://src/foo.py`, `conflict://resolve/src/foo.py?strategy=theirs` | Git unmerged index + working tree |

## Safety

- URI paths normalize to POSIX `/` segments; `\` is rejected (Windows edge cases).
- `..`, `.`, and null bytes are rejected before store/git access.
- `conflict://` repo-relative paths are re-validated with `path_within()`.
- Existing filesystem APIs unchanged; internal URIs are an additive pre-pass in
  `harness/tool_dispatch.py`.

## Verification (2026-07-06)

```
python -m pytest tests/test_internal_uri.py -q
# 17 passed in 2.75s
```

## Out of scope

- Write/mutate via internal URIs (read-only by design).
- Release/push (explicitly excluded for this worker run).
