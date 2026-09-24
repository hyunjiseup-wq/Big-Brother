"""Read-only real Gateway reconnect probe. Does not import or start the moderation bot."""
import asyncio
import contextlib
import json
import os
import time

import discord
from dotenv import load_dotenv

import runtime_lock


class ReconnectProbe(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.initial_ready = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.recovered = asyncio.Event()
        self.injected = False
        self.recovery_event = None

    async def on_ready(self):
        if self.injected:
            self.recovery_event = "READY"
            self.recovered.set()
        self.initial_ready.set()

    async def on_disconnect(self):
        if self.injected:
            self.disconnected.set()

    async def on_resumed(self):
        if self.injected:
            self.recovery_event = "RESUMED"
            self.recovered.set()


async def run_probe(token: str) -> dict:
    client = ReconnectProbe()
    task = asyncio.create_task(client.start(token, reconnect=True))
    async def wait_event(event):
        waiter = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait((task, waiter), timeout=45,
                                         return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
                raise RuntimeError("Gateway client exited early")
            if waiter not in done:
                raise TimeoutError("Gateway event timed out")
        finally:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
    try:
        await wait_event(client.initial_ready)
        before = await client.fetch_user(client.user.id)
        initial_socket = client.ws
        client.injected = True
        start = time.monotonic()
        # A real socket close on this diagnostic session only; no firewall/PC network changes.
        await asyncio.wait_for(initial_socket.close(code=4000), timeout=10)
        await wait_event(client.disconnected)
        await wait_event(client.recovered)
        await asyncio.wait_for(client.wait_until_ready(), timeout=10)
        after = await client.fetch_user(client.user.id)
        if client.ws is initial_socket or before.id != after.id:
            raise RuntimeError("Reconnect identity/socket verification failed")
        return {"ok": True, "initial_ready": True, "disconnect_observed": True,
                "recovery_event": client.recovery_event, "new_websocket": True,
                "rest_identity_verified": True, "seconds": round(time.monotonic() - start, 3),
                "discord_py": discord.__version__, "messages_sent": 0,
                "moderation_bot_started": False}
    finally:
        await client.close()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def main():
    load_dotenv()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print('[FAIL] DISCORD_BOT_TOKEN missing')
        return 1
    try:
        lock = runtime_lock.acquire_instance_lock()
    except OSError:
        print('[FAIL] Bot instance lock occupied; diagnostic refused')
        return 1
    try:
        print(json.dumps(asyncio.run(run_probe(token)), ensure_ascii=False))
        return 0
    except Exception as error:
        # Never print credentials, request headers, gateway session IDs or raw exceptions.
        print(json.dumps({"ok": False, "error_type": type(error).__name__}))
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
