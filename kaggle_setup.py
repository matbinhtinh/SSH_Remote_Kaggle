"""
SSH / code-server access to a Kaggle notebook container.

    from kaggle_setup import *
    export_kaggle_env()
    start_ssh(public_keys=SSH_PUBLIC_KEY)          # sshd on 127.0.0.1:SSH_PORT, key auth
    url = start_cloudflared(port=SSH_PORT)         # free, no account: https://xxx.trycloudflare.com
    print_client_help(url)

SSH goes through a Cloudflare quick tunnel (ngrok TCP tunnels require a verified card).
code-server can still be exposed over ngrok HTTP with start_vscode() + start_ngrok().
"""
import os
import re
import secrets
import shlex
import socket
import subprocess
import time

SSH_PORT = 2201                      # dedicated port, does not clash with the image's own sshd
SSHD_CONFIG = "/etc/ssh/sshd_config_kaggle"
SSHD_PID = "/run/sshd_kaggle.pid"
CLOUDFLARED = "/usr/local/bin/cloudflared"
CLOUDFLARED_LOG = "/tmp/cloudflared_ssh.log"
TUNNEL_INFO = "/kaggle/working/ssh_tunnel.txt"


def _log(msg):
    print(f"[INFO] {msg}", flush=True)


def _run(cmd, step, check=True):
    """Run a shell command; print the real error output on failure."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[ERROR] {step} failed (exit {r.returncode}):\n{(r.stderr or r.stdout).strip()}", flush=True)
        if check:
            raise RuntimeError(f"{step} failed")
    return r


def _port_open(port, host="127.0.0.1", timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            return s.recv(64).decode(errors="replace").strip()
    except OSError:
        return ""


def get_secret(*names, default=""):
    """
    First non-empty value among Kaggle Secrets / environment variables `names`.
    Values are stripped, so a placeholder secret such as " " (Kaggle does not accept
    empty secrets) counts as unset.
    """
    client = None
    try:
        from kaggle_secrets import UserSecretsClient
        client = UserSecretsClient()
    except Exception:
        pass
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
        if client is not None:
            try:
                value = (client.get_secret(name) or "").strip()
                if value:
                    return value
            except Exception:
                pass
    return default


def export_kaggle_env():
    """
    SSH sessions do not inherit the notebook container's environment, so CUDA
    (nvidia-smi, libcuda via LD_LIBRARY_PATH) is invisible over SSH. Persist it.
    """
    keep = re.compile(r"^(PATH|LD_LIBRARY_PATH|CUDA_[A-Z_]*|NVIDIA_[A-Z_]*|KAGGLE_[A-Z_]*|PYTHONPATH)=")
    with open("/proc/1/environ", "rb") as f:
        items = [kv.decode(errors="replace") for kv in f.read().split(b"\0") if kv]
    lines = []
    for kv in items:
        if keep.match(kv):
            k, v = kv.split("=", 1)
            lines.append(f"export {k}={shlex.quote(v)}")
    os.makedirs("/etc/profile.d", exist_ok=True)
    with open("/etc/profile.d/kaggle_env.sh", "w") as f:
        f.write("\n".join(lines) + "\n")
    bashrc = os.path.expanduser("~/.bashrc")
    hook = "[ -f /etc/profile.d/kaggle_env.sh ] && . /etc/profile.d/kaggle_env.sh"
    content = open(bashrc).read() if os.path.exists(bashrc) else ""
    if hook not in content:
        # must be at the very top: Ubuntu's .bashrc returns early for non-interactive shells
        with open(bashrc, "w") as f:
            f.write(hook + "\n" + content)
    _log(f"exported {len(lines)} env vars to SSH sessions (/etc/profile.d/kaggle_env.sh)")


def start_ssh(public_keys="", password="", port=SSH_PORT, install=True):
    """
    Start a dedicated sshd on 127.0.0.1:port (reachable only through the tunnel).
    :param public_keys: one or more OpenSSH public keys (newline separated).
    :param password: optional root password; password login stays disabled when empty.
    """
    _log("***** SETUP SSH *****")
    if not os.path.exists("/usr/sbin/sshd"):
        if not install:
            raise RuntimeError("openssh-server is not installed")
        _log("installing openssh-server...")
        _run("apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server",
             "install openssh-server")

    # host keys are not generated when openssh-server is installed inside a container
    _run("ssh-keygen -A", "generate host keys")
    os.makedirs("/run/sshd", exist_ok=True)

    keys = [k.strip() for k in (public_keys or "").splitlines() if k.strip()]
    if keys:
        ssh_dir = os.path.expanduser("~/.ssh")
        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
        ak = os.path.join(ssh_dir, "authorized_keys")
        existing = open(ak).read().splitlines() if os.path.exists(ak) else []
        new = [k for k in keys if k not in existing]
        with open(ak, "a") as f:
            for k in new:
                f.write(k + "\n")
        os.chmod(ssh_dir, 0o700)
        os.chmod(ak, 0o600)
        _log(f"authorized_keys: {len(new)} new, {len(existing)} existing")
    elif not password:
        raise ValueError("give at least a public key (SSH_PUBLIC_KEY) or a password (SSH_PASSWORD)")

    if password:
        _run(f"echo {shlex.quote('root:' + password)} | chpasswd", "set root password")
        _log("root password set")

    config = f"""\
