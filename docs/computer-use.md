# Use the browser and native apps

Ask the pilot to work in Marionette's browser or in a running desktop app.
Tools run in the active conversation. Switching conversations cancels native
control and revokes app access.

## In-app browser

The pilot opens the Browser pane when needed. Its tools operate the selected
visible tab, including that tab's existing login. They do not substitute a
separate Chrome session. Complete login yourself when the pilot requests a
handoff.

The pilot reads the page before clicking or typing. It can switch tabs, take
screenshots, send keys, and use screenshot coordinates for canvas or embedded
content. Element references expire when the document or selected tab changes.
Screenshot coordinates expire after input or a viewport change.

## Native apps

1. Open the app you want the pilot to use.
2. Ask the pilot to inspect or operate that app.
3. In Marionette's permission dialog, choose **Allow for this session** or **Deny**.
4. To end access immediately, choose **Stop computer control** above the workspace.

Access lets the selected model provider receive the app's accessibility text
and, when supported, a screenshot of the selected window. It also lets the
pilot send mouse and keyboard input. App access does not authorize unrelated
messages, purchases, deletion, uploads, or permission changes.

On macOS, enable Marionette under **System Settings > Privacy & Security >
Accessibility** and **Screen Recording**. Relaunch if macOS requests it.
The native helper is bundled for Intel and Apple Silicon; Xcode is not required
in the installed app. Native window screenshots require macOS 14 or later.

On Windows, keep the desktop unlocked. Marionette and the target app must run
at the same integrity level. Protected windows and apps that reject UI
Automation or window capture may be unavailable. Linux supports the in-app
browser, but native desktop control is not available.

Text-only pilots use accessibility text and element references. Vision-capable
pilots can also inspect screenshots. Native input returns a fresh app state so
the pilot can verify the outcome. A successful input event alone is not proof
that a task finished.

The pilot cannot approve its own permissions by controlling Marionette.
Keyboard chords are limited to app-local editing and navigation; OS launchers
and app-switching shortcuts are not supported by this tool.
App restarts, session switches, window changes, and revoked access invalidate
old targets. The controller retains at most eight recent screenshots and removes
its tracked screenshots when access ends. Screenshots already included in a
conversation remain subject to that conversation's retention settings.

## Solo pilots

Manually added API pilots can use these tools without worker credentials.
Worker availability is independent of the pilot model. By default, broad tasks
and multiple file reads do not force delegation. The pilot can use workers when
an independent slice benefits from parallel work.

For deployments that deliberately require strict orchestration, set
`HARNESS_SWARM_GATE=1` or `HARNESS_DELEGATE_GATE=1`. These gates do not block
direct progress when no worker route is available. Filesystem permissions,
repeat-call guards, and total-turn budgets still apply.
