import glob
import os
import secrets
import subprocess
import threading
import time
import urllib.request

os.environ["PYTHONUNBUFFERED"] = "1"

GATEWAY_PORT = 18789
LOG_PATH = "/kaggle/working/openclaw_debug.log"
log_file = open(LOG_PATH, "a", encoding="utf-8")


# ---------- logging helpers (everything shows in the Kaggle log AND the file) ----------
def log(msg):
    line = f"[runner] {msg}"
    print(line, flush=True)
    log_file.write(line + "\n")
    log_file.flush()


def _pump(proc, name):
    for line in iter(proc.stdout.readline, ""):
        out = f"[{name}] {line.rstrip()}"
        print(out, flush=True)
        log_file.write(out + "\n")
        log_file.flush()


def spawn(cmd, name, env=None):
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
    )
    t = threading.Thread(target=_pump, args=(proc, name), daemon=True)
    t.start()
    proc._pump = t
    return proc


def run(cmd, name, env=None):
    proc = spawn(cmd, name, env)
    rc = proc.wait()
    proc._pump.join(timeout=5)
    if rc != 0:
        log(f"Command failed (exit {rc}): {' '.join(cmd)}")
    return rc


log("=== OPENCLAW KAGGLE RUNNER INITIALIZING ===")


# ---------- 1. Load secrets from the Kaggle dataset ----------
def load_keys():
    candidates = ["/kaggle/input/keys-env/keys.env.txt"]
    candidates += glob.glob("/kaggle/input/**/keys.env.txt", recursive=True)
    for path in candidates:
        if not os.path.exists(path):
            continue
        log(f"Loading secrets from {path}")
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")
        return
    log("WARNING: keys.env.txt not found anywhere under /kaggle/input. Contents:")
    for p in glob.glob("/kaggle/input/**", recursive=True)[:50]:
        log(f"   {p}")


load_keys()

HF_TOKEN = os.environ.get("HF_TOKEN", "")
CLOUDFLARE_TUNNEL_TOKEN = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "")

# Gateway password/token (needed because the gateway is reachable through a public tunnel)
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
if not GATEWAY_TOKEN:
    GATEWAY_TOKEN = secrets.token_urlsafe(32)
    log("OPENCLAW_GATEWAY_TOKEN not set in keys.env.txt - generated a temporary one.")
    log(f"Temporary gateway token (add it to keys.env.txt to keep it): {GATEWAY_TOKEN}")
os.environ["OPENCLAW_GATEWAY_TOKEN"] = GATEWAY_TOKEN


# ---------- 2. Make sure Node >= 22.16 is installed (OpenClaw needs it) ----------
def node_version():
    try:
        out = subprocess.run(["node", "-v"], capture_output=True, text=True).stdout.strip()
        major, minor = out.lstrip("v").split(".")[:2]
        return int(major), int(minor)
    except Exception:
        return (0, 0)


if node_version() < (22, 16):
    log(f"Node too old/missing ({node_version()}). Installing Node 24...")
    run(["bash", "-c",
         "curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && apt-get install -y nodejs"],
        "node-install")
log(f"Node version: {node_version()}")


# ---------- 3. Install OpenClaw itself ----------
log("Installing OpenClaw (npm install -g openclaw@latest)...")
if run(["npm", "install", "-g", "openclaw@latest"], "npm-install") != 0:
    raise SystemExit("OpenClaw install failed - see the [npm-install] lines above.")
run(["openclaw", "--version"], "openclaw-version")


# ---------- 4. Restore saved state from Hugging Face into ~/.openclaw ----------
STATE_DIR = os.path.expanduser("~/.openclaw")
os.makedirs(STATE_DIR, exist_ok=True)

if HF_TOKEN:
    try:
        from huggingface_hub import snapshot_download
        log("Pulling OpenClaw state from Hugging Face...")
        snapshot_download(
            repo_id="reikai/openclaw-state",
            repo_type="dataset",
            token=HF_TOKEN,
            local_dir=STATE_DIR,
            ignore_patterns=["node_modules/*", "out/*", "build/*", ".cache/*", "*.log", ".git/*"],
        )
        log("State synced from Hugging Face.")
    except Exception as e:
        log(f"HF pull skipped: {e}")
else:
    log("HF_TOKEN not found. Skipping HF state pull.")


# ---------- 5. Cloudflare Tunnel ----------
cloudflared_path = "/tmp/cloudflared"
if not os.path.exists(cloudflared_path):
    run(["curl", "-fsSL", "--output", cloudflared_path,
         "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"],
        "cloudflared-download")
    run(["chmod", "+x", cloudflared_path], "chmod")


def start_tunnel():
    return spawn([cloudflared_path, "--no-autoupdate", "tunnel", "run", "--token", CLOUDFLARE_TUNNEL_TOKEN],
                 "tunnel")


tunnel_process = None
if CLOUDFLARE_TUNNEL_TOKEN:
    tunnel_process = start_tunnel()
    log(f"Cloudflare Tunnel launched. In the Cloudflare dashboard the public hostname must point to "
        f"http://localhost:{GATEWAY_PORT}")
else:
    log("WARNING: CLOUDFLARE_TUNNEL_TOKEN is missing - no public URL will work.")


# ---------- 6. Start the OpenClaw gateway ----------
def start_gateway():
    env = os.environ.copy()  # includes GEMINI_API_KEY and OPENCLAW_GATEWAY_TOKEN
    return spawn(
        ["openclaw", "gateway",
         "--port", str(GATEWAY_PORT),
         "--bind", "loopback",
         "--auth", "token",
         "--allow-unconfigured"],
        "gateway", env=env,
    )


def wait_healthy(proc, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{GATEWAY_PORT}/healthz", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


log(f"Starting OpenClaw gateway on port {GATEWAY_PORT}...")
gateway_process = start_gateway()
if wait_healthy(gateway_process):
    log("Gateway is healthy.")
else:
    log("Gateway did NOT become healthy - check the [gateway] lines above for the real error.")


# ---------- 7. Keep-alive loop (restarts anything that dies) ----------
log("=== OPENCLAW RUNNER ACTIVE. ENTERING KEEP-ALIVE LOOP ===")
try:
    while True:
        time.sleep(60)
        if tunnel_process and tunnel_process.poll() is not None:
            log("Tunnel process died! Restarting...")
            tunnel_process = start_tunnel()
        if gateway_process.poll() is not None:
            log(f"Gateway died (exit {gateway_process.returncode})! Restarting...")
            gateway_process = start_gateway()
except KeyboardInterrupt:
    log("Runner shut down gracefully.")
