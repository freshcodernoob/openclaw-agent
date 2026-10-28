import os
import sys
import subprocess
import time

# Force unbuffered output
os.environ["PYTHONUNBUFFERED"] = "1"

# Setup debug log file handle in /kaggle/working/
log_file_path = "/kaggle/working/openclaw_debug.log"
log_file = open(log_file_path, "a", encoding="utf-8")

print("=== OPENCLAW KAGGLE RUNNER INITIALIZING ===", flush=True)

# Read keys from Kaggle dataset mount (/kaggle/input/keys-env/keys.env.txt)
env_file_path = "/kaggle/input/keys-env/keys.env.txt"
if os.path.exists(env_file_path):
    print(f"Loading secrets from Kaggle dataset: {env_file_path}", flush=True)
    try:
        with open(env_file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")
    except Exception as e:
        print(f"Error parsing keys.env.txt: {e}", flush=True)
else:
    print(f"WARNING: Dataset keys file not found at {env_file_path}", flush=True)

HF_TOKEN = os.environ.get("HF_TOKEN", "")
CLOUDFLARE_TUNNEL_TOKEN = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

OPENCLAW_DIR = "/kaggle/working/openclaw"
os.makedirs(OPENCLAW_DIR, exist_ok=True)

# 1. Safe Hugging Face State Sync (Namespace: reikai)
def hf_pull():
    if not HF_TOKEN:
        print("HF_TOKEN not found. Skipping HF state pull.", flush=True)
        return
    try:
        from huggingface_hub import snapshot_download
        print("Pulling lightweight OpenClaw state from Hugging Face...", flush=True)
        snapshot_download(
            repo_id="reikai/openclaw-state",
            repo_type="dataset",
            token=HF_TOKEN,
            local_dir=OPENCLAW_DIR,
            ignore_patterns=["node_modules/*", "out/*", "build/*", ".cache/*", "*.log", ".git/*"]
        )
        print("Successfully synced state from Hugging Face.", flush=True)
    except Exception as e:
        print(f"HF pull skipped/bypassed: {e}", flush=True)

hf_pull()

# 2. Setup and Start Cloudflare Tunnel (`cloudflared`) pointing to custom domain openclaw.kiyumi.online
print("Setting up and starting Cloudflare Tunnel...", flush=True)
cloudflared_path = "/tmp/cloudflared"
if not os.path.exists(cloudflared_path):
    subprocess.run(["curl", "-L", "--output", cloudflared_path, "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"], check=True)
    subprocess.run(["chmod", "+x", cloudflared_path], check=True)

tunnel_process = None
if CLOUDFLARE_TUNNEL_TOKEN:
    tunnel_process = subprocess.Popen(
        [cloudflared_path, "tunnel", "run", "--token", CLOUDFLARE_TUNNEL_TOKEN],
        stdout=log_file,
        stderr=log_file
    )
    print("Cloudflare Tunnel process launched for openclaw.kiyumi.online.", flush=True)
else:
    print("WARNING: CLOUDFLARE_TUNNEL_TOKEN is missing!", flush=True)

# 3. Build & Launch OpenClaw Web App on port 3000
print("Preparing OpenClaw Web UI...", flush=True)
os.chdir(OPENCLAW_DIR)

if os.path.exists("package.json"):
    print("Installing Node dependencies...", flush=True)
    subprocess.run(["npm", "install"], check=False)
    
    print("Building OpenClaw application...", flush=True)
    subprocess.run(["npm", "run", "build"], check=False)

env = os.environ.copy()
env["PORT"] = "3000"
if GEMINI_API_KEY:
    env["GEMINI_API_KEY"] = GEMINI_API_KEY

print("Starting OpenClaw service on port 3000...", flush=True)
openclaw_process = subprocess.Popen(
    ["npm", "run", "start"],
    stdout=log_file,
    stderr=log_file,
    env=env
)
print("OpenClaw background service initiated on port 3000.", flush=True)

# 4. Keep-Alive Loop (Prevents Kaggle container exit)
print("=== OPENCLAW RUNNER FULLY ACTIVE. ENTERING KEEP-ALIVE LOOP ===", flush=True)
try:
    while True:
        time.sleep(60)
        if tunnel_process and tunnel_process.poll() is not None:
            print("Tunnel process died! Restarting...", flush=True)
            tunnel_process = subprocess.Popen(
                [cloudflared_path, "tunnel", "run", "--token", CLOUDFLARE_TUNNEL_TOKEN],
                stdout=log_file,
                stderr=log_file
            )
except KeyboardInterrupt:
    print("Runner shut down gracefully.", flush=True)
