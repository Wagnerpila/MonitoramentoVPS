#!/usr/bin/env python3
"""Coletor + servidor do painel de monitoramento da VPS.

Lê CPU, memória, disco e rede direto do Linux (/proc e /sys), guarda um
histórico em memória e serve a página (index.html) e a API (/api/metrics).
Se o Docker estiver acessível, também mostra CPU, memória e rede de cada
aplicativo (container) do EasyPanel em /api/apps.
Se a Evolution API estiver configurada, avisa por WhatsApp quando algo passa
do limite ou quando um aplicativo para (veja a seção de alertas abaixo).
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
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

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
_app_hist = {}  # chave do app -> deque de pontos {t, cpu, mem, rx, tx} (a hora mais recente)
hist_lock = threading.Lock()
_inspect = {}   # id do container -> (instante, detalhes): reinícios, início e saúde mudam pouco
INSPECT_EVERY = 60  # segundos entre consultas de detalhes do mesmo container


def short_image(name):
    """'n8nio/n8n:latest@sha256:abc...' -> 'n8nio/n8n:latest'."""
    if name.startswith("sha256:"):
        return name[7:19]
    return name.split("@", 1)[0]


def parse_docker_time(s):
    """'2026-10-12T10:11:12.123456789Z' -> epoch; None se o container nunca iniciou."""
    try:
        if not s or s.startswith("0001-"):
            return None
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def container_details(cid, now):
    """Reinícios, hora de início e saúde. Consulta no máximo 1x por minuto por container."""
    hit = _inspect.get(cid)
    if hit and now - hit[0] < INSPECT_EVERY:
        return hit[1]
    try:
        d = docker_get(f"/containers/{cid}/json")
        state = d.get("State") or {}
        info = {
            "restarts": int(d.get("RestartCount") or 0),
            "started": parse_docker_time(state.get("StartedAt")),
            "health": (state.get("Health") or {}).get("Status"),
            "oom": bool(state.get("OOMKilled")),
        }
    except Exception:
        info = hit[1] if hit else None  # falhou (ex.: proxy sem permissão): mantém o último valor
    _inspect[cid] = (now, info)
    return info


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

    # detalhes que mudam pouco (reinícios, início, saúde): em paralelo e com cache
    with ThreadPoolExecutor(max_workers=8) as ex:
        details = dict(zip(ids, ex.map(lambda i: container_details(i, now), ids)))
    for cid in list(_inspect):
        if cid not in stats:
            del _inspect[cid]

    groups = {}
    for c in containers:
        g = groups.setdefault(app_name(c), {
            "running": 0, "states": [], "status": "", "cpu": 0.0,
            "mem_used": 0, "mem_limit": None, "rx": 0.0, "tx": 0.0,
            "image": "", "old": 0, "restarts": 0, "started": None,
            "health": None, "oom": False, "list": [],
        })
        row = {
            "id": c["Id"][:12],
            "name": (c.get("Names") or ["/?"])[0].lstrip("/"),
            "state": c.get("State", "?"),
            "status": c.get("Status", ""),
            "cpu": 0.0, "mem_used": 0, "rx": 0, "tx": 0, "started": None,
        }
        if c.get("State") == "running":
            g["running"] += 1
            g["status"] = c.get("Status", "")
            g["image"] = g["image"] or short_image(c.get("Image", ""))
            st = stats.get(c["Id"])
            if st:
                cpu, mem, lim, rx, tx = container_numbers(c["Id"], st, now, host_mem)
                g["cpu"] += cpu
                g["mem_used"] += mem
                g["rx"] += rx
                g["tx"] += tx
                if lim:
                    g["mem_limit"] = (g["mem_limit"] or 0) + lim
                row.update(cpu=round(cpu, 1), mem_used=mem, rx=round(rx), tx=round(tx))
            d = details.get(c["Id"])
            if d:
                g["restarts"] += d["restarts"]
                if d["started"] and (g["started"] is None or d["started"] < g["started"]):
                    g["started"] = d["started"]
                if d["health"] == "unhealthy" or (d["health"] and g["health"] != "unhealthy"):
                    g["health"] = d["health"]
                g["oom"] = g["oom"] or d["oom"]
                row["started"] = d["started"]
        else:
            g["states"].append(c.get("State", "?"))
            g["old"] += 1  # container parado do mesmo serviço (deploy anterior ou falha) que o Docker ainda guarda
            g["image"] = g["image"] or short_image(c.get("Image", ""))
            if not g["running"] and not g["status"]:
                g["status"] = c.get("Status", "")
        g["list"].append(row)

    apps = []
    for key, g in groups.items():
        with hist_lock:
            hist = _app_hist.setdefault(key, deque(maxlen=HISTORY))
            hist.append({"t": int(now), "cpu": round(g["cpu"], 1), "mem": g["mem_used"],
                         "rx": round(g["rx"]), "tx": round(g["tx"])})
            recent_cpu = [p["cpu"] for p in list(hist)[-APP_HISTORY:]]
        project, _, service = key.partition("_")
        if not service:
            project, service = "", key
        g["list"].sort(key=lambda r: (r["state"] != "running", r["name"]))
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
            "cpu_hist": recent_cpu,
            "image": g["image"],
            "restarts": g["restarts"],
            "old": g["old"],
            "started": g["started"],
            "health": g["health"],
            "oom": g["oom"],
            "containers": g["list"],
        })
    with hist_lock:
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


# ---------- alertas por WhatsApp (Evolution API) ----------
def _num(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return float(default)


EVO_URL = os.getenv("EVOLUTION_URL", "").strip().rstrip("/")
EVO_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "").strip()
EVO_KEY = os.getenv("EVOLUTION_APIKEY", "").strip()
ALERT_NUMBERS = []
for _n in os.getenv("ALERT_NUMBER", "").split(","):
    _digits = "".join(ch for ch in _n if ch.isdigit())
    if _digits:
        ALERT_NUMBERS.append(_digits)
ALERTS_ENABLED = bool(EVO_URL and EVO_INSTANCE and EVO_KEY and ALERT_NUMBERS)

# Limites (em %). Use 0 para desligar um alerta.
THRESHOLDS = {
    "cpu": _num("ALERT_CPU_PCT", 90),          # CPU da VPS
    "mem": _num("ALERT_MEM_PCT", 90),          # memória da VPS
    "disk": _num("ALERT_DISK_PCT", 85),        # disco da VPS
    "app_cpu": _num("ALERT_APP_CPU_PCT", 90),  # CPU de um app (100 = 1 vCPU inteira)
    "app_mem": _num("ALERT_APP_MEM_PCT", 90),  # memória de um app, em % do limite dele
}
SUSTAIN = _num("ALERT_SUSTAIN_SECONDS", 120)    # quanto tempo o problema precisa durar
DOWN_AFTER = _num("ALERT_DOWN_SECONDS", 60)     # quanto tempo um app parado precisa durar
COOLDOWN = _num("ALERT_COOLDOWN_MINUTES", 30) * 60  # intervalo entre lembretes
LOCAL_TZ = timezone(timedelta(hours=_num("TZ_OFFSET_HOURS", -3)))  # Brasília = -3

alert_lock = threading.RLock()
alert_state = {}                  # chave do alerta -> estado
alert_events = deque(maxlen=20)   # últimos envios
seen_running = set()              # apps que já vimos rodando (só esses geram alerta de "parado")


def missing_alert_vars():
    pairs = [("EVOLUTION_URL", EVO_URL), ("EVOLUTION_INSTANCE", EVO_INSTANCE),
             ("EVOLUTION_APIKEY", EVO_KEY), ("ALERT_NUMBER", ALERT_NUMBERS)]
    return [name for name, value in pairs if not value]


def hb(b):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.0f} {unit}" if unit in ("B", "KB") else f"{b:.1f} {unit}"
        b /= 1024


def _post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "apikey": EVO_KEY},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def _send_one(url, number, text):
    # Evolution v2 usa {"number","text"}; a v1 usa {"number","textMessage":{"text"}}.
    # Só tentamos o formato antigo se o novo for recusado com 400.
    bodies = ({"number": number, "text": text},
              {"number": number, "textMessage": {"text": text}})
    last = ""
    for body in bodies:
        try:
            _post_json(url, body)
            return True, "ok"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:150].decode(errors='replace')}"
            if e.code != 400:
                break
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
    return False, last


def send_whatsapp(text):
    if not ALERTS_ENABLED:
        return False, "alertas não configurados (faltam: " + ", ".join(missing_alert_vars()) + ")"
    url = f"{EVO_URL}/message/sendText/{quote(EVO_INSTANCE)}"
    problems = []
    for number in ALERT_NUMBERS:
        ok, detail = _send_one(url, number, text)
        if not ok:
            problems.append(f"final {number[-4:]}: {detail}")
    return (not problems), ("; ".join(problems) or "enviado")


def notify(kind, message):
    stamp = datetime.now(LOCAL_TZ).strftime("%d/%m %H:%M")
    ok, detail = send_whatsapp(f"{message}\n🕒 {stamp}")
    with alert_lock:
        alert_events.append({
            "t": int(time.time()), "kind": kind, "ok": ok, "detail": detail,
            "text": message.replace("\n", " · ")[:180],
        })
    return ok, detail


def current_conditions():
    """Devolve (problemas_atuais, dados_de_apps_validos, apps_rodando)."""
    conds = {}   # chave -> (texto do alerta, texto de normalizado)
    with lock:
        n = dict(latest)
    if n:
        if THRESHOLDS["cpu"] > 0 and n["cpu"] >= THRESHOLDS["cpu"]:
            conds["host-cpu"] = (f"CPU da VPS em {n['cpu']:.0f}% (limite {THRESHOLDS['cpu']:.0f}%)",
                                 "CPU da VPS voltou ao normal")
        if THRESHOLDS["mem"] > 0 and n["mem"] >= THRESHOLDS["mem"]:
            conds["host-mem"] = (f"Memória da VPS em {n['mem']:.0f}% ({hb(n['mem_used'])} de {hb(n['mem_total'])})",
                                 "Memória da VPS voltou ao normal")
        disk_pct = 100.0 * n["disk_used"] / n["disk_total"]
        if THRESHOLDS["disk"] > 0 and disk_pct >= THRESHOLDS["disk"]:
            conds["host-disk"] = (f"Disco da VPS em {disk_pct:.0f}% ({hb(n['disk_used'])} de {hb(n['disk_total'])})",
                                  "Disco da VPS voltou ao normal")

    apps_ok = False
    running_keys = set()
    if DOCKER_ENABLED:
        with apps_lock:
            snap = dict(apps_state)
        if snap["t"] and not snap["error"]:
            apps_ok = True
            for a in snap["apps"]:
                name = f"{a['project']}/{a['service']}" if a["project"] else a["service"]
                if a["state"] == "running":
                    running_keys.add(a["key"])
                    seen_running.add(a["key"])
                    if THRESHOLDS["app_cpu"] > 0 and a["cpu"] >= THRESHOLDS["app_cpu"]:
                        conds[f"app-cpu:{a['key']}"] = (
                            f"App {name} usando {a['cpu']:.0f}% de CPU (100% = 1 vCPU)",
                            f"CPU do app {name} voltou ao normal")
                    if THRESHOLDS["app_mem"] > 0 and a["mem_limit"]:
                        pct = 100.0 * a["mem_used"] / a["mem_limit"]
                        if pct >= THRESHOLDS["app_mem"]:
                            conds[f"app-mem:{a['key']}"] = (
                                f"App {name} usando {pct:.0f}% do limite de memória "
                                f"({hb(a['mem_used'])} de {hb(a['mem_limit'])})",
                                f"Memória do app {name} voltou ao normal")
                elif a["key"] in seen_running:
                    conds[f"app-down:{a['key']}"] = (
                        f"App {name} parou ({a['status'] or a['state']})",
                        f"App {name} voltou a rodar")
    return conds, apps_ok, running_keys


def process_alerts(conds, apps_ok, running_keys, now):
    """Atualiza o estado dos alertas e devolve as mensagens a enviar."""
    outbox = []
    for key, (text, ok_text) in conds.items():
        st = alert_state.setdefault(key, {"since": now, "fired": False, "last": 0.0})
        st["text"], st["ok_text"] = text, ok_text
        need = DOWN_AFTER if key.startswith("app-down:") else SUSTAIN
        if not st["fired"] and now - st["since"] >= need:
            outbox.append(("alerta", f"🚨 *Alerta · {HOST_LABEL}*\n{text}"))
            st["fired"], st["last"] = True, now
        elif st["fired"] and now - st["last"] >= COOLDOWN:
            outbox.append(("lembrete", f"⚠️ *Continua em alerta · {HOST_LABEL}*\n{text}"))
            st["last"] = now
    for key in list(alert_state):
        if key in conds:
            continue
        is_app = key.startswith("app-")
        if is_app and not apps_ok:
            continue  # Docker indisponível agora: não dá para saber, mantém como está
        st = alert_state.pop(key)
        if not st["fired"]:
            continue
        if is_app and key.split(":", 1)[1] not in running_keys:
            continue  # o app sumiu da lista (removido de propósito): não avisa "normalizado"
        outbox.append(("normalizado", f"✅ *Normalizado · {HOST_LABEL}*\n{st['ok_text']}"))
    return outbox


def alerts_loop():
    time.sleep(INTERVAL * 3)  # espera as primeiras coletas
    while True:
        t0 = time.time()
        try:
            conds, apps_ok, running_keys = current_conditions()
            with alert_lock:
                outbox = process_alerts(conds, apps_ok, running_keys, t0)
            for kind, message in outbox:
                notify(kind, message)
        except Exception as e:
            print(f"erro no verificador de alertas: {type(e).__name__}: {e}", flush=True)
        time.sleep(max(INTERVAL - (time.time() - t0), 1))


def alerts_snapshot():
    with alert_lock:
        active = [{"key": k, "text": s.get("text", ""), "since": int(s["since"]), "fired": s["fired"]}
                  for k, s in alert_state.items()]
        events = list(alert_events)[::-1]
    return {
        "enabled": ALERTS_ENABLED,
        "missing": missing_alert_vars(),
        "numbers": ["final " + n[-4:] for n in ALERT_NUMBERS],
        "docker": DOCKER_ENABLED,
        "thresholds": THRESHOLDS,
        "sustain": SUSTAIN,
        "down_after": DOWN_AFTER,
        "cooldown": COOLDOWN,
        "active": active,
        "events": events,
    }


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
                snap = dict(apps_state)
            snap["apps"] = [{k: v for k, v in a.items() if k != "containers"} for a in snap["apps"]]
            return self._send(200, json.dumps(snap).encode(), "application/json")

        if url.path == "/api/app":
            q = parse_qs(url.query)
            key = (q.get("key", [""])[0])
            since = int(q.get("since", ["0"])[0] or 0)
            with apps_lock:
                snap = dict(apps_state)
            found = next((a for a in snap["apps"] if a["key"] == key), None)
            with hist_lock:
                pts = [p for p in _app_hist.get(key, ()) if p["t"] > since]
            with lock:
                host = dict(latest)
            alerts = [a for a in alerts_snapshot()["active"] if a["key"].endswith(":" + key)]
            payload = {
                "found": found is not None, "app": found, "history": pts,
                "interval": INTERVAL, "host": host, "alerts": alerts,
                "enabled": snap["enabled"], "error": snap["error"],
                "thresholds": THRESHOLDS,
            }
            return self._send(200, json.dumps(payload).encode(), "application/json")

        if url.path == "/api/alerts":
            return self._send(200, json.dumps(alerts_snapshot()).encode(), "application/json")

        self._send(404, b"Nao encontrado", "text/plain; charset=utf-8")

    def do_POST(self):
        url = urlparse(self.path)
        if not self._authorized():
            return self._send(
                401, b"Acesso restrito", "text/plain; charset=utf-8",
                {"WWW-Authenticate": 'Basic realm="Monitor VPS"'},
            )
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(min(length, 65536))

        if url.path == "/api/alert-test":
            ok, detail = notify(
                "teste", f"✅ *Teste · {HOST_LABEL}*\nO monitor consegue enviar mensagens por aqui.")
            return self._send(200, json.dumps({"ok": ok, "detail": detail}).encode(), "application/json")

        self._send(404, b"Nao encontrado", "text/plain; charset=utf-8")


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    if DOCKER_ENABLED:
        threading.Thread(target=apps_loop, daemon=True).start()
    if ALERTS_ENABLED:
        threading.Thread(target=alerts_loop, daemon=True).start()
        print(f"Alertas por WhatsApp ativos para {len(ALERT_NUMBERS)} numero(s)", flush=True)
    else:
        print("Alertas por WhatsApp desativados (faltam: " + ", ".join(missing_alert_vars()) + ")", flush=True)
    print(f"Monitor rodando na porta {PORT} (coleta a cada {INTERVAL}s)", flush=True)
    print(f"Docker: {DOCKER_HOST or 'nao configurado (tabela de aplicativos desativada)'}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
