from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
import json
import os
import random
import uuid
import asyncio
import logging
import time
from contextlib import asynccontextmanager

logger = logging.getLogger("uvicorn.error")
RECONNECT_GRACE_SECONDS = int(os.environ.get("RECONNECT_GRACE_SECONDS", "300"))
APP_VERSION = "2026-11-17-reconnect-v1"
connections = {}
disconnect_tasks = {}


@asynccontextmanager
async def lifespan(app):
    logger.info("Starting Artale version=%s grace_seconds=%s", APP_VERSION, RECONNECT_GRACE_SECONDS)
    yield
    tasks = list(disconnect_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    disconnect_tasks.clear()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

SITE_PASSWORD = "123456"   # 玩家連線密碼
ADMIN_PASSWORD = "30678"   # 管理頁密碼

MAX_PLAYERS = 4
ROWS = 10
COLS = 4

rooms = {}
client_room = {}


def generate_room_id():
    return f"{random.randint(0, 9999):04d}"


def make_room(room_id: str):
    return {
        "room_id": room_id,
        "host_id": None,
        "players": {},  # client_id -> {name, color, ws}
        "grid": [[None for _ in range(COLS)] for _ in range(ROWS)]
    }


def build_room_payload(room):
    players = []
    for client_id, p in room["players"].items():
        players.append({
            "client_id": client_id,
            "name": p["name"],
            "color": p["color"],
            "is_host": client_id == room["host_id"],
            "connected": p["ws"] is not None,
            "reconnect_until": p.get("reconnect_until")
        })

    return {
        "type": "room_update",
        "room_id": room["room_id"],
        "players": players,
        "grid": room["grid"],
        "rows": ROWS,
        "cols": COLS
    }


async def send_json(ws: WebSocket, data: dict):
    await ws.send_text(json.dumps(data, ensure_ascii=False))


def cancel_disconnect_timer(client_id: str):
    task = disconnect_tasks.pop(client_id, None)
    if task and task is not asyncio.current_task():
        task.cancel()


async def broadcast_room(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return

    payload = build_room_payload(room)
    dead = []
    for client_id, player in list(room["players"].items()):
        socket = player["ws"]
        if socket is None:
            continue
        try:
            await send_json(socket, payload)
        except Exception:
            dead.append((client_id, socket))

    changed = False
    for client_id, socket in dead:
        changed = await mark_disconnected(client_id, socket, broadcast=False) or changed
    if changed:
        await broadcast_room(room_id)


async def remove_client(client_id: str):
    """Permanently remove a player after expiry, switching rooms, or an admin kick."""
    cancel_disconnect_timer(client_id)
    room_id = client_room.pop(client_id, None)
    room = rooms.get(room_id)
    if not room or client_id not in room["players"]:
        return

    del room["players"][client_id]
    for row in room["grid"]:
        for col, cell in enumerate(row):
            if cell and cell["client_id"] == client_id:
                row[col] = None

    if room["host_id"] == client_id:
        online = [cid for cid, p in room["players"].items() if p["ws"] is not None]
        remaining = online or list(room["players"])
        room["host_id"] = remaining[0] if remaining else None

    if not room["players"]:
        rooms.pop(room_id, None)
    else:
        await broadcast_room(room_id)


async def expire_disconnected(client_id: str, player: dict):
    try:
        await asyncio.sleep(RECONNECT_GRACE_SECONDS)
        room = rooms.get(client_room.get(client_id))
        if (disconnect_tasks.get(client_id) is asyncio.current_task()
                and room and room["players"].get(client_id) is player
                and player["ws"] is None):
            logger.info("Reconnect grace expired client=%s room=%s", client_id, room["room_id"])
            await remove_client(client_id)
    finally:
        if disconnect_tasks.get(client_id) is asyncio.current_task():
            disconnect_tasks.pop(client_id, None)


async def mark_disconnected(client_id: str, socket: WebSocket, broadcast=True):
    room_id = client_room.get(client_id)
    room = rooms.get(room_id)
    player = room["players"].get(client_id) if room else None
    # A late close from an old socket must never remove the replacement connection.
    if not player or player["ws"] is not socket:
        return False
    player["ws"] = None
    player["reconnect_until"] = time.time() + RECONNECT_GRACE_SECONDS
    cancel_disconnect_timer(client_id)
    disconnect_tasks[client_id] = asyncio.create_task(expire_disconnected(client_id, player))
    if broadcast:
        await broadcast_room(room_id)
    return True


async def remove_client_from_current_room(client_id: str):
    await remove_client(client_id)


def get_admin_data():
    room_list = []
    total_online = 0

    for room_id, room in rooms.items():
        players = []
        for client_id, p in room["players"].items():
            players.append({
                "client_id": client_id,
                "name": p["name"],
                "color": p["color"],
                "is_host": client_id == room["host_id"],
                "connected": p["ws"] is not None,
                "reconnect_until": p.get("reconnect_until")
            })

        total_online += sum(p["connected"] for p in players)

        room_list.append({
            "room_id": room_id,
            "count": len(players),
            "online_count": sum(p["connected"] for p in players),
            "players": players
        })

    return {
        "total_online": total_online,
        "room_count": len(room_list),
        "rooms": room_list
    }


async def kick_client_by_id(client_id: str):
    room = rooms.get(client_room.get(client_id))
    player = room["players"].get(client_id) if room else None
    socket = connections.pop(client_id, None)
    if socket is None and player:
        socket = player["ws"]
    # Remove immediately; intentional kicks must not reserve a seat or auto-rejoin.
    await remove_client(client_id)
    if socket:
        try:
            await send_json(socket, {"type": "kicked", "message": "你已被管理員移出房間"})
            await socket.close(code=4003, reason="Removed by administrator")
        except Exception:
            pass
    logger.info("Admin removed client=%s", client_id)


async def kick_room(room_id: str):
    room = rooms.get(room_id)
    if room:
        for client_id in list(room["players"]):
            await kick_client_by_id(client_id)


async def kick_all():
    for room_id in list(rooms):
        await kick_room(room_id)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {"request": request, "version": APP_VERSION})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, key: str = ""):
    if key != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="無權限")
    return templates.TemplateResponse(request, "admin.html", {"request": request, "key": key})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": APP_VERSION}


