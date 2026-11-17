#!/usr/bin/env bash
# One-time owner-run Netcup console bootstrap. No root login settings changed.
# Usage: bash create_deploy_user.sh 'ssh-ed25519 AAAA... optional-label'
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this bootstrap as root from the provider console." >&2
  exit 1
fi
if [[ "$#" -ne 1 ]]; then
  echo "Usage: bash create_deploy_user.sh 'ssh-ed25519 PUBLIC_KEY'" >&2
  exit 2
fi

public_key="$1"
if [[ "${public_key}" != ssh-ed25519\ * || "${public_key}" == *$'\n'* ]]; then
  echo "Expected exactly one Ed25519 SSH public key." >&2
  exit 2
fi
if ! printf '%s\n' "${public_key}" | ssh-keygen -l -f - >/dev/null 2>&1; then
  echo "SSH public key verification failed." >&2
  exit 2
fi

deploy_user="ae-deploy"
expected_home="/home/${deploy_user}"
if ! id -u "${deploy_user}" >/dev/null 2>&1; then
  useradd --create-home --user-group --shell /bin/bash "${deploy_user}"
fi

actual_home="$(getent passwd "${deploy_user}" | cut -d: -f6)"
if [[ "${actual_home}" != "${expected_home}" ]]; then
  echo "Refusing to modify unexpected home directory." >&2
  exit 3
fi

ssh_dir="${expected_home}/.ssh"
auth_file="${ssh_dir}/authorized_keys"
install -d -m 0700 -o "${deploy_user}" -g "${deploy_user}" "${ssh_dir}"
if [[ -L "${auth_file}" ]]; then
  echo "Refusing symlinked authorized_keys." >&2
  exit 3
fi
if [[ ! -e "${auth_file}" ]]; then
  install -m 0600 -o "${deploy_user}" -g "${deploy_user}" /dev/null "${auth_file}"
fi
if [[ ! -f "${auth_file}" ]]; then
  echo "Refusing non-regular authorized_keys." >&2
  exit 3
fi
if ! grep -Fxq -- "${public_key}" "${auth_file}"; then
  printf '%s\n' "${public_key}" >> "${auth_file}"
fi
chown "${deploy_user}:${deploy_user}" "${ssh_dir}" "${auth_file}"
chmod 0700 "${ssh_dir}"
chmod 0600 "${auth_file}"

echo "Installed requested public SSH key for ${deploy_user}; no sudo permissions granted."
printf '%s\n' "${public_key}" | ssh-keygen -l -f -
