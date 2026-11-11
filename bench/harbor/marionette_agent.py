"""Harbor adapter for Marionette (`harness exec`).

Marionette runs as shipped: its own system prompt, tools, compaction and
Puppetmaster workers. The adapter gives it the facts the leaderboard gives
every harness: one OpenAI-compatible endpoint, the model id and the model's
context window. Marionette sends no reply limit, so the endpoint's proxy
supplies the model's longest reply.

The pilot reaches the endpoint as a saved external endpoint
(``harness exec --endpoint``). Puppetmaster workers reach the same endpoint
through ``OPENROUTER_BASE_URL``, so every worker call goes to the same model.

Use:
    harbor run -d terminal-bench@2.1 \
      --agent-import-path bench.harbor.marionette_agent:Marionette \
      -m deepseek-v4.1-flash \
      --ak endpoint=http://host.docker.internal:8790/v1 \
      --ak source=/abs/path/pm_harness-<ver>-py3-none-any.whl \
      --ae MARIONETTE_ENDPOINT_KEY=<proxy token>
"""
from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from urllib.parse import urlsplit
from typing import override

from pydantic import Field

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.options import InstalledAgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

KEY_ENV = "MARIONETTE_ENDPOINT_KEY"
PYTHON = "3.12"
TASK_PATH = "/tmp/marionette-task.md"
WHEEL_DIR = "/tmp/marionette-wheel"
PATH_SETUP = 'export PATH="$HOME/.local/bin:$PATH"; '
INSTALL_RETRY_WAITS = (10, 30, 60)
# Offline runs on a local Docker that cannot switch network policy after start
# (Docker Desktop's kernel lacks nft fib inet, so Harbor's egress control is off).
# The task container gets NET_ADMIN (task compose), root locks egress to the
# endpoint after install, and the agent runs without NET_ADMIN/NET_RAW, so
# nothing it starts can change the rules.
LOCK_PROBE = "https://pypi.org/simple/"
DROP_NET_CAPS = (
    'if [ "$(id -u)" = 0 ]; then exec setpriv --bounding-set -net_admin,-net_raw '
    "--inh-caps -net_admin,-net_raw -- bash -c %s; else exec bash -c %s; fi"
)


def egress_lock_script(endpoint: str) -> str:
    """Root shell script: allow loopback and the endpoint host:port, drop the rest."""
    url = urlsplit(endpoint)
    host, port = url.hostname, url.port or (443 if url.scheme == "https" else 80)
    return (
        "set -e; iptables -F OUTPUT; "
        "iptables -A OUTPUT -o lo -j ACCEPT; "
        "iptables -A OUTPUT -d %s -p tcp --dport %d -j ACCEPT; "
        "iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT; "
        "iptables -P OUTPUT DROP; "
        "ip6tables -P OUTPUT DROP 2>/dev/null || true" % (shlex.quote(host), port)
    )


def without_net_caps(command: str) -> str:
    quoted = shlex.quote(command)
    return DROP_NET_CAPS % (quoted, quoted)


def tagged_endpoint(endpoint: str, trial: str) -> str:
    """Put the trial name in the path (``.../t/<trial>/v1``) so the meter can
    split its log per trial. The meter matches on the path suffix."""
    base = endpoint.rstrip("/")
    if not trial or not base.endswith("/v1"):
        return base
    return "%s/t/%s/v1" % (base[: -len("/v1")], trial)


class MarionetteOptions(InstalledAgentOptions):
    endpoint: str = Field(description="OpenAI-compatible base URL the agent may call (the proxy).")
    context_window: int = Field(default=1_048_576, description="The model's context window in tokens.")
    source: str = Field(default="pm-harness", description="pip spec, git URL, or a local wheel path to upload.")
    egress_lock: bool = Field(default=False, description=(
        "Local offline runs only: after install, allow egress to the endpoint and nothing else. "
        "The task container needs cap_add NET_ADMIN."))


