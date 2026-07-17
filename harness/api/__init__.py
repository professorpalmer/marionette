"""HTTP route bodies peeled from ``harness.server`` (zero-behavior-change).

Handlers in ``server.Handler`` stay as thin path delegates; auth, attach, and
``serve()`` remain in ``server.py``. Session CRUD lives in
``harness.api.sessions``; SSE ring + pump/write in ``harness.api.sse``; stream
route bodies in ``harness.api.streams``; jobs/swarm JSON in ``harness.api.jobs``;
wiki connect/handoff/graph/status/ingest in ``harness.api.wiki``; MCP
list/add/remove/start/stop/call/catalog in ``harness.api.mcp``; provider key,
OAuth, pools, and model catalog/visibility in ``harness.api.providers``;
file tree/read/write/preview/upload in ``harness.api.files``.
"""
