#!/usr/bin/env python3
"""Coletor + servidor do painel de monitoramento da VPS.

Lê CPU, memória, disco e rede direto do Linux (/proc e /sys), guarda um
histórico em memória e serve a página (index.html) e a API (/api/metrics).
Se o Docker estiver acessível, também mostra CPU, memória e rede de cada
aplicativo (container) do EasyPanel em /api/apps.
Só usa a biblioteca padrão do Python.
"""
import base64
import hmac
import http.client
import json
import os
import shutil
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
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


# ---------- aplicativos (containers Docker do EasyPanel) ----------
# DOCKER_HOST pode ser:
#   unix:///var/run/docker.sock   (socket montado direto no container)
#   tcp://nome-do-proxy:2375      (recomendado: docker-socket-proxy somente leitura)
DOCKER_HOST = os.getenv("DOCKER_HOST", "").strip()
if not DOCKER_HOST and os.path.exists("/var/run/docker.sock"):
    DOCKER_HOST = "unix:///var/run/docker.sock"
DOCKER_ENABLED = bool(DOCKER_HOST)
APP_HISTORY = 120  # pontos de CPU por app para o mini gráfico (120 x 5 s = 10 min)


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP por cima de um socket Unix (para falar com /var/run/docker.sock)."""

    def __init__(self, path, timeout=8):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._unix_path)
        self.sock = s


def docker_get(path):
    if DOCKER_HOST.startswith("tcp://"):
        host, _, port = DOCKER_HOST[6:].rstrip("/").partition(":")
        conn = http.client.HTTPConnection(host, int(port or 2375), timeout=8)
    else:
        sock_path = DOCKER_HOST[7:] if DOCKER_HOST.startswith("unix://") else DOCKER_HOST
        conn = UnixHTTPConnection(sock_path)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"a API do Docker respondeu {resp.status}")
        return json.loads(body)
    finally:
        conn.close()


apps_state = {"enabled": DOCKER_ENABLED, "error": None, "t": 0, "apps": []}
apps_lock = threading.Lock()
_prev = {}      # id do container -> (cpu_total, sys_total, rx, tx, instante)
_app_hist = {}  # nome do serviço -> deque com o CPU% recente


def app_name(c):
    """Nome do serviço. No EasyPanel é 'projeto_servico' (ex.: evolution_monitoramento)."""
    labels = c.get("Labels") or {}
    name = labels.get("com.docker.swarm.service.name")
    if not name:
        name = (c.get("Names") or ["/?"])[0].lstrip("/")
    return name


def safe_stats(cid):
    try:
        return docker_get(f"/containers/{cid}/stats?stream=false&one-shot=true")
    except Exception:
        return None


def container_numbers(cid, st, now, host_mem):
    cs = st.get("cpu_stats", {})
    cpu_total = cs.get("cpu_usage", {}).get("total_usage", 0)
    sys_total = cs.get("system_cpu_usage", 0)
    ncpu = (cs.get("online_cpus")
            or len(cs.get("cpu_usage", {}).get("percpu_usage") or [])
            or os.cpu_count() or 1)
    nets = (st.get("networks") or {}).values()
    rx = sum(n.get("rx_bytes", 0) for n in nets)
    tx = sum(n.get("tx_bytes", 0) for n in nets)

    cpu_pct = rx_rate = tx_rate = 0.0
    prev = _prev.get(cid)
    if prev:
        dt = max(now - prev[4], 0.001)
        d_cpu, d_sys = cpu_total - prev[0], sys_total - prev[1]
        if d_sys > 0 and d_cpu >= 0:
            cpu_pct = d_cpu / d_sys * ncpu * 100
        rx_rate = max(rx - prev[2], 0) / dt
        tx_rate = max(tx - prev[3], 0) / dt
    _prev[cid] = (cpu_total, sys_total, rx, tx, now)

    ms = st.get("memory_stats", {})
    mstats = ms.get("stats", {})
    cache = mstats.get("inactive_file", mstats.get("total_inactive_file", mstats.get("cache", 0)))
    mem_used = max(ms.get("usage", 0) - cache, 0)
    limit = ms.get("limit") or 0
    mem_limit = limit if 0 < limit < host_mem * 0.98 else None  # sem limite = mostra só o uso
    return cpu_pct, mem_used, mem_limit, rx_rate, tx_rate


def sample_apps():
    now = time.time()
    containers = docker_get("/containers/json?all=1")
    running = [c for c in containers if c.get("State") == "running"]
    ids = [c["Id"] for c in running]
    with ThreadPoolExecutor(max_workers=8) as ex:
        stats = dict(zip(ids, ex.map(safe_stats, ids)))
    for cid in list(_prev):
        if cid not in stats:
            del _prev[cid]
    host_mem = read_mem()[0]

    groups = {}
    for c in containers:
        g = groups.setdefault(app_name(c), {
            "running": 0, "states": [], "status": "", "cpu": 0.0,
            "mem_used": 0, "mem_limit": None, "rx": 0.0, "tx": 0.0,
        })
        if c.get("State") == "running":
            g["running"] += 1
            g["status"] = c.get("Status", "")
            st = stats.get(c["Id"])
            if st:
                cpu, mem, lim, rx, tx = container_numbers(c["Id"], st, now, host_mem)
                g["cpu"] += cpu
                g["mem_used"] += mem
                g["rx"] += rx
                g["tx"] += tx
                if lim:
                    g["mem_limit"] = (g["mem_limit"] or 0) + lim
        else:
            g["states"].append(c.get("State", "?"))
            if not g["running"] and not g["status"]:
                g["status"] = c.get("Status", "")

    apps = []
    for key, g in groups.items():
        hist = _app_hist.setdefault(key, deque(maxlen=APP_HISTORY))
        hist.append(round(g["cpu"], 1))
        project, _, service = key.partition("_")
        if not service:
            project, service = "", key
        apps.append({
            "key": key,
            "project": project,
            "service": service,
            "replicas": g["running"],
            "state": "running" if g["running"] else (g["states"][0] if g["states"] else "unknown"),
            "status": g["status"],
            "cpu": round(g["cpu"], 1),
            "mem_used": g["mem_used"],
            "mem_limit": g["mem_limit"],
            "rx": round(g["rx"]),
            "tx": round(g["tx"]),
            "cpu_hist": list(hist),
        })
    for key in list(_app_hist):
        if key not in groups:
            del _app_hist[key]
    apps.sort(key=lambda a: a["mem_used"], reverse=True)
    return apps


def apps_loop():
    while True:
        t0 = time.time()
        try:
            apps = sample_apps()
            with apps_lock:
                apps_state.update(apps=apps, error=None, t=int(time.time()))
        except Exception as e:
            with apps_lock:
                apps_state.update(error=f"{type(e).__name__}: {e}")
        time.sleep(max(INTERVAL - (time.time() - t0), 1))


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

        if url.path == "/api/apps":
            with apps_lock:
                payload = json.dumps(apps_state).encode()
            return self._send(200, payload, "application/json")

        self._send(404, b"Nao encontrado", "text/plain; charset=utf-8")


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    if DOCKER_ENABLED:
        threading.Thread(target=apps_loop, daemon=True).start()
    print(f"Monitor rodando na porta {PORT} (coleta a cada {INTERVAL}s)", flush=True)
    print(f"Docker: {DOCKER_HOST or 'nao configurado (tabela de aplicativos desativada)'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
