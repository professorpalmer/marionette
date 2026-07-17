"""HTTP route bodies peeled from ``harness.server`` (zero-behavior-change).

Handlers in ``server.Handler`` stay as thin path delegates; auth and
``serve()`` remain in ``server.py``. Session CRUD lives in
``harness.api.sessions``; attach/rebuild helpers in ``harness.api.attach``;
SSE ring + pump/write + chat/events replay in ``harness.api.sse``; stream
route bodies in ``harness.api.streams``; pilot hot-swap in
``harness.api.pilot``; terminal control + stream in ``harness.api.terminals``;
jobs/swarm JSON in ``harness.api.jobs``; wiki
connect/handoff/graph/status/ingest in ``harness.api.wiki``; MCP
list/add/remove/start/stop/call/catalog in ``harness.api.mcp``; provider key,
OAuth, pools, and model catalog/visibility in ``harness.api.providers``;
file tree/read/write/preview/upload in ``harness.api.files``; usage /
context-usage in ``harness.api.usage``; codegraph indexer runtime in
``harness.api.codegraph_index``; workspace recent/forget persistence in
``harness.api.workspace``; legacy shell assets in ``harness.api.static``.
"""
