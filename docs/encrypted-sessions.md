# Encrypted sessions

Encrypted sessions keep conversation data encrypted on disk. The switch is
opt-in. It applies to new sessions only. Existing sessions do not change.

## Turn it on

1. Open **Settings > Safety > Encrypted sessions**.
2. Select **Encrypt new sessions**.
3. Save the recovery code that Marionette shows. It shows one time only.
4. Select **I saved the code**.

To stop, clear the switch. New sessions are then plain. Encrypted sessions
stay encrypted and stay readable.

## What is encrypted

These items are encrypted for each encrypted session:

- Transcript, title, and compaction archive.
- Attachments, uploads, and spilled tool output.
- Session search index (it is kept in memory only).
- Todos, goal, scratch, and prompt queue.
- The diff lines that undo checkpoints record for each edit.
- Memory that an encrypted session saves. Only encrypted sessions see it.
- The continuity note of a scheduled run, when encryption is on.

These items are not encrypted:

- Your workspace files.
- Undo snapshots. Marionette keeps them as objects in the workspace's own git
  folder, and each label names the file path.
- What model providers and MCP servers receive. The model still gets the
  full conversation.
- Session metadata: the session id, workspace path, dates, and token counts.

## Keys

Each encrypted session has its own key. A master key wraps the session keys.
Your OS key store keeps the master key: Keychain on macOS, the Windows
credential store, or Secret Service on Linux. A recovery code also wraps it.

Marionette refuses to turn on encryption when the system has no OS key store.
On Linux, the `basic_text` store does not count.

Delete an encrypted session to destroy its key on this disk. The copies of
its data on this disk are then unreadable. A backup made before the delete
still holds the wrapped key.

## Locked sessions

If the OS key store does not give the master key, encrypted sessions are
locked. They show as "Encrypted session" with no content, and new sessions
cannot start while the switch is on.

1. Open **Settings > Safety > Encrypted sessions**.
2. Enter the recovery code.
3. Select **Unlock**.

Marionette then stores the key in this system's key store again.

## Limits

These features are off in an encrypted session:

- Workers (`run_swarm`, `run_implement`, `run_parallel`, `run_flow`). Worker
  jobs keep their goals in the plain Puppetmaster store.
- The browser tool. Every session shares one browser profile.
- CLI pilots (Claude Code, Cursor CLI). These CLIs keep their own plain history.
- Wiki capture, skill and rule distill, and refine proposals.

Hooks get only the session id and `"sealed": true`, not the prompt or title.

Encryption protects data at rest: a copy of `~/.pmharness`, a backup, a sync
folder, or another user account. It does not protect against software that
runs as you while Marionette is unlocked.
