# SSH_Remote_Kaggle

SSH (and optionally code-server) into a Kaggle notebook, for free, with no ngrok TCP needed.

```
Windows â”€â”€ ssh kaggle â”€â”€> cloudflared access â”€â”€> https://xxxx.trycloudflare.com â”€â”€> cloudflared tunnel â”€â”€> sshd 127.0.0.1:2201 (Kaggle)
```

## 1. One-time client setup (Windows)

```powershell
git clone https://github.com/matbinhtinh/SSH_Remote_Kaggle.git
cd SSH_Remote_Kaggle
.\client\kaggle_ssh.ps1 https://placeholder.trycloudflare.com -Setup
```
`-Setup` installs `cloudflared` (winget), creates the key `~/.ssh/kaggle_ed25519`, and prints the **public key**.

## 2. Kaggle Secrets (Add-ons â†’ Secrets, attach to the notebook)

| Secret | Required | Content |
|---|---|---|
| `SSH_PUBLIC_KEY` | yes | The public key printed in step 1 (`ssh-ed25519 AAAA...`) |
| `SSH_PASSWORD` | no | Root password. Leave empty to allow SSH keys only (recommended) |
| `NGROK_TOKEN` | no | Only for publishing code-server over ngrok HTTP |
| `VSCODE_PASSWORD` | no | code-server password (generated randomly when empty) |

Kaggle does not accept empty secrets: enter a single space `" "` to mean "unset" (values are trimmed). The old names `ID_RSA_PUB`, `SSH_PASS`, `NGROK_TOKEN_1` still work. An environment variable with the same name takes priority over the secret.

## 3. Each Kaggle session

1. Import `ssh-remote.ipynb` into Kaggle and turn **Internet On** (and a GPU if needed).
2. Run cells 1 â†’ 3 and copy the printed `https://xxxx.trycloudflare.com` link (also saved to `/kaggle/working/ssh_tunnel.txt`).
3. On Windows:
   ```powershell
   .\client\kaggle_ssh.ps1 https://xxxx.trycloudflare.com
   ssh kaggle
   ```
The link changes every time cell 3 is rerun or the session restarts; just rerun the step above.

## Fixes compared to the original version

- The ngrok TCP tunnel for SSH silently failed (`except: print('failt')`): free accounts cannot open TCP tunnels (ERR_NGROK_8013). SSH now goes through a Cloudflare quick tunnel; ngrok is only used for HTTP, and its errors are printed in full.
- The root password was mistakenly taken from `NGROK_TOKEN_1` instead of `SSH_PASS`.
- `sshd` could not start because the container has no host keys â†’ `ssh-keygen -A`.
- The image's sshd listens on `127.0.0.1:2222`, and `sed 's/^#Port/...'` did not change it â†’ a separate sshd with its own config (`/etc/ssh/sshd_config_kaggle`, port 2201), key-only unless `SSH_PASSWORD` is set.
- The tunnel pointed to `localhost` â†’ resolved to `::1` while sshd only listens on IPv4 â†’ now uses `127.0.0.1`.
- SSH sessions could not see the GPU (missing `PATH`/`LD_LIBRARY_PATH` from the container) â†’ `export_kaggle_env()`.
- code-server used the fixed password `12345` and bound to `0.0.0.0` â†’ random password / from secret, bound to `127.0.0.1`.
- Invalid ngrok region `en` â†’ `eu`, ...
