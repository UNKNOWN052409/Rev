#!/usr/bin/env python3
"""
Rev CLI — M2M AI Bridge Command Line Interface
================================================
Research-grade CLI for the Rev universal AI bridge.

Usage:
  rev login <provider> [--email X --pass Y | --cookies FILE | --google]
  rev chat <provider> "message" [--model M] [--stream]
  rev serve [--port 8000] [--api-key KEY]
  rev search "query" [--limit N]
  rev scrape <url> [--format markdown]
  rev models
  rev status
  rev token <provider> [--refresh]
  rev revoke <provider>

Examples:
  rev login qwen --email user@x.com --pass secret
  rev login notion --cookies notion_cookies.json
  rev chat qwen "write a python function" --model qwen3.8-max
  rev search "latest AI research papers" --limit 5
  rev scrape https://example.com --format markdown
  rev serve --port 8000
"""

import argparse
import json
import os
import sys

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rev_auth import get_store, GoogleOAuthCapture


def cmd_login(args):
    """Login to a provider (email/pass, cookies, or Google OAuth)."""
    provider = args.provider
    store = get_store()

    if args.google:
        # Google OAuth flow
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            print("[!] Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET env vars")
            print("    Get them from: https://console.cloud.google.com/apis/credentials")
            return 1
        oauth = GoogleOAuthCapture(client_id, client_secret)
        auth_url = oauth.get_auth_url()
        print(f"\n[Google OAuth] Open this URL in your browser:\n")
        print(f"  {auth_url}\n")
        print("  After authorizing, you'll be redirected to localhost:8085/callback")
        print("  Copy the 'code' parameter from the redirect URL.\n")
        code = input("  Enter the authorization code: ").strip()
        if not code:
            print("[!] No code provided")
            return 1
        tokens = oauth.exchange_code(code)
        if "access_token" in tokens:
            store.set_token(
                provider,
                access_token=tokens["access_token"],
                refresh_token=tokens.get("refresh_token"),
                expires_in=tokens.get("expires_in"),
                scope=tokens.get("scope"),
                metadata={"auth_method": "google_oauth"},
            )
            print(f"[+] {provider}: Google OAuth token saved!")
            return 0
        else:
            print(f"[!] Token exchange failed: {tokens}")
            return 1

    elif args.cookies:
        # Cookie-based auth
        cookies_path = args.cookies
        if not os.path.exists(cookies_path):
            print(f"[!] Cookie file not found: {cookies_path}")
            return 1
        with open(cookies_path) as f:
            cookies_data = f.read().strip()
        # Try to parse as JSON (cookie export format)
        try:
            parsed = json.loads(cookies_data)
            if isinstance(parsed, list):
                # Cookie editor export format
                cookie_str = "; ".join(
                    f"{c['name']}={c['value']}" for c in parsed
                    if isinstance(c, dict) and "name" in c)
            elif isinstance(parsed, dict):
                cookie_str = "; ".join(f"{k}={v}" for k, v in parsed.items())
            else:
                cookie_str = cookies_data
        except json.JSONDecodeError:
            cookie_str = cookies_data
        store.set_cookies(provider, cookie_str)
        # Also try to extract token from cookies
        import re
        token_match = re.search(r'token=([^;]+)', cookie_str)
        if token_match:
            store.set_token(provider, token_match.group(1),
                            metadata={"auth_method": "cookies"})
        print(f"[+] {provider}: cookies saved ({len(cookie_str)} chars)")
        return 0

    elif args.email and args.password:
        # Email/password auth — delegate to provider-specific login
        print(f"[*] {provider}: logging in with email/password...")
        print(f"    Email: {args.email}")
        # This would trigger the browser-based login flow
        # For now, store the credentials for the connector to use
        store.set_token(
            provider,
            access_token="",  # Will be filled by connector
            metadata={
                "auth_method": "email_password",
                "email": args.email,
                "password": args.password,
            },
        )
        print(f"[+] {provider}: credentials stored. Run 'rev chat {provider} ...' to trigger login.")
        return 0

    else:
        print("[!] Provide --email/--pass, --cookies FILE, or --google")
        return 1


