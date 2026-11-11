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

import shlex
from pathlib import Path
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

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(environment, ("curl", "bash", "git"))
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
        await self.exec_as_agent(environment, env=env, command=(
            PATH_SETUP + "harness exec --task-file %s --endpoint %s --model %s --api-key-env %s "
              "--context-window %d --events %s/marionette-events.jsonl </dev/null 2>%s/marionette-stderr.txt "
              "| tee %s/marionette-stdout.txt" % (
                TASK_PATH, shlex.quote(endpoint), shlex.quote(self.model_name or ""), KEY_ENV,
                self.options.context_window, logs, logs, logs)
        ))

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        return None
