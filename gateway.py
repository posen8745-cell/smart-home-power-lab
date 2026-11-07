#!/usr/bin/env python3
"""Classroom-only Wi-Fi power dashboard. Never connects to or switches mains.

Default values are fictitious. External JSON is UNVERIFIED and can never be
silently replaced by a convincing simulated 'healthy' reading after an error.
"""
from __future__ import annotations

import argparse
from collections import deque, defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import threading
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
ALLOWED = {"/": "monitor.html", "/monitor.html": "monitor.html", "/index.html": "index.html", "/mock_panel.json": "mock_panel.json", "/README.md": "README.md", "/PRODUCT_PLAN.md": "PRODUCT_PLAN.md"}
SCENARIOS = {"normal", "overload", "smart", "mixed"}
SAMPLE_SECONDS = 5
MAX_GAP_SECONDS = 15
SOURCE_MAX_AGE_SECONDS = 120


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_panel(path=ROOT / "mock_panel.json"):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("source") != "SIMULATED_DATA_ONLY" or not isinstance(data.get("panel", {}).get("branches"), list):
        raise ValueError("invalid simulation configuration")
    return data


def _branch_electrical(panel, branch):
    v = float(branch.get("voltageV", panel.get("voltageV", 110)))
    pf = float(branch.get("powerFactorAssumed", panel.get("powerFactorAssumed", 1)))
    amps = float(branch["ratingA"])
    if not (0 < v <= 240 and 0 < pf <= 1 and 0 < amps <= 200):
        raise ValueError("invalid electrical example")
    return v, pf, amps


def _reading_age(timestamp):
    if not isinstance(timestamp, str):
        return None
    try:
        d = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if d.tzinfo is None:
            return None
        return (datetime.now(timezone.utc) - d.astimezone(timezone.utc)).total_seconds()
    except (ValueError, OverflowError):
        return None


def recommendations(zones, total_w, main_limit, devices_by_branch, quality_ok=True):
    if not quality_ok:
        return [{"level": "critical", "title": "量測資料不可用或過期", "detail": "請檢查資料來源與更新時間，不要將舊讀值當作即時負載。"}]
    notes = []
    for z in sorted(zones, key=lambda row: row["ratio"], reverse=True):
        if z["ratio"] >= 1:
            choices = sorted((x for x in devices_by_branch.get(z["id"], []) if x.get("on") and x.get("priority", 1) > 1), key=lambda d: (-d["priority"], -d["watts"]))
            to_free = max(0, z["watts"] - z["limitW"] * .8)
            chosen, freed = [], 0
            for d in choices:
                chosen.append(d["name"])
                freed += d["watts"]
                if freed >= to_free:
                    break
            if freed < to_free:
                detail = "同時負載高，建議由住戶先檢視非必要電器；不可依軟體自行改動電箱。"
            else:
                detail = f"建議錯峰使用：將{'、'.join(chosen)}移到不同時段（非遠端斷電）。"
            notes.append({"level": "critical", "title": f"{z['name']} 超過示範門檻", "detail": detail + " 此門檻不是實際斷路器跳脫預測。"})
        elif z["ratio"] >= .8:
            notes.append({"level": "warning", "title": f"{z['name']} 負載偏高", "detail": "可考慮錯開高功率電器的同時使用時間；不代表電費一定會降低。"})
    if main_limit is not None and total_w > main_limit:
        notes.insert(0, {"level": "critical", "title": "示範總負載偏高", "detail": "建議檢查各分路用電，同時使用尖峰不等於耗電總度數。"})
    if not notes and zones:
        b = max(zones, key=lambda z: z["watts"])
        notes.append({"level": "info", "title": "目前用電最多的分路", "detail": f"{b['name']} 約 {b['watts']:.0f} W（{b['branch']}）；此為瞬時功率，不是累計度數。"})
    return notes


