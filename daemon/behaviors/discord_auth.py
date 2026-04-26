"""One-shot Discord OAuth setup. Run manually: uv run python -m behaviors.discord_auth"""

import asyncio
import json
import time

CLIENT_ID = "1372561907680149644"
CLIENT_SECRET = "WCVByCOz2eR7aywq4xe1RIgnMRr0Vh9i"
REDIRECT_URI = "http://localhost"
TOKEN_PATH = __import__("os").path.join(
    __import__("os").path.dirname(__import__("os").path.dirname(__file__)),
    ".discord_token",
)


async def main() -> None:
    import httpx
    import websockets

    print(f"Token will be saved to: {TOKEN_PATH}")

    ws = None
    for port in range(6463, 6473):
        try:
            ws = await asyncio.wait_for(
                websockets.connect(
                    f"ws://127.0.0.1:{port}/?v=1&encoding=json",
                    origin="https://streamkit.discord.com",
                ),
                timeout=2.0,
            )
            print(f"Connected to Discord RPC on port {port}")
            break
        except Exception:
            continue

    if ws is None:
        print("ERROR: Discord not running or RPC unavailable")
        return

    ready = json.loads(await ws.recv())
    if ready.get("evt") != "READY":
        print(f"ERROR: unexpected handshake: {ready}")
        return

    await ws.send(json.dumps({
        "cmd": "AUTHORIZE",
        "args": {
            "client_id": CLIENT_ID,
            "scopes": ["rpc", "rpc.voice.read", "rpc.voice.write"],
            "redirect_uri": REDIRECT_URI,
        },
        "nonce": "auth1",
    }))
    print("Check Discord for authorization prompt...")
    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=120.0))
    code = resp.get("data", {}).get("code")
    if not code:
        print(f"ERROR: {resp.get('data')}")
        return

    print("Exchanging code for token...")
    r = httpx.post("https://discord.com/api/oauth2/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    if r.status_code != 200:
        print(f"ERROR: token exchange {r.status_code}: {r.text}")
        return

    td = r.json()
    token = td["access_token"]

    await ws.send(json.dumps({
        "cmd": "AUTHENTICATE",
        "args": {"access_token": token},
        "nonce": "auth2",
    }))
    auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
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
    print(f"Token saved to {TOKEN_PATH}")
    print("The daemon will use this token automatically.")
    await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
