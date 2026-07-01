// Pure helpers for choosing a remote/ref during passive update checks.
//
// Adapted from the Hermes Agent desktop updater (update-remote.cjs, MIT,
// Nous Research). Marionette self-updates by tracking a git checkout, so a
// background `git fetch origin` runs on launch. If the user's origin is the
// official repo over SSH and their GitHub SSH key is FIDO2/passkey-backed, that
// background fetch triggers an unexplained hardware-touch prompt. For passive
// checks against the official repo we substitute the public HTTPS URL, which
// needs no auth and cannot prompt. Active apply flows use the user's own origin.
//
// Extracted so the remote detection is unit-testable without booting Electron.

const OFFICIAL_REPO_HTTPS_URL = "https://github.com/professorpalmer/pm-harness.git";
const OFFICIAL_REPO_CANONICAL = "github.com/professorpalmer/pm-harness";

// Normalize common GitHub remote URL forms to `host/owner/repo` (lowercased, no
// trailing slash, no .git suffix) so SSH and HTTPS forms of the same repo
// compare equal.
function canonicalGitHubRemote(url) {
  if (!url) return "";
  let value = String(url).trim();
  if (value.startsWith("git@github.com:")) {
    value = `github.com/${value.slice("git@github.com:".length)}`;
  } else if (value.startsWith("ssh://git@github.com/")) {
    value = `github.com/${value.slice("ssh://git@github.com/".length)}`;
  } else {
    try {
      const parsed = new URL(value);
      if (parsed.hostname && parsed.pathname) value = `${parsed.hostname}${parsed.pathname}`;
    } catch {
      // Leave non-URL forms unchanged.
    }
  }
  value = value.trim().replace(/\/+$/, "");
  if (value.endsWith(".git")) value = value.slice(0, -4);
  return value.toLowerCase();
}

function isSshRemote(url) {
  const value = String(url || "").trim().toLowerCase();
  return value.startsWith("git@") || value.startsWith("ssh://");
}

function isOfficialSshRemote(url) {
  return isSshRemote(url) && canonicalGitHubRemote(url) === OFFICIAL_REPO_CANONICAL;
}

// The remote to `git fetch` from for a PASSIVE check. For the official repo over
// SSH we swap in the public HTTPS URL to avoid a passkey touch prompt; every
// other remote (a fork, an HTTPS origin, a private mirror) is fetched as-is.
function chooseFetchRemote(originUrl) {
  if (isOfficialSshRemote(originUrl)) return OFFICIAL_REPO_HTTPS_URL;
  return "origin";
}

module.exports = {
  OFFICIAL_REPO_HTTPS_URL,
  OFFICIAL_REPO_CANONICAL,
  canonicalGitHubRemote,
  isSshRemote,
  isOfficialSshRemote,
  chooseFetchRemote,
};