@app.get("/admin-data")
async def admin_data(key: str):
    if key != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="無權限")
    return JSONResponse(get_admin_data())


@app.post("/admin-kick")
async def admin_kick(client_id: str, key: str):
    if key != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="無權限")
    await kick_client_by_id(client_id)
    return {"ok": True}


@app.post("/admin-kick-room")
async def admin_kick_room(room_id: str, key: str):
    if key != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="無權限")
    await kick_room(room_id)
    return {"ok": True}


@app.post("/admin-kick-all")
async def admin_kick_all(key: str):
    if key != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="無權限")
    await kick_all()
    return {"ok": True}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    password = ws.query_params.get("p")

    if password != SITE_PASSWORD:
        await ws.close()
        return

    await ws.accept()

    # ✅ 關鍵：優先使用前端帶來的固定 client_id
    client_id = ws.query_params.get("cid")
    if not client_id:
        client_id = str(uuid.uuid4())[:8]

    previous = connections.get(client_id)
    connections[client_id] = ws
    if previous and previous is not ws:
        try:
            await previous.close(code=4001, reason="Connection replaced")
        except Exception:
            pass
    logger.info("WebSocket connected client=%s", client_id)

    try:
        while True:
            raw = await ws.receive_text()
            if connections.get(client_id) is not ws:
                break
            data = json.loads(raw)
            action = data.get("action")

            # 如果之後改了網站密碼，舊連線下次送訊息會被踢
            password = ws.query_params.get("p")
            if password != SITE_PASSWORD:
                await ws.close()
                break

            if action == "create_room":
                name = data.get("name", "").strip()
                color = data.get("color", "#3b82f6").strip()

                if not name:
                    await send_json(ws, {"type": "error", "message": "請輸入名稱"})
                    continue

                await remove_client_from_current_room(client_id)
                room_id = generate_room_id()
                while room_id in rooms:
                    room_id = generate_room_id()

                room = make_room(room_id)
                room["host_id"] = client_id
                room["players"][client_id] = {
                    "name": name,
                    "color": color,
                    "ws": ws
                }

                rooms[room_id] = room
                client_room[client_id] = room_id

                await send_json(ws, {
                    "type": "joined",
                    "room_id": room_id,
                    "client_id": client_id
                })
                await broadcast_room(room_id)

            elif action == "join_room":
                name = data.get("name", "").strip()
                color = data.get("color", "#3b82f6").strip()
                room_id = data.get("room_id", "").strip()

                if not name or not room_id:
                    await send_json(ws, {"type": "error", "message": "請輸入名稱、顏色與房號"})
                    continue

                room = rooms.get(room_id)
                if not room:
                    current = rooms.get(client_room.get(client_id))
                    still_joined = current and current["players"].get(client_id, {}).get("ws") is ws
                    await send_json(ws, {
                        "type": "error" if still_joined else "session_expired",
                        "message": "房間已結束或伺服器已重啟，請重新建立或加入房間"
                    })
                    continue

                if len(room["players"]) >= MAX_PLAYERS and client_id not in room["players"]:
                    await send_json(ws, {"type": "error", "message": "房間已滿（含斷線保留中的玩家）"})
                    continue

                current_room_id = client_room.get(client_id)
                if current_room_id != room_id:
                    await remove_client_from_current_room(client_id)
                cancel_disconnect_timer(client_id)

                # ✅ 保留同一 client_id 的格子，只更新連線 / 名字 / 顏色
                room["players"][client_id] = {
                    "name": name,
                    "color": color,
                    "ws": ws
                }
                client_room[client_id] = room_id

                # 若房主空缺，重連者可補上
                if room["host_id"] is None:
                    room["host_id"] = client_id

                # ✅ 把這個人以前點過的格子名稱與顏色同步更新
                for r in range(ROWS):
                    for c in range(COLS):
                        cell = room["grid"][r][c]
                        if cell and cell["client_id"] == client_id:
                            cell["name"] = name
                            cell["color"] = color

                await send_json(ws, {
                    "type": "joined",
                    "room_id": room_id,
                    "client_id": client_id
                })
                logger.info("Player joined client=%s room=%s rejoin=%s", client_id, room_id, current_room_id == room_id)
                await broadcast_room(room_id)

            elif action == "change_color":
                color = data.get("color", "").strip()

                room_id = client_room.get(client_id)
                room = rooms.get(room_id)
                if room is None or room["players"].get(client_id, {}).get("ws") is not ws:
                    continue

                if not color:
                    continue

                room["players"][client_id]["color"] = color

                # 已點的格子也一起改色
                for r in range(ROWS):
                    for c in range(COLS):
                        cell = room["grid"][r][c]
                        if cell and cell["client_id"] == client_id:
                            cell["color"] = color

                await broadcast_room(room_id)

            elif action == "click_cell":
                row = data.get("row")
                col = data.get("col")

                room_id = client_room.get(client_id)
                room = rooms.get(room_id)
                if room is None or room["players"].get(client_id, {}).get("ws") is not ws:
                    continue

                if not isinstance(row, int) or not isinstance(col, int):
                    continue
                if row < 0 or row >= ROWS or col < 0 or col >= COLS:
                    continue

                current = room["grid"][row][col]
                me = room["players"][client_id]

                # 同一個人同一層只能點一格
                my_col_in_this_row = None
                for c in range(COLS):
                    cell = room["grid"][row][c]
                    if cell and cell["client_id"] == client_id:
                        my_col_in_this_row = c
                        break

                if current is None:
                    if my_col_in_this_row is not None and my_col_in_this_row != col:
                        await send_json(ws, {"type": "error", "message": "你在這一層已經點過一格了"})
                        continue

                    room["grid"][row][col] = {
                        "client_id": client_id,
                        "name": me["name"],
                        "color": me["color"]
                    }

                elif current["client_id"] == client_id:
                    room["grid"][row][col] = None

                else:
                    await send_json(ws, {"type": "error", "message": "這格已經被別人點了"})
                    continue

                await broadcast_room(room_id)

            elif action == "reset_me":
                room_id = client_room.get(client_id)
                room = rooms.get(room_id)
                if room is None or room["players"].get(client_id, {}).get("ws") is not ws:
                    continue

                for r in range(ROWS):
                    for c in range(COLS):
                        cell = room["grid"][r][c]
                        if cell and cell["client_id"] == client_id:
                            room["grid"][r][c] = None

                await broadcast_room(room_id)

            elif action == "reset_all":
                room_id = client_room.get(client_id)
                room = rooms.get(room_id)
                if room is None or room["players"].get(client_id, {}).get("ws") is not ws:
                    continue

                if room["host_id"] != client_id:
                    await send_json(ws, {"type": "error", "message": "只有房主可以全部重置"})
                    continue

                room["grid"] = [[None for _ in range(COLS)] for _ in range(ROWS)]
                await broadcast_room(room_id)

            elif action == "ping":
                await send_json(ws, {"type": "pong"})

    except WebSocketDisconnect as exc:
        logger.info("WebSocket disconnected client=%s code=%s", client_id, exc.code)
    except Exception:
        logger.exception("WebSocket failed client=%s", client_id)
    finally:
        if connections.get(client_id) is ws:
            connections.pop(client_id, None)
        await mark_disconnected(client_id, ws)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
