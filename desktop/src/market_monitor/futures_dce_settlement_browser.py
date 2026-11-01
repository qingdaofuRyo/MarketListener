"""Read DCE settlement responses through an already open authorized Android page."""
from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from urllib.request import urlopen
from urllib.parse import urlsplit

import websocket

from .futures_settlement_parameters import SettlementSourceError

PAGE = "http://www.dce.com.cn/dce/channel/list/181.html"
SOURCE = "http://www.dce.com.cn/dcereport/publicweb/tradepara/futAndOptSettle"


class DceSettlementBrowser:
    """No navigation or credential persistence; only the user's 181 session."""

    def __init__(self, adb: Path | None = None, adb_port: int = 5038) -> None:
        candidate = Path(r"C:\leidian\LDPlayer14\adb.exe")
        self.adb = adb or (candidate if candidate.is_file() else Path(shutil.which("adb") or "adb"))
        self.adb_port = adb_port
        self.forward_port: int | None = None
        self.device = ""
        self.sock: Any = None
        self.sequence = 0
        self.events: list[dict[str, Any]] = []
        devices = self._adb("devices")
        for line in devices.splitlines():
            fields = line.split()
            if len(fields) != 2 or fields[1] != "device":
                continue
            device = fields[0]
            sockets = self._adb("-s", device, "shell", "cat", "/proc/net/unix")
            names = sorted(set(re.findall(r"@((?:chrome|webview)[\w.-]*devtools_remote[\w.-]*)", sockets)))
            for name in names:
                port = int(self._adb("-s", device, "forward", "tcp:0", f"localabstract:{name}").strip())
                try:
                    with urlopen(f"http://127.0.0.1:{port}/json", timeout=4) as response:
                        targets = json.load(response)
                    target = next((item for item in targets if item.get("type") == "page"
                                   and item.get("url", "").split("?")[0].split("#")[0] == PAGE), None)
                    if target:
                        ws = urlsplit(target["webSocketDebuggerUrl"])
                        if ws.scheme != "ws" or ws.hostname not in {"127.0.0.1", "localhost"} or ws.port != port:
                            raise ValueError("invalid local browser debugger")
                        self.device, self.forward_port = device, port
                        self.sock = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=45, suppress_origin=True)
                        self.command("Network.enable", {"maxTotalBufferSize": 30_000_000, "maxResourceBufferSize": 20_000_000})
                        return
                except (OSError, ValueError, KeyError, websocket.WebSocketException):
                    pass
                self._adb("-s", device, "forward", "--remove", f"tcp:{port}")
        raise SettlementSourceError("DCE_181_PAGE_NOT_OPEN", status="DEFERRED")

    def _adb(self, *args: str) -> str:
        try:
            result = subprocess.run([str(self.adb), "-P", str(self.adb_port), *args],
                                    capture_output=True, text=True, timeout=15, check=True,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            raise SettlementSourceError("DCE_ANDROID_DEBUGGER_UNAVAILABLE", status="DEFERRED") from None

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sequence += 1
        self.sock.send(json.dumps({"id": self.sequence, "method": method, "params": params or {}}))
        while True:
            event = json.loads(self.sock.recv())
            if event.get("id") == self.sequence:
                if "error" in event:
                    raise SettlementSourceError("DCE_BROWSER_COMMAND_FAILED")
                return event.get("result", {})
            if event.get("method"):
                self.events.append(event)

    def capture(self, trading_day: str) -> dict[str, Any]:
        self.events.clear()
        params = {"varietyId": "all", "tradeDate": trading_day.replace("-", ""), "tradeType": "1", "lang": "zh"}
        body = json.dumps(params, separators=(",", ":"))
        expression = """(async()=>{const frame=[...document.querySelectorAll('iframe')]
          .find(x=>x.src.includes('/frontend/dcereport/'));if(!frame?.contentWindow)
          throw Error('missing official report');const response=await frame.contentWindow.fetch(
          '/dcereport/publicweb/tradepara/futAndOptSettle',{method:'POST',
          headers:{'Content-Type':'application/json'},body:BODY});
          await response.text();return {status:response.status,mime:response.headers.get('content-type')};})()""".replace("BODY", json.dumps(body))
        try:
            result = self.command("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
            value = result.get("result", {}).get("value", {})
            if result.get("exceptionDetails") or value.get("status") != 200:
                raise SettlementSourceError("DCE_BROWSER_HTTP_REFUSED")
            mime = str(value.get("mime") or "").split(";", 1)[0].strip().lower()
            if mime not in {"application/json", "application/octet-stream"}:
                raise SettlementSourceError("DCE_BROWSER_MEDIA_TYPE_UNSUPPORTED")
            request = next((event["params"] for event in self.events
                            if event.get("method") == "Network.requestWillBeSent"
                            and event["params"].get("request", {}).get("url", "").split("?")[0] == SOURCE
                            and event["params"]["request"].get("method") == "POST"
                            and event["params"]["request"].get("postData") == body), None)
            if not request:
                raise SettlementSourceError("DCE_DATE_REQUEST_NOT_OBSERVED")
            original = self.command("Network.getResponseBody", {"requestId": request["requestId"]})
            raw = base64.b64decode(original["body"], validate=True) if original.get("base64Encoded") else original["body"].encode("utf-8")
            if len(raw) > 20_000_000:
                raise SettlementSourceError("DCE_RESPONSE_TOO_LARGE")
            if not raw.lstrip().startswith(b"{"):
                raise SettlementSourceError("DCE_BROWSER_RESPONSE_NOT_JSON")
            return {"raw": raw, "source_url": SOURCE, "margin_unit": "DECIMAL",
                    "request_evidence": {"method": "POST", "source_url": SOURCE,
                                         "post_data": body, "post_data_sha256": sha256(body.encode()).hexdigest(),
                                         "trade_date_verified": True}}
        except (OSError, ValueError, KeyError, websocket.WebSocketException):
            raise SettlementSourceError("DCE_BROWSER_RESPONSE_FAILED") from None

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except (OSError, websocket.WebSocketException):
                pass
        if self.forward_port is not None:
            try:
                self._adb("-s", self.device, "forward", "--remove", f"tcp:{self.forward_port}")
            except SettlementSourceError:
                pass


__all__ = ("DceSettlementBrowser",)
