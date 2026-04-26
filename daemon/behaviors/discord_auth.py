"""One-shot Discord OAuth setup via IPC. Run: uv run python -m behaviors.discord_auth"""

import json
import struct
import time
import os

CLIENT_ID = "1372561907680149644"
CLIENT_SECRET = "WCVByCOz2eR7aywq4xe1RIgnMRr0Vh9i"
REDIRECT_URI = "http://localhost"
TOKEN_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".discord_token")
PIPES = ["\\\\.\\pipe\\discord-ipc-" + str(i) for i in range(10)]

OP_HANDSHAKE = 0
OP_FRAME = 1


def _send(h, payload: dict, opcode: int = OP_FRAME) -> None:
    import win32file
    raw = json.dumps(payload).encode("utf-8")
    win32file.WriteFile(h, struct.pack("<II", opcode, len(raw)) + raw)


def _recv(h) -> dict:
    import win32file
    _, header = win32file.ReadFile(h, 8)
    _op, length = struct.unpack("<II", header)
    _, data = win32file.ReadFile(h, length)
    return json.loads(data.decode("utf-8"))


def main() -> None:
    import httpx
    import win32file

    print(f"Token will be saved to: {TOKEN_PATH}")

    h = None
    for path in PIPES:
        try:
            h = win32file.CreateFile(
                path,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
            print(f"Connected to {path}")
            break
        except Exception:
            continue

    if h is None:
        print("ERROR: Discord not running or IPC unavailable")
        return

    try:
        _send(h, {"v": 1, "client_id": CLIENT_ID}, opcode=OP_HANDSHAKE)
        ready = _recv(h)
        if ready.get("evt") != "READY":
            print(f"ERROR: unexpected handshake: {ready}")
            return
        print("Handshake OK")

        _send(h, {
            "cmd": "AUTHORIZE",
            "args": {
                "client_id": CLIENT_ID,
                "scopes": ["rpc", "rpc.voice.read", "rpc.voice.write"],
            },
            "nonce": "auth1",
        })
        print("Check Discord for authorization prompt...")
        resp = _recv(h)
        print(f"AUTHORIZE response: {json.dumps(resp, indent=2)[:500]}")
        code = resp.get("data", {}).get("code")
        if not code or isinstance(code, int):
            print(f"ERROR: no auth code in response")
            return
        code = str(code)

        print(f"Got code: {code[:8]}... (len={len(code)})")
        print("Exchanging code for token...")
        r = httpx.post("https://discord.com/api/oauth2/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        })
        print(f"Exchange status: {r.status_code}")
        if r.status_code != 200:
            print(f"ERROR: token exchange {r.status_code}: {r.text}")
            return

        td = r.json()
        token = td["access_token"]

        _send(h, {
            "cmd": "AUTHENTICATE",
            "args": {"access_token": token},
            "nonce": "auth2",
        })
        auth = _recv(h)
        if auth.get("evt") == "ERROR":
            print(f"ERROR: {auth.get('data')}")
            return

        user = auth.get("data", {}).get("user", {})
        print(f"Authenticated as {user.get('username')}")

        with open(TOKEN_PATH, "w") as f:
            json.dump({
                "access_token": token,
                "refresh_token": td.get("refresh_token", ""),
                "expires_at": time.time() + td.get("expires_in", 604800),
            }, f)
        print(f"Token saved. The daemon will pick it up automatically.")
    finally:
        win32file.CloseHandle(h)


if __name__ == "__main__":
    main()
