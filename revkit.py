#!/usr/bin/env python3
"""
revkit — Rev Kit ka MITM CLI.
================================
URL do -> browser khulta hai (persistent profile, login yaad rehta hai)
-> saare API requests capture (full MITM: headers+body+response, HTTPS
   bhi — playwright response hook) -> endpoint inventory map -> user
   intent analysis -> JSON report.

"User ne konsa manga tha?" — intent engine captured requests ko analyze
karke bata deta hai: kaunsa endpoint user-kaam (login/search/play/
generate/purchase...) karta hai, kaunsa telemetry/tracker hai.

Usage:
    revkit map https://www.netflix.com                       # full map
    revkit map https://www.netflix.com --watch 60            # 60s auto
    revkit map https://www.netflix.com --filter ai           # sirf AI-ish
    revkit report captures/<app>_map.json                    # report padho
    revkit endpoints captures/netflix_map.json              # sirf URLs
    revkit intent captures/netflix_map.json                 # intent deep
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

CAPTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
os.makedirs(CAPTURES_DIR, exist_ok=True)

# ---------------------------------------------------------------- noise --
NOISE_HOSTS = (
    "google-analytics", "googletagmanager", "doubleclick", "sentry",
    "hotjar", "segment.io", "facebook.com", "facebook.net", "mixpanel",
    "amplitude", "clarity.ms", "bat.bing", "adservice", "adsystem",
    "scorecardresearch", "quantserve", "chartbeat", "newrelic", "nrdata",
    "bugsnag", "loggly", "datadog", "fullstory", "mouseflow",
    "optimizely", "crazyegg", "yandex.ruMetrica", "branch.io", "appsflyer",
    "kochava", "adjust.com", "singular.net", "tenjin.io", "facebook",
)
NOISE_PATH_HINTS = ("telemetry", "analytics", "beacon", "pixel", "track",
                    "collect", "metric", "logging", "log", "statsd")


def is_noise(url):
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()
    if any(h in host for h in NOISE_HOSTS):
        return True
    if any(h in path for h in NOISE_PATH_HINTS):
        return True
    return False


# --------------------------------------------------------------- intent --
# user-kaam wale endpoints ke signatures (path + method + body hints)
INTENT_SIGS = [
    # (intent name, path regex, methods, body hint regex or None, weight)
    ("login",       r"sign[-_]?in|login|auth|session|oauth|password|signin", ("POST",), r"passw|email|credential|otp|token", 9),
    ("signup",      r"sign[-_]?up|register|create[-_]?account|join|invite", ("POST",), r"email|name|phone", 8),
    ("graphql",     r"graphql", ("POST",), None, 8),
    ("wall-captcha", r"captcha|recaptcha|challenge|turnstile|hcaptcha|arkose|funcaptcha|perimeterx|datadome|attestation", ("GET", "POST"), None, 5),
    ("wall-captcha", r"captcha|recaptcha|challenge|turnstile|hcaptcha|arkose|funcaptcha", ("GET",), None, 4),
    ("search",      r"search|query|find|lookup|browse|filter|titles|suggest", ("POST", "GET"), r"q=|query|term|keyword|search", 7),
    ("play",        r"play|stream|watch|video|episode|title|vod|playback|manifest|pinning|schedule", ("POST", "GET"), r"video|title|episode|id=|jwt|drm|manifest", 8),
    ("generate-ai", r"ai|assistant|chat|completion|llm|prompt|copilot|generate|inference|summar", ("POST",), r"prompt|message|input|query|instruction", 9),
    ("purchase",    r"cart|checkout|payment|order|billing|subscribe|plan|pay", ("POST",), r"card|payment|price|plan|order", 9),
    ("book",        r"book|reserve|seat|ticket|schedule|appointment|slot", ("POST",), r"date|time|seat|ticket|slot", 8),
    ("message",     r"message|inbox|dm|chat|conversation|mail|send", ("POST",), r"body|text|message|content", 7),
    ("upload",      r"upload|media|asset|file|image|photo|s3|presign", ("POST", "PUT"), r"file|upload|presign|content", 7),
    ("download",     r"download|export|pdf|report|dump", ("POST", "GET"), None, 5),
    ("vote-like",   r"like|vote|rating|reaction|favorite|heart|upvote", ("POST", "PUT"), None, 6),
    ("follow",      r"follow|friend|connect|subscribe", ("POST", "PUT"), r"user|id", 6),
    ("settings",    r"settings|profile|preferences|account|config", ("POST", "PUT", "PATCH"), r"setting|name|value", 4),
]

# asset/CDN noise — report me hide (endpoints list me rehte hain)
ASSET_RE = re.compile(
    r"\.(css|js|mjs|woff2?|ttf|otf|png|jpe?g|webp|gif|svg|ico|mp4)"
    r"($|\?)|/dnm(t)?/api/|/genc/|/ffe/|assets\.|/static/",
    re.I)


def is_asset(u):
    return bool(ASSET_RE.search(u or ""))

AI_HINTS = ("ai", "chat", "completion", "generate", "assistant",
            "prompt", "inference", "llm", "summar", "copilot", "stream")


def classify_endpoint(ep):
    """endpoint -> (intent, score, reason)"""
    url_l = (ep.get("url") or "").lower()
    method = ep.get("method", "GET")
    body = (ep.get("req_body") or "")[:4000].lower()
    ct = (ep.get("res_content_type") or "").lower()
    headers = ep.get("req_headers") or {}
    auth = any(h.lower().startswith("authorization") for h in headers)

    best = ("other", 0, f"{method} {url_l[:60]}")
    for name, path_re, methods, body_re, weight in INTENT_SIGS:
        if method not in methods:
            continue
        if not re.search(path_re, url_l):
            continue
        score = weight
        reason = f"path~{name}"
        if body_re and re.search(body_re, body):
            score += 2
            reason += "+body"
        if auth:
            score += 1
            authz = "auth"
        if "json" in ct or "stream" in ct:
            score += 1
        if score > best[1]:
            best = (name, score, reason)
    return best


def analyze(map_path):
    """captured map -> intent report: user ne konsa manga?"""
    with open(map_path) as f:
        m = json.load(f)
    eps = m.get("endpoints", [])
    print("=" * 64)
    print(f" REVKIT INTENT REPORT — {m.get('url', '?')}")
    print(f" captured: {m.get('captured_at')}, {len(eps)} endpoints")
    print("=" * 64)

    # classify sab
    scored = []
    for ep in eps:
        intent, score, reason = classify_endpoint(ep)
        scored.append((score, intent, ep))

    scored.sort(key=lambda x: -x[0])
    user_actions = [s for s in tested_sanity(scored)]
    print(f"\n[USER INTENT] {m.get('intent_summary', 'n/a')}")
    print(f"\n{'score':>5}  {'intent':<12} {url_header('METHOD URL')}")
    print("-" * 64)
    for score, intent, ep in scored:
        mark = "★" if score >= 9 else " "
        print(f"{score:>4}{mark} {intent:<12} {ep['method']:<6} {ep['url'][:70]}")
        if ep.get("req_body"):
            print(f"      body: {ep['req_body'][:90]}")
    return scored


def url_header(s):
    return s


def tested_sanity(scored):
    """generator: pehle 40 tak"""
    for s in scored[:40]:
        yield s


# ------------------------------------------------------------------ map --
def do_map(url, watch, fltr, headless=True, duration_hint=None):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[!] playwright missing: /home/kali/Rev/venv/bin/python use karo")
        sys.exit(1)

    host = urlparse(url).netloc.replace("www.", "") or "site"
    app = re.sub(r"[^a-z0-9]+", "_", host.lower())[:40]
    out_path = os.path.join(CAPTURES_DIR, f"{app}_map.json")

    captured = []
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = {runtime: {}, loadTimes: () => {}, csi: () => {}};
"""
    with sync_playwright() as p:
        profile = os.path.join(CAPTURES_DIR, f"profile_{app}")
        os.makedirs(profile, exist_ok=True)
        b = p.chromium.launch_persistent_context(
            profile, headless=headless,
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        page = b.pages[0] if b.pages else b.new_page()
        page.add_init_script(STEALTH)

        def on_response(resp):
            try:
                req = resp.request
                u = resp.url
                if is_noise(u):
                    return
                post = req.post_data or ""
                if req.method == "GET" and not post and resp.status >= 400:
                    return
                # all methods capture — full inventory chahiye
                try:
                    body = resp.text()
                except Exception:
                    body = ""
                captured.append({
                    "url": u,
                    "method": req.method,
                    "status": resp.status,
                    "req_headers": {k: v for k, v in req.headers.items()
                                    if k.lower() not in ("content-length", "host",
                                                         "accept-encoding", "connection")},
                    "req_body": post[:12000],
                    "res_content_type": resp.headers.get("content-type", ""),
                    "res_body": body[:6000],
                    "ts": time.time(),
                })
            except Exception:
                pass

        page.on("response", on_response)
        print("=" * 64)
        print(f" REVKIT MAP — {url}")
        print(f" {watch}s capture window. Browser khula hai:")
        print("  1. Site use karo (login, search, play, kuch bhi)")
        print("  2. Jo manga tha wo karo — intent engine dekhega")
        print("  3. Enter maaro")
        print("=" * 64)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[!] load: {str(e)[:80]}")
        if watch:
            print(f"[*] auto-watch {watch}s...")
            try:
                page.wait_for_timeout(watch * 1000)
            except Exception:
                pass
        else:
            try:
                input(f"\n[Enter dabao jab site ka kaam ho jaye] > ")
            except EOFError:
                page.wait_for_timeout(20000)
        b.close()

    # dedupe by (method, url-no-query)
    seen, unique = set(), []
    for c in captured:
        key = (c["method"], c["url"].split("?")[0])
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    if fltr:
        unique = [c for c in unique
                  if any(h in c["url"].lower() for h in AI_HINTS) or fltr in c["url"].lower()]

    # classify for summary
    intents = {}
    for ep in unique:
        intent, score, reason = classify_endpoint(ep)
        ep["intent"] = intent
        ep["intent_score"] = score
        intents.setdefault(intent, []).append(ep["url"])

    # top intent summary — "user ne konsa manga?"
    top = sorted(intents.items(), key=lambda kv: -len(kv[1]))
    summary = ", ".join(f"{k}({len(v)})" for k, v in top[:5])

    result = {
        "url": url,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_requests": len(captured),
        "unique_endpoints": len(unique),
        "intent_summary": summary,
        "intents": {k: v[:10] for k, v in intents.items()},
        "endpoints": unique,
    }
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n[+] {len(captured)} reqs captured, {len(unique)} unique endpoints")
    print(f"[+] INTENT SUMMARY: {summary}")
    print(f"[+] SAVED: {out_path}")
    print("[+] Report: revkit report <file>  |  endpoints: revkit endpoints <file>")
    return out_path


def do_report(map_path):
    """map -> human report (colored scores)"""
    with open(map_path) as f:
        m = json.load(f)
    print("=" * 64)
    print(f" REVKIT REPORT — {m.get('url', '?')}")
    print(f" {m.get('captured_at')} | {m.get('total_requests')} reqs -> "
          f"{m.get('unique_endpoints')} unique")
    print(f" INTENT: {m.get('intent_summary')}")
    print("=" * 64)
    actions, assets = [], 0
    for ep in m.get("endpoints", []):
        if is_asset(ep.get("url")):
            assets += 1
            continue
        # re-classify live (map ke baad SIGS update ho sakte hain)
        intent, s, _ = classify_endpoint(ep)
        ep["intent"], ep["intent_score"] = intent, s
        star = "★" if s >= 8 else " "
        print(f"{s:>4}{star} [{intent:<12}] {ep['method']:<6} "
              f"{ep['url'][:66]}")
        if ep.get("req_body"):
            print(f"       body: {str(ep['req_body'])[:80]}")
    if assets:
        print(f"     ({assets} asset/CDN endpoints hidden — endpoints cmd me hain)")


def do_endpoints(map_path, intent=None):
    with open(map_path) as f:
        m = json.load(f)
    eps = m.get("endpoints", [])
    if intent:
        eps = [e for e in filter_eps(eps, intent)]
    for ep in eps:
        print(f"{ep['method']:<6} {ep['url']}")


def filter_eps(eps, intent):
    for e in eps:
        if e.get("intent") == intent or intent.lower() in (e.get("url", "") + " " + str(e.get("req_body", ""))).lower():
            yield e


def do_proxy(port=8080, app_filter=""):
    """mitmdump proxy mode — phone/app capture (mobile_re.py addon).

    Rev Kit ka asli MITM: phone WiFi proxy is host:port pe point karo,
    mitm.it se cert install — addon endpoints+tokens+bodies capture
    karta hai re_capture/ me. Ctrl+C jab capture khatam."""
    import subprocess, sys as _sys
    addon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mobile_re.py")
    if not os.path.exists(addon):
        print(f"[!] mobile_re.py missing: {addon}")
        _sys.exit(1)
    print("=" * 64)
    print(" REVKIT PROXY MODE — phone/app MITM")
    print(f" 1. Phone WiFi proxy -> <is-host-ip>:{port}")
    print(" 2. Phone browser -> http://mitm.it (CA cert install)")
    print(" 3. App use karo — saare endpoints/tokens capture honge")
    print(" 4. Ctrl+C jab ho jaye")
    print("=" * 64)
    cmd = ["mitmdump", "-s", addon, "--listen-port", str(port)]
    if app_filter:
        os.environ["REVKIT_FILTER"] = app_filter
    try:
        subprocess.run(cmd)
    except FileNotFoundError:
        print("[!] mitmdump nahi hai — pip install mitmproxy")
        _sys.exit(1)
    # capture ke baad auto-report
    cap_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "re_capture")
    ep_file = os.path.join(cap_dir, "endpoints.json")
    if os.path.exists(ep_file):
        print(f"\n[+] captured endpoints: {ep_file}")
        print("[+] revkit report-proxy re_capture/endpoints.json  (TODO next)")
    else:
        print("[*] re_capture/ me data hai — endpoints.json bana abhi")


