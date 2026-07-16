# Discord MCP recipe (optional)

Marionette does **not** ship a first-party Discord bot product. You can still
wire an open-source MIT Discord MCP into the pilot the same way as any other
Docker/HTTP MCP: run the server yourself, keep the token in the container, then
register the HTTP endpoint with Marionette.

This guide mirrors the path we use locally. Other MIT Discord MCP images work
the same if they expose streamable HTTP at `/mcp`.

## What you get

- The pilot can call Discord tools via MCP (`call_mcp`) after the server is
  running and registered.
- You can ask the pilot to post swarm summaries, open threads, or read channel
  context once the bot is in your guild.
- Discord → Marionette “slash command that starts a swarm” is **your** bridge
  (bot → HTTP → harness). Marionette only owns the MCP client side.

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or any
  Docker engine that can publish a local port)
- A Discord application + bot token
  ([Developer Portal](https://discord.com/developers/applications))
- Marionette **v0.9.74+** (loopback HTTP MCP allowed; `manage_mcp` available)

## 1. Create and invite the bot

1. In the Discord Developer Portal, create an application → **Bot** → reset /
   copy the token. Store it somewhere private; never paste it into chat or
   `mcp.json`.
2. Under **OAuth2 → URL Generator**, enable `bot` (and any scopes/permissions
   your chosen MCP docs require — often message read/send, and privileged
   intents if the server needs message content).
3. Open the generated invite URL and add the bot to your server.

Exact intents/permissions depend on the MCP implementation. Prefer the
upstream project README when in doubt.

## 2. Run the MCP container

Example using [SaseQ/discord-mcp](https://github.com/SaseQ/discord-mcp) (MIT),
which defaults to HTTP on **8085** — the same shape Marionette’s State → MCP
empty-state hints use:

```powershell
docker run -d --name discord-mcp --restart unless-stopped `
  -p 8085:8085 `
  -e SPRING_PROFILES_ACTIVE=http `
  -e DISCORD_TOKEN=YOUR_BOT_TOKEN_HERE `
  saseq/discord-mcp:latest
```

Confirm the container is healthy, then hit the endpoint shape
`http://localhost:8085/mcp` (or `http://127.0.0.1:8085/mcp`).

Keep `DISCORD_TOKEN` in the container environment only. Do not put the token in
Marionette’s `~/.pmharness/mcp.json`.

Opt out of loopback MCP entirely with `PMHARNESS_MCP_ALLOW_PRIVATE=0` if you
want the old SSRF-strict default.

## 3. Register with Marionette

**UI:** State → MCP → Add → name `discord-mcp`, URL
`http://localhost:8085/mcp` → save/start.

**Pilot (preferred after Docker is up):** ask something like:

> Wire discord-mcp at http://localhost:8085/mcp with manage_mcp.

The pilot should call `manage_mcp` `add` (then `start` if needed). Do **not**
shell-edit `mcp.json` and do **not** mid-turn restart the harness for MCP
wiring — `manage_mcp` is enough; restart soft-refuses during an open turn.

Boot already calls `start_all()` for configured servers, so a registered HTTP
MCP comes back on the next Marionette launch if the container is still up.

## 4. Use it

- List tools under State → MCP once the server shows running.
- Ask the pilot to use Discord tools (`call_mcp`) for channel reads/posts.
- For swarm work: run the swarm in Marionette as usual, then ask the pilot to
  post the tracker summary / findings into a channel via the Discord MCP tools.

### Optional: Discord-triggered swarms

If you want a Discord message or slash command to *start* a Marionette swarm,
that listener lives outside Marionette (your bot or a tiny sidecar calling the
harness/Puppetmaster API). Document that bridge in your own ops notes; we do
not ship it.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Unsafe MCP URL: blocked loopback` | Update to v0.9.74+ or unset `PMHARNESS_MCP_ALLOW_PRIVATE=0`. |
| State → MCP empty / 0 tools | Container down, wrong URL, or server never `start`ed. Check `docker ps` and Play in the MCP pane. |
| Pilot tries `POST /api/restart` mid-turn | Soft-refused on purpose. Finish the turn; use Settings → Restart only if you really need a backend reload. |
| Token leaked in chat | Rotate the bot token in the Developer Portal; recreate the container env. |

## Related

- State pane → MCP (empty-state hints the `localhost:8085/mcp` shape)
- Pilot tool `manage_mcp` (list / add / start / stop / remove)
- [SaseQ/discord-mcp](https://github.com/SaseQ/discord-mcp) (example MIT server)
