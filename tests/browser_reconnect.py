"""Browser regression: python tests/browser_reconnect.py

Starts its own local server. Requires `pip install -r requirements-dev.txt`
and `python -m playwright install chromium`.
"""
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import httpx
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server


async def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    errors = []
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env={**os.environ, "RECONNECT_GRACE_SECONDS": "300"}, stdout=log, stderr=log,
        )
        try:
            async with httpx.AsyncClient(base_url=base) as http:
                for _ in range(100):
                    try:
                        if (await http.get("/healthz")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
                else:
                    raise RuntimeError("Local server did not start")
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                    context = await browser.new_context()
                    page = await context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(base)
                    await page.locator("#name").fill("Reconnect test")
                    await page.get_by_role("button", name="建立房間", exact=True).click()
                    await expect(page.locator("#connText")).to_have_text("已連線")
                    room = await page.locator("#currentRoom").inner_text()
                    cell = page.locator(".cell-btn").first
                    await cell.click()
                    await expect(cell).to_have_text("Reconnect test")
                    await page.reload()
                    await expect(page.locator("#connText")).to_have_text("已連線")
                    await expect(page.locator("#currentRoom")).to_have_text(room)
                    await expect(cell).to_have_text("Reconnect test")
                    print("PASS: refresh automatically rejoins with the original progress", flush=True)

                    await page.evaluate("ws.close()")
                    await expect(page.locator("#connText")).to_contain_text("重連")
                    await expect(page.locator("#connText")).to_have_text("已連線", timeout=10000)
                    await expect(cell).to_have_text("Reconnect test")
                    print("PASS: disconnected player automatically returns to the same room", flush=True)

                    # Keep the tab idle with no management page or HTTP polling.
                    # This spans two 25-second application heartbeats.
                    pongs = []
                    page.on("websocket", lambda ws: ws.on("framereceived", lambda frame: pongs.append(frame)))
                    await page.evaluate("ws.close()")
                    await expect(page.locator("#connText")).to_contain_text("重連")
                    await expect(page.locator("#connText")).to_have_text("已連線", timeout=10000)
                    before = len(pongs)
                    await page.wait_for_timeout(51000)
                    assert len([f for f in pongs[before:] if '"pong"' in f]) >= 2
                    await expect(page.locator("#connText")).to_have_text("已連線")
                    await expect(cell).to_have_text("Reconnect test")
                    print("PASS: idle tab exchanges heartbeats without any admin page open", flush=True)

                    # Simulate an open socket whose pong messages never reach the app.
                    await page.evaluate('''() => {
                        window.staleSocket = ws;
                        const handle = ws.onmessage;
                        ws.onmessage = event => {
                            if (JSON.parse(event.data).type !== "pong") handle(event);
                        };
                        stopHeartbeat();
                        pingServer();
                    }''')
                    await page.wait_for_function("ws !== window.staleSocket && hasJoined", timeout=22000)
                    await expect(cell).to_have_text("Reconnect test")
                    print("PASS: missing pong triggers a fresh connection and restores progress", flush=True)

                    # A second tab shares the fixed client id, without causing a reconnect loop.
                    second = await context.new_page()
                    second.on("pageerror", lambda error: errors.append(str(error)))
                    await second.goto(base)
                    await expect(second.locator("#connText")).to_have_text("已連線")
                    await expect(page.locator("#connText")).to_contain_text("其他分頁")
                    await expect(second.locator(".cell-btn").first).to_have_text("Reconnect test")
                    cid = await second.evaluate("getOrCreateClientId()")
                    response = await http.post("/admin-kick", params={"key": server.ADMIN_PASSWORD, "client_id": cid})
                    response.raise_for_status()
                    await expect(second.locator("#connText")).to_contain_text("管理員")
                    await expect(second.locator("#currentRoom")).to_have_text("-")
                    assert await second.evaluate("localStorage.getItem(STORAGE_KEY)") is None
                    await second.wait_for_timeout(2000)
                    assert (await http.get("/admin-data", params={"key": server.ADMIN_PASSWORD})).json()["room_count"] == 0
                    print("PASS: duplicate tabs stop reconnecting; admin kick clears saved session", flush=True)
                    assert not errors, errors
                    await browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
