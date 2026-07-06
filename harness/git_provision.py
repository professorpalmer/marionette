from __future__ import annotations

import os
import json
import time
import base64
import subprocess
import shutil
import urllib.request
import urllib.parse
import urllib.error

from .secure_files import restrict_to_owner

class GitProvisioner:
    def detect_gh(self) -> dict:
        if not shutil.which("gh"):
            return {"available": False, "user": None}
        try:
            # Run gh auth status
            subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
            # Run gh api user --jq .login
            res_user = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
            if res_user.returncode == 0:
                login = res_user.stdout.strip()
                if login:
                    return {"available": True, "user": login}
        except Exception:
            pass
        return {"available": False, "user": None}

    def github_token(self) -> str | None:
        if not shutil.which("gh"):
            return None
        try:
            res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
            if res.returncode == 0:
                tok = res.stdout.strip()
                if tok:
                    return tok
        except Exception:
            pass
        return None

    def device_flow_start(self, client_id: str | None = None) -> dict:
        if not client_id:
            client_id = os.environ.get("PMHARNESS_GH_CLIENT_ID", "178c6fc778ccc68e1d6a")
        
        url = "https://github.com/login/device/code"
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "scope": "repo"
        }).encode("utf-8")
        
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Accept": "application/json"}
        )
        
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                return {
                    "device_code": res_data.get("device_code"),
                    "user_code": res_data.get("user_code"),
                    "verification_uri": res_data.get("verification_uri"),
                    "interval": res_data.get("interval", 5),
                    "expires_in": res_data.get("expires_in", 900)
                }
        except Exception as e:
            return {"error": str(e)}

    def device_flow_poll(self, client_id: str | None, device_code: str) -> dict:
        if not client_id:
            client_id = os.environ.get("PMHARNESS_GH_CLIENT_ID", "178c6fc778ccc68e1d6a")
        
        url = "https://github.com/login/oauth/access_token"
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
        }).encode("utf-8")
        
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Accept": "application/json"}
        )
        
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                err = res_data.get("error")
                if not err:
                    tok = res_data.get("access_token")
                    if tok:
                        return {"status": "authorized", "token": tok}
                    else:
                        return {"status": "error", "error": "No token in response"}
                elif err in ("authorization_pending", "slow_down"):
                    return {"status": "pending"}
                else:
                    return {"status": "error", "error": err}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _github_request(self, method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict, dict]:
        url = f"https://api.github.com{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "pm-harness",
            "Accept": "application/vnd.github+json"
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method
        )
        
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                res_body = json.loads(response.read().decode("utf-8"))
                return response.status, res_body, dict(response.headers)
        except urllib.error.HTTPError as e:
            try:
                err_body = json.loads(e.read().decode("utf-8"))
            except Exception:
                err_body = {}
            return e.code, err_body, dict(e.headers)
        except Exception as e:
            return 500, {"message": str(e)}, {}

    def provision_wiki_repo(self, token: str, repo_name: str = "my-portable-llm-wiki") -> dict:
        if not token:
            return {"ok": False, "error": "No token provided"}
        try:
            # 1. GET /user -> login
            status, user_data, _ = self._github_request("GET", "/user", token)
            if status != 200:
                return {"ok": False, "error": f"Failed to fetch GitHub user: {user_data.get('message', 'status ' + str(status))}"}
            
            login = user_data.get("login")
            if not login:
                return {"ok": False, "error": "Could not determine GitHub login"}
            
            # 2. Check if <login>/<repo_name> exists
            repo_path = f"/repos/{login}/{repo_name}"
            status, repo_data, _ = self._github_request("GET", repo_path, token)
            
            created = False
            if status == 200:
                repo_full_name = repo_data.get("full_name", f"{login}/{repo_name}")
                html_url = repo_data.get("html_url", f"https://github.com/{login}/{repo_name}")
            elif status == 404:
                # Create it
                create_body = {
                    "name": repo_name,
                    "private": True,
                    "auto_init": True,
                    "description": "Portable LLM Wiki - cross-LLM durable memory"
                }
                create_status, create_data, _ = self._github_request("POST", "/user/repos", token, create_body)
                if create_status not in (200, 201):
                    return {"ok": False, "error": f"Failed to create repo: {create_data.get('message', 'status ' + str(create_status))}"}
                created = True
                repo_full_name = create_data.get("full_name", f"{login}/{repo_name}")
                html_url = create_data.get("html_url", f"https://github.com/{login}/{repo_name}")
            else:
                return {"ok": False, "error": f"Failed to check repository status: {repo_data.get('message', 'status ' + str(status))}"}

            # 3. Seed it
            contents_path = f"/repos/{login}/{repo_name}/contents/index.md"
            if created:
                time.sleep(1)
                
            get_contents_status, contents_data, _ = self._github_request("GET", contents_path, token)
            
            seeded = False
            if get_contents_status == 404:
                content_str = "# Portable LLM Wiki\n\nPortable LLM Wiki - cross-LLM durable memory\n"
                content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
                seed_body = {
                    "message": "seed index.md with starter template",
                    "content": content_b64
                }
                seed_status, seed_data, _ = self._github_request("PUT", contents_path, token, seed_body)
                if seed_status not in (200, 201):
                    time.sleep(1.5)
                    seed_status, seed_data, _ = self._github_request("PUT", contents_path, token, seed_body)
                    if seed_status not in (200, 201):
                        return {"ok": False, "error": f"Failed to seed repository index.md: {seed_data.get('message', 'status ' + str(seed_status))}"}
                seeded = True
            elif get_contents_status == 200:
                seeded = False

            return {
                "ok": True,
                "repo_full_name": repo_full_name,
                "html_url": html_url,
                "created": created,
                "seeded": seeded
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}


def _get_git_connection_path() -> str:
    return os.path.expanduser("~/.pmharness/git_connection.json")


def _get_git_token_path() -> str:
    return os.path.expanduser("~/.pmharness/git_token")


def save_connection(method: str, repo_full_name: str, html_url: str) -> None:
    path = _get_git_connection_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {
        "method": method,
        "repo_full_name": repo_full_name,
        "html_url": html_url,
        "connected_at": time.time()
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def save_device_token(token: str) -> None:
    path = _get_git_token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        mode = 0o600
        fd = os.open(path, flags, mode)
        with os.fdopen(fd, 'w', encoding="utf-8") as f:
            f.write(token)
    except Exception:
        with open(path, "w", encoding="utf-8") as f:
            f.write(token)
    restrict_to_owner(path)


def load_connection() -> dict | None:
    path = _get_git_connection_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def load_device_token() -> str | None:
    path = _get_git_token_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return None


def delete_connection() -> None:
    for p in [_get_git_connection_path(), _get_git_token_path()]:
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


def get_status() -> dict:
    provisioner = GitProvisioner()
    gh_info = provisioner.detect_gh()
    
    conn = load_connection()
    connected = False
    wiki_repo = None
    html_url = None
    
    if conn:
        method = conn.get("method")
        wiki_repo = conn.get("repo_full_name")
        html_url = conn.get("html_url")
        if method == "gh":
            tok = provisioner.github_token()
            if tok:
                connected = True
        elif method == "device":
            tok = load_device_token()
            if tok:
                connected = True
                
    return {
        "gh_available": gh_info["available"],
        "gh_user": gh_info["user"],
        "wiki_repo": wiki_repo,
        "html_url": html_url,
        "connected": connected
    }
