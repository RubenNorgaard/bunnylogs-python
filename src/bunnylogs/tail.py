"""
bunnylogs-tail — stream logs from a BunnyLogs logspace in real time.

Usage:
    bunnylogs-tail <name-or-uuid> [--endpoint URL]
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import http.client
import json
import re
import ssl
import sys
import urllib.parse
from pathlib import Path

import certifi
import websockets
import websockets.exceptions

_DEFAULT_ENDPOINT = "https://bunnylogs.com"
_AUTH_FILE = Path.home() / ".bunnylogs" / "auth"
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)
_LEVEL_COLORS = {
    "DEBUG":    "\033[2m",
    "INFO":     "\033[32m",
    "WARNING":  "\033[33m",
    "ERROR":    "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"
_USE_COLOR = sys.stdout.isatty()


# ── auth storage ──────────────────────────────────────────────────────────────

def _load_session(endpoint: str) -> str | None:
    if not _AUTH_FILE.exists():
        return None
    try:
        data = json.loads(_AUTH_FILE.read_text())
        return data.get(endpoint, {}).get("session_id")
    except Exception:
        return None


def _save_session(endpoint: str, session_id: str) -> None:
    _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if _AUTH_FILE.exists():
        try:
            data = json.loads(_AUTH_FILE.read_text())
        except Exception:
            pass
    data.setdefault(endpoint, {})["session_id"] = session_id
    _AUTH_FILE.write_text(json.dumps(data, indent=2))
    _AUTH_FILE.chmod(0o600)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _parse_endpoint(endpoint: str) -> tuple[str, str]:
    p = urllib.parse.urlparse(endpoint)
    return (p.scheme or "https"), (p.netloc or p.path)


def _make_conn(scheme: str, host: str) -> http.client.HTTPConnection:
    if scheme == "https":
        return http.client.HTTPSConnection(host, context=_SSL_CTX, timeout=10)
    return http.client.HTTPConnection(host, timeout=10)


def _extract_set_cookies(headers: list) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for name, value in headers:
        if name.lower() == "set-cookie":
            part = value.split(";")[0].strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()
    return cookies


def _login(endpoint: str, email: str, password: str) -> str | None:
    """POST credentials to allauth, return session ID or None on failure."""
    scheme, host = _parse_endpoint(endpoint)

    conn = _make_conn(scheme, host)
    conn.request("GET", "/accounts/login/")
    resp = conn.getresponse()
    resp.read()
    csrf_cookies = _extract_set_cookies(list(resp.getheaders()))
    conn.close()

    csrf = csrf_cookies.get("csrftoken", "")
    cookie_str = "; ".join(f"{k}={v}" for k, v in csrf_cookies.items())

    data = urllib.parse.urlencode({
        "csrfmiddlewaretoken": csrf,
        "login": email,
        "password": password,
    }).encode()

    conn = _make_conn(scheme, host)
    conn.request(
        "POST",
        "/accounts/login/",
        body=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(data)),
            "Cookie": cookie_str,
            "Referer": f"{endpoint}/accounts/login/",
        },
    )
    resp = conn.getresponse()
    resp.read()
    login_cookies = _extract_set_cookies(list(resp.getheaders()))
    conn.close()

    return login_cookies.get("sessionid")


def _resolve_name(endpoint: str, name: str, session_id: str) -> str | None:
    """
    Call /api/logspaces/?name=<name> and return the matching UUID.
    Returns None if the session has expired (HTTP 401).
    Exits with an error message if the name is not found.
    """
    scheme, host = _parse_endpoint(endpoint)
    path = f"/api/logspaces/?name={urllib.parse.quote(name)}"

    conn = _make_conn(scheme, host)
    conn.request("GET", path, headers={"Cookie": f"sessionid={session_id}"})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()

    if resp.status == 401:
        return None

    if resp.status != 200:
        print(f"Error: server returned HTTP {resp.status} when resolving logspace name.", file=sys.stderr)
        sys.exit(1)

    matches = json.loads(body).get("logspaces", [])

    if not matches:
        print(f"Error: no logspace named '{name}' found.", file=sys.stderr)
        sys.exit(1)

    if len(matches) == 1:
        return matches[0]["uuid"]

    print(f"Multiple logspaces named '{name}':", file=sys.stderr)
    for i, ls in enumerate(matches, 1):
        print(f"  [{i}] {ls['uuid']}  ({ls['role']})", file=sys.stderr)
    while True:
        try:
            raw = input("Pick one (number): ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(1)
        if raw.isdigit() and 1 <= int(raw) <= len(matches):
            return matches[int(raw) - 1]["uuid"]


def _prompt_login(endpoint: str) -> str:
    """Interactively prompt for credentials, log in, persist session."""
    print(f"Log in to {endpoint}:", file=sys.stderr)
    try:
        email = input("  Email: ").strip()
        password = getpass.getpass("  Password: ")
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        sys.exit(1)

    session_id = _login(endpoint, email, password)
    if not session_id:
        print("Login failed — check your credentials.", file=sys.stderr)
        sys.exit(1)

    _save_session(endpoint, session_id)
    print("Logged in.", file=sys.stderr)
    return session_id


# ── streaming ─────────────────────────────────────────────────────────────────

def _print_entry(entry: dict) -> None:
    ts = (entry.get("timestamp") or entry.get("received_at") or "")[:23]
    level = (entry.get("level") or "INFO").upper()
    program = entry.get("program") or ""
    message = entry.get("message") or ""

    if _USE_COLOR:
        color = _LEVEL_COLORS.get(level, "")
        level_col = f"{color}{level:<8}{_RESET}"
    else:
        level_col = f"{level:<8}"

    print(f"{ts}  {level_col}  {program}  {message}", flush=True)


async def _stream(ws_uri: str, session_id: str | None) -> bool:
    """
    Connect to the WebSocket and print entries as they arrive.
    Returns False when the server rejects the connection due to auth.
    """
    headers: dict[str, str] = {}
    if session_id:
        headers["Cookie"] = f"sessionid={session_id}"

    try:
        async with websockets.connect(ws_uri, additional_headers=headers) as ws:
            print("Connected. Streaming logs… (Ctrl-C to stop)\n", file=sys.stderr)
            async for raw in ws:
                _print_entry(json.loads(raw))
    except websockets.exceptions.WebSocketException as exc:
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return False
        code = getattr(exc, "code", None)
        if code is None:
            rcvd = getattr(exc, "rcvd", None)
            code = getattr(rcvd, "code", None)
        if code is not None and code >= 4000:
            return False
        raise

    return True


# ── entry point ───────────────────────────────────────────────────────────────

async def _run(args: argparse.Namespace) -> None:
    endpoint = args.endpoint.rstrip("/")
    scheme, host = _parse_endpoint(endpoint)
    ws_scheme = "wss" if scheme == "https" else "ws"
    target = args.logspace

    is_uuid = bool(_UUID_RE.match(target))
    session_id = _load_session(endpoint)

    if not is_uuid:
        if not session_id:
            print("Resolving a logspace by name requires authentication.", file=sys.stderr)
            session_id = _prompt_login(endpoint)
        uuid = _resolve_name(endpoint, target, session_id)
        if uuid is None:
            # session expired
            session_id = _prompt_login(endpoint)
            uuid = _resolve_name(endpoint, target, session_id)
            if uuid is None:
                sys.exit(1)
        target = uuid

    ws_uri = f"{ws_scheme}://{host}/ws/logs/{target}"

    ok = await _stream(ws_uri, session_id)
    if ok:
        return

    print("Authentication required.", file=sys.stderr)
    session_id = _prompt_login(endpoint)
    ok = await _stream(ws_uri, session_id)
    if not ok:
        print("Authentication failed.", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="bunnylogs-tail",
        description="Stream logs from a BunnyLogs logspace.",
    )
    parser.add_argument("logspace", help="Logspace UUID or name")
    parser.add_argument(
        "--endpoint",
        default=_DEFAULT_ENDPOINT,
        metavar="URL",
        help=f"BunnyLogs base URL (default: {_DEFAULT_ENDPOINT})",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nDisconnected.", file=sys.stderr)


if __name__ == "__main__":
    main()
