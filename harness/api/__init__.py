"""HTTP route bodies peeled from ``harness.server`` (zero-behavior-change).

Handlers in ``server.Handler`` stay as thin path delegates; auth, SSE, attach,
and ``serve()`` remain in ``server.py``.
"""
