"""Tests for git login and provisioning flow."""
import json
import base64
import urllib.request
import urllib.error
import urllib.parse
from unittest.mock import patch, MagicMock
import pytest

from harness.git_provision import GitProvisioner, get_status, save_connection, delete_connection
import harness.server as srv
import json as _json
import threading as _threading
import urllib.request as _urlreq


def _server():
    import harness.server as srv
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = _threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
    req = _urlreq.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET")
    return _urlreq.urlopen(req, timeout=10)


def _post(port, path, body, headers=None):
    req = _urlreq.Request(f"http://127.0.0.1:{port}{path}",
                          data=_json.dumps(body).encode(),
                          headers=headers or {}, method="POST")
    return _urlreq.urlopen(req, timeout=10)

def test_detect_gh_missing():
    provisioner = GitProvisioner()
    with patch("shutil.which", return_value=None):
        res = provisioner.detect_gh()
        assert res == {"available": False, "user": None}

def test_detect_gh_available():
    provisioner = GitProvisioner()
    with patch("shutil.which", return_value="/usr/local/bin/gh"), \
         patch("subprocess.run") as mock_run:
        
        # mock subprocess run success
        mock_run.return_value = MagicMock(returncode=0, stdout="test-user")
        res = provisioner.detect_gh()
        assert res == {"available": True, "user": "test-user"}

def test_device_flow_start_request():
    provisioner = GitProvisioner()
    mock_response = MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.read.return_value = json.dumps({
        "device_code": "dev123",
        "user_code": "user456",
        "verification_uri": "https://github.com/login/device",
        "interval": 5,
        "expires_in": 900
    }).encode("utf-8")
    
    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        res = provisioner.device_flow_start("client_id_override")
        
        assert res["device_code"] == "dev123"
        assert res["user_code"] == "user456"
        assert mock_urlopen.called
        
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://github.com/login/device/code"
        assert req.get_header("Accept") == "application/json"

def test_provision_wiki_repo_create_and_seed(monkeypatch):
    monkeypatch.setattr("harness.git_provision.time.sleep", lambda _s: None)
    provisioner = GitProvisioner()
    
    calls = []
    def mock_gh_request(method, path, token, body=None):
        calls.append((method, path, body))
        if path == "/user":
            return 200, {"login": "test-user"}, {}
        elif path == "/repos/test-user/my-portable-llm-wiki" and method == "GET":
            return 404, {}, {}
        elif path == "/user/repos" and method == "POST":
            return 201, {"full_name": "test-user/my-portable-llm-wiki", "html_url": "https://github.com/test-user/my-portable-llm-wiki"}, {}
        elif path == "/repos/test-user/my-portable-llm-wiki/contents/index.md" and method == "GET":
            return 404, {}, {}
        elif path == "/repos/test-user/my-portable-llm-wiki/contents/index.md" and method == "PUT":
            return 201, {"content": {}}, {}
        return 500, {}, {}
        
    with patch.object(provisioner, "_github_request", side_effect=mock_gh_request):
        res = provisioner.provision_wiki_repo("test-token")
        
        assert res["ok"] is True
        assert res["repo_full_name"] == "test-user/my-portable-llm-wiki"
        assert res["created"] is True
        assert res["seeded"] is True
        
        assert ("GET", "/user", None) in calls
        assert ("GET", "/repos/test-user/my-portable-llm-wiki", None) in calls
        assert ("POST", "/user/repos", {"name": "my-portable-llm-wiki", "private": True, "auto_init": True, "description": "Portable LLM Wiki - cross-LLM durable memory"}) in calls
        assert ("GET", "/repos/test-user/my-portable-llm-wiki/contents/index.md", None) in calls
        
        # Verify base64 content
        seed_call = [c for c in calls if c[0] == "PUT"][0]
        seed_body = seed_call[2]
        assert seed_body["message"] == "seed index.md with starter template"
        decoded = base64.b64decode(seed_body["content"]).decode("utf-8")
        assert "# Portable LLM Wiki" in decoded

def test_provision_wiki_repo_idempotent():
    provisioner = GitProvisioner()
    
    calls = []
    def mock_gh_request(method, path, token, body=None):
        calls.append((method, path, body))
        if path == "/user":
            return 200, {"login": "test-user"}, {}
        elif path == "/repos/test-user/my-portable-llm-wiki" and method == "GET":
            return 200, {"full_name": "test-user/my-portable-llm-wiki", "html_url": "https://github.com/test-user/my-portable-llm-wiki"}, {}
        elif path == "/repos/test-user/my-portable-llm-wiki/contents/index.md" and method == "GET":
            return 200, {"name": "index.md"}, {}
        return 500, {}, {}
        
    with patch.object(provisioner, "_github_request", side_effect=mock_gh_request):
        res = provisioner.provision_wiki_repo("test-token")
        
        assert res["ok"] is True
        assert res["repo_full_name"] == "test-user/my-portable-llm-wiki"
        assert res["created"] is False
        assert res["seeded"] is False

def test_get_api_git_status_403_without_token():
    httpd, port, _ = _server()
    try:
        try:
            _get(port, "/api/git/status")
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()

def test_get_api_git_status_success_with_token():
    httpd, port, srv = _server()
    try:
        # Mock git connection status
        with patch("harness.git_provision.load_connection", return_value={
            "method": "device",
            "repo_full_name": "test-user/my-portable-llm-wiki",
            "html_url": "https://github.com/test-user/my-portable-llm-wiki"
        }), patch("harness.git_provision.load_device_token", return_value="dummy-token"):
            
            resp = _get(port, "/api/git/status", headers={"X-Harness-Token": srv._TOKEN})
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["connected"] is True
            assert data["wiki_repo"] == "test-user/my-portable-llm-wiki"
            assert "gh_available" in data
    finally:
        httpd.shutdown()
