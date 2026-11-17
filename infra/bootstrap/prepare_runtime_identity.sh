#!/usr/bin/env bash
# Stage 4.2 / 6.3: owner-approved ONE-TIME root console setup only.
# This file is deliberately not called by CI or any automatic deployment.
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Refusing: run from the approved root console only." >&2
  exit 1
fi

runtime_user="ae-runtime"
runtime_home="/var/lib/ai-agent-engine"
runtime_shell="/usr/sbin/nologin"

if getent passwd "${runtime_user}" >/dev/null; then
  IFS=: read -r _ _ uid gid _ actual_home actual_shell < <(getent passwd "${runtime_user}")
  if [[ "${uid}" -eq 0 || "${actual_home}" != "${runtime_home}" || "${actual_shell}" != "${runtime_shell}" ]]; then
    echo "Refusing to modify an unexpected pre-existing runtime account." >&2
    exit 2
  fi
  if [[ "$(getent group "${runtime_user}" | cut -d: -f3)" != "${gid}" ]]; then
    echo "Refusing: runtime primary group is unexpected." >&2
    exit 2
  fi
else
  if getent group "${runtime_user}" >/dev/null; then
    echo "Refusing to reuse a pre-existing runtime group." >&2
    exit 2
  fi
  useradd --system --user-group --home-dir "${runtime_home}" --shell "${runtime_shell}" "${runtime_user}"
fi

for directory in /opt/ai-agent-engine /opt/ai-agent-engine/releases "${runtime_home}"; do
  if [[ -L "${directory}" || ( -e "${directory}" && ! -d "${directory}" ) ]]; then
    echo "Refusing unexpected path type: ${directory}" >&2
    exit 3
  fi
done
if [[ -d "${runtime_home}" && "$(stat -c '%U:%G' "${runtime_home}")" != "${runtime_user}:${runtime_user}" ]]; then
  echo "Refusing to change ownership of existing runtime state." >&2
  exit 3
fi

install -d -m 0755 -o root -g root /opt/ai-agent-engine /opt/ai-agent-engine/releases
install -d -m 0700 -o "${runtime_user}" -g "${runtime_user}" "${runtime_home}"

echo "Runtime identity prepared. No sudo, SSH login, engine service, or secret was enabled."
