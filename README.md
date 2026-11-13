# Marionette

Desktop coding harness. Any model drives, Puppetmaster does the work.
Paste one API key and you got the whole thing: chat pilot, workers, swarms, flows.
No Cursor, Claude Code, or Codex CLI needed.
It makes $0 off your tokens, so its whole job is spending fewer of them, and it shows you the receipts.

<a href="https://professorpalmer.github.io/marionette/"><img src="docs/assets/media/hero.webp" alt="Screen recording of Marionette running a three-worker swarm audit and streaming the findings" width="100%" /></a>

Current release: **v0.9.663**. Still pre-1.0 on purpose.
Puppetmaster is the bundled kernel, not a second thing to set up: `puppetmaster-ai==1.50.0` gets pinned into the venv for you.

## Quick start

macOS / Linux:

```bash
curl -fsSL https://professorpalmer.github.io/marionette/install.sh | bash
```

Windows (PowerShell):

```powershell
irm https://professorpalmer.github.io/marionette/install.ps1 | iex
```

Then:

```bash
marionette
```

1. Settings > API keys. Paste any full stack key: OpenRouter, Anthropic, OpenAI, Gemini, Bedrock, Codex OAuth, OpenCode Go, etc.
2. Open a project folder.
3. Ask it something real. Pilot and workers both run on that one key.

Something off? `marionette doctor` checks the whole environment.
Updates are the `update` pill in the status bar.
Installers (DMG, `.exe`, AppImage) are on [Releases](https://github.com/professorpalmer/marionette/releases/latest) too, same app underneath.

## Index

### Start

| Doc | What it covers |
|---|---|
| [Install and update](docs/start/install.md) | Installers, launcher commands, requirements, the two distribution paths, run from a checkout |
| [Configuration](docs/start/configuration.md) | Every env var, compact residual, Jev opt-in, how job costs get priced |
| [Demo](docs/start/demo.md) | 10-minute live walkthrough |

### What it does

| Doc | What it covers |
|---|---|
| [Capabilities](docs/features/capabilities.md) | The three panes and the full capability table |
| [Encrypted sessions](docs/features/encrypted-sessions.md) | Opt-in encryption at rest for new sessions |
| [Computer use](docs/features/computer-use.md) | Pilot drives the in-app browser and native apps |
| [Discord MCP](docs/features/discord-mcp.md) | Optional recipe, not a first-party Discord product |

### How it works

| Doc | What it covers |
|---|---|
| [Why Marionette](docs/design/why.md) | No best model. A kernel, not a model vendor |
| [Architecture](docs/design/architecture.md) | The pilot loop, the seam, durable state, package map |
| [Distiller](docs/design/distiller.md) | Finished sessions into skills, rules, and wiki pages |
| [`harness/`](harness/README.md) / [`pmharness/`](pmharness/README.md) / [`webapp/`](webapp/README.md) | Directory guides: product backend, research rig, desktop app |

### Evidence

| Doc | What it covers |
|---|---|
| [Benchmarks](docs/evidence/benchmarks.md) | SWE-bench Lite: 29% lower spend with routing + durable retries, 90 vs 98 resolved, one seed. NL2Repo-Bench: 91.1% mean pass rate vs ~40% published |
| [Findings](docs/evidence/findings.md) | Which models can drive the harness |
| [Router evaluation](docs/evidence/router-evaluation.md) | Recorded router comparison |

### Develop

| Doc | What it covers |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | Setup, ground rules, PR flow (branch off `dev`) |
| [AGENTS.md](AGENTS.md) | Rules for agents working in this repo |
| [Releasing](docs/develop/releasing.md) | How a merge reaches every install, tags, GitHub Releases |
| [NOTICE.md](NOTICE.md) | Third-party attributions |

Full list: the [docs index](docs/README.md).