Port {port}
ListenAddress 127.0.0.1
PidFile {SSHD_PID}
PermitRootLogin {'yes' if password else 'prohibit-password'}
PubkeyAuthentication yes
PasswordAuthentication {'yes' if password else 'no'}
KbdInteractiveAuthentication no
UsePAM no
AuthorizedKeysFile .ssh/authorized_keys
X11Forwarding no
PrintMotd no
AcceptEnv LANG LC_*
ClientAliveInterval 30
Subsystem sftp /usr/lib/openssh/sftp-server
"""
    with open(SSHD_CONFIG, "w") as f:
        f.write(config)
    _run(f"/usr/sbin/sshd -t -f {SSHD_CONFIG}", "validate sshd config")

    # restart our own sshd instance so config changes always apply
    if os.path.exists(SSHD_PID):
        subprocess.run(f"kill $(cat {SSHD_PID}) 2>/dev/null", shell=True)
        time.sleep(1)
    _run(f"/usr/sbin/sshd -f {SSHD_CONFIG}", "start sshd")

    for _ in range(10):
        banner = _port_open(port)
        if banner.startswith("SSH-"):
            _log(f"sshd is running on 127.0.0.1:{port} ({banner})")
            return True
        time.sleep(0.5)
    raise RuntimeError(f"sshd does not answer on 127.0.0.1:{port}")


def start_cloudflared(port=SSH_PORT, timeout=60):
    """Expose 127.0.0.1:port through a Cloudflare quick tunnel; returns the https URL."""
    _log("***** SETUP CLOUDFLARED *****")
    if not os.path.exists(CLOUDFLARED):
        _run(f"wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 "
             f"-O {CLOUDFLARED} && chmod +x {CLOUDFLARED}", "download cloudflared")
    # stop a previous SSH tunnel of ours (other cloudflared tunnels are left alone)
    # ([c] keeps pkill from matching the shell that runs it)
    subprocess.run(f"pkill -f '[c]loudflared tunnel .*ssh://127.0.0.1:{port}$'", shell=True)
    # 127.0.0.1, not localhost: localhost may resolve to ::1 while sshd listens on IPv4 only
    subprocess.Popen(
        f"nohup {CLOUDFLARED} tunnel --no-autoupdate --url ssh://127.0.0.1:{port} > {CLOUDFLARED_LOG} 2>&1 &",
        shell=True,
    )
    url = None
    deadline = time.time() + timeout
    while time.time() < deadline and not url:
        time.sleep(1)
        if os.path.exists(CLOUDFLARED_LOG):
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", open(CLOUDFLARED_LOG).read())
            url = m.group(0) if m else None
    if not url:
        print(open(CLOUDFLARED_LOG).read()[-2000:] if os.path.exists(CLOUDFLARED_LOG) else "(no log)")
        raise RuntimeError("cloudflared did not report a tunnel URL")
    # wait until the tunnel is registered at the edge
    while time.time() < deadline:
        if "Registered tunnel connection" in open(CLOUDFLARED_LOG).read():
            break
        time.sleep(1)
    try:
        with open(TUNNEL_INFO, "w") as f:
            f.write(url + "\n")
    except OSError:
        pass
    _log(f"tunnel: {url}  (saved to {TUNNEL_INFO}, log: {CLOUDFLARED_LOG})")
    return url


def print_client_help(url, host_alias="kaggle", key_path="~/.ssh/kaggle_ed25519"):
    host = url.replace("https://", "").strip("/")
    print(f"""
{'=' * 70}
SSH tunnel: {url}