def cmd_chat(args):
    """Send a chat message to a provider."""
    provider = args.provider
    message = args.message
    model = args.model or provider
    stream = args.stream

    # Import connector
    from universal_bridge import CONNECTOR_CLASSES
    if provider not in CONNECTOR_CLASSES:
        print(f"[!] Unknown provider: {provider}")
        print(f"    Available: {list(CONNECTOR_CLASSES.keys())}")
        return 1

    connector_cls = CONNECTOR_CLASSES[provider]
    connector = connector_cls()
    connector.start()

    if not connector.is_logged_in():
        print(f"[!] {provider}: not logged in. Run 'rev login {provider}' first.")
        return 1

    messages = [{"role": "user", "content": message}]

    if stream:
        def on_chunk(piece):
            print(piece, end="", flush=True)
        try:
            result = connector.chat(messages, model=model, stream_cb=on_chunk)
            print()  # newline after stream
        except Exception as e:
            print(f"\n[!] Error: {e}")
            return 1
    else:
        try:
            result = connector.chat(messages, model=model)
            print(result)
        except Exception as e:
            print(f"[!] Error: {e}")
            return 1

    return 0


def cmd_serve(args):
    """Start the universal M2M server."""
    from universal_server import main as server_main
    sys.argv = ["universal_server.py", "--serve",
                "--port", str(args.port),
                "--api-key", args.api_key]
    server_main()


def cmd_search(args):
    """Web search via Firecrawl."""
    from rev_firecrawl import get_firecrawl
    fc = get_firecrawl()

    if not fc.health():
        print("[!] Firecrawl not running. Start it with:")
        print("    docker compose up -d")
        print("    (or set FIRECRAWL_URL env var)")
        return 1

    query = args.query
    limit = args.limit

    print(f"[*] Searching: {query}")
    results = fc.search(query, limit=limit)

    if not results:
        print("[!] No results found")
        return 0

    for i, r in enumerate(results, 1):
        print(f"\n[{i}] {r['title']}")
        print(f"    {r['url']}")
        if r.get("description"):
            print(f"    {r['description'][:150]}")
        if args.content and r.get("content"):
            print(f"    ---")
            print(f"    {r['content'][:500]}")

    return 0


def cmd_scrape(args):
    """Scrape a URL via Firecrawl."""
    from rev_firecrawl import get_firecrawl
    fc = get_firecrawl()

    if not fc.health():
        print("[!] Firecrawl not running. Start it with: docker compose up -d")
        return 1

    url = args.url
    fmt = args.format

    print(f"[*] Scraping: {url}")
    result = fc.scrape(url, formats=[fmt])

    if fmt == "markdown":
        print(result.get("markdown", ""))
    elif fmt == "html":
        print(result.get("html", ""))
    else:
        print(json.dumps(result, indent=2))

    return 0


def cmd_models(args):
    """List available models."""
    from universal_bridge import CONNECTOR_CLASSES
    print("Available providers and models:\n")
    for name, cls in CONNECTOR_CLASSES.items():
        aliases = getattr(cls, "MODEL_ALIASES", {})
        print(f"  {name}:")
        for alias, real in aliases.items():
            print(f"    {alias:25s} → {real}")
        print()


