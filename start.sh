#!/usr/bin/env bash
# ============================================================
# Qwen -> OpenAI-Compatible API : Master Control (REAL)
# ============================================================
#   ./start.sh --login      # EK BAAR login (browser bridge primary)
#   ./start.sh --serve      # browser-bridge server (recommended)
#   ./start.sh --serve-http # HTTP replay server (token wala path)
#   ./start.sh --mitm       # phone/Burp-style capture mode
#   ./start.sh --local      # backup: local GGUF model (offline)
#   ./start.sh --test       # mock self-test
#   ./start.sh --status     # sab check karo
# ============================================================
set -u
cd "$(dirname "$0")"
PY="./venv/bin/python"

banner() {
  echo "=============================================="
  echo " QWEN -> API KIT | $1"
  echo "=============================================="
}

check_venv() {
  if [ ! -x "$PY" ]; then
    echo "[!] venv nahi mila. Setup:"
    echo "    python3.11 -m venv venv"
    echo "    ./venv/bin/pip install -r requirements.txt openai"
    echo "    ./venv/bin/python -m playwright install chromium"
    exit 1
  fi
}

case "${1:---help}" in

  # ---------------- UNIVERSAL (Qwen + Notion + Figma) ----------------
  --universal)
    banner "UNIVERSAL SERVER (qwen + notion + figma as OpenAI API)"
    check_venv
    $PY universal_server.py --serve --port 8000 --api-key m2m-key
    ;;

  --capture)
    banner "FLOW CAPTURE (Notion/Figma AI endpoint MITM capture)"
    check_venv
    $PY capture_flow.py --app "${2:-notion}"
    ;;

  --login-app)
    banner "CONNECTOR LOGIN (visible browser)"
    check_venv
    $PY universal_server.py --login "${2:-notion}"
    ;;

  # ---------------- METHOD A: browser bridge (PRIMARY) ----------------
  --login)
    banner "STEP 1: One-Time Login (persistent profile)"
    check_venv
    $PY qwen_browser_bridge.py --login
    ;;

  --serve)
    banner "METHOD A: Browser Bridge Server (WAF-proof)"
    check_venv
    $PY qwen_browser_bridge.py --serve --port 8001
    ;;

  --serve-headed)
    banner "METHOD A: Bridge Server (visible browser mode)"
    check_venv
    $PY qwen_browser_bridge.py --serve --headed --port 8001
    ;;

  # ---------------- METHOD B: HTTP replay ----------------
  --serve-http)
    banner "METHOD B: HTTP Replay Server (token-based)"
    check_venv
    if ! grep -q "APNA_QWEN_TOKEN_YAHAN" config.json 2>/dev/null; then
      $PY app_to_api_server.py
    else
      echo "[!] config.json me token nahi hai."
      echo "    Token do tarike se milta hai:"
      echo "    1. --login ke baad browser_profile se (advanced)"
      echo "    2. Burp/mitm capture se Bearer copy karke config.json me daalo"
      echo "    3. Ya seedha Method A use karo (--serve) — token ki zaroorat hi nahi"
      exit 1
    fi
    ;;

  # ---------------- METHOD B2: manual MITM capture ----------------
  --mitm)
    banner "METHOD B2: Manual Capture (phone/emulator proxy)"
    check_venv
    IP=$(hostname -I | awk '{print $1}')
    echo "[*] mitmdump on :8082"
    echo "  Phone: WiFi proxy -> ${IP}:8082 | cert: http://mitm.it"
    echo "  Pinning ho toh: frida -U -f com.qwen.app -l ssl_unpin.js"
    echo "  Burp bhi chalega (same port pe rakho)"
    echo
    ./venv/bin/mitmdump -s mobile_re.py -p 8082 --set block_global=false
    echo
    $PY auto_pipeline.py && echo "[+] Ab './start.sh --serve-http'"
    ;;

  # ---------------- METHOD C: local GGUF ----------------
  --local)
    banner "METHOD C: Local GGUF Backup (offline)"
    chmod +x serve_qwen_api.sh 2>/dev/null
    ./serve_qwen_api.sh
    ;;

  # ---------------- SELF TEST ----------------
  --test)
    banner "SELF-TEST (mock chain, 8 checks)"
    check_venv
    $PY run_full_test.py
    ;;

  # ---------------- STATUS ----------------
  --status)
    banner "STATUS"
    $PY - <<'EOF'
import json, os, socket
def port(p):
    s = socket.socket(); s.settimeout(1)
    r = s.connect_ex(("127.0.0.1", p)) == 0; s.close(); return r
prof = os.path.exists(os.path.join("browser_profile", "Default"))
print(f" venv            : {'OK' if os.path.exists('venv/bin/python') else 'MISSING'}")
print(f" chromium        : {'OK' if os.path.exists(os.path.expanduser('~/.cache/ms-playwright')) else 'MISSING'}")
print(f" browser profile : {'OK (login done)' if prof else 'MISSING (./start.sh --login chalao)'}")
print(f" bridge  :8001   : {'RUNNING' if port(8001) else 'down'}")
cfg = "config.json"
if os.path.exists(cfg):
    c = json.load(open(cfg))
    tok = c.get("upstream_headers", {}).get("Authorization", "")
    ph = "PLACEHOLDER (Method B ke liye token chahiye)" if "YAHAN" in tok else f"set ({tok[:18]}...)"
    print(f" http-replay cfg : {ph}")
flag = "re_capture/token_expired.flag"
if os.path.exists(flag):
    print(" !! token_expired.flag — Method B ka token refresh karo")
EOF
    ;;

  *)
    cat <<'USAGE'
Qwen -> OpenAI-Compatible API Kit

  ./start.sh --universal  EK server: qwen+notion+figma as OpenAI API (M2M)
  ./start.sh --capture    Notion/Figma ka AI endpoint capture karo
  ./start.sh --login-app  connector login (notion/figma)
  ./start.sh --login      qwen login (browser bridge)
  ./start.sh --serve      qwen-only server
  ./start.sh --serve-http HTTP replay mode (token chahiye)
  ./start.sh --mitm       phone/Burp capture mode
  ./start.sh --local      offline local GGUF backup
  ./start.sh --test       self-test
  ./start.sh --status     status check

Quick start:
  1) ./start.sh --login    <- browser me login (Google OAuth chalega)
  2) ./start.sh --serve    <- server chalu
  3) client:

     from openai import OpenAI
     client = OpenAI(base_url="http://localhost:8001/v1", api_key="x")
     r = client.chat.completions.create(model="qwen",
         messages=[{"role":"user","content":"hello"}])
     print(r.choices[0].message.content)
USAGE
    ;;
esac
