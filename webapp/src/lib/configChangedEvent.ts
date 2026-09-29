/**
 * harness-config-changed may carry { repoChanged }. A session switch inside
 * one project sets it false: nothing repo-wide (file tree, git status) can
 * have changed, so those panes skip their full reload. Dispatches without a
 * detail keep meaning "anything may have changed".
 */
export function dispatchConfigChanged(detail?: { repoChanged: boolean }): void {
  window.dispatchEvent(detail ? new CustomEvent("harness-config-changed", { detail }) : new Event("harness-config-changed"));
}

export function configChangeKeepsRepo(event: Event): boolean {
  return (event as CustomEvent<{ repoChanged?: boolean } | undefined>).detail?.repoChanged === false;
}
