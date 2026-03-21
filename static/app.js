let ws = null;
let myClientId = "";
let players = [];
let grid = [];
let rows = 10;
let cols = 4;

// 這裡先寫死，之後你要改密碼就改這兩個
const PASSWORD = "123456";



function connectWs() {
    const protocol = location.protocol === "https:" ? "wss" : "ws";

    // 用 query string 傳帳密，對應 server.py 的 ws.query_params
    const PASSWORD = "123456";

const wsUrl =
`${protocol}://${location.host}/ws?p=${encodeURIComponent(PASSWORD)}`;

    ws = new WebSocket(wsUrl);

    ws.onopen = function () {
        setText("connText", "已連線");
    };

    ws.onclose = function () {
        setText("connText", "連線中斷，3 秒後重連");
        setTimeout(connectWs, 3000);
    };

    ws.onmessage = function (event) {
        const data = JSON.parse(event.data);

        if (data.type === "joined") {
            myClientId = data.client_id;
            setText("currentRoom", data.room_id);
            document.getElementById("roomId").value = data.room_id;
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

        if (data.type === "error") {
            alert(data.message || "發生錯誤");
        }
    };

    ws.onerror = function () {
        setText("connText", "連線失敗");
    };
}

function setText(id, text) {
    document.getElementById(id).textContent = text;
}

function getName() {
    return document.getElementById("name").value.trim();
}

function getRoomId() {
    return document.getElementById("roomId").value.trim().toUpperCase();
}

function getColor() {
    return document.getElementById("color").value;
}

function updateMyColorPreview() {
    const color = getColor();
    document.getElementById("myColorPreview").style.background = color;
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
}

function changeColor() {
    const color = getColor();
    updateMyColorPreview();

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

function renderPlayers() {
    const wrap = document.getElementById("players");
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
        document.getElementById("copyMsg").textContent = "已複製";
        setTimeout(() => {
            document.getElementById("copyMsg").textContent = "";
        }, 1500);
    }).catch(() => {
        alert("複製失敗");
    });
}

function renderGrid() {
    const table = document.getElementById("gridTable");
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

document.getElementById("color").addEventListener("input", changeColor);
updateMyColorPreview();
connectWs();