#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${NETVAULT_UPDATE_URL:-https://github.com/iihciyekub/netvault.git}"
PACKAGE_URL="git+${REPO_URL}"

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
