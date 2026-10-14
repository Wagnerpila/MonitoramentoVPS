#!/usr/bin/env python3
"""Coletor + servidor do painel de monitoramento da VPS.

Lê CPU, memória, disco e rede direto do Linux (/proc e /sys), guarda um
histórico em memória e serve a página (index.html) e a API (/api/metrics).
Só usa a biblioteca padrão do Python.
"""
import base64
import hmac
import json
import os
import shutil
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.getenv("PORT", "8080"))
INTERVAL = int(os.getenv("SAMPLE_INTERVAL", "5"))      # segundos entre coletas
HISTORY = int(os.getenv("HISTORY_POINTS", "720"))      # 720 x 5s = 1 hora
DISK_PATH = os.getenv("DISK_PATH", "/")
HOST_LABEL = os.getenv("HOST_LABEL", "VPS")
AUTH_USER = os.getenv("DASH_USER", "")
AUTH_PASS = os.getenv("DASH_PASS", "")

# Se a pasta /sys do host estiver montada em /host/sys, as interfaces de rede
# lidas são as da VPS e não as do container.
SYS_NET = "/host/sys/class/net" if os.path.isdir("/host/sys/class/net") else "/sys/class/net"

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "index.html"), "rb") as f:
    INDEX_HTML = f.read()


# ---------- leitura das métricas ----------
def read_cpu():
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    return sum(vals[:8]), idle


def read_mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":")
            info[key] = int(val.split()[0]) * 1024
    total = info["MemTotal"]
    available = info.get("MemAvailable", info.get("MemFree", 0))
    swap_total = info.get("SwapTotal", 0)
    swap_used = swap_total - info.get("SwapFree", 0)
    return total, total - available, swap_total, swap_used


def net_ifaces():
    names = []
    try:
        for n in os.listdir(SYS_NET):
            # interfaces físicas/virtio têm "device"; veth, docker0, br-* não
            if n != "lo" and os.path.exists(f"{SYS_NET}/{n}/device"):
                names.append(n)
        if not names:
            names = [n for n in os.listdir(SYS_NET)
                     if not n.startswith(("lo", "veth", "docker", "br-"))]
    except OSError:
        pass
    return names


def read_net():
    rx = tx = 0
    for n in net_ifaces():
        try:
            with open(f"{SYS_NET}/{n}/statistics/rx_bytes") as f:
                rx += int(f.read())
            with open(f"{SYS_NET}/{n}/statistics/tx_bytes") as f:
                tx += int(f.read())
        except OSError:
            pass
    return rx, tx


def read_uptime():
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


# ---------- coleta em segundo plano ----------
history = deque(maxlen=HISTORY)
latest = {}
lock = threading.Lock()


def sampler():
    prev_cpu = read_cpu()
    prev_net = read_net()
    prev_t = time.time()
    time.sleep(INTERVAL)
    while True:
        now = time.time()
        cpu = read_cpu()
        net = read_net()
        dt = max(now - prev_t, 0.001)

        d_total = cpu[0] - prev_cpu[0]
        d_idle = cpu[1] - prev_cpu[1]
        cpu_pct = 100.0 * (1 - d_idle / d_total) if d_total > 0 else 0.0
        rx_rate = max(net[0] - prev_net[0], 0) / dt
        tx_rate = max(net[1] - prev_net[1], 0) / dt

        mem_total, mem_used, swap_total, swap_used = read_mem()
        disk = shutil.disk_usage(DISK_PATH)
        load = os.getloadavg()

        point = {
            "t": int(now),
            "cpu": round(cpu_pct, 1),
            "mem": round(100.0 * mem_used / mem_total, 1),
            "rx": round(rx_rate),
            "tx": round(tx_rate),
        }
        snapshot = {
            **point,
            "label": HOST_LABEL,
            "cpus": os.cpu_count(),
            "load": [round(x, 2) for x in load],
            "uptime": int(read_uptime()),
            "mem_total": mem_total,
            "mem_used": mem_used,
            "swap_total": swap_total,
            "swap_used": swap_used,
            "disk_total": disk.total,
            "disk_used": disk.used,
            "rx_total": net[0],
            "tx_total": net[1],
            "ifaces": net_ifaces(),
        }
        with lock:
            history.append(point)
            latest.clear()
            latest.update(snapshot)

        prev_cpu, prev_net, prev_t = cpu, net, now
        time.sleep(max(INTERVAL - (time.time() - now), 0.5))


# ---------- servidor HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "VPSMonitor/1.0"

    def log_message(self, fmt, *args):  # silencia o log de cada requisição
        pass

    def _authorized(self):
        if not (AUTH_USER and AUTH_PASS):
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                ok_user = hmac.compare_digest(user, AUTH_USER)
                ok_pass = hmac.compare_digest(pwd, AUTH_PASS)
                return ok_user and ok_pass
            except Exception:
                return False
        return False

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)

        if url.path == "/healthz":
            return self._send(200, b"ok", "text/plain")

        if not self._authorized():
            return self._send(
                401, b"Acesso restrito", "text/plain; charset=utf-8",
                {"WWW-Authenticate": 'Basic realm="Monitor VPS"'},
            )

        if url.path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")

        if url.path == "/api/metrics":
            since = int(parse_qs(url.query).get("since", ["0"])[0] or 0)
            with lock:
                pts = [p for p in history if p["t"] > since]
                payload = {"now": dict(latest), "history": pts, "interval": INTERVAL}
            return self._send(200, json.dumps(payload).encode(), "application/json")

        self._send(404, b"Nao encontrado", "text/plain; charset=utf-8")


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    print(f"Monitor rodando na porta {PORT} (coleta a cada {INTERVAL}s)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
