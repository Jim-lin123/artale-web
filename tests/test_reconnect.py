"""Run with: python -m unittest discover -s tests -v

Uses a local server with a one-second grace period. Never calls production.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import httpx
import websockets
import server


class ReconnectTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.ws_base = f"ws://127.0.0.1:{port}/ws"
        cls.log = tempfile.TemporaryFile()
        cls.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "RECONNECT_GRACE_SECONDS": "1"},
            stdout=cls.log, stderr=cls.log,
        )
        for _ in range(100):
            try:
                if httpx.get(cls.base + "/healthz", timeout=0.2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        cls.process.terminate()
        cls.log.seek(0)
        raise RuntimeError(cls.log.read().decode())

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=10)
        cls.log.close()

    async def asyncSetUp(self):
        self.http = httpx.AsyncClient(base_url=self.base)
        self.sockets = []
        await self.admin_post("/admin-kick-all")

    async def asyncTearDown(self):
        for ws in self.sockets:
            await ws.close()
        await self.admin_post("/admin-kick-all")
        await self.http.aclose()

    async def admin_post(self, path, **params):
        r = await self.http.post(path, params={"key": server.ADMIN_PASSWORD, **params})
        r.raise_for_status()
        return r.json()

    async def admin(self):
        r = await self.http.get("/admin-data", params={"key": server.ADMIN_PASSWORD})
        r.raise_for_status()
        return r.json()

    async def connect(self, cid=None):
        cid = cid or "test_" + uuid.uuid4().hex[:10]
        ws = await websockets.connect(f"{self.ws_base}?p={server.SITE_PASSWORD}&cid={cid}")
        self.sockets.append(ws)
        return ws, cid

    async def send(self, ws, **data):
        await ws.send(json.dumps(data))

    async def receive(self, ws, kind):
        async with asyncio.timeout(3):
            while True:
                data = json.loads(await ws.recv())
                if data["type"] == kind:
                    return data

    async def create(self):
        ws, cid = await self.connect()
        await self.send(ws, action="create_room", name="Test host", color="#123456")
        joined = await self.receive(ws, "joined")
        await self.receive(ws, "room_update")
        return ws, cid, joined["room_id"]

    async def join(self, room_id, cid=None):
        ws, cid = await self.connect(cid)
        await self.send(ws, action="join_room", room_id=room_id, name="Test player", color="#abcdef")
        await self.receive(ws, "joined")
        state = await self.receive(ws, "room_update")
        return ws, cid, state

    async def test_disconnect_rejoin_preserves_last_player_room_and_grid(self):
        ws, cid, room_id = await self.create()
        await self.send(ws, action="click_cell", row=2, col=1)
        await self.receive(ws, "room_update")
        await ws.close()
        await asyncio.sleep(0.05)
        data = await self.admin()
        self.assertEqual(data["total_online"], 0)
        self.assertEqual(data["room_count"], 1)
        self.assertFalse(data["rooms"][0]["players"][0]["connected"])
        self.assertIsNotNone(data["rooms"][0]["players"][0]["reconnect_until"])
        new_ws, _, state = await self.join(room_id, cid)
        self.assertEqual(state["grid"][2][1]["client_id"], cid)
        self.assertEqual(state["grid"][2][1]["color"], "#abcdef")
        self.assertTrue(state["players"][0]["is_host"])
        await asyncio.sleep(1.15)  # The cancelled expiry must not remove a rejoined player.
        await self.send(new_ws, action="ping")
        await self.receive(new_ws, "pong")
        self.assertEqual((await self.admin())["total_online"], 1)

    async def test_old_socket_close_cannot_remove_replacement(self):
        old, cid, room_id = await self.create()
        await self.send(old, action="click_cell", row=0, col=0)
        await self.receive(old, "room_update")
        replacement, _, state = await self.join(room_id, cid)
        await old.wait_closed()
        self.assertEqual(old.close_code, 4001)
        await asyncio.sleep(1.15)
        await self.send(replacement, action="change_color", color="#00ff00")
        state = await self.receive(replacement, "room_update")
        self.assertEqual(len(state["players"]), 1)
        self.assertTrue(state["players"][0]["connected"])
        self.assertEqual(state["grid"][0][0]["color"], "#00ff00")

    async def test_expiry_clears_grid_and_transfers_host(self):
        host, cid, room_id = await self.create()
        peer, peer_id, _ = await self.join(room_id)
        await self.send(host, action="click_cell", row=1, col=0)
        while True:
            state = await self.receive(peer, "room_update")
            if state["grid"][1][0]:
                break
        await host.close()
        await asyncio.sleep(1.2)
        data = await self.admin()
        self.assertEqual(data["total_online"], 1)
        self.assertEqual(data["rooms"][0]["count"], 1)
        self.assertEqual(data["rooms"][0]["players"][0]["client_id"], peer_id)
        self.assertTrue(data["rooms"][0]["players"][0]["is_host"])
        await self.send(peer, action="change_color", color="#aabbcc")
        while True:
            state = await self.receive(peer, "room_update")
            if len(state["players"]) == 1:
                break
        self.assertIsNone(state["grid"][1][0])

    async def test_last_player_expiry_deletes_room_and_reports_expired(self):
        ws, cid, room_id = await self.create()
        await ws.close()
        await asyncio.sleep(1.2)
        self.assertEqual((await self.admin())["room_count"], 0)
        ws, _ = await self.connect(cid)
        await self.send(ws, action="join_room", room_id=room_id, name="Test", color="#123456")
        await self.receive(ws, "session_expired")

    async def test_offline_seat_reserved_and_failed_join_keeps_existing_room(self):
        host, _, room_id = await self.create()
        peers = [await self.join(room_id) for _ in range(3)]
        await peers[0][0].close()
        other, cid, other_room = await self.create()
        await self.send(other, action="join_room", room_id=room_id, name="Other", color="#123456")
        await self.receive(other, "error")
        data = await self.admin()
        self.assertEqual(len(data["rooms"]), 2)
        own = next(r for r in data["rooms"] if r["room_id"] == other_room)
        self.assertEqual(own["players"][0]["client_id"], cid)
        _, _, state = await self.join(room_id, peers[0][1])
        self.assertEqual(len(state["players"]), 4)

    async def test_kick_online_and_reserved_players_is_immediate(self):
        ws, cid, room_id = await self.create()
        peer, peer_id, _ = await self.join(room_id)
        await self.admin_post("/admin-kick", client_id=cid)
        await self.receive(ws, "kicked")
        await ws.wait_closed()
        self.assertEqual(ws.close_code, 4003)
        data = await self.admin()
        self.assertEqual(data["rooms"][0]["count"], 1)
        await peer.close()
        await asyncio.sleep(0.05)
        await self.admin_post("/admin-kick-room", room_id=room_id)
        self.assertEqual((await self.admin())["room_count"], 0)

    async def test_invalid_room_does_not_clear_an_existing_session(self):
        ws, cid, room_id = await self.create()
        await self.send(ws, action="join_room", room_id="missing", name="Test", color="#123456")
        await self.receive(ws, "error")
        await self.send(ws, action="click_cell", row=0, col=0)
        state = await self.receive(ws, "room_update")
        self.assertEqual(state["room_id"], room_id)
        self.assertEqual(state["grid"][0][0]["client_id"], cid)

    async def test_heartbeat_and_existing_game_rules(self):
        host, cid, room_id = await self.create()
        peer, _, _ = await self.join(room_id)
        await self.send(peer, action="reset_all")
        await self.receive(peer, "error")
        await self.send(host, action="click_cell", row=0, col=0)
        await self.receive(host, "room_update")
        await self.send(host, action="click_cell", row=0, col=1)
        await self.receive(host, "error")
        for _ in range(6):
            await self.send(host, action="ping")
            await self.receive(host, "pong")
            await asyncio.sleep(0.2)
        await self.send(host, action="reset_all")
        state = await self.receive(host, "room_update")
        self.assertTrue(all(cell is None for row in state["grid"] for cell in row))
        self.assertEqual((await self.admin())["total_online"], 2)
        self.assertEqual((await self.http.get("/admin-data", params={"key": "wrong"})).status_code, 403)
        home = await self.http.get("/")
        self.assertIn(f'/static/app.js?v={server.APP_VERSION}', home.text)
        self.assertEqual((await self.http.get("/healthz")).json()["version"], server.APP_VERSION)


if __name__ == "__main__":
    unittest.main()
