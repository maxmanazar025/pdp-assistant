#!/usr/bin/env python3
"""One-time Google OAuth helper: prints the GOOGLE_REFRESH_TOKEN for .env.

Run it on a computer that has a browser (your laptop, not the server):

    python scripts/google_auth.py --client-id XXX --client-secret YYY

It opens Google's consent page for Calendar + Tasks, receives the authorisation code on
http://127.0.0.1:8765 and exchanges it for a refresh token. Sign in as the account the
bot should manage (maxmanazar025@gmail.com). Only the standard library is used.
"""
from __future__ import annotations

import argparse
import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

SCOPES = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/tasks"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    redirect_uri = f"http://127.0.0.1:{args.port}"
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": args.client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
    })
    received: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            received.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Tayyor ✅</h2><p>Bu oynani yopib, terminalga qayting.</p>".encode())

        def log_message(self, *_: object) -> None:
            return

    print("\n1) Open this URL and sign in as the account the bot should manage:\n")
    print(url, "\n")
    webbrowser.open(url)
    with http.server.HTTPServer(("127.0.0.1", args.port), Handler) as server:
        while "code" not in received and "error" not in received:
            server.handle_request()

    if received.get("error") or received.get("state") != state:
        print("Authorisation failed:", received.get("error", "state mismatch"), file=sys.stderr)
        return 1

    body = urllib.parse.urlencode({
        "code": received["code"], "client_id": args.client_id, "client_secret": args.client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body), timeout=30) as response:  # noqa: S310
        tokens = json.load(response)
    refresh = tokens.get("refresh_token")
    if not refresh:
        print("Google returned no refresh token. Remove the app's access at "
              "https://myaccount.google.com/permissions and run this script again.", file=sys.stderr)
        return 1
    print("2) Add these lines to your .env:\n")
    print(f"GOOGLE_CLIENT_ID={args.client_id}")
    print(f"GOOGLE_CLIENT_SECRET={args.client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={refresh}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