def unavailable_status(config, reason):
    return {
        "timestamp": utc_now(), "sourceTimestamp": None, "source": "EXTERNAL_UNAVAILABLE",
        "sourceDescription": "外部資料不可用；不以虛構資料冒充即時讀值", "stale": True,
        "readingAgeSeconds": None, "scenario": "external-file", "panel": config["panel"]["name"],
        "mappingVerified": False, "mainLimitW": None, "totalW": None,
        "totalAEstimated": None, "zones": [], "recommendations": recommendations([], 0, None, {}, quality_ok=False),
        "error": str(reason)[:180],
        "safetyNote": "資料中斷時不顯示綠色安全狀態。本頁不是電氣保護器。",
    }


def compute_status(config, scenario="normal", telemetry=None):
    panel = config["panel"]
    branch_defs = panel["branches"]
    ids = [b["id"] for b in branch_defs]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError("duplicate or empty branch identifiers")
    by_branch = {bid: [] for bid in ids}
    source_ts = utc_now()
    stale = False
    age = 0
    deferred = {}
    if telemetry is None:
        scen = next((s for s in config["scenarios"] if s["id"] == scenario), None)
        if not scen:
            raise ValueError("unknown scenario")
        enabled = set(scen["enabled"])
        for dev in config["devices"]:
            if dev["branchId"] not in by_branch:
                raise ValueError("device assigned to unknown branch")
            x = dict(dev)
            x["on"] = x["id"] in enabled
            by_branch[x["branchId"]].append(x)
        requested = {bid: sum(d["watts"] for d in ds if d["on"]) for bid, ds in by_branch.items()}
        powers = dict(requested)
        if scen.get("smart"):
            # Schedule-only demo: adjusts hypothetical concurrent loads, not physical devices.
            for branch in branch_defs:
                bid = branch["id"]
                v, pf, rating = _branch_electrical(panel, branch)
                target = v * pf * rating * .8
                if powers[bid] <= target:
                    continue
                for d in sorted((x for x in by_branch[bid] if x["on"] and x["priority"] > 1), key=lambda x: (-x["priority"], -x["watts"])):
                    if powers[bid] <= target:
                        break
                    powers[bid] -= d["watts"]
                    deferred.setdefault(bid, []).append(d["name"])
        mode = "SIMULATED_DATA_ONLY"
        desc = "虛構 JSON 情境（不是電箱讀值）"
    else:
        if not isinstance(telemetry, dict) or not isinstance(telemetry.get("zones"), dict):
            raise ValueError("external JSON requires zones object")
        vals = telemetry["zones"]
        if any(bid not in vals for bid in ids):
            raise ValueError("external data missing one or more branches")
        powers = {}
        for bid in ids:
            value = vals[bid]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0 <= value < 100000):
                raise ValueError("invalid external W value")
            powers[bid] = float(value)
        requested = dict(powers)
        mode = "EXTERNAL_UNVERIFIED"
        desc = "外部 JSON（來源與計量精度未驗證）"
        source_ts = telemetry.get("timestamp")
        age = _reading_age(source_ts)
        stale = age is None or age < -10 or age > SOURCE_MAX_AGE_SECONDS
    zones = []
    for branch in branch_defs:
        bid = branch["id"]
        v, pf, amp = _branch_electrical(panel, branch)
        # v*PF*I is a simplified classroom model, not a breaker protection curve.
        max_w = v * pf * amp
        watts = float(powers[bid])
        ratio = watts / max_w
        zones.append({
            "id": bid, "name": branch.get("areaLabel") or branch["name"],
            "branch": branch["name"], "voltageV": v, "pfAssumed": pf,
            "mappingVerified": bool(branch.get("mappingVerified", False)),
            "watts": round(watts, 2), "requestedW": round(requested[bid], 2),
            "deferredDevices": deferred.get(bid, []),
            "currentA": round(watts / (v * pf), 2), "currentIsEstimated": True,
            "limitW": round(max_w, 2), "ratio": round(ratio, 4),
            "status": "over" if ratio >= 1 else "high" if ratio >= .8 else "normal",
            "devices": [d["name"] for d in by_branch[bid] if d["on"]] if telemetry is None else [],
        })
    total_w = sum(z["watts"] for z in zones)
    voltages = {z["voltageV"] for z in zones}
    pfs = {z["pfAssumed"] for z in zones}
    # Total current is NOT additive between mixed-voltage circuits.
    main_w = round(float(panel["mainBreaker"]["ratingA"]) * next(iter(voltages)) * next(iter(pfs)), 2) if len(voltages) == 1 and len(pfs) == 1 else None
    main_a = round(total_w / next(iter(voltages)) / next(iter(pfs)), 2) if main_w is not None else None
    notes = recommendations(zones, total_w, main_w, by_branch, quality_ok=not stale)
    for z in zones:
        if z["deferredDevices"]:
            notes.insert(0, {"level": "info", "title": f"{z['name']} 示範錯峰安排", "detail": f"原同時需求 {z['requestedW']:.0f} W，改為 {z['watts']:.0f} W；稍後使用：{'、'.join(z['deferredDevices'])}。同樣使用時間下，總度數不會因錯峰自動下降。"})
    return {
        "timestamp": utc_now(), "sourceTimestamp": source_ts, "source": mode,
        "sourceDescription": desc, "stale": stale, "readingAgeSeconds": round(age, 1) if age is not None else None,
        "scenario": scenario if telemetry is None else "external-file", "panel": panel["name"],
        "mappingVerified": bool(panel.get("mappingVerified", False)),
        "mainLimitW": main_w, "totalW": round(total_w, 2), "totalAEstimated": main_a,
        "zones": zones, "recommendations": notes,
        "safetyNote": "僅為資料展示與用電安排建議；非即時電氣安全或遠端跳脫系統。",
    }


