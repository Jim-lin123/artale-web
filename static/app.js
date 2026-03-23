let ws = null;
let myClientId = "";
let players = [];
let grid = [];
let rows = 10;
let cols = 4;

const PASSWORD = "123456"; // 要跟 server.py 的 SITE_PASSWORD 一樣
let heartbeatTimer = null;
let reconnectTimer = null;

const STORAGE_KEY = "artale_room_state_v1";
const CLIENT_ID_KEY = "artale_client_id_v1";

function getOrCreateClientId() {
    let cid = localStorage.getItem(CLIENT_ID_KEY);
    if (!cid) {
        cid = Math.random().toString(36).slice(2, 10);
        localStorage.setItem(CLIENT_ID_KEY, cid);
    }
    return cid;
}

function saveRoomState(data) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
}

function loadRoomState() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        return raw ? JSON.parse(raw) : null;
    } catch (e) {
        return null;
    }
}

function clearRoomState() {
    localStorage.removeItem(STORAGE_KEY);
}

function connectWs() {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const clientId = getOrCreateClientId();
    const wsUrl =
        `${protocol}://${location.host}/ws?p=${encodeURIComponent(PASSWORD)}&cid=${encodeURIComponent(clientId)}`;

    ws = new WebSocket(wsUrl);

    ws.onopen = function () {
        setText("connText", "已連線");
        startHeartbeat();
        tryAutoRejoin();
    };

    ws.onclose = function () {
        setText("connText", "連線中斷，3 秒後重連");
        stopHeartbeat();

        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
        }

        reconnectTimer = setTimeout(connectWs, 3000);
    };

    ws.onmessage = function (event) {
        const data = JSON.parse(event.data);

        if (data.type === "joined") {
            myClientId = data.client_id;
            setText("currentRoom", data.room_id);

            const roomInput = document.getElementById("roomId");
            if (roomInput) {
                roomInput.value = data.room_id;
            }

            const name = getName();
            const color = getColor();

            saveRoomState({
                room_id: data.room_id,
                name: name,
                color: color
            });
        }

        if (data.type === "room_update") {
            players = data.players || [];
            grid = data.grid || [];
            rows = data.rows || 10;
            cols = data.cols || 4;
            setText("currentRoom", data.room_id || "-");
            renderPlayers();
            renderGrid();
        }

        if (data.type === "pong") {
            return;
        }

        if (data.type === "error") {
            alert(data.message || "發生錯誤");
        }
    };

    ws.onerror = function () {
        setText("connText", "連線失敗");
    };
}

function startHeartbeat() {
    stopHeartbeat();

    heartbeatTimer = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ action: "ping" }));
        }
    }, 2 * 60 * 1000); // 每 2 分鐘一次
}

function stopHeartbeat() {
    if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
    }
}

function tryAutoRejoin() {
    const saved = loadRoomState();
    if (!saved) return;
    if (!saved.room_id || !saved.name || !saved.color) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    const nameInput = document.getElementById("name");
    const roomInput = document.getElementById("roomId");
    const colorInput = document.getElementById("color");

    if (nameInput) nameInput.value = saved.name;
    if (roomInput) roomInput.value = saved.room_id;
    if (colorInput) colorInput.value = saved.color;

    updateMyColorPreview();

    ws.send(JSON.stringify({
        action: "join_room",
        name: saved.name,
        room_id: saved.room_id,
        color: saved.color
    }));
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (el) {
        el.textContent = text;
    }
}

function getName() {
    return document.getElementById("name").value.trim();
}

function getRoomId() {
    return document.getElementById("roomId").value.trim();
}

function getColor() {
    return document.getElementById("color").value;
}

function updateMyColorPreview() {
    const color = getColor();
    const preview = document.getElementById("myColorPreview");
    if (preview) {
        preview.style.background = color;
    }
    setText("myColorText", color);
}

function send(obj) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        alert("WebSocket 尚未連線");
        return;
    }
    ws.send(JSON.stringify(obj));
}

function createRoom() {
    const name = getName();
    const color = getColor();

    if (!name) {
        alert("請輸入名稱");
        return;
    }

    updateMyColorPreview();

    // 建新房前清掉舊房資訊，但保留固定 client_id
    clearRoomState();

    send({
        action: "create_room",
        name: name,
        color: color
    });
}

function joinRoom() {
    const name = getName();
    const roomId = getRoomId();
    const color = getColor();

    if (!name || !roomId) {
        alert("請輸入名稱與房號");
        return;
    }

    updateMyColorPreview();

    send({
        action: "join_room",
        name: name,
        room_id: roomId,
        color: color
    });

    saveRoomState({
        room_id: roomId,
        name: name,
        color: color
    });
}

function changeColor() {
    const color = getColor();
    updateMyColorPreview();

    const saved = loadRoomState();
    if (saved) {
        saved.color = color;
        saveRoomState(saved);
    }

    if (!ws || ws.readyState !== WebSocket.OPEN) {
        return;
    }

    send({
        action: "change_color",
        color: color
    });
}

function resetMe() {
    send({ action: "reset_me" });
}

function resetAll() {
    send({ action: "reset_all" });
}

function clickCell(row, col) {
    send({
        action: "click_cell",
        row: row,
        col: col
    });
}

function leaveRoomMemory() {
    clearRoomState();
    setText("currentRoom", "-");
    const roomInput = document.getElementById("roomId");
    if (roomInput) {
        roomInput.value = "";
    }
}

function renderPlayers() {
    const wrap = document.getElementById("players");
    if (!wrap) return;

    wrap.innerHTML = "";

    players.forEach(p => {
        const div = document.createElement("div");
        div.className = "player-tag";
        div.style.background = p.color;
        div.textContent =
            p.name +
            (p.client_id === myClientId ? "（你）" : "") +
            (p.is_host ? "［房主］" : "");
        wrap.appendChild(div);
    });
}

function copyRoomCode() {
    const roomCode = document.getElementById("currentRoom").textContent.trim();

    if (!roomCode || roomCode === "-") {
        alert("目前沒有房號");
        return;
    }

    navigator.clipboard.writeText(roomCode).then(() => {
        const msg = document.getElementById("copyMsg");
        if (msg) {
            msg.textContent = "已複製";
            setTimeout(() => {
                msg.textContent = "";
            }, 1500);
        }
    }).catch(() => {
        alert("複製失敗");
    });
}

function renderGrid() {
    const table = document.getElementById("gridTable");
    if (!table) return;

    table.innerHTML = "";

    for (let r = rows - 1; r >= 0; r--) {
        const tr = document.createElement("tr");

        const label = document.createElement("td");
        label.className = "floor-label";
        label.textContent = `${r + 1}F`;
        tr.appendChild(label);

        for (let c = 0; c < cols; c++) {
            const td = document.createElement("td");
            const btn = document.createElement("button");
            btn.className = "cell-btn";

            const cell = grid[r] ? grid[r][c] : null;

            if (cell) {
                btn.style.background = cell.color;
                btn.style.borderColor = cell.color;
                btn.style.color = "#fff";
                btn.textContent = cell.name;
            } else {
                btn.style.background = "#fff";
                btn.style.borderColor = "#d1d5db";
                btn.style.color = "#374151";
                btn.textContent = "";
            }

            btn.onclick = function () {
                clickCell(r, c);
            };

            td.appendChild(btn);
            tr.appendChild(td);
        }

        table.appendChild(tr);
    }
}

window.addEventListener("beforeunload", () => {
    // 如果你不想關頁面就清除記錄，可以把這行註解掉
    // clearRoomState();
});

document.getElementById("color").addEventListener("input", changeColor);
updateMyColorPreview();
connectWs();