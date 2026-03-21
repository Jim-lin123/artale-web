from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
import uuid
import json
import os

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

SITE_PASSWORD = "123456"     # 玩家連線密碼
ADMIN_PASSWORD = "30678"    # 管理頁密碼

MAX_PLAYERS = 4
ROWS = 10
COLS = 4

rooms = {}
client_room = {}


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
            "is_host": client_id == room["host_id"]
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


async def broadcast_room(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return

    payload = build_room_payload(room)
    dead = []

    for client_id, player in list(room["players"].items()):
        try:
            await send_json(player["ws"], payload)
        except Exception:
            dead.append(client_id)

    for client_id in dead:
        await remove_client(client_id)


async def remove_client(client_id: str):
    room_id = client_room.get(client_id)
    if not room_id:
        return

    room = rooms.get(room_id)
    if room and client_id in room["players"]:
        del room["players"][client_id]

        for r in range(ROWS):
            for c in range(COLS):
                cell = room["grid"][r][c]
                if cell and cell["client_id"] == client_id:
                    room["grid"][r][c] = None

        if room["host_id"] == client_id:
            remain = list(room["players"].keys())
            room["host_id"] = remain[0] if remain else None

        if len(room["players"]) == 0:
            del rooms[room_id]
        else:
            await broadcast_room(room_id)

    client_room.pop(client_id, None)


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
                "is_host": client_id == room["host_id"]
            })

        total_online += len(players)

        room_list.append({
            "room_id": room_id,
            "count": len(players),
            "players": players
        })

    return {
        "total_online": total_online,
        "room_count": len(room_list),
        "rooms": room_list
    }


async def kick_client_by_id(client_id: str):
    room_id = client_room.get(client_id)
    room = rooms.get(room_id) if room_id else None

    if room and client_id in room["players"]:
        try:
            await room["players"][client_id]["ws"].close()
        except Exception:
            pass


async def kick_room(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return

    for client_id, p in list(room["players"].items()):
        try:
            await p["ws"].close()
        except Exception:
            pass


async def kick_all():
    for room_id, room in list(rooms.items()):
        for client_id, p in list(room["players"].items()):
            try:
                await p["ws"].close()
            except Exception:
                pass


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, key: str = ""):
    if key != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="無權限")
    return templates.TemplateResponse("admin.html", {"request": request, "key": key})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


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
    params = ws.query_params
    password = params.get("p")

    if password != SITE_PASSWORD:
        await ws.close()
        return

    await ws.accept()
    client_id = str(uuid.uuid4())[:8]

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            action = data.get("action")

            # 改網站密碼後，舊連線下次操作會被踢
            params = ws.query_params
            password = params.get("p")
            if password != SITE_PASSWORD:
                await ws.close()
                break

            if action == "create_room":
                name = data.get("name", "").strip()
                color = data.get("color", "#3b82f6").strip()

                if not name:
                    await send_json(ws, {"type": "error", "message": "請輸入名稱"})
                    continue

                room_id = str(uuid.uuid4())[:6].upper()
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
                room_id = data.get("room_id", "").strip().upper()

                if not name or not room_id:
                    await send_json(ws, {"type": "error", "message": "請輸入名稱、顏色與房號"})
                    continue

                room = rooms.get(room_id)
                if not room:
                    await send_json(ws, {"type": "error", "message": "找不到房間"})
                    continue

                if len(room["players"]) >= MAX_PLAYERS:
                    await send_json(ws, {"type": "error", "message": "房間已滿"})
                    continue

                room["players"][client_id] = {
                    "name": name,
                    "color": color,
                    "ws": ws
                }
                client_room[client_id] = room_id

                await send_json(ws, {
                    "type": "joined",
                    "room_id": room_id,
                    "client_id": client_id
                })
                await broadcast_room(room_id)

            elif action == "change_color":
                color = data.get("color", "").strip()

                room_id = client_room.get(client_id)
                room = rooms.get(room_id)
                if room is None or client_id not in room["players"]:
                    continue

                if not color:
                    continue

                room["players"][client_id]["color"] = color

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
                if room is None or client_id not in room["players"]:
                    continue

                if not isinstance(row, int) or not isinstance(col, int):
                    continue
                if row < 0 or row >= ROWS or col < 0 or col >= COLS:
                    continue

                current = room["grid"][row][col]
                me = room["players"][client_id]

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
                if room is None or client_id not in room["players"]:
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
                if room is None or client_id not in room["players"]:
                    continue

                if room["host_id"] != client_id:
                    await send_json(ws, {"type": "error", "message": "只有房主可以全部重置"})
                    continue

                room["grid"] = [[None for _ in range(COLS)] for _ in range(ROWS)]
                await broadcast_room(room_id)

    except WebSocketDisconnect:
        await remove_client(client_id)
    except Exception as e:
        print("WebSocket error:", e)
        await remove_client(client_id)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)