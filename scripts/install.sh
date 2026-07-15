#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${NETVAULT_UPDATE_URL:-https://github.com/iihciyekub/netvault.git}"
RELEASE_TAG="${NETVAULT_RELEASE_TAG:-}"

if [[ -z "${RELEASE_TAG}" && "${REPO_URL}" == https://github.com/*/* ]]; then
  RELEASE_PAGE="$(curl -fsSL -o /dev/null -w '%{url_effective}' "${REPO_URL%.git}/releases/latest")"
  RELEASE_TAG="${RELEASE_PAGE##*/}"
fi
if [[ -n "${RELEASE_TAG}" && ! "${RELEASE_TAG}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Invalid NetVault release tag: ${RELEASE_TAG}" >&2
  exit 1
fi

PACKAGE_URL="git+${REPO_URL}${RELEASE_TAG:+@${RELEASE_TAG}}"

if command -v uv >/dev/null 2>&1; then
  uv tool install --force "${PACKAGE_URL}"
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "${PACKAGE_URL}"
else
  cat >&2 <<'EOF'
NetVault needs uv or pipx for a clean command-line install.

Recommended:
  brew install uv

Then run:
  curl -fsSL https://raw.githubusercontent.com/iihciyekub/netvault/main/scripts/install.sh | bash
EOF
  exit 1
fi

cat <<'EOF'
NetVault installed.

Try:
  nv login https://iiaide.com/nv
  nv list
EOF

nv --version
