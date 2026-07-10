# Session decisions — 2026-07-09 (v0.9.27)

## Anthropic prompt-cache 1h/5m split

- Stable breakpoints (system + last tool): `ttl=1h` by default.
- Moving history breakpoints: no ttl (Anthropic 5m write).
- Override: `HARNESS_ANTHROPIC_CACHE_TTL=5m|off`.
- Paired with Puppetmaster v1.16.0 (`PUPPETMASTER_ANTHROPIC_CACHE_TTL`).

## CI

- Loosened `test_parallel_reads_timing_sanity` non-Windows bound 1.2 -> 1.35 after macOS flake at 1.2003s (unrelated to cache TTL).

