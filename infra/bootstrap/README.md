# One-time deploy user bootstrap (Stage 4.2 / step 2)

This directory contains a **reviewable, owner-run** helper for a new non-root `ae-deploy` account on the already-provisioned Netcup Debian 13 VPS.

- Requires provider console access as root and **one valid Ed25519 public SSH key** provided as the script's sole argument.
- If the account is missing, creates it with a home directory; adds the public key to `/home/ae-deploy/.ssh/authorized_keys` idempotently.
- Grants **no sudo** and makes **no change** to existing root authentication, SSH daemon configuration, firewall, or running services.
- Does not generate, read, print, transmit, or store an SSH **private** key. Never commit a private key or provider credentials.
- Requires a staged, separately stored deploy private key before use. After the owner runs it, test the actual SSH identity without root privileges.
- Do not run by downloading and piping directly to a shell. Review the pinned commit version of the script first. One-time console steps will be prepared only when the key and GitHub secret handling are ready.
- This script is **preparation only**, not proof of login, connectivity, or a completed Stage 4.2/2.

Step 2 can be accepted only when the owner has installed the public key, a live SSH connection using the matching private key succeeds, and the account's rights are inspected. Until then leave its checkbox open.
