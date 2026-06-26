from __future__ import annotations
import json
import pytest
from harness.pilot import build_tools_schema, parse_tool_calls, parse_inline_tool_calls, PilotAction, PilotError
from harness.wiki import WikiClient

def test_superpowers_schema():
    schemas = build_tools_schema()
    cg_schema = [s for s in schemas if s["function"]["name"] == "search_codegraph"][0]
    wiki_schema = [s for s in schemas if s["function"]["name"] == "query_wiki"][0]
    
    assert cg_schema["function"]["parameters"]["required"] == ["query"]
    assert wiki_schema["function"]["parameters"]["required"] == ["question"]

def test_parse_superpowers_tool_calls():
    # Test standard tool calls parsing
    tc = [
        {
            "id": "tc-cg",
            "type": "function",
            "function": {
                "name": "search_codegraph",
                "arguments": json.dumps({"query": "my_symbol", "kind": "context"})
            }
        },
        {
            "id": "tc-wiki",
            "type": "function",
            "function": {
                "name": "query_wiki",
                "arguments": json.dumps({"question": "how to query?"})
            }
        }
    ]
    actions = parse_tool_calls(tc)
    assert len(actions) == 2
    
    assert actions[0].kind == "search_codegraph"
    assert actions[0].query == "my_symbol"
    assert actions[0].arguments == {"query": "my_symbol", "kind": "context"}
    
    assert actions[1].kind == "query_wiki"
    assert actions[1].arguments == {"question": "how to query?"}

def test_parse_inline_superpowers_tool_calls():
    # Test inline XML parsing if used by some models
    content = """
    I will search the graph.
    <tool_call>
    {"name": "search_codegraph", "arguments": {"query": "AuthService"}}
    </tool_call>
    and then query wiki:
    <tool_call>
    {"name": "query_wiki", "arguments": {"question": "What is AuthService?"}}
    </tool_call>
    """
    actions = parse_inline_tool_calls(content)
    assert len(actions) == 2
    assert actions[0].kind == "search_codegraph"
    assert actions[0].query == "AuthService"
    assert actions[1].kind == "query_wiki"
    assert actions[1].arguments == {"question": "What is AuthService?"}

def test_wiki_query_not_configured(monkeypatch):
    monkeypatch.delenv("HARNESS_WIKI_URL", raising=False)
    monkeypatch.delenv("HARNESS_WIKI_TOKEN", raising=False)
    
    client = WikiClient()
    assert client.query("test question") == "wiki not configured"

def test_wiki_query_success(monkeypatch):
    captured = {}
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"answer": "Auth uses JWT in middleware.py"}).encode()
            
    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp()
        
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    
    client = WikiClient(base_url="http://wiki", token="secret")
    res = client.query("How does Auth work?")
    assert res == "Auth uses JWT in middleware.py"
    assert captured["url"] == "http://wiki/owner/query"
    assert captured["body"] == {"question": "How does Auth work?"}
    assert captured["auth"] == "Bearer secret"

def test_wiki_query_fallback(monkeypatch):
    # If the POST endpoint fails, mock /wiki/manifest.json GET request
    calls = []
    class FakeManifestResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({
                "pages": [
                    {"slug": "auth", "title": "Authentication", "description": "Auth uses JWT"}
                ]
            }).encode()

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        # Always fail the POST requests to simulate missing endpoints
        if req.method == "POST":
            raise Exception("Endpoint not found")
        return FakeManifestResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    
    client = WikiClient(base_url="http://wiki", token="secret")
    res = client.query("What pages exist?")
    assert "Fallback wiki index summary:" in res
    assert "- Authentication (auth): Auth uses JWT" in res
    assert "http://wiki/wiki/manifest.json" in calls
