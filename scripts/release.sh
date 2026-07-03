#!/bin/bash
# Source-run release: bump the version, tag it, and push to main. There is no
# DMG, no notarization, and no uploaded binary -- Marionette runs from a source
# checkout, so a release reaches everyone the moment they hit "Update & Relaunch"
# (git pull + rebuild). The tag/version exists only so app.getVersion() and the
# update pill can show a human-readable version.
#
# Usage:  scripts/release.sh 0.7.0   ["release notes line"]
set -euo pipefail

VERSION="${1:-}"
NOTES="${2:-}"
if [ -z "$VERSION" ]; then
  echo "usage: scripts/release.sh X.Y.Z [\"notes\"]" >&2
  exit 1
fi
VERSION="${VERSION#v}"
TAG="v${VERSION}"

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$REPO_ROOT"

echo "== preflight =="
if [ -n "$(git status --porcelain | grep -v 'results/' || true)" ]; then
  echo "ERROR: working tree is dirty. Commit or stash first." >&2
  git status --short | grep -v 'results/' >&2
  exit 1
fi
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "main" ]; then
  echo "ERROR: not on main (on $BRANCH)." >&2
  exit 1
fi
# releases push as professorpalmer (gh drifts to cary_jepp)
gh auth switch --user professorpalmer >/dev/null 2>&1 || true

echo "== set version $VERSION =="
python3 - "$VERSION" <<'PY'
import re, sys
v = sys.argv[1]
p = "webapp/package.json"
s = open(p).read()
s = re.sub(r'"version":\s*"[^"]*"', f'"version": "{v}"', s, count=1)
open(p, "w").write(s)
print("package.json ->", v)
PY

echo "== commit + tag + push =="
git -c user.name=professorpalmer -c user.email=professorpalmer@users.noreply.github.com \
  add webapp/package.json
git -c user.name=professorpalmer -c user.email=professorpalmer@users.noreply.github.com \
  commit -q -m "release: ${TAG}" || echo "(nothing to commit)"
git tag -f "$TAG"
git push origin main
git push -f origin "$TAG"

# A GitHub Release is optional (source-run needs no attached binary), but we cut
# one anyway so the tag carries notes and shows up on the releases page.
echo "== github release (notes only, no assets) =="
REL_NOTES="${NOTES:-Marionette ${VERSION}}"
if gh release view "$TAG" --repo professorpalmer/marionette >/dev/null 2>&1; then
  gh release edit "$TAG" --repo professorpalmer/marionette --notes "$REL_NOTES"
else
  gh release create "$TAG" \
    --repo professorpalmer/marionette \
    --title "Marionette ${VERSION}" \
    --notes "$REL_NOTES" \
    --latest
fi

echo
echo "DONE. Release ${TAG} tagged + pushed."
echo "Checkouts on an older commit get ${VERSION} on their next 'Update & Relaunch' (git pull + rebuild)."