def do_report_proxy(cap_dir):
    """proxy capture (re_capture/) -> intent report (browser-map jaisa)."""
    ep_file = os.path.join(cap_dir, "endpoints.json")
    sess_file = os.path.join(cap_dir, "session.jsonl")
    eps = []
    if os.path.exists(ep_file):
        try:
            raw = json.load(open(ep_file))
            # mobile_re.py dict format: {"GET url": {count, statuses}}
            if isinstance(raw, dict):
                for k, v in raw.items():
                    method, _, url = k.partition(" ")
                    if url and "://" not in url:
                        url = "https://" + url
                    eps.append({"method": method or "GET", "url": url,
                                "status": (v.get("statuses") or [0])[0]})
            else:
                eps = raw
        except Exception:
            eps = []
    if not eps and os.path.exists(sess_file):
        # jsonl fallback
        seen = set()
        with open(sess_file) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                key = (r.get("method", "GET"), (r.get("url") or "").split("?")[0])
                if key in seen:
                    continue
                seen.add(key)
                eps.append(r)
    if not eps:
        print("[!] koi capture nahi — revkit proxy chalao pehle")
        sys.exit(1)
    print("=" * 64)
    print(f" REVKIT PROXY REPORT — {cap_dir}")
    print(f" {len(eps)} unique endpoints")
    print("=" * 64)
    scored = []
    for ep in eps:
        if is_asset(ep.get("url") or ""):
            continue
        intent, s, _ = classify_endpoint(ep)
        scored.append((s, intent, ep))
    scored.sort(key=lambda x: -x[0])
    for s, intent, ep in scored:
        star = "★" if s >= 8 else " "
        print(f"{s:>4}{star} [{intent:<12}] {ep.get('method','?'):<6} "
              f"{(ep.get('url') or '')[:66]}")


