"""HTTP route bodies peeled from ``harness.server`` (zero-behavior-change).

Handlers in ``server.Handler`` stay as thin path delegates; auth, SSE stream
methods, attach, and ``serve()`` remain in ``server.py``. Ring buffer
primitives live in ``harness.api.sse``.
"""