class State:
    def __init__(self, config, telemetry_path=None):
        self.lock = threading.Lock()
        self.config = config
        self.scenario = "normal"
        self.telemetry_path = telemetry_path
        self.history = deque(maxlen=720)
        self.latest = None
        self.energy = defaultdict(float)
        self.session_seconds = 0.0
        self.last_mono = None
        self.previous = None

    def sample(self, now_mono=None):
        now_mono = time.monotonic() if now_mono is None else now_mono
        with self.lock:
            if self.telemetry_path is None:
                status = compute_status(self.config, self.scenario)
            else:
                try:
                    raw = json.loads(self.telemetry_path.read_text(encoding="utf-8"))
                    status = compute_status(self.config, telemetry=raw)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    status = unavailable_status(self.config, type(exc).__name__)
            # Integrate the previous valid reading over a bounded elapsed interval.
            # Missing samples, stale input or scenario switches are not silently filled.
            if (self.previous is not None and self.last_mono is not None
                    and 0 < now_mono - self.last_mono <= MAX_GAP_SECONDS
                    and not self.previous["stale"] and not status["stale"]
                    and status["source"] == self.previous["source"]
                    and status["scenario"] == self.previous["scenario"]):
                dt = now_mono - self.last_mono
                self.session_seconds += dt
                for z in self.previous["zones"]:
                    self.energy[z["id"]] += z["watts"] * dt / 3_600_000
            status["energySinceStartKWh"] = round(sum(self.energy.values()), 7)
            status["energyByBranchKWh"] = {b["id"]: round(self.energy[b["id"]], 7) for b in self.config["panel"]["branches"]}
            status["integratedSeconds"] = round(self.session_seconds, 1)
            status["energyNote"] = "只積分本次程式執行期間的連續有效資料；非完整日／月度數，也不能核對帳單。"
            self.latest = status
            self.history.append({"timestamp":status["timestamp"], "totalW":status["totalW"], "source":status["source"], "stale":status["stale"], "energySinceStartKWh":status["energySinceStartKWh"]})
            self.previous = status
            self.last_mono = now_mono
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
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 1))  # no packet sent
            return sock.getsockname()[0]
    except OSError:
        return "此電腦區域網 IP"


