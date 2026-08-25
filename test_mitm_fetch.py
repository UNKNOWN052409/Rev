"""Standalone MITM fetch test — step by step prints"""
import json
import sys
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

# captured browser request se bx-umidtoken
_flow = json.load(open("captured_v2_flow.json"))
UMID = _flow["requests"][0]["headers"].get("bx-umidtoken", "")

chunks = []

def main():
    print("[1] playwright launch...", flush=True)
    with sync_playwright() as p:
        print("[2] persistent context...", flush=True)
        b = p.chromium.launch_persistent_context(
            "browser_profile", headless=True, user_agent=UA,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        print("[3] page...", flush=True)
        page = b.pages[0] if b.pages else b.new_page()
        page.add_init_script(STEALTH)
        print("[4] goto...", flush=True)
        page.goto("https://chat.qwen.ai", wait_until="domcontentloaded",
                  timeout=45000)
        print("[5] wait...", flush=True)
        page.wait_for_timeout(3000)
        print("[6] expose_function...", flush=True)
        page.expose_function(
            "pyChunk",
            lambda c: (chunks.append(c),
                       print("CHUNK:", c[:70], flush=True)))
        print("[7] chats/new evaluate...", flush=True)
        result = page.evaluate(
            """async () => {
                const withTimeout = (p, ms) =>
                    Promise.race([p, new Promise((_, rej) =>
                        setTimeout(() => rej(new Error("fetch timeout " + ms)), ms))]);
                try {
                    const r1 = await withTimeout(fetch("/api/v2/chats/new", {
                        method: "POST",
                        headers: {"Content-Type": "application/json",
                                  "Accept": "application/json, text/plain, */*",
                                  "X-Request-Id": crypto.randomUUID(),
                                  "source": "web",
                                  "version": "0.2.87",
                                  "bx-v": "2.5.37",
                                  "timezone": new Date().toString()},
                        credentials: "include",
                        body: JSON.stringify({chatId: "",
                            models: ["qwen3.7-plus"], project_id: "",
                            timestamp: Date.now(), chat_type: "t2t",
                            chat_mode: "normal"}),
                    }), 15000);
                    const j1 = await r1.json();
                    const cid = j1?.data?.id;
                    return cid ? {ok: true, cid}
                               : {error: JSON.stringify(j1).slice(0, 150)};
                } catch (e) {
                    return {error: String(e).slice(0, 150)};
                }
            }""")
        print("[8] result:", result, flush=True)

        if result.get("ok"):
            print("[9] completions evaluate...", flush=True)
            result2 = page.evaluate(
                """async (args) => {
                    const [cid, umid] = args;
                    const withTimeout = (p, ms) =>
                        Promise.race([p, new Promise((_, rej) =>
                            setTimeout(() => rej(new Error("to " + ms)), ms))]);
                    try {
                    const ts = Math.floor(Date.now() / 1000);
                    const payload = {
                        stream: true, version: "2.1",
                        incremental_output: true,
                        chatId: cid, parentId: "", chat_id: cid,
                        chat_mode: "normal", model: "qwen3.7-plus",
                        parent_id: null,
                        messages: [{id: null, fid: crypto.randomUUID(),
                            parentId: null, childrenIds: [],
                            role: "user", content: "Say MITM-OK",
                            user_action: "chat", files: [],
                            timestamp: ts, models: ["qwen3.7-plus"],
                            model: "", chat_type: "t2t",
                            feature_config: {thinking_enabled: false,
                                output_schema: "phase",
                                research_mode: "normal",
                                auto_thinking: false,
                                thinking_mode: "Auto",
                                thinking_format: "summary",
                                auto_search: false},
                            extra: {meta: {subChatType: "t2t"}},
                            sub_chat_type: "t2t", parent_id: null}],
                        timestamp: ts};
                    const r2 = await withTimeout(fetch(
                        "/api/v2/chat/completions?chat_id=" + cid, {
                        method: "POST",
                        headers: {"Content-Type": "application/json",
                                  "Accept": "application/json",
                                  "X-Request-Id": crypto.randomUUID(),
                                  "x-accel-buffering": "no",
                                  "source": "web",
                                  "version": "0.2.87",
                                  "bx-v": "2.5.37",
                                  "bx-umidtoken": umid,
                                  "timezone": new Date().toString()},
                        credentials: "include",
                        body: JSON.stringify(payload)}), 20000);
                    if (!r2.ok) return {error: "status " + r2.status};
                    const ct = r2.headers.get("content-type") || "";
                    if (ct.includes("text/html")) {
                        const t = await r2.text();
                        return {error: "WAF: " + t.slice(0, 100)};
                    }
                    const reader = r2.body.getReader();
                    const dec = new TextDecoder();
                    let buf = "", count = 0;
                    // overall stream deadline 90s
                    const deadline = Date.now() + 90000;
                    while (true) {
                        if (Date.now() > deadline)
                            return {error: "stream deadline", chunks: count};
                        const {done, value} = await Promise.race([
                            reader.read(),
                            new Promise((_, rej) => setTimeout(
                                () => rej(new Error("read stall")), 30000)),
                        ]);
                        if (done) break;
                        buf += dec.decode(value, {stream: true});
                        const lines = buf.split("\\n");
                        buf = lines.pop();
                        for (const L of lines) {
                            const t = L.trim();
                            if (t.startsWith("data:")) {
                                count++;
                                window.pyChunk(t.slice(5).trim());
                            }
                        }
                    }
                    return {ok: true, chunks: count};
                    } catch (e) {
                        return {error: String(e).slice(0, 150)};
                    }
                }""", [result["cid"], UMID])
            print("[10] result2:", result2, flush=True)
        b.close()
    print(f"[11] TOTAL CHUNKS: {len(chunks)}", flush=True)


if __name__ == "__main__":
    main()
