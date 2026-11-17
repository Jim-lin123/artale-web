# Artale 羅密歐任務進度板

FastAPI / WebSocket 共用進度板，部署於 Render。

## 連線恢復

- 玩家斷線後保留座位、房主身分和格子 5 分鐘，重連同一個瀏覽器身分即可恢復。
- 每 25 秒發送心跳，15 秒未收到回應時重建連線；網路恢復或回到頁面時主動檢查。
- 同一瀏覽器的多個分頁共用身分，最新分頁接管連線，舊分頁停止自動重連。
- 管理員踢人立即移除進度，不適用斷線保留，也不會自動回房。
- 超過保留期限才清除離線玩家；房間最後一位玩家到期後刪房。
- 房間仍儲存在程序記憶體中，部署、服務重啟或 Render 休眠後無法恢復。

`RECONNECT_GRACE_SECONDS` 可設定後端保留秒數（預設 300）；如更改，也需同步調整介面提示。
`/healthz` 回傳版本；Render 日誌記錄連線、重連、斷線代碼及保留期限到期事件。
瀏覽器主控台記錄斷線代碼、頁面可見狀態與心跳逾時。

## 本機與測試

```sh
pip install -r requirements-dev.txt
uvicorn server:app --host 127.0.0.1 --port 8000
```

```sh
python -m unittest discover -s tests -v
python -m playwright install chromium
python tests/browser_reconnect.py
```

測試各自啟動本機伺服器，不會操作正式站。瀏覽器測試含 51 秒閒置及心跳逾時檢查，約需 75 秒。
