"""CLI entry point for Claude Max Proxy."""

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def cmd_auth():
    """Run OAuth PKCE authentication flow."""
    from auth import OAuthFlow

    flow = OAuthFlow()

    # Check existing tokens
    if not flow.store.is_expired():
        print("You already have valid tokens.")
        choice = input("Re-authenticate? (y/N): ").strip().lower()
        if choice != "y":
            return

    print("\n=== Claude Max Proxy — OAuth Login ===")
    print("1. Opening browser for authentication...")
    url = flow.start_login()
    print(f"   If browser didn't open, go to:\n   {url}\n")

    print("2. Log in with your Claude Pro/Max account")
    print("3. Authorize the application")
    print("4. Copy the authorization code from the page\n")

    code = input("Paste authorization code here: ").strip()
    if not code or len(code) < 10:
        print("ERROR: Invalid code.")
        sys.exit(1)

    print("\nExchanging code for tokens...")
    asyncio.run(flow.exchange_code(code))
    print("SUCCESS — tokens saved. You can now start the proxy.\n")


def cmd_status():
    """Show token status."""
    from auth import TokenStore
    store = TokenStore()
    data = store.load()
    if not data:
        print("No tokens found. Run: python run.py auth")
        return

    import time
    expires_at = data.get("expires_at", 0)
    remaining = expires_at - int(time.time())
    if remaining > 0:
        mins = remaining // 60
        print(f"Token valid — expires in {mins} minutes")
    else:
        print(f"Token EXPIRED {abs(remaining) // 60} minutes ago. Will auto-refresh on next request.")


def cmd_serve():
    """Start the proxy server."""
    from auth import TokenStore
    store = TokenStore()
    if not store.load():
        print("No tokens found. Run: python run.py auth")
        sys.exit(1)

    from config import HOST, PORT
    import uvicorn

    print(f"\n=== Claude Max Proxy ===")
    print(f"Listening on http://{HOST}:{PORT}")
    print(f"Anthropic Messages API: http://{HOST}:{PORT}/v1/messages")
    print(f"Models list: http://{HOST}:{PORT}/v1/models")
    print(f"\nFor litellm, set api_base=http://{HOST}:{PORT}/v1\n")

    uvicorn.run("server:app", host=HOST, port=PORT, log_level="info")


def main():
    if len(sys.argv) < 2:
        print("Usage: python run.py <command>")
        print("")
        print("Commands:")
        print("  auth    — Authenticate with Claude Pro/Max (OAuth)")
        print("  status  — Show token status")
        print("  serve   — Start the proxy server")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "auth":
        cmd_auth()
    elif cmd == "status":
        cmd_status()
    elif cmd == "serve":
        cmd_serve()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