def main():
    ap = argparse.ArgumentParser(prog="revkit",
                                description="Rev Kit MITM CLI — URL do, endpoints + user-intent map")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("map", help="URL do -> MITM capture + intent map")
    m.add_argument("url")
    m.add_argument("--watch", type=int, default=0,
                   help="auto-capture window seconds (default interactive)")
    m.add_argument("--filter", default="",
                   help="sirf in URLs ko rakho (keyword)")
    m.add_argument("--headed", action="store_true",
                   help="visible browser (login ke liye)")

    r = sub.add_parser("report", help="map file ka human report")
    r.add_argument("file")
    r.add_argument("--intent", default="")

    e = sub.add_parser("endpoints", help="sirf endpoint list")
    e.add_argument("file")
    e.unit = None
    e.add_argument("--intent", default="")

    a = sub.add_parser("intent", help="intent deep-dive")
    a.add_argument("file")

    px = sub.add_parser("proxy", help="REAL proxy MITM — phone/app capture (mitmdump)")
    px.add_argument("--port", type=int, default=8080)
    px.add_argument("--filter", default="", help="sirf is keyword wale hosts")

    rpx = sub.add_parser("report-proxy", help="proxy capture ka intent report")
    rpx.add_argument("cap_dir", nargs="?", default="re_capture")

    args = ap.parse_args()
    if args.cmd == "map":
        do_map(args.url, args.watch, args.filter, headless=not args.headed)
    elif args.cmd == "report":
        do_report(args.file)
    elif args.cmd == "endpoints":
        do_endpoints(args.file, getattr(args, "intent", ""))
    elif args.cmd == "intent":
        do_report(args.file)
    elif args.cmd == "proxy":
        do_proxy(args.port, args.filter)
    elif args.cmd == "report-proxy":
        do_report_proxy(args.cap_dir)


if __name__ == "__main__":
    main()