def make_handler(state, pin):
    failures = defaultdict(deque)
    guard = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        server_version = "PowerLabDemo/0.4"
        def log_message(self, fmt, *args):
            print("[LAN] " + fmt % args)
        def _send(self, body, typ, status=200):
            self.send_response(status)
            for key, val in {
                "Content-Type":typ, "Content-Length":str(len(body)), "Cache-Control":"no-store",
                "X-Content-Type-Options":"nosniff", "Referrer-Policy":"no-referrer",
                "X-Frame-Options":"DENY", "Content-Security-Policy":"frame-ancestors 'none'",
            }.items(): self.send_header(key,val)
            self.end_headers()
            self.wfile.write(body)
        def _json(self, data, code=200):
            self._send(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8", code)
        def _authorized(self):
            ip = self.client_address[0]
            with guard:
                seq = failures[ip]
                while seq and time.monotonic()-seq[0] > 60:
                    seq.popleft()
                if len(seq) >= 10:
                    self._json({"error":"示範碼錯誤過多，請稍後再試"},429)
                    return False
            ok = secrets.compare_digest(self.headers.get("X-Demo-Pin", ""), pin)
            if not ok:
                with guard: failures[ip].append(time.monotonic())
                self._json({"error":"請輸入本次啟動的 6 位示範碼"},401)
            else:
                with guard: failures[ip].clear()
            return ok
        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/status":
                if self._authorized():
                    with state.lock: data=state.latest
                    self._json(data)
            elif path == "/api/history":
                if self._authorized():
                    with state.lock: data=list(state.history)
                    self._json({"items":data})
            elif path in ALLOWED:
                name=ALLOWED[path]
                typ="text/html; charset=utf-8" if name.endswith(".html") else "application/json; charset=utf-8" if name.endswith(".json") else "text/markdown; charset=utf-8"
                self._send((ROOT/name).read_bytes(),typ)
            else:
                self._json({"error":"not found"},404)
        def do_POST(self):
            if urlparse(self.path).path != "/api/demo/scenario":
                self._json({"error":"not found"},404)
                return
            if not self._authorized(): return
            try:
                size=int(self.headers.get("Content-Length","0"))
                if not 2<=size<=256: raise ValueError("size")
                content=json.loads(self.rfile.read(size))
                if not isinstance(content,dict) or not isinstance(content.get("scenario"),str): raise ValueError("input")
                if not state.change_scenario(content["scenario"]):
                    self._json({"error":"僅能切換虛構情境；外部資料模式禁止切換"},400)
                    return
                self._json({"ok":True, "scenario":content["scenario"]})
            except (ValueError, KeyError, TypeError):
                self._json({"error":"invalid request"},400)
    return Handler


def main():
    p=argparse.ArgumentParser(description="Wi-Fi LAN classroom demo, no physical switching")
    p.add_argument("--port",type=int,default=8765)
    p.add_argument("--host",default="0.0.0.0",help="LAN binding; never port-forward")
    p.add_argument("--telemetry",type=Path,help="unverified external JSON data file")
    p.add_argument("--pin",help=argparse.SUPPRESS)
    args=p.parse_args()
    pin=args.pin or f"{secrets.randbelow(900000)+100000}"
    state=State(load_panel(),args.telemetry)
    state.sample()
    stop=threading.Event()
    def worker():
        while not stop.wait(SAMPLE_SECONDS): state.sample()
    threading.Thread(target=worker, daemon=True).start()
    print("\n=== PowerLab v4｜教學模擬與用電分配；無電箱連線、無跳脫控制 ===")
    print(f"電腦： http://127.0.0.1:{args.port}/monitor.html")
    print(f"手機同 Wi-Fi： http://{local_ip()}:{args.port}/monitor.html")
    print(f"啟動示範碼： {pin}")
    print("不可向網際網路開放埠口；此 HTTP+短碼僅限可信任區域網示範。\n")
    server=ThreadingHTTPServer((args.host,args.port),make_handler(state,pin))
    try: server.serve_forever()
    except KeyboardInterrupt: print("\n已停止")
    finally: stop.set();server.server_close()

if __name__ == "__main__": main()