def cmd_status(args):
    """Show auth status for all providers."""
    store = get_store()
    providers = store.list_providers()

    if not providers:
        print("[!] No providers configured. Run 'rev login <provider>' first.")
        return 0

    print("Provider Status:\n")
    for p in providers:
        info = store.info(p)
        status = "✓ authenticated" if not info.get("expired") else "✗ expired"
        print(f"  {p:12s} {status}")
        print(f"    token: {info.get('token_preview', 'N/A')}")
        print(f"    method: {info.get('metadata', {}).get('auth_method', 'unknown')}")
        if info.get("expires_at"):
            import time
            remaining = info["expires_at"] - time.time()
            if remaining > 0:
                print(f"    expires: {int(remaining)}s")
        print()

    # Firecrawl status
    from rev_firecrawl import get_firecrawl
    fc = get_firecrawl()
    fc_status = "✓ running" if fc.health() else "✗ not running"
    print(f"  {'firecrawl':12s} {fc_status}")

    return 0


def cmd_token(args):
    """Show or refresh a provider's token."""
    store = get_store()
    provider = args.provider

    if args.refresh:
        print(f"[*] Refreshing {provider} token...")
        # Trigger re-login
        print("[!] Auto-refresh not yet implemented for this provider.")
        print(f"    Run 'rev login {provider}' to re-authenticate.")
        return 1

    token = store.get_token(provider)
    if token:
        print(token)
        return 0
    else:
        print(f"[!] No token for {provider}")
        return 1


def cmd_revoke(args):
    """Revoke a provider's token."""
    store = get_store()
    store.revoke(args.provider)
    print(f"[+] {args.provider}: token revoked")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="rev",
        description="Rev — M2M AI Bridge CLI (research-grade)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  rev login qwen --email user@x.com --pass secret
  rev login notion --cookies cookies.json
  rev chat qwen "hello" --model qwen3.8-max --stream
  rev search "AI research papers" --limit 5
  rev scrape https://example.com
  rev serve --port 8000
  rev status
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # login
    p_login = subparsers.add_parser("login", help="Login to a provider")
    p_login.add_argument("provider", help="Provider name (qwen, notion, deepseek)")
    p_login.add_argument("--email", help="Email address")
    p_login.add_argument("--pass", dest="password", help="Password")
    p_login.add_argument("--cookies", help="Path to cookies file (JSON)")
    p_login.add_argument("--google", action="store_true", help="Use Google OAuth")
    p_login.set_defaults(func=cmd_login)

    # chat
    p_chat = subparsers.add_parser("chat", help="Send a chat message")
    p_chat.add_argument("provider", help="Provider name")
    p_chat.add_argument("message", help="Message to send")
    p_chat.add_argument("--model", help="Model to use")
    p_chat.add_argument("--stream", action="store_true", help="Stream output")
    p_chat.set_defaults(func=cmd_chat)

    # serve
    p_serve = subparsers.add_parser("serve", help="Start M2M server")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--api-key", default="m2m-key")
    p_serve.set_defaults(func=cmd_serve)

    # search
    p_search = subparsers.add_parser("search", help="Web search (Firecrawl)")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=5)
    p_search.add_argument("--content", action="store_true",
                          help="Show page content")
    p_search.set_defaults(func=cmd_search)

    # scrape
    p_scrape = subparsers.add_parser("scrape", help="Scrape a URL (Firecrawl)")
    p_scrape.add_argument("url", help="URL to scrape")
    p_scrape.add_argument("--format", default="markdown",
                          choices=["markdown", "html", "rawHtml"])
    p_scrape.set_defaults(func=cmd_scrape)

    # models
    p_models = subparsers.add_parser("models", help="List available models")
    p_models.set_defaults(func=cmd_models)

    # status
    p_status = subparsers.add_parser("status", help="Show auth status")
    p_status.set_defaults(func=cmd_status)

    # token
    p_token = subparsers.add_parser("token", help="Show/refresh token")
    p_token.add_argument("provider")
    p_token.add_argument("--refresh", action="store_true")
    p_token.set_defaults(func=cmd_token)

    # revoke
    p_revoke = subparsers.add_parser("revoke", help="Revoke token")
    p_revoke.add_argument("provider")
    p_revoke.set_defaults(func=cmd_revoke)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
