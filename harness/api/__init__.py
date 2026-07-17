"""HTTP route bodies peeled from ``harness.server`` (zero-behavior-change).

Handlers in ``server.Handler`` stay as thin path delegates; auth, attach, and
``serve()`` remain in ``server.py``. Session CRUD lives in
``harness.api.sessions``; SSE ring + pump/write in ``harness.api.sse``; stream
route bodies in ``harness.api.streams``.
"""
