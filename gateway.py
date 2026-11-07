#!/usr/bin/env python3
"""Wi-Fi LAN demo server for the smart-home power lab.

SAFETY: Never connects to mains, a distribution board, a breaker, or a relay.
All default readings are simulated. No physical control endpoint exists.
"""
from __future__ import annotations

import argparse
from collections import deque
import csv
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import secrets
import socket
import threading
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
ALLOWED = {"/": "monitor.html", "/monitor.html": "monitor.html", "/index.html": "index.html", "/mock_panel.json": "mock_panel.json"}
SCENARIOS = {"normal", "overload", "smart", "mixed"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_panel(path=ROOT / "mock_panel.json"):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data.get("source") == "SIMULATED_DATA_ONLY", "Only simulation configuration is supported"
    return data


def recommendations(zones, total_w, main_limit, devices_by_branch):
    notes = []
    for zone in sorted(zones, key=lambda item: item["ratio"], reverse=True):
        if zone["ratio"] >= 1:
            needed = max(0, zone["watts"] - zone["limitW"] * 0.8)
            alternatives = sorted((x for x in devices_by_branch.get(zone["id"], []) if x["on"] and x["priority"] > 1), key=lambda x: (-x["priority"], -x["watts"]))
            choices, freed = [], 0
            for device in alternatives:
                choices.append(device["name"])
                freed += device["watts"]
                if freed >= needed:
                    break
            action = f"建議錯峰使用：{'、'.join(choices)}。" if choices and freed >= needed else "請降低此區同時用電需求並交由專業人員確認。"
            notes.append({"level": "critical", "title": f"{zone['name']} 超出示範門檻", "detail": action + " 這不是實際斷路器動作預測。"})
        elif zone["ratio"] >= .8:
            notes.append({"level": "warning", "title": f"{zone['name']} 負載偏高", "detail": "建議將可延後使用的高功率電器改在其他時段使用；這是自訂示範門檻。"})
    if total_w > main_limit:
        notes.insert(0, {"level": "critical", "title": "示範總負載偏高", "detail": "建議錯開不同區域的高功率電器，降低同時使用的尖峰需求。"})
    if not notes:
        busiest = max(zones, key=lambda item: item["watts"], default=None)
        if busiest:
            notes.append({"level": "info", "title": "目前最高用電區域", "detail": f"{busiest['name']} 約 {busiest['watts']:.0f} W；目前均未超過自訂示範門檻。"})
    return notes


def compute_status(config, scenario="normal", telemetry=None):
    panel = config["panel"]
    v = float(panel["voltageV"])
    main_limit = v * float(panel["mainBreaker"]["ratingA"])
    by_branch = {b["id"]: [] for b in panel["branches"]}
    if telemetry is None:
        scen = next((s for s in config["scenarios"] if s["id"] == scenario), None)
        if scen is None:
            raise ValueError("unknown scenario")
        enabled = set(scen["enabled"])
        for dev in config["devices"]:
            x = dict(dev)
            x["on"] = x["id"] in enabled
            by_branch[x["branchId"]].append(x)
        power_by_branch = {key: sum(d["watts"] for d in devs if d["on"]) for key,devs in by_branch.items()}
        requested_by_branch = dict(power_by_branch)
        deferred = {}
        if scen.get("smart"):
            # Demo-only *voluntary* time shifting; no switches, relays or breakers are controlled.
            for branch in panel["branches"]:
                bid = branch["id"]
                target = v * float(branch["ratingA"]) * 0.8
                if power_by_branch[bid] <= target:
                    continue
                candidates = sorted(
                    (d for d in by_branch[bid] if d["on"] and d["priority"] > 1),
                    key=lambda d: (-d["priority"], -d["watts"]),
                )
                for device in candidates:
                    if power_by_branch[bid] <= target:
                        break
                    power_by_branch[bid] -= device["watts"]
                    deferred.setdefault(bid, []).append(device["name"])
        mode = "SIMULATED_DATA_ONLY"
        description = "JSON 示範情境，非實測電流"
        source_ts = utc_now()
        stale = False
    else:
        if not isinstance(telemetry, dict) or not isinstance(telemetry.get("zones"), dict):
            raise ValueError("telemetry needs zones object")
        vals = telemetry["zones"]
        power_by_branch = {}
        requested_by_branch = None
        deferred = {}
        for branch in panel["branches"]:
            value = vals.get(branch["id"], 0)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not (0 <= value < 100000):
                raise ValueError("invalid zone watts")
            power_by_branch[branch["id"]] = float(value)
        mode = "EXTERNAL_UNVERIFIED"
        description = "外部 JSON 輸入（未驗證量測來源）"
        source_ts = telemetry.get("timestamp") or "未提供來源時間"
        stale = True
        if telemetry.get("timestamp"):
            try:
                t = datetime.fromisoformat(telemetry["timestamp"].replace("Z", "+00:00"))
                stale = abs((datetime.now(timezone.utc) - t.astimezone(timezone.utc)).total_seconds()) > 120
            except (ValueError, TypeError):
                stale = True
    zones = []
    for branch in panel["branches"]:
        bid = branch["id"]
        watts = float(power_by_branch[bid])
        limit_w = v * float(branch["ratingA"])
        ratio = watts / limit_w if limit_w > 0 else 0
        zones.append({
            "id": bid,
            "name": branch.get("areaLabel") or branch["name"],
            "branch": branch["name"],
            "watts": round(watts, 2),
            "requestedW": round(requested_by_branch[bid] if requested_by_branch is not None else watts, 2),
            "deferredDevices": deferred.get(bid, []),
            "currentA": round(watts / v, 2),  # simplified demo estimate, NOT measured A
            "limitW": round(limit_w, 2),
            "ratio": round(ratio, 4),
            "status": "over" if ratio >= 1 else "high" if ratio >= .8 else "normal",
            "devices": [x["name"] for x in by_branch[bid] if x["on"]] if telemetry is None else [],
        })
    total = sum(x["watts"] for x in zones)
    notes = recommendations(zones, total, main_limit, by_branch)
    for zone in zones:
        if zone["deferredDevices"]:
            notes.insert(0, {"level":"info", "title":f"{zone['name']} 示範錯峰安排", "detail":
                f"原需求 {zone['requestedW']:.0f} W，改為 {zone['watts']:.0f} W；建議稍後再使用：{'、'.join(zone['deferredDevices'])}。本網站沒有真的關閉設備。"})
    return {
        "timestamp": utc_now(), "sourceTimestamp": source_ts,
        "source": mode, "sourceDescription": description, "stale": stale,
        "scenario": scenario if telemetry is None else "external-file",
        "panel": panel["name"], "mainLimitW": round(main_limit, 2),
        "totalW": round(total, 2), "totalAEstimated": round(total/v, 2),
        "zones": zones,
        "recommendations": notes,
        "safetyNote": "教學展示／用電分配建議，不是實體過電流保護或遠端斷電；實際配電請交由專業人員。",
    }


class State:
    def __init__(self, config, telemetry_path=None):
        self.lock = threading.Lock()
        self.config = config
        self.scenario = "normal"
        self.telemetry_path = telemetry_path
        self.history = deque(maxlen=720)
        self.latest = None

    def sample(self):
        with self.lock:
            external = None
            if self.telemetry_path is not None:
                try:
                    external = json.loads(self.telemetry_path.read_text(encoding="utf-8"))
                    status = compute_status(self.config, telemetry=external)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    status = compute_status(self.config, scenario=self.scenario)
                    status["source"] = "INPUT_ERROR_FALLBACK_SIMULATED"
                    status["sourceDescription"] = "外部 JSON 無法讀取；已改用虛構資料（"+type(exc).__name__+"）"
                    status["stale"] = True
            else:
                status = compute_status(self.config, scenario=self.scenario)
            self.latest = status
            self.history.append({"timestamp": status["timestamp"], "totalW": status["totalW"], **{z["id"]:z["watts"] for z in status["zones"]}})
            return status

    def change_scenario(self, scenario):
        if self.telemetry_path is not None or scenario not in SCENARIOS:
            return False
        with self.lock:
            self.scenario = scenario
        self.sample()
        return True


def local_ip():
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 1))  # no traffic sent
            return sock.getsockname()[0]
    except OSError:
        return "你的電腦局域網 IP"


