#!/usr/bin/env python3
"""Rev MCP — browser + reverse-engineering tools via MCP SSE.

Ek hi server, do faces:
  - Browser tools   : capture flows, token harvest, page-context fetch
  - Rev-engine tools: capture -> flow-template pipeline, forensics

burp-mcp wala hi pattern (FastMCP, SSE transport).
Serving/API proxy.git me hai — ye sirf capture/RE surface hai.

Run:
    python3 server.py            # :9877 SSE
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REV_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# rev-serving (adapters) bhi reachable — capture validate ke liye
PROXY_SERVING = os.path.join(
    os.path.dirname(REV_ROOT), "proxy", "rev-serving")

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("rev", host="0.0.0.0", port=9877)

STATE = {"mitm_proc": None, "mitm_port": None}


# ================================================================
# Browser tools
# ================================================================

@mcp.tool()
def browser_capture_flow(app: str, url: str = "",
                         auto_pick: bool = False) -> str:
    """Kisi bhi web app ka AI endpoint flow capture karo (browser).

    Playwright persistent-profile browser khulta hai (visible) — tum
    app me AI feature use karo, tool saare POST requests capture karke
    AI endpoint choose karta hai. Output: connectors/<app>_flow.json
    (jo proxy.git ke serving layer me replay hota hai).
    app: notion | figma | qwen | koi-bhi-nam
    """
    cmd = [sys.executable, os.path.join(REV_ROOT, "capture_flow.py"),
           "--app", app]
    if url:
        cmd += ["--url", url]
    if auto_pick:
        cmd += ["--auto"]
    if not os.environ.get("DISPLAY"):
        return ("[!] DISPLAY nahi — visible browser chahiye. "
                "Desktop pe chalao ya xvfb-run use karo.")
    r = subprocess.run(cmd, cwd=REV_ROOT, capture_output=True,
                       text=True, timeout=600)
    out = r.stdout.strip()[-1500:]
    flow = os.path.join(REV_ROOT, "connectors", f"{app}_flow.json")
    status = "SAVED" if os.path.exists(flow) else "FAIL"
    return f"[capture {app}] exit={r.returncode} flow={status}\n{out}"


@mcp.tool()
def browser_harvest_token(app: str = "qwen") -> str:
    """Browser se login token harvest karo (persistent profile).

    app=qwen: chat.qwen.ai kholta hai, guest/logged-in session se
    localStorage token + bx-umidcookie nikaal ke qwen_token.json
    banata hai (proxy serving isi ko padhta hai).
    """
    if app != "qwen":
        return f"[!] abhi sirf qwen supported (mila: {app})"
    out_path = os.path.join(REV_ROOT, "qwen_token.json")
    r = subprocess.run(
        [sys.executable,
         os.path.join(REV_ROOT, "qwen_token_harvest.py")],
        cwd=REV_ROOT, capture_output=True, text=True, timeout=300)
    ok = os.path.exists(out_path)
    return (f"[harvest {app}] exit={r.returncode} "
            f"token={'SAVED' if ok else 'FAIL'}\n"
            f"{r.stdout.strip()[-800:]}")


@mcp.tool()
def mitm_start(port: int = 8082) -> str:
    """mitmdump capture proxy start karo (phone/external MITM).

    Phone ka WiFi proxy is port pe point karo. HTTPS apps ke liye
    phone me mitmproxy CA install karo (http://mitm.it).
    Captures re_capture/session.jsonl me jaate hain.
    """
    if STATE["mitm_proc"] and STATE["mitm_proc"].poll() is None:
        return f"[info] already running on :{STATE['mitm_port']}"
    log = open(os.path.join(REV_ROOT, "mitm.log"), "ab")
    p = subprocess.Popen(
        ["mitmdump", "-s", os.path.join(REV_ROOT, "mobile_re.py"),
         "-p", str(port), "--set", "block_global=false"],
        cwd=REV_ROOT, stdout=log, stderr=log)
    STATE["mitm_proc"] = p
    STATE["mitm_port"] = port
    time.sleep(1.5)
    if p.poll() is None:
        return (f"[ok] mitmdump RUNNING on :{port}\n"
                f"Phone proxy -> <this-ip>:{port} | cert: http://mitm.it")
    return "[!] start fail — log: mitm.log"


@mcp.tool()
def mitm_stop() -> str:
    """Capture proxy band karo."""
    if not STATE["mitm_proc"] or STATE["mitm_proc"].poll() is not None:
        return "[info] pehle se band hai"
    STATE["mitm_proc"].terminate()
    STATE["mitm_proc"] = None
    return "[ok] stopped"


@mcp.tool()
def mitm_status() -> str:
    """Proxy + capture store ka status."""
    running = bool(STATE["mitm_proc"]
                   and STATE["mitm_proc"].poll() is None)
    sess = os.path.join(REV_ROOT, "re_capture", "session.jsonl")
    n = 0
    if os.path.exists(sess):
        with open(sess, encoding="utf-8") as f:
            n = sum(1 for _ in f)
    return f"proxy={'RUNNING :' + str(STATE['mitm_port']) if running else 'stopped'} | captured_flows={n}"


# ================================================================
# Rev-engine tools (capture -> flow-template -> serving handoff)
# ================================================================

@mcp.tool()
def rev_analyze_capture() -> str:
    """Captured session (re_capture/) se AI endpoint + auth headers +
    body template + model IDs auto-extract karo.

    Output: re_capture/endpoints.json + flow summary text.
    """
    r = subprocess.run(
        [sys.executable, os.path.join(REV_ROOT, "auto_pipeline.py")],
        cwd=REV_ROOT, capture_output=True, text=True, timeout=300)
    ep = os.path.join(REV_ROOT, "re_capture", "endpoints.json")
    summary = ""
    if os.path.exists(ep):
        summary = open(ep, encoding="utf-8").read()[:1200]
    return f"[analyze] exit={r.returncode}\n{r.stdout.strip()[-600:]}\n{summary}"


@mcp.tool()
def rev_list_connectors() -> str:
    """Capture state ki inventory — kaunsa flow/token kis app ka hai,
    proxy serving me kya plugged hai."""
    rows = []
    conn_dir = os.path.join(REV_ROOT, "connectors")
    for f in sorted(os.listdir(conn_dir)) if os.path.isdir(conn_dir) else []:
        if f.endswith("_flow.json") or f == "ds_chat_capture.json":
            rows.append(f"flow   : connectors/{f}")
    for f in ("qwen_token.json", "notion_token_v2.txt"):
        p = os.path.join(REV_ROOT, f)
        if os.path.exists(p):
            rows.append(f"token  : {f} (age "
                        f"{int(time.time() - os.path.getmtime(p))}s)")
    serv = os.path.join(PROXY_SERVING, "qwen_token.json")
    rows.append(f"serving: proxy/rev-serving qwen_token "
                f"{'PLUGGED' if os.path.exists(serv) else 'missing'}")
    return "\n".join(rows) or "[!] kuch nahi mila"


@mcp.tool()
def rev_handoff_to_serving() -> str:
    """Captured tokens/flows ko proxy.git ke rev-serving/ me copy karo.

    Ye Rev (capture) -> proxy (serve) handoff hai: qwen_token.json,
    notion_token_v2.txt, connectors/*_flow.json, ds_chat_capture.json.
    """
    import shutil
    moved = []
    os.makedirs(os.path.join(PROXY_SERVING, "connectors"),
                exist_ok=True)
    pairs = [
        ("qwen_token.json", "qwen_token.json"),
        ("notion_token_v2.txt", "notion_token_v2.txt"),
    ]
    conn_dir = os.path.join(REV_ROOT, "connectors")
    for f in os.listdir(conn_dir) if os.path.isdir(conn_dir) else []:
        if f.endswith("_flow.json") or f == "ds_chat_capture.json":
            pairs.append((os.path.join("connectors", f),
                          os.path.join("connectors", f)))
    for src_rel, dst_rel in pairs:
        src = os.path.join(REV_ROOT, src_rel)
        dst = os.path.join(PROXY_SERVING, dst_rel)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            moved.append(dst_rel)
    return "[ok] handoff done:\n  " + "\n  ".join(moved) if moved \
        else "[!] copy karne ko kuch nahi mila"


@mcp.tool()
def rev_forensics_sniff(prompt: str) -> str:
    """Tokenizer forensics — prompt ka per-model token count estimate
    (api_forensics_v2 fingerprinting). Kabhi kaam aata hai jab pata
    karna ho ki reply kis model se aaya."""
    r = subprocess.run(
        [sys.executable, os.path.join(REV_ROOT, "api_forensics_v2.py"),
         "--prompt", prompt, "--claim", "unknown", "--hypotheses",
         "qwen2.5-72b,gpt-4o"],
        cwd=REV_ROOT, capture_output=True, text=True, timeout=120)
    return r.stdout.strip()[-1200:] or r.stderr.strip()[-400:]


# ================================================================
# Server
# ================================================================

if __name__ == "__main__":
    print("[rev-mcp] browser + rev-engine tools on :9877 (SSE)",
          file=sys.stderr)
    mcp.run(transport="sse")
