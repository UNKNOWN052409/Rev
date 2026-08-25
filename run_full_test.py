"""
Full-chain test harness — ek hi process me:
  mock upstream + adapter spawn -> saare tests chalao -> teardown

Usage: ./venv/bin/python run_full_test.py
"""

import json
import socket
import subprocess
import sys
import time
import urllib.request
import os

PORT_MOCK = 9999
PORT_ADAP = 8001
PY = "./venv/bin/python"


def wait_port(port, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = socket.socket()
        s.settimeout(1)
        ok = s.connect_ex(("127.0.0.1", port)) == 0
        s.close()
        if ok:
            return True
        time.sleep(0.4)
    return False


def post(port, path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    # stale servers agar kuch port pakde hue hain toh unhe maar do
    for pat in ("mock_qwen_upstream", "app_to_api_server"):
        subprocess.run(["pkill", "-f", f"{PY} {pat}"], capture_output=True)
    time.sleep(1)
    for f in ("re_capture/token_expired.flag",):
        if os.path.exists(f):
            os.remove(f)

    procs = []
    results = []

    def check(name, cond, extra=""):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")

    try:
        # config.json ko MOCK pe swap karo (real config preserve)
        real_cfg = json.load(open("config.json")) if os.path.exists("config.json") else None
        mock_cfg = {
            "upstream_url": f"http://127.0.0.1:{PORT_MOCK}/api/chat/completions",
            "upstream_headers": {
                "Authorization": "Bearer TESTTOKEN_valid_abc123",
                "Content-Type": "application/json",
                "User-Agent": "test-agent",
            },
            "body_template": {"model": "${MODEL}", "messages": "${MESSAGES}",
                              "stream": True},
            "model_map": {"qwen": "qwen-max-latest"},
        }
        json.dump(mock_cfg, open("config.json", "w"), indent=2)

        print("[*] starting mock upstream :9999 ...")
        procs.append(subprocess.Popen(
            [PY, "mock_qwen_upstream.py"],
            stdout=open("logs_mock.log", "w"), stderr=subprocess.STDOUT))
        check("mock upstream up", wait_port(PORT_MOCK))

        print("[*] starting adapter :8001 ...")
        procs.append(subprocess.Popen(
            [PY, "app_to_api_server.py"],
            stdout=open("logs_adapter.log", "w"), stderr=subprocess.STDOUT))
        check("adapter up", wait_port(PORT_ADAP))

        print("\n[*] functional tests:")

        st, body = post(PORT_ADAP, "/v1/chat/completions",
                        {"model": "qwen",
                         "messages": [{"role": "user", "content": "hello"}],
                         "stream": False})
        data = json.loads(body)
        check("non-stream completion", st == 200 and
              "Mock reply" in data["choices"][0]["message"]["content"])

        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT_ADAP}/v1/chat/completions",
            data=json.dumps({"model": "qwen",
                             "messages": [{"role": "user", "content": "x"}],
                             "stream": True}).encode(),
            headers={"Content-Type": "application/json"})
        raw = urllib.request.urlopen(req, timeout=30).read().decode()
        chunks = [l for l in raw.splitlines() if l.startswith("data: {")]
        check("stream SSE chunks", len(chunks) >= 3 and "data: [DONE]" in raw,
              f"({len(chunks)} chunks)")

        st, body = post(PORT_ADAP, "/v1/chat/completions",
                        {"model": "qwen",
                         "messages": [{"role": "user",
                                       "content": "Delhi ka weather?"}],
                         "stream": False,
                         "tools": [{"type": "function", "function": {
                             "name": "get_weather", "description": "w",
                             "parameters": {"type": "object", "properties": {
                                 "city": {"type": "string"}}}}}]})
        tc = json.loads(body)["choices"][0]["message"].get("tool_calls")
        check("tool-call shim", st == 200 and tc and
              tc[0]["function"]["name"] == "get_weather")

        st = 0
        try:
            r = urllib.request.urlopen(
                f"http://127.0.0.1:{PORT_ADAP}/v1/models", timeout=10)
            st, body = r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            body = e.read().decode()
        models = json.loads(body)
        check("/v1/models", st == 200 and
              any(m["id"] == "qwen" for m in models.get("data", [])))

        # ---- 401 watchdog path ----
        cfg = json.load(open("config.json"))
        real_token = cfg["upstream_headers"]["Authorization"]
        cfg["upstream_headers"]["Authorization"] = "BROKEN"
        json.dump(cfg, open("config.json", "w"), indent=2)

        # sirf adapter restart karo — mock upstream zinda rehna chahiye
        for p in procs:
            if "app_to_api_server" in str(p.args):
                p.terminate()
        time.sleep(1.5)
        procs = [p for p in procs if "app_to_api_server" not in str(p.args)]
        procs.append(subprocess.Popen([PY, "app_to_api_server.py"],
                                      stdout=open("logs_adapter.log", "w"),
                                      stderr=subprocess.STDOUT))
        # purana wala port chhodne me time lag sakta hai
        time.sleep(3)
        check("adapter restart (bad token)", wait_port(PORT_ADAP))

        st, body = post(PORT_ADAP, "/v1/chat/completions",
                        {"model": "qwen",
                         "messages": [{"role": "user", "content": "hi"}],
                         "stream": False})
        flag_exists = os.path.exists("re_capture/token_expired.flag")
        check("401 propagates + flag written", st == 401 and flag_exists)

        # restore
        cfg["upstream_headers"]["Authorization"] = real_token
        json.dump(cfg, open("config.json", "w"), indent=2)

    finally:
        for p in procs:
            p.terminate()
        time.sleep(0.5)
        for p in procs:
            p.kill()
        # real config wapas
        if real_cfg is not None:
            json.dump(real_cfg, open("config.json", "w"), indent=2)
            print("[*] real config.json restored")
        # test flag cleanup
        if os.path.exists("re_capture/token_expired.flag"):
            os.remove("re_capture/token_expired.flag")

    print("\n" + "=" * 46)
    passed = sum(1 for _, ok in results if ok)
    print(f" RESULT: {passed}/{len(results)} tests passed")
    print("=" * 46)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