Windows (PowerShell) - update ~/.ssh/config, then connect:
    .\\client\\kaggle_ssh.ps1 {url}
    ssh {host_alias}

Manual ~/.ssh/config entry (needs cloudflared on the client):
Host {host_alias}
    HostName {host}
    User root
    IdentityFile {key_path}
    ProxyCommand cloudflared access ssh --hostname %h
    StrictHostKeyChecking accept-new
    UserKnownHostsFile ~/.ssh/known_hosts_{host_alias}
    ServerAliveInterval 30
{'=' * 70}
""", flush=True)


def start_vscode(ws_dir="/kaggle/working", password="", port=9000, vscode_dir="~/.vscode", install=True,
                 extensions=("ms-python.python", "ms-toolsai.jupyter")):
    """Run code-server on 127.0.0.1:port inside `screen`. Returns the password used."""
    _log("***** SETUP CODE-SERVER *****")
    password = password or secrets.token_urlsafe(12)
    extensions_dir = os.path.expanduser(f"{vscode_dir}/extensions")
    user_data_dir = os.path.expanduser(f"{vscode_dir}/user_data")
    os.makedirs(extensions_dir, exist_ok=True)
    os.makedirs(user_data_dir, exist_ok=True)
    if install and subprocess.run("command -v code-server", shell=True, capture_output=True).returncode != 0:
        _run("curl -fsSL https://code-server.dev/install.sh | sh", "install code-server")
    if subprocess.run("command -v screen", shell=True, capture_output=True).returncode != 0:
        _run("apt-get install -y -qq screen", "install screen")
    subprocess.run("screen -S vscode -X quit", shell=True, capture_output=True)
    _run(f"PASSWORD={shlex.quote(password)} screen -dmS vscode code-server --bind-addr 127.0.0.1:{port} "
         f"--user-data-dir={user_data_dir} --extensions-dir={extensions_dir} --disable-telemetry {ws_dir}",
         "start code-server")
    for ext in extensions:
        _run(f"code-server --extensions-dir={extensions_dir} --install-extension {ext}", f"install {ext}",
             check=False)
    _log(f"code-server on 127.0.0.1:{port}")
    return password


def start_ngrok(ngrok_tokens, binds=None, regions=("us", "eu", "ap", "au", "sa", "jp", "in")):
    """
    Expose local ports over ngrok (default: code-server over HTTP).
    Note: free ngrok accounts cannot open TCP tunnels (ERR_NGROK_8013) - use start_cloudflared for SSH.
    """
    binds = binds or {"vscode": {"port": 9000, "type": "http"}}
    _log("***** SETUP NGROK *****")
    try:
        from pyngrok import ngrok, conf
    except ImportError:
        _run("pip install -q pyngrok", "install pyngrok")
        from pyngrok import ngrok, conf
    ngrok.kill()
    tokens = [t for t in ([ngrok_tokens] if isinstance(ngrok_tokens, str) else ngrok_tokens) if t]
    for token in tokens:
        for region in regions:
            try:
                conf.get_default().region = region
                ngrok.set_auth_token(token)
                info = {}
                for name, b in binds.items():
                    info[name] = ngrok.connect(b.get("port", 80), b.get("type", "http")).public_url
                for name, u in info.items():
                    _log(f"{name}: {u}")
                return info
            except Exception as e:  # show the real reason (e.g. ERR_NGROK_8013, invalid token)
                print(f"[ERROR] ngrok region={region}: {e}", flush=True)
                ngrok.kill()
    raise RuntimeError("ngrok: all tokens/regions failed")