def make_handler(state, pin):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PowerLabDemo/0.3"
        def log_message(self, fmt, *args):
            print("[LAN] "+fmt%args)
        def _send(self, body, typ, status=200, extra=None):
            self.send_response(status)
            self.send_header("Content-Type",typ)
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if extra:
                for key,val in extra.items(): self.send_header(key,val)
            self.end_headers()
            self.wfile.write(body)
        def _json(self, data, code=200):
            self._send(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8",code)
        def _authorized(self):
            # Comparison is constant-time. This is a classroom pairing code, not production authentication.
            if not secrets.compare_digest(self.headers.get("X-Demo-Pin", ""), pin):
                self._json({"error":"請在手機頁面輸入啟動時顯示的 6 位示範碼"},401)
                return False
            return True
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/status":
                if self._authorized():
                    with state.lock: d=state.latest
                    self._json(d)
            elif path == "/api/history":
                if self._authorized():
                    with state.lock: d=list(state.history)
                    self._json({"items":d})
            elif path in ALLOWED:
                name = ALLOWED[path]
                typ = "text/html; charset=utf-8" if name.endswith(".html") else "application/json; charset=utf-8"
                self._send((ROOT/name).read_bytes(),typ)
            else:
                self._json({"error":"not found"},404)
        def do_POST(self):
            if urlparse(self.path).path != "/api/demo/scenario":
                self._json({"error":"not found"},404); return
            if not self._authorized(): return
            try:
                ln = int(self.headers.get("Content-Length","0"))
                if ln<2 or ln>256: raise ValueError("bad size")
                data = json.loads(self.rfile.read(ln))
                scene = data["scenario"]
                if not isinstance(scene,str): raise ValueError("bad value")
                if not state.change_scenario(scene):
                    self._json({"error":"只允許切換虛構情境；外部資料模式不能切換"},400);return
                self._json({"ok":True,"scenario":scene})
            except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                self._json({"error":"invalid request"},400)
    return Handler


def main():
    parser=argparse.ArgumentParser(description="Wi-Fi read-only home power monitoring classroom prototype")
    parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--host",default="0.0.0.0",help="LAN binding; do not port-forward")
    parser.add_argument("--telemetry",type=Path,help="optional local JSON data adapter (unverified, no direct mains)")
    parser.add_argument("--pin",help=argparse.SUPPRESS)
    args=parser.parse_args()
    pin=args.pin or f"{secrets.randbelow(900000)+100000}"
    state=State(load_panel(),args.telemetry)
    state.sample()
    stop=threading.Event()
    def worker():
        while not stop.wait(5): state.sample()
    threading.Thread(target=worker,daemon=True).start()
    ip=local_ip()
    print("\n=== 家庭用電 Wi-Fi 示範原型｜僅模擬讀值／不控制電箱 ===")
    print(f"電腦瀏覽： http://127.0.0.1:{args.port}/monitor.html")
    print(f"同一 Wi-Fi 的手機： http://{ip}:{args.port}/monitor.html")
    print(f"本次示範碼（手機輸入）： {pin}")
    print("請勿轉發連結或做路由器 Port Forwarding；不是正式資安或電氣安全產品。\n")
    server=ThreadingHTTPServer((args.host,args.port),make_handler(state,pin))
    try:server.serve_forever()
    except KeyboardInterrupt:print("\n已停止")
    finally:stop.set();server.server_close()

if __name__=="__main__": main()
