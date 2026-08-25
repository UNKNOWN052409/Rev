"""
Notion Pure-HTTP Client — ZERO browser
=======================================
1. loginWithEmail (API) -> token_v2 cookie
2. getSpaces -> workspace/space ids
3. AI endpoint probe battery -> jo live hai wahi use
4. AI chat -> SSE parse -> reply
5. connectors/notion_flow.json save (universal server ke liye)

Usage:
    ./venv/bin/python notion_pure.py --email <email> --pass <password>
    ./venv/bin/python notion_pure.py --email X --pass Y --prompt "hello"
"""

import argparse
import json
import os
import sys
import time
import uuid

from curl_cffi import requests as creq

BASE = "https://www.notion.so"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
FLOW_OUT = "connectors/notion_flow.json"

# Notion AI endpoint candidates (jo live nikle wahi use hoga)
AI_CANDIDATES = [
    "/api/v3/runInferenceTransaction",
    "/api/v3/getChatCompletion",
    "/api/v3/sendChatMessage",
    "/api/v3/getAiChatCompletion",
    "/api/v3/aiThreadChat",
    "/api/v3/createAiThread",
    "/api/v3/getAiPageQwA",
    "/api/v3/notionaiTaskComplete",
    "/api/v3/navigateToAiChat",
]


class NotionPure:
    def __init__(self):
        self.s = creq.Session(impersonate="chrome131")
        self.s.headers.update({
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Origin": BASE,
            "Referer": f"{BASE}/",
            "Accept": "application/json, text/event-stream",
        })
        self.token_v2 = None
        self.space_id = None
        self.user_id = None

    # ---------------- auth ----------------
    def login(self, email, password):
        r = self.s.post(f"{BASE}/api/v3/loginWithEmail",
                        json={"email": email, "password": password},
                        timeout=30)
        data = self._json(r)
        if r.status_code != 200 or (isinstance(data, dict)
                                    and data.get("name") in (
                                        "NotionUserIdInvalid", "APIError")):
            raise RuntimeError(f"login fail [{r.status_code}]: "
                               f"{json.dumps(data)[:200]}")
        self.token_v2 = self.s.cookies.get("token_v2")
        if not self.token_v2:
            raise RuntimeError("token_v2 cookie nahi mila")
        print(f"[+] LOGIN OK (pure HTTP, zero browser)")
        print(f"[+] token_v2: {self.token_v2[:35]}...")
        return True

    def _auth_headers(self):
        return {"Cookie": f"token_v2={self.token_v2}"}

    def get_spaces(self):
        r = self.s.post(f"{BASE}/api/v3/getSpaces",
                        headers=self._auth_headers(),
                        json={}, timeout=30)
        data = self._json(r)
        spaces = []
        try:
            for res in data.get("results", []):
                for sid, sdata in (res.get("space") or {}).items():
                    name = sdata.get("value", {}).get("name", "?")
                    spaces.append({"id": sid, "name": name})
        except Exception:
            pass
        print(f"[+] {len(spaces)} spaces: "
              f"{[s['name'] for s in spaces][:5]}")
        if spaces:
            self.space_id = spaces[0]["id"]
        return spaces

    def get_user(self):
        r = self.s.post(f"{BASE}/api/v3/getCurrent_user",
                        headers=self._auth_headers(), json={}, timeout=30)
        d = self._json(r)
        self.user_id = (d.get("results", [{}])[0].get("id")
                        if isinstance(d, dict) else None) or None
        return self.user_id

    # ---------------- AI endpoint discovery ----------------
    def probe_ai(self, prompt="hi"):
        """Har candidate ko valid auth se maar ke dekho —
        404 = dead, baaki kuch = LIVE"""
        live = []
        for path in AI_CANDIDATES:
            url = BASE + path
            try:
                body = self._ai_body(path, prompt)
                r = self.s.post(url, headers=self._auth_headers(),
                                json=body, timeout=25)
                is_html = r.text.lstrip().startswith("<!DOCTYPE")
                status = r.status_code
                tag = "DEAD" if (status == 404 or is_html) else "LIVE?"
                print(f"  {path:42s} {status} {tag}")
                if tag == "LIVE?":
                    live.append((path, status, r.text[:150]))
            except Exception as e:
                print(f"  {path:42s} ERR {str(e)[:50]}")
        return live

    def _ai_body(self, path, prompt):
        """Candidate ke hisaab se plausible body"""
        if "Inference" in path:
            return {"request": {
                "type": "text", "prompt": prompt, "spaceId": self.space_id}}
        if "Chat" in path.lower() or "chat" in path:
            return {"spaceId": self.space_id,
                    "messages": [{"role": "user", "content": prompt}]}
        if "QwA" in path:
            return {"spaceId": self.space_id, "question": prompt,
                    "pageId": None}
        return {"spaceId": self.space_id, "prompt": prompt,
                "messages": [{"role": "user", "content": prompt}]}

    # ---------------- AI chat ----------------
    def ai_chat(self, path, prompt):
        url = BASE + path
        r = self.s.post(url, headers=self._auth_headers(),
                        json=self._ai_body(path, prompt),
                        timeout=120, stream=True)
        if r.status_code != 200:
            raise RuntimeError(f"AI call fail [{r.status_code}]: "
                               f"{r.text[:200]}")
        pieces = []
        ctype = r.headers.get("content-type", "")
        if "event-stream" in ctype or "stream" in ctype:
            for line in r.iter_lines():
                if isinstance(line, bytes):
                    line = line.decode(errors="replace")
                line = line.strip()
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    p = self._parse_piece(raw)
                    if p:
                        pieces.append(p)
                        print(p, end="", flush=True)
        else:
            d = self._json(r)
            pieces.append(json.dumps(d)[:500])
        print()
        return "".join(pieces)

    @staticmethod
    def _parse_piece(raw):
        try:
            d = json.loads(raw)
        except Exception:
            return None
        # Notion SSE shapes — capture ke baad exact tune hoga
        if isinstance(d, dict):
            if "message" in d and isinstance(d["message"], str):
                return d["message"]
            ch = (d.get("choices") or [{}])[0]
            delta = ch.get("delta", {}) or {}
            if delta.get("content"):
                return delta["content"]
            for key in ("content", "text", "answer", "output"):
                v = d.get(key)
                if isinstance(v, str):
                    return v
        return None

    @staticmethod
    def _json(r):
        try:
            return r.json()
        except Exception:
            return {}

    # ---------------- flow save (universal server) ----------------
    def save_flow(self, path):
        flow = {
            "_meta": {"app": "notion", "mode": "pure-http",
                      "endpoint": BASE + path,
                      "captured_at": time.strftime("%Y-%m-%d %H:%M:%S")},
            "auth": {"type": "cookie", "cookie_name": "token_v2"},
            "url": BASE + path,
            "method": "POST",
            "space_id": self.space_id,
            "note": "token_v2 se pure HTTP replay — browser zero",
        }
        os.makedirs("connectors", exist_ok=True)
        json.dump(flow, open(FLOW_OUT, "w"), indent=2)
        print(f"[+] flow saved: {FLOW_OUT}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email")
    ap.add_argument("--pass", dest="password")
    ap.add_argument("--prompt", default="Say NOTION-OK")
    ap.add_argument("--path", default="",
                    help="AI endpoint (probe me jo live mile)")
    args = ap.parse_args()

    if not (args.email and args.password):
        ap.error("--email aur --pass do")

    n = NotionPure()
    try:
        n.login(args.email, args.password)
    except RuntimeError as e:
        print(f"[!] {e}")
        print("    (creds galat honge ya Notion ne login route badla)")
        return 1
    n.get_spaces()
    n.get_user()

    if args.path:
        live = [(args.path, 0, "")]
    else:
        print("\n[*] AI endpoint probe...")
        live = n.probe_ai()

    if not live:
        print("[!] koi AI endpoint live nahi mila — "
              "Notion ne routes badal diye honge, capture_flow.py fallback hai")
        return 1

    print(f"\n[*] AI test: {live[0][0]}")
    reply = n.ai_chat(live[0][0], args.prompt)
    print(f"REPLY: {reply[:300]}")
    n.save_flow(live[0][0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
