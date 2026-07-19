#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${NETVAULT_UPDATE_URL:-https://github.com/iihciyekub/netvault.git}"
RELEASE_TAG="${NETVAULT_RELEASE_TAG:-}"
UV_INSTALL_URL="${NETVAULT_UV_INSTALL_URL:-https://astral.sh/uv/install.sh}"

download() {
  curl --fail --silent --show-error --location \
    --retry 3 --connect-timeout 10 --max-time 120 "$@"
}

normalized_repository_url() {
  local normalized="${1#git+}"
  printf '%s\n' "${normalized}"
}

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
  elif [[ -x "${HOME}/.local/bin/uv" ]]; then
    printf '%s\n' "${HOME}/.local/bin/uv"
  elif [[ -x "${HOME}/.cargo/bin/uv" ]]; then
    printf '%s\n' "${HOME}/.cargo/bin/uv"
  else
    return 1
  fi
}

github_repository_slug() {
  local normalized
  normalized="$(normalized_repository_url "$1")"
  normalized="${normalized%.git}"
  normalized="${normalized%/}"
  if [[ "${normalized}" =~ ^https://github\.com/([^/[:space:]]+/[^/[:space:]]+)$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  elif [[ "${normalized}" =~ ^git@github\.com:([^/[:space:]]+/[^/[:space:]]+)$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
  else
    return 1
  fi
}

uv_tool_bin_dir() {
  local resolved=""
  if resolved="$("${UV_BIN}" tool dir --bin 2>/dev/null)" && [[ -n "${resolved}" ]]; then
    printf '%s\n' "${resolved}"
  elif [[ -n "${UV_TOOL_BIN_DIR:-}" ]]; then
    printf '%s\n' "${UV_TOOL_BIN_DIR}"
  elif [[ -n "${XDG_BIN_HOME:-}" ]]; then
    printf '%s\n' "${XDG_BIN_HOME}"
  else
    printf '%s\n' "${HOME}/.local/bin"
  fi
}

UV_BIN="$(find_uv || true)"
if [[ -z "${UV_BIN}" ]]; then
  command -v curl >/dev/null 2>&1 || {
    echo "NetVault needs curl to install uv." >&2
    exit 1
  }
  echo "uv was not found. Installing uv from ${UV_INSTALL_URL} ..."
  UV_INSTALL_SCRIPT="$(mktemp "${TMPDIR:-/tmp}/netvault-uv-install.XXXXXX")"
  trap 'rm -f "${UV_INSTALL_SCRIPT}"' EXIT
  download --output "${UV_INSTALL_SCRIPT}" "${UV_INSTALL_URL}"
  sh "${UV_INSTALL_SCRIPT}"
  UV_BIN="$(find_uv || true)"
fi
if [[ -z "${UV_BIN}" ]]; then
  echo "uv was installed but could not be located." >&2
  echo "Open a new terminal and run this installer again." >&2
  exit 1
fi

GITHUB_SLUG="$(github_repository_slug "${REPO_URL}" || true)"
if [[ -z "${RELEASE_TAG}" && -n "${GITHUB_SLUG}" ]]; then
  command -v curl >/dev/null 2>&1 || {
    echo "NetVault needs curl to resolve the latest release." >&2
    exit 1
  }
  RELEASE_PAGE="$(
    download --output /dev/null --write-out '%{url_effective}' \
      "https://github.com/${GITHUB_SLUG}/releases/latest"
  )"
  RELEASE_TAG="${RELEASE_PAGE##*/}"
fi
if [[ -n "${RELEASE_TAG}" && ! "${RELEASE_TAG}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Invalid NetVault release tag: ${RELEASE_TAG}" >&2
  exit 1
fi

if [[ -n "${GITHUB_SLUG}" && -n "${RELEASE_TAG}" ]]; then
  RELEASE_VERSION="${RELEASE_TAG#v}"
  PACKAGE_URL="https://github.com/${GITHUB_SLUG}/releases/download/${RELEASE_TAG}/netvault-${RELEASE_VERSION}-py3-none-any.whl"
else
  NORMALIZED_REPO_URL="$(normalized_repository_url "${REPO_URL}")"
  PACKAGE_URL="git+${NORMALIZED_REPO_URL}${RELEASE_TAG:+@${RELEASE_TAG}}"
fi

echo "Installing NetVault ${RELEASE_TAG:-from ${REPO_URL}} ..."
"${UV_BIN}" tool install --force "${PACKAGE_URL}"
if ! "${UV_BIN}" tool update-shell; then
  echo "Warning: NetVault was installed, but PATH could not be updated automatically." >&2
fi

TOOL_BIN_DIR="$(uv_tool_bin_dir)"
NV_BIN="${TOOL_BIN_DIR}/nv"
NETVAULT_BIN="${TOOL_BIN_DIR}/netvault"
if [[ ! -x "${NV_BIN}" || ! -x "${NETVAULT_BIN}" ]]; then
  echo "NetVault was installed, but both CLI commands were not created in ${TOOL_BIN_DIR}." >&2
  exit 1
fi

cat <<'EOF'
NetVault installed.

Try:
  nv login https://iiaide.com/nv
  nv list

If nv is not available in this terminal yet, open a new terminal and try again.
EOF

INSTALLED_VERSION="$("${NV_BIN}" --version)"
printf '%s\n' "${INSTALLED_VERSION}"
if [[ -n "${RELEASE_TAG}" && "${INSTALLED_VERSION}" != *"${RELEASE_TAG#v}"* ]]; then
  echo "Expected NetVault ${RELEASE_TAG#v}, but the installed command reported: ${INSTALLED_VERSION}" >&2
  exit 1
fi