class Marionette(BaseInstalledAgent):
    """Marionette in its headless mode, one pilot turn per task."""

    options_model = MarionetteOptions
    options: MarionetteOptions

    @staticmethod
    @override
    def name() -> str:
        return "marionette"

    @override
    def get_version_command(self) -> str | None:
        return PATH_SETUP + "python -c 'import harness; print(harness.__version__)' 2>/dev/null || harness --version"

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip().splitlines()[-1].strip()

    def _install_spec(self) -> str:
        local = Path(self.options.source)
        if local.suffix == ".whl" and local.is_file():
            return "%s/%s" % (WHEEL_DIR, local.name)
        return self.options.source

    async def _ensure_packages(self, environment: BaseEnvironment, packages: tuple, required: bool) -> None:
        """Install system packages. Mirror 404s are transient, so retry."""
        for wait in INSTALL_RETRY_WAITS:
            try:
                await self.ensure_system_dependencies(environment, packages)
                return
            except Exception as exc:  # harbor raises a plain error with the apt output
                last = exc
                self.logger.warning("system packages %s failed, retry in %ds: %s", packages, wait, str(exc)[:200])
                await asyncio.sleep(wait)
        if required:
            raise last
        self.logger.warning("continuing without optional packages %s", packages)

    async def _install_iptables(self, environment: BaseEnvironment) -> None:
        """Harbor's dependency helper has no iptables entry, so call the package manager."""
        cmd = ("command -v iptables >/dev/null 2>&1 || "
               "(apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables) || "
               "apk add --no-cache iptables")
        for wait in INSTALL_RETRY_WAITS:
            try:
                await self.exec_as_root(environment, command=cmd)
                return
            except Exception as exc:
                last = exc
                self.logger.warning("iptables install failed, retry in %ds: %s", wait, str(exc)[:200])
                await asyncio.sleep(wait)
        raise last

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await self._ensure_packages(environment, ("curl", "bash"), required=True)
        await self._ensure_packages(environment, ("git",), required=False)
        if self.options.egress_lock:
            await self._install_iptables(environment)
        local = Path(self.options.source)
        if local.suffix == ".whl" and local.is_file():
            await self.exec_as_agent(environment, command="mkdir -p %s" % WHEEL_DIR)
            await environment.upload_file(local, "%s/%s" % (WHEEL_DIR, local.name))
        await self.exec_as_agent(environment, command=(
            "set -euo pipefail; "
            "if ! command -v uv >/dev/null 2>&1; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi; "
            + PATH_SETUP +
            "uv python install %s && " % PYTHON
            + "uv tool install --python %s %s && harness --help >/dev/null" % (PYTHON, shlex.quote(self._install_spec()))
        ))

    @override
    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        await self._upload_config_text(environment, content=instruction, remote_path=TASK_PATH, filename="task.md")
        (self.logs_dir / "instruction.md").write_text(instruction)
        logs = self.environment_logs_dir
        endpoint = tagged_endpoint(self.options.endpoint, self.logs_dir.parent.name)
        key = self._get_env(KEY_ENV) or "local"
        env = {
            KEY_ENV: key,
            # Puppetmaster workers: same endpoint, same key, so the same model.
            "OPENROUTER_API_KEY": key,
            "OPENROUTER_BASE_URL": endpoint,
        }
        if self.options.egress_lock:
            await self._lock_egress(environment, endpoint)
        await self.exec_as_agent(environment, env=env, command=self._wrap(
            PATH_SETUP + "harness exec --task-file %s --endpoint %s --model %s --api-key-env %s "
              "--context-window %d --events %s/marionette-events.jsonl </dev/null 2>%s/marionette-stderr.txt "
              "| tee %s/marionette-stdout.txt" % (
                TASK_PATH, shlex.quote(endpoint), shlex.quote(self.model_name or ""), KEY_ENV,
                self.options.context_window, logs, logs, logs)
        ))

    def _wrap(self, command: str) -> str:
        return without_net_caps(command) if self.options.egress_lock else command

    async def _lock_egress(self, environment: BaseEnvironment, endpoint: str) -> None:
        """Lock egress, then prove it from the agent side. Never run a trial unlocked."""
        await self.exec_as_root(environment, command=egress_lock_script(endpoint))
        probe = await self.exec_as_agent(environment, command=without_net_caps(
            "curl -s -m 8 -o /dev/null -w %%{http_code} %s || true; iptables -P OUTPUT ACCEPT 2>/dev/null && echo UNLOCKED || true" % LOCK_PROBE))
        out = (getattr(probe, "stdout", "") or "").strip()
        if "UNLOCKED" in out or (out[:3].isdigit() and out[:3] != "000"):
            raise RuntimeError("egress lock failed: probe saw %r" % out[:200])
        (self.logs_dir / "egress-lock.txt").write_text("locked to %s, probe %s\n" % (endpoint, out or "blocked"))

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        return None
