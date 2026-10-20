#!/usr/bin/env python3
"""
pve.py - Proxmox VE control toolkit (Python 3.8+, standard library only).

Part of the open "proxmox-lab" agent skill, but works fine by hand:
VMs, containers, templates, snapshots, host networking, SDN and whole labs
from JSON blueprints - all through the Proxmox REST API with an API token.

QUICK START
  pve.py init --host 192.0.2.10 --token-id claude@pve!agent   # writes pve.env (you add the secret)
  pve.py doctor                                                # checks connectivity, auth, features
  pve.py status

CONFIG  First env file found wins: --env, $PVE_ENV, then pve.env / .env in
        $PVE_HOME, the current dir, this script's dir and its parent.
        Real environment variables override file values.
          PVE_HOST            pve.example.lan or 192.0.2.10  (https, port 8006 assumed)
          PVE_TOKEN_ID        user@realm!tokenname            (or PVE_USER + short PVE_TOKEN_ID)
          PVE_TOKEN_SECRET    xxxxxxxx-xxxx-....
          PVE_VERIFY_SSL      0 (default: self-signed certs) | 1
        PROXMOX_HOST / PROXMOX_USER / PROXMOX_TOKEN_ID / PROXMOX_TOKEN_SECRET also work.
        Optional: PVE_HOME (data dir: labs/, keys/; default ~/.proxmox-lab),
          PVE_BRIDGE (default bridge; auto = the node's management bridge),
          PVE_LAB_ZONE (SDN zone for lab networks, default "labs"),
          PVE_LAB_SUBNETS (pool that lab /24s are carved from, default 10.66.0.0/16),
          PVE_BUDGET (seconds; stop cleanly and exit 3 so a re-run resumes).

SAFETY  Anything destructive (delete, lab destroy, host network apply, ...) only
        prints what it WOULD do unless you add --yes.  Exit code 2 = dry run.

Run  `pve.py -h`  or  `pve.py <command> -h`.
"""
import argparse
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import ssl
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    """Where labs/ and keys/ live: $PVE_HOME, else a portable install next to the script, else ~/.proxmox-lab."""
    if os.environ.get("PVE_HOME"):
        return os.path.abspath(os.path.expanduser(os.environ["PVE_HOME"]))
    for d in (HERE, os.path.dirname(HERE)):
        if os.path.isdir(os.path.join(d, "labs")) or os.path.isfile(os.path.join(d, "pve.env")):
            if os.access(d, os.W_OK):
                return d
    return os.path.join(os.path.expanduser("~"), ".proxmox-lab")


DATA_DIR = _data_dir()
LABS_DIR = os.path.join(DATA_DIR, "labs")
KEYS_DIR = os.path.join(DATA_DIR, "keys")
LAB_ZONE = os.environ.get("PVE_LAB_ZONE", "labs")
LAB_SUBNETS = os.environ.get("PVE_LAB_SUBNETS", "10.66.0.0/16")
JSON_OUT = False

# Cloud images the toolkit can turn into VM templates automatically.
CLOUD_IMAGES = {
    "ubuntu-26.04": "https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img",
    "ubuntu-24.04": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
    "ubuntu-22.04": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
    "debian-13": "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2",
    "debian-12": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
    "rocky-9": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
    "alma-9": "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
}
DISTRO_ALIASES = {
    "ubuntu": "ubuntu-24.04", "noble": "ubuntu-24.04", "jammy": "ubuntu-22.04", "resolute": "ubuntu-26.04",
    "ubuntu-24": "ubuntu-24.04", "ubuntu-22": "ubuntu-22.04", "ubuntu-26": "ubuntu-26.04",
    "debian": "debian-13", "trixie": "debian-13", "bookworm": "debian-12",
    "rocky": "rocky-9", "rockylinux": "rocky-9", "alma": "alma-9", "almalinux": "alma-9",
}


# --------------------------------------------------------------------------- output helpers
def say(*a):
    print(*a, file=sys.stderr if JSON_OUT else sys.stdout, flush=True)


def die(msg, code=1):
    print("ERROR: " + str(msg), file=sys.stderr, flush=True)
    sys.exit(code)


def emit(data, text_fn=None):
    if JSON_OUT or text_fn is None:
        print(json.dumps(data, indent=2, default=str))
    else:
        text_fn(data)


def table(rows, cols):
    """cols: list of (key_or_callable, header)."""
    if not rows:
        say("(none)")
        return
    hdr = [h for _, h in cols]
    data = []
    for r in rows:
        line = []
        for k, _ in cols:
            v = k(r) if callable(k) else r.get(k)
            line.append("" if v is None else str(v))
        data.append(line)
    w = [max(len(x) for x in col) for col in zip(hdr, *data)]
    fmt = lambda vals: "  ".join(v.ljust(w[i]) for i, v in enumerate(vals)).rstrip()
    say(fmt(hdr))
    say(fmt(["-" * x for x in w]))
    for d in data:
        say(fmt(d))


def hb(n):
    if n in (None, ""):
        return ""
    n = float(n)
    for u in ["B", "K", "M", "G", "T", "P"]:
        if abs(n) < 1024 or u == "P":
            return ("%.0f%s" % (n, u)) if u in ("B", "K") else ("%.1f%s" % (n, u))
        n /= 1024.0


def natkey(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


def need_yes(args, what):
    if not getattr(args, "yes", False):
        say("DRY RUN - nothing changed. Would: " + what)
        say("Re-run with --yes to actually do it.")
        sys.exit(2)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------- parsing helpers
def mem_mb(v, default=None):
    """'4G' -> 4096, '512M' -> 512, 2048 -> 2048, '4' -> 4096 (bare numbers < 64 are GB)."""
    if v in (None, ""):
        return default
    s = str(v).strip().upper().replace("IB", "").rstrip("B")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([KMGT]?)$", s)
    if not m:
        die("Bad memory size '%s' (use e.g. 2048, 2G, 512M)" % v)
    n, u = float(m.group(1)), m.group(2)
    if not u:
        return int(n) if n >= 64 else int(n * 1024)
    return int({"K": n / 1024, "M": n, "G": n * 1024, "T": n * 1024 * 1024}[u])


def disk_gb(v, default=None):
    """'32G' -> 32, '1T' -> 1024, 20 -> 20 (bare numbers are GB)."""
    if v in (None, ""):
        return default
    s = str(v).strip().upper().replace("IB", "").rstrip("B")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([KMGT]?)$", s)
    if not m:
        die("Bad disk size '%s' (use e.g. 32, 32G, 1T)" % v)
    n, u = float(m.group(1)), (m.group(2) or "G")
    gb = {"K": n / 1024 / 1024, "M": n / 1024, "G": n, "T": n * 1024}[u]
    return max(1, int(math.ceil(gb)))


def cfg_size_gb(diskstr):
    m = re.search(r"size=(\d+(?:\.\d+)?)([KMGT])", diskstr or "")
    if not m:
        return None
    n, u = float(m.group(1)), m.group(2)
    return {"K": n / 1024 / 1024, "M": n / 1024, "G": n, "T": n * 1024}[u]


def parse_kv(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            die("Expected key=value, got '%s'" % it)
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


def parse_netarg(s):
    """'vmbr0' | 'bridge=vmbr1,ip=10.0.0.5/24,gw=10.0.0.1,vlan=20,firewall=1,model=e1000'"""
    if "=" not in s:
        return {"bridge": s, "ip": "dhcp"}
    d = {}
    for part in s.split(","):
        if not part:
            continue
        if "=" not in part:
            die("Bad --net item '%s'" % part)
        k, v = part.split("=", 1)
        k = k.strip().lower()
        k = {"tag": "vlan", "gateway": "gw"}.get(k, k)
        d[k] = v.strip()
    if "bridge" not in d:
        die("--net needs bridge=... (got '%s')" % s)
    d.setdefault("ip", "dhcp")
    if d.get("firewall") in ("1", "true", "yes"):
        d["firewall"] = True
    return d


def ip_with_prefix(ip, prefixlen=24):
    return ip if "/" in ip else "%s/%s" % (ip, prefixlen)


def ip_add(ip, n):
    addr, _, pfx = ip.partition("/")
    a = ipaddress.ip_address(addr) + n
    return "%s/%s" % (a, pfx) if pfx else str(a)


def sanitize_name(s, maxlen=63):
    s = re.sub(r"[^a-z0-9-]+", "-", str(s).lower()).strip("-")
    return (s[:maxlen].strip("-")) or "x"


def gen_password(n=16):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def read_ssh_key(v):
    if not v:
        return None
    if v == "auto":
        return lab_ssh_key()[0]
    if os.path.isfile(os.path.expanduser(v)):
        with open(os.path.expanduser(v), encoding="utf-8") as f:
            return f.read().strip()
    if re.match(r"^(ssh-|ecdsa-|sk-)", v.strip()):
        return v.strip()
    die("--ssh-key must be a public key string, a .pub file path, or 'auto'")


def lab_ssh_key():
    """Return (pubkey, private_key_path) for the shared lab key, generating it if needed."""
    priv = os.path.join(KEYS_DIR, "lab_ed25519")
    pub = priv + ".pub"
    if not os.path.isfile(pub):
        kg = shutil.which("ssh-keygen")
        if not kg:
            return None, None
        os.makedirs(KEYS_DIR, exist_ok=True)
        r = subprocess.run([kg, "-q", "-t", "ed25519", "-N", "", "-C", "pve-lab", "-f", priv],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0 or not os.path.isfile(pub):
            return None, None
        say("  generated lab SSH key %s" % relpath(priv))
    with open(pub, encoding="utf-8") as f:
        return f.read().strip(), priv


# --------------------------------------------------------------------------- config
def load_env_file(path):
    vals = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()  # also kills Windows \r
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            vals[k] = v
    return vals


def env_search_dirs():
    return [DATA_DIR, os.getcwd(), HERE, os.path.dirname(HERE)]


def _looks_like_pve_env(path):
    try:
        keys = load_env_file(path)
    except (OSError, UnicodeDecodeError):
        return False
    return any(k.startswith(("PVE_", "PROXMOX_")) for k in keys)


def resolve_config(env_path=None):
    explicit = bool(env_path or os.environ.get("PVE_ENV"))
    path = env_path or os.environ.get("PVE_ENV")
    searched = env_search_dirs()
    if not path:
        for d in searched:
            for n in ("pve.env", ".env"):
                p = os.path.join(d, n)
                if os.path.isfile(p) and _looks_like_pve_env(p):
                    path = p
                    break
            if path:
                break
    if path and not os.path.isfile(path):
        die("env file not found: %s" % path)
    vals = load_env_file(path) if path else {}

    def g(*keys):
        srcs = (vals, os.environ) if explicit else (os.environ, vals)
        for src in srcs:
            for k in keys:
                if src.get(k):
                    return src[k].strip()
        return None

    host = g("PVE_HOST", "PROXMOX_HOST")
    if not host:
        die("No Proxmox connection configured. Run `pve.py init --host <ip-or-name> --token-id user@realm!name` "
            "or create pve.env (looked in: %s)" % ", ".join(dict.fromkeys(searched)))
    token = g("PVE_TOKEN")
    if not token:
        user = g("PVE_USER", "PROXMOX_USER")
        tid = g("PVE_TOKEN_ID", "PROXMOX_TOKEN_ID")
        sec = g("PVE_TOKEN_SECRET", "PROXMOX_TOKEN_SECRET")
        if not (tid and sec):
            die("No API token configured (PVE_TOKEN_ID + PVE_TOKEN_SECRET)")
        if "!" not in tid:
            if not user:
                die("PVE_USER is needed when PVE_TOKEN_ID is not the full user@realm!name form")
            tid = "%s!%s" % (user, tid)
        token = "%s=%s" % (tid, sec)
    base = host if "://" in host else "https://" + host
    u = urllib.parse.urlparse(base)
    base = "%s://%s:%s" % (u.scheme or "https", u.hostname, u.port or 8006)
    verify = (g("PVE_VERIFY_SSL") or "0").lower() in ("1", "true", "yes")
    return {"base": base, "token": token, "verify": verify, "env_path": path,
            "tokenid": token.split("=", 1)[0], "host": u.hostname}


# --------------------------------------------------------------------------- API client
class PVEError(Exception):
    pass


class Budget(Exception):
    """Raised when the --budget time limit is reached; the Proxmox task keeps running server-side."""

    def __init__(self, what="", upid=None):
        Exception.__init__(self, what)
        self.what, self.upid = what, upid


DEADLINE = None  # epoch seconds; set from --budget / $PVE_BUDGET


def check_budget(margin=0, what="next step"):
    if DEADLINE and time.time() > DEADLINE - margin:
        raise Budget(what)


def _clean(params):
    out = {}
    for k, v in (params or {}).items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = 1 if v else 0
        out[k] = v
    return out


class PVE:
    def __init__(self, cfg, timeout=90):
        self.cfg = cfg
        self.root = cfg["base"]
        self.base = cfg["base"] + "/api2/json"
        self.auth = "PVEAPIToken=" + cfg["token"]
        ctx = ssl.create_default_context()
        if not cfg.get("verify"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.ctx = ctx
        self.timeout = timeout
        self._cache = {}

    def request(self, method, path, params=None, full=False):
        url = self.base + (path if path.startswith("/") else "/" + path)
        params = _clean(params)
        headers = {"Authorization": self.auth, "Accept": "application/json"}
        data = None
        if method in ("GET", "DELETE"):
            if params:
                url += "?" + urllib.parse.urlencode(params, doseq=True)
        else:
            data = urllib.parse.urlencode(params, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as r:
                    body = r.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                msg = e.reason or ""
                try:
                    j = json.loads(body)
                    if j.get("errors"):
                        msg += " | " + json.dumps(j["errors"])
                    elif j.get("message"):
                        msg += " | " + str(j["message"]).strip()
                except Exception:
                    if body.strip():
                        msg += " | " + body.strip()[:400]
                if e.code == 401:
                    msg += " (token rejected - check PVE_TOKEN_* in %s)" % self.cfg.get("env_path")
                raise PVEError("%s %s -> HTTP %s: %s" % (method, path, e.code, msg))
            except (urllib.error.URLError, OSError) as e:
                if method != "GET" or attempt == 2:
                    raise PVEError("Cannot reach Proxmox at %s: %s. Check the host/port and that this machine can "
                                   "reach it; sandboxed agents (e.g. Claude Cowork) need network egress that allows "
                                   "LAN hosts."
                                   % (self.root, getattr(e, "reason", e)))
                time.sleep(1.5)
        j = json.loads(body) if body else {}
        return j if full else j.get("data")

    def get(self, path, params=None, **kw):
        return self.request("GET", path, params, **kw)

    def post(self, path, params=None):
        return self.request("POST", path, params)

    def put(self, path, params=None):
        return self.request("PUT", path, params)

    def delete(self, path, params=None):
        return self.request("DELETE", path, params)

    # ---- tasks
    def task_log(self, node, upid, limit=40):
        try:
            lines = self.get("/nodes/%s/tasks/%s/log" % (node, urllib.parse.quote(upid, safe="")),
                             {"limit": 500}) or []
        except PVEError:
            return ""
        return "\n".join(l.get("t", "") for l in lines[-limit:])

    def wait(self, upid, timeout=1800, label=None, quiet=False):
        if not (isinstance(upid, str) and upid.startswith("UPID:")):
            return upid
        node = upid.split(":")[1]
        q = urllib.parse.quote(upid, safe="")
        t0 = time.time()
        dots = 0
        while True:
            st = self.get("/nodes/%s/tasks/%s/status" % (node, q)) or {}
            if st.get("status") == "stopped":
                es = st.get("exitstatus", "")
                if es == "OK" or es.startswith("WARNINGS"):
                    if es != "OK" and not quiet:
                        say("    (%s: %s)" % (label or st.get("type"), es))
                    return es
                raise PVEError("Task '%s' failed: %s\n--- task log ---\n%s"
                               % (label or st.get("type"), es, self.task_log(node, upid)))
            if time.time() - t0 > timeout:
                raise PVEError("Timed out after %ss waiting for %s (%s) - it may still be running on %s"
                               % (timeout, label or "task", upid, node))
            if DEADLINE and time.time() > DEADLINE:
                raise Budget(label or st.get("type") or "task", upid)
            time.sleep(1.0 if time.time() - t0 < 10 else 3.0)
            dots += 1
            if not quiet and dots % 20 == 0:
                say("    ... still waiting for %s (%ds)" % (label or "task", time.time() - t0))

    # ---- cached lookups
    def version(self):
        if "version" not in self._cache:
            self._cache["version"] = self.get("/version") or {}
        return self._cache["version"]

    def ver(self):
        return tuple(int(x) for x in re.findall(r"\d+", self.version().get("version", "0"))[:3])

    def nodes(self):
        if "nodes" not in self._cache:
            self._cache["nodes"] = sorted(self.get("/nodes") or [], key=lambda n: n["node"])
        return self._cache["nodes"]

    def online_nodes(self):
        return [n for n in self.nodes() if n.get("status") == "online"]

    def guests(self, refresh=True):
        if refresh or "guests" not in self._cache:
            gs = self.get("/cluster/resources", {"type": "vm"}) or []
            stale = [g for g in gs if not g.get("name") or g.get("status") == "unknown"
                     or re.match(r"^VM \d+$", g.get("name") or "")]
            if stale:  # cluster cache lags a few seconds behind new guests: ask their nodes directly
                for node in {g["node"] for g in stale}:
                    for kind in ("qemu", "lxc"):
                        try:
                            fresh = {int(x["vmid"]): x for x in self.get("/nodes/%s/%s" % (node, kind)) or []}
                        except PVEError:
                            continue
                        for g in stale:
                            f = fresh.get(int(g["vmid"]))
                            if g["node"] == node and g.get("type") == kind and f:
                                if f.get("name") and not re.match(r"^VM \d+$", f["name"]):
                                    g["name"] = f["name"]
                                g["status"] = f.get("status") or g.get("status")
                                for k in ("maxmem", "maxdisk", "cpus", "tags", "template"):
                                    if f.get(k) is not None and not g.get(k):
                                        g[k] = f[k]
                                g.setdefault("maxcpu", f.get("cpus"))
            self._cache["guests"] = gs
        return self._cache["guests"]

    def storages(self, node, refresh=False):
        k = "st:" + node
        if refresh or k not in self._cache:
            self._cache[k] = self.get("/nodes/%s/storage" % node) or []
        return self._cache[k]

    def invalidate(self, prefix=""):
        for k in list(self._cache):
            if k.startswith(prefix):
                del self._cache[k]


# --------------------------------------------------------------------------- lookups
def gpath(g):
    return "/nodes/%s/%s/%s" % (g["node"], g["type"], g["vmid"])


def kind_label(t):
    return {"qemu": "vm", "lxc": "ct"}.get(t, t)


def find_guest(api, ident, kinds=("qemu", "lxc")):
    gs = [g for g in api.guests() if g.get("type") in kinds]
    s = str(ident).strip()
    match = (lambda g: int(g["vmid"]) == int(s)) if s.isdigit() else \
        (lambda g: (g.get("name") or "").lower() == s.lower())
    m = [g for g in gs if match(g)]
    if not m:  # cluster cache can lag behind brand-new guests - ask every node directly
        for n in api.online_nodes():
            for kind in kinds:
                try:
                    for x in api.get("/nodes/%s/%s" % (n["node"], kind)) or []:
                        x = dict(x, node=n["node"], type=kind, vmid=int(x["vmid"]))
                        if match(x):
                            m.append(x)
                except PVEError:
                    pass
    if not m:
        die("No %s named/numbered '%s'" % ("/".join(kind_label(k) for k in kinds), ident))
    if len(m) > 1:
        die("Name '%s' is ambiguous: %s - use the VMID" % (
            ident, ", ".join("%s (%s on %s)" % (g["vmid"], kind_label(g["type"]), g["node"]) for g in m)))
    return m[0]


def default_bridge(api, node=None):
    """$PVE_BRIDGE, else the bridge carrying the node's default gateway (its LAN), else vmbr0."""
    if os.environ.get("PVE_BRIDGE") and os.environ["PVE_BRIDGE"] != "auto":
        return os.environ["PVE_BRIDGE"]
    node = node or pick_node(api)
    key = "bridge:" + node
    if key not in api._cache:
        try:
            ifs = [i for i in (api.get("/nodes/%s/network" % node) or []) if i.get("type") == "bridge"]
        except PVEError:
            ifs = []
        pick = next((i["iface"] for i in ifs if i.get("gateway")), None) \
            or next((i["iface"] for i in ifs if i.get("cidr") or i.get("address")), None) \
            or next((i["iface"] for i in ifs if i["iface"] == "vmbr0"), None) \
            or (ifs[0]["iface"] if ifs else "vmbr0")
        api._cache[key] = pick
    return api._cache[key]


def resolve_bridge(api, node, bridge):
    return default_bridge(api, node) if bridge in (None, "", "auto") else bridge


def pick_node(api, pref=None):
    nodes = api.online_nodes()
    if not nodes:
        die("No online Proxmox nodes")
    if pref and pref != "auto":
        for n in api.nodes():
            if n["node"] == pref:
                if n.get("status") != "online":
                    die("Node %s is offline" % pref)
                return pref
        die("Unknown node '%s'. Nodes: %s" % (pref, ", ".join(n["node"] for n in api.nodes())))
    nodes = sorted(nodes, key=lambda n: (n.get("maxmem", 0) - n.get("mem", 0)), reverse=True)
    return nodes[0]["node"]


TYPE_PREF = {"zfspool": 0, "lvmthin": 1, "btrfs": 1, "rbd": 2, "lvm": 3, "nfs": 4, "cifs": 4, "dir": 6}


def pick_storage(api, node, content, pref=None):
    allst = api.storages(node)
    if pref and pref != "auto":
        for s in allst:
            if s["storage"] == pref:
                if content not in (s.get("content") or "").split(","):
                    die("Storage '%s' on %s does not hold '%s' (content: %s)" % (pref, node, content, s.get("content")))
                return pref
        die("Storage '%s' not found on node %s (have: %s)" % (pref, node, ", ".join(s["storage"] for s in allst)))
    sts = [s for s in allst if s.get("active", 1) and s.get("enabled", 1)
           and content in (s.get("content") or "").split(",")]
    if not sts:
        die("No active storage on %s supports '%s'" % (node, content))
    if content in ("images", "rootdir"):
        sts.sort(key=lambda s: (TYPE_PREF.get(s.get("type"), 9), -(s.get("avail") or 0)))
    else:
        sts.sort(key=lambda s: (0 if s["storage"] == "local" else 1, -(s.get("avail") or 0)))
    return sts[0]["storage"]


class VMIDs:
    def __init__(self, api, start=None):
        self.api = api
        self.used = {int(g["vmid"]) for g in api.guests()}
        self.start = int(start) if start else None

    def take(self, want=None):
        if want:
            want = int(want)
            if want in self.used:
                die("VMID %s is already in use" % want)
            self.used.add(want)
            return want
        i = self.start if self.start else int(self.api.get("/cluster/nextid"))
        while i in self.used:
            i += 1
        self.used.add(i)
        return i


def active_tasks(api, node, vmid=None, typefilter=None):
    params = {"source": "active"}
    if vmid:
        params["vmid"] = vmid
    if typefilter:
        params["typefilter"] = typefilter
    try:
        return [t for t in (api.get("/nodes/%s/tasks" % node, params) or []) if not t.get("endtime")]
    except PVEError:
        return []


def wait_active(api, node, vmid=None, typefilter=None, label=None):
    """Wait for tasks a previous (interrupted) run left running."""
    ts = active_tasks(api, node, vmid, typefilter)
    for t in ts:
        say("  waiting for running task %s %s ..." % (t.get("type"), t.get("id") or ""))
        api.wait(t["upid"], label=label or t.get("type"), timeout=3600)
    return bool(ts)


def cpu_default(api):
    return "x86-64-v2-AES" if api.ver() >= (8,) else "kvm64"


def storage_content(api, node, storage, content):
    try:
        return api.get("/nodes/%s/storage/%s/content" % (node, storage), {"content": content}) or []
    except PVEError:
        return []


def list_content(api, node, content):
    out = []
    for s in api.storages(node):
        if content in (s.get("content") or "").split(",") and s.get("active", 1):
            out += [i["volid"] for i in storage_content(api, node, s["storage"], content)]
    return out


def resolve_iso(api, node, spec):
    if ":" in spec:
        return spec
    isos = list_content(api, node, "iso")
    m = [v for v in isos if spec.lower() in v.split("/")[-1].lower()]
    if not m:
        die("No ISO matching '%s' on %s. ISOs: %s" % (spec, node, ", ".join(v.split("/")[-1] for v in isos) or "none"))
    m.sort(key=natkey, reverse=True)
    return m[0]


# ---- container templates
def _match_tmpl(names, spec):
    spec = spec.lower()
    base = lambda v: v.split("/")[-1].lower()
    if "turnkey" not in spec:
        names = [v for v in names if "turnkey" not in base(v)]
    m = [v for v in names if base(v).startswith(spec)] or [v for v in names if spec in base(v)]
    plain = [v for v in m if re.search(r"-(standard|default)_", base(v))]
    m = plain or m
    if spec == "ubuntu":
        lts = [v for v in m if re.match(r"ubuntu-(\d+)\.04", base(v))
               and int(re.match(r"ubuntu-(\d+)", base(v)).group(1)) % 2 == 0]
        m = lts or m
    m.sort(key=lambda v: natkey(base(v)), reverse=True)
    return m[0] if m else None


def find_ct_template(api, node, spec):
    """-> ('have', volid) | ('download', aplinfo_template_name)"""
    if ":" in spec:
        return ("have", spec)
    have = _match_tmpl(list_content(api, node, "vztmpl"), spec)
    if have:
        return ("have", have)
    apl = api.get("/nodes/%s/aplinfo" % node) or []
    names = [a["template"] for a in apl]
    pick = _match_tmpl(names, spec)
    if not pick:
        fams = sorted(set(re.sub(r"_.*", "", n) for n in names), key=natkey)
        die("No container template matches '%s'. Available: %s" % (spec, ", ".join(fams)))
    return ("download", pick)


def ensure_ct_template(api, node, spec, storage=None):
    how, val = find_ct_template(api, node, spec)
    if how == "have":
        return val
    if wait_active(api, node, typefilter="download", label="download in progress"):
        how, val = find_ct_template(api, node, spec)
        if how == "have":
            return val
    st = pick_storage(api, node, "vztmpl", storage)
    say("  downloading CT template %s -> %s on %s ..." % (val, st, node))
    api.wait(api.post("/nodes/%s/aplinfo" % node, {"storage": st, "template": val}),
             label="CT template download", timeout=1800)
    return "%s:vztmpl/%s" % (st, val)


# ---- VM templates
def distro_key(spec):
    s = str(spec).lower().strip()
    return DISTRO_ALIASES.get(s, s)


def find_vm_template(api, spec, node=None):
    s = str(spec).strip()
    tpls = [g for g in api.guests() if g.get("type") == "qemu" and g.get("template")]
    if node:
        tpls = [g for g in tpls if g["node"] == node] or tpls
    if s.isdigit():
        m = [g for g in tpls if int(g["vmid"]) == int(s)]
        return m[0] if m else None
    key = distro_key(s)
    nm = lambda g: (g.get("name") or "").lower()
    for cand in (s.lower(), key, "tpl-" + key, key + "-template", key + "-tpl"):
        m = [g for g in tpls if nm(g) == cand]
        if m:
            return m[0]
    m = [g for g in tpls if key in nm(g)] or [g for g in tpls if s.lower() in nm(g)]
    if m:
        m.sort(key=lambda g: natkey(nm(g)), reverse=True)
        return m[0]
    return None


def template_storage_shared(api, tpl):
    """True if every disk of the template lives on shared storage (then cross-node clones are cheap)."""
    try:
        cfg = api.get(gpath(dict(tpl, type="qemu")) + "/config") or {}
    except PVEError:
        return False
    shared = {s["storage"] for s in api.storages(tpl["node"]) if s.get("shared")}
    disks = [v for k, v in cfg.items() if re.match(r"(scsi|virtio|sata|ide|efidisk|tpmstate)\d+$", k)
             and "media=cdrom" not in str(v)]
    return bool(disks) and all(str(d).split(":")[0] in shared for d in disks)


def ensure_import_storage(api, node):
    for s in api.storages(node):
        if "import" in (s.get("content") or "").split(",") and s.get("active", 1):
            return s["storage"], "import"
    if api.ver() >= (8, 2):
        try:
            conf = api.get("/storage/local") or {}
            if conf.get("type") == "dir":
                newc = ",".join([c for c in (conf.get("content") or "").split(",") if c] + ["import"])
                api.put("/storage/local", {"content": newc})
                api.invalidate("st:")
                say("  enabled 'import' content on storage 'local'")
                return "local", "import"
        except PVEError as e:
            say("  could not enable import content on 'local': %s" % e)
    return pick_storage(api, node, "iso"), "iso"


def build_vm_template(api, distro, node=None, storage=None, vmid=None, bridge=None, name=None, agent=True):
    key = distro_key(distro)
    if key not in CLOUD_IMAGES:
        die("Unknown cloud image '%s'. Known: %s" % (distro, ", ".join(sorted(CLOUD_IMAGES))))
    url = CLOUD_IMAGES[key]
    node = pick_node(api, node)
    bridge = resolve_bridge(api, node, bridge)
    tname = name or "tpl-" + key
    existing = find_vm_template(api, tname, node)
    if existing and existing["node"] == node and (existing.get("name") or "").lower() == tname:
        say("  template %s already exists on %s (VMID %s)" % (tname, node, existing["vmid"]))
        return existing
    # resume a template VM an interrupted run left half-built
    half = [g for g in api.guests() if g.get("type") == "qemu" and not g.get("template")
            and (g.get("name") or "").lower() == tname and g["node"] == node]
    if half:
        g = half[0]
        say("  resuming half-built template %s (%s)" % (tname, g["vmid"]))
        wait_active(api, node, g["vmid"], label="template create")
        return finish_template(api, node, g["vmid"], tname, bridge, agent)
    disk_st = pick_storage(api, node, "images", storage)
    imp_st, content = ensure_import_storage(api, node)
    stem = re.sub(r"\.(img|qcow2|raw)$", "", url.rsplit("/", 1)[-1])
    fname = stem + (".qcow2" if content == "import" else ".img")
    volid = "%s:%s/%s" % (imp_st, content, fname)
    have = [i["volid"] for i in storage_content(api, node, imp_st, content)]
    if volid not in have and wait_active(api, node, typefilter="download", label="download in progress"):
        have = [i["volid"] for i in storage_content(api, node, imp_st, content)]
    if volid not in have:
        say("  downloading %s to %s on %s (this can take a few minutes) ..." % (url, imp_st, node))
        api.wait(api.post("/nodes/%s/storage/%s/download-url" % (node, imp_st),
                          {"content": content, "filename": fname, "url": url}),
                 label="image download", timeout=3600)
    vmid = vmid or VMIDs(api, start=9000).take()
    p = {
        "vmid": vmid, "name": tname, "ostype": "l26", "memory": 2048, "cores": 2,
        "cpu": cpu_default(api), "scsihw": "virtio-scsi-single",
        "scsi0": "%s:0,import-from=%s,discard=on,iothread=1,ssd=1" % (disk_st, volid),
        "ide2": "%s:cloudinit" % disk_st, "boot": "order=scsi0", "serial0": "socket",
        "net0": "virtio,bridge=%s" % bridge, "ipconfig0": "ip=dhcp", "agent": "enabled=%d" % (1 if agent else 0),
        "tags": "template;cloudimg",
        "description": "Cloud-init template built by pve.py from %s on %s" % (url, now_iso()),
    }
    say("  creating template VM %s (%s) on %s ..." % (tname, vmid, node))
    api.wait(api.post("/nodes/%s/qemu" % node, p), label="create template", timeout=1800)
    return finish_template(api, node, vmid, tname, bridge, agent)


def finish_template(api, node, vmid, tname, bridge, agent=True):
    if agent:
        prep_template_vm(api, node, vmid, tname, bridge)
    api.wait(api.post("/nodes/%s/qemu/%s/template" % (node, vmid)), label="convert to template")
    say("  template ready: %s (VMID %s on %s)" % (tname, vmid, node))
    return {"vmid": int(vmid), "node": node, "name": tname, "type": "qemu", "template": 1}


# ---- SSH helpers (used to find DHCP addresses and to bake the guest agent into templates)
PREP_MARK = "pve.py:agent-ready"
PREP_USER = "pveprep"


def bridge_subnet(api, node, bridge):
    try:
        for i in api.get("/nodes/%s/network" % node) or []:
            if i.get("iface") == bridge and i.get("cidr"):
                return str(ipaddress.ip_interface(i["cidr"]).network)
    except PVEError:
        pass
    return None


_SSH_KEYCOPY = None


def ssh_cmd(user, host, keyfile, connect_timeout=5):
    """ssh argv using a private 0600 copy of the key (mounted folders often have loose permissions)."""
    global _SSH_KEYCOPY
    exe = shutil.which("ssh")
    if not exe:
        raise PVEError("no ssh client on this machine")
    if _SSH_KEYCOPY is None:
        import tempfile
        fd, _SSH_KEYCOPY = tempfile.mkstemp(prefix="pvekey-")
        with os.fdopen(fd, "w", encoding="utf-8") as f, open(keyfile, encoding="utf-8") as src:
            f.write(src.read())
        os.chmod(_SSH_KEYCOPY, 0o600)
    return [exe, "-i", _SSH_KEYCOPY, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=" + os.devnull, "-o", "ConnectTimeout=%d" % connect_timeout,
            "-o", "LogLevel=ERROR", "%s@%s" % (user, host)]


def ping_sweep(hosts):
    """Live hosts via parallel ping (cheap; some sandboxed networks choke on parallel TCP connects)."""
    from concurrent.futures import ThreadPoolExecutor
    exe = shutil.which("ping")
    if not exe:
        return list(hosts)

    def p(h):
        argv = [exe, "-n", "1", "-w", "1000", h] if os.name == "nt" else [exe, "-c", "1", "-W", "1", h]
        try:
            return h if subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=5).returncode == 0 else None
        except (subprocess.TimeoutExpired, OSError):
            return None
    with ThreadPoolExecutor(64) as ex:
        return [h for h in ex.map(p, hosts) if h]


def scan_port(subnet, port=22, timeout=1.0, exclude=()):
    import socket
    from concurrent.futures import ThreadPoolExecutor
    net = ipaddress.ip_network(subnet, strict=False)
    if net.num_addresses > 1024:
        raise PVEError("subnet %s too large to scan" % subnet)
    hosts = ping_sweep([str(h) for h in net.hosts() if str(h) not in exclude])

    def probe(h):
        try:
            with socket.create_connection((h, port), timeout=timeout):
                return h
        except OSError:
            return None
    with ThreadPoolExecutor(4) as ex:
        return [h for h in ex.map(probe, hosts) if h]


def find_hosts_by_name(names, subnet, users=("lab", "root"), keyfile=None):
    """Log in with the lab key to every SSH host in `subnet` and match `hostname` -> {name: (ip, user)}."""
    from concurrent.futures import ThreadPoolExecutor
    keyfile = keyfile or lab_ssh_key()[1]
    if not keyfile:
        return {}
    want = {n.split(".")[0].lower(): n for n in names}
    hosts = scan_port(subnet)

    def attempt(h):
        for u in users:
            try:
                r = subprocess.run(ssh_cmd(u, h, keyfile, 4) + ["hostname"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=12, universal_newlines=True)
            except subprocess.TimeoutExpired:
                continue
            if r.returncode == 0:
                return h, u, r.stdout.strip().split(".")[0].lower()
        return h, None, None
    found = {}
    with ThreadPoolExecutor(4) as ex:
        for h, u, hn in ex.map(attempt, hosts):
            if hn in want:
                found[want[hn]] = (h, u)
    return found


PREP_SCRIPT = r"""
cloud-init status --wait >/dev/null 2>&1 || true
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  for i in 1 2 3; do
    apt-get -o DPkg::Lock::Timeout=180 update -qq &&
    apt-get -o DPkg::Lock::Timeout=180 install -y -qq qemu-guest-agent && break
    sleep 5
  done
else
  for i in 1 2 3; do dnf install -y -q qemu-guest-agent && break; sleep 5; done
fi
if ! command -v qemu-ga >/dev/null 2>&1 && [ ! -x /usr/sbin/qemu-ga ] && [ ! -x /usr/bin/qemu-ga ]; then
  echo PREP-FAILED: qemu-guest-agent did not install; exit 1
fi
systemctl enable qemu-guest-agent >/dev/null 2>&1 || true
systemctl start qemu-guest-agent >/dev/null 2>&1 || true
sync
nohup sh -c 'sleep 3; rm -f /root/pveprep.sh; cloud-init clean --logs --machine-id >/dev/null 2>&1 || cloud-init clean --logs >/dev/null 2>&1;
  userdel -rf %(user)s >/dev/null 2>&1; rm -f /etc/sudoers.d/90-cloud-init-users;
  truncate -s0 /etc/machine-id; rm -f /var/lib/dbus/machine-id; history -c; poweroff' >/dev/null 2>&1 &
echo PREP-OK
""" % {"user": PREP_USER}


def prep_template_vm(api, node, vmid, name, bridge):
    """Boot the imported image once on `bridge`, install qemu-guest-agent, wipe instance data, power off."""
    g = {"vmid": vmid, "node": node, "type": "qemu", "name": name}
    path = gpath(g)
    cfg = api.get(path + "/config") or {}
    desc = cfg.get("description") or ""
    if PREP_MARK in desc:
        return True
    pub, keyfile = lab_ssh_key()
    subnet = bridge_subnet(api, node, bridge)
    if not (pub and keyfile and subnet and shutil.which("ssh")):
        say("  (no ssh/lab key/bridge subnet - template stays without guest agent)")
        return False
    say("  baking qemu-guest-agent into the template (one-time boot on %s) ..." % bridge)
    m = re.search(r"pve\.py:prep-started=(\d+)", desc)
    t0 = int(m.group(1)) if m else int(time.time())
    if not m:
        desc = desc + "\npve.py:prep-started=%d" % t0
        api.put(path + "/config", {"description": desc})
    if guest_status(api, g) != "running":
        api.put(path + "/config", {"ciuser": PREP_USER, "sshkeys": urllib.parse.quote(pub + "\n", safe=""),
                                   "ipconfig0": "ip=dhcp", "agent": "enabled=1"})
        power(api, g, "start")
    host = None
    while time.time() - t0 < 300:
        check_budget(margin=30, what="waiting for template VM to boot")
        time.sleep(8)
        f = find_hosts_by_name([name], subnet, users=(PREP_USER,), keyfile=keyfile)
        if name in f:
            host = f[name][0]
            break
    if not host:
        say("  could not reach the template VM over SSH in %s (needs DHCP + internet on %s, reachable from here)"
            " - leaving it without guest agent" % (subnet, bridge))
        power(api, g, "stop")
        api.put(path + "/config", {"agent": "enabled=0", "delete": "ciuser,sshkeys",
                                   "description": desc + "\n" + PREP_MARK + " (skipped: unreachable)"})
        return False
    say("    found it at %s, installing agent ..." % host)
    tmo = 300 if not DEADLINE else max(60, int(DEADLINE - time.time()) + 20)
    try:
        r = subprocess.run(ssh_cmd(PREP_USER, host, keyfile) +
                           ["sudo tee /root/pveprep.sh >/dev/null && sudo bash /root/pveprep.sh </dev/null"],
                           input=PREP_SCRIPT,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=tmo, universal_newlines=True)
        ok = "PREP-OK" in r.stdout
    except subprocess.TimeoutExpired:
        raise Budget("installing guest agent in template")
    if not ok:
        raise PVEError("guest agent install failed in template VM:\n%s" % r.stdout[-1500:])
    for _ in range(60):
        if guest_status(api, g) == "stopped":
            break
        time.sleep(3)
    else:
        power(api, g, "stop")
    api.put(path + "/config", {"delete": "ciuser,sshkeys", "description": desc + "\n" + PREP_MARK})
    say("    guest agent installed; image cleaned and sealed")
    return True


# --------------------------------------------------------------------------- guest operations
def nic_string_vm(n, mac=None, model=None):
    model = n.get("model") or model or "virtio"
    s = "%s=%s" % (model, mac) if mac else model
    s += ",bridge=%s" % n["bridge"]
    if n.get("vlan"):
        s += ",tag=%s" % n["vlan"]
    if n.get("firewall"):
        s += ",firewall=1"
    return s


def ipconfig_string(n):
    ip = n.get("ip") or "dhcp"
    if ip == "dhcp":
        return "ip=dhcp"
    s = "ip=%s" % ip_with_prefix(ip)
    if n.get("gw"):
        s += ",gw=%s" % n["gw"]
    return s


def clone_vm(api, tpl, name, vmid, node=None, storage=None, full=None, pool=None):
    tnode = tpl["node"]
    target = node or tnode
    cross = target != tnode
    shared = cross and template_storage_shared(api, tpl)
    if storage or cross:
        full = True
    p = {"newid": vmid, "name": name, "full": 1 if full else 0}
    if pool:
        p["pool"] = pool
    if shared:
        p["target"] = target  # shared storage: clone straight onto the other node
    if storage:
        p["storage"] = storage
    elif full and not shared:
        p["storage"] = pick_storage(api, tnode, "images")
    say("  cloning %s (%s) -> %s (%s)%s ..." % (tpl.get("name"), tpl["vmid"], name, vmid, " [full]" if full else ""))
    try:
        api.wait(api.post("/nodes/%s/qemu/%s/clone" % (tnode, tpl["vmid"]), p), label="clone " + name)
    except PVEError as e:
        if not full and re.search(r"linked clone|not supported|feature", str(e), re.I):
            say("    linked clone not possible here, doing a full clone")
            p["full"] = 1
            p["storage"] = pick_storage(api, tnode, "images")
            api.wait(api.post("/nodes/%s/qemu/%s/clone" % (tnode, tpl["vmid"]), p), label="clone " + name)
        else:
            raise
    if cross and not shared:
        say("    migrating %s to %s ..." % (name, target))
        api.wait(api.post("/nodes/%s/qemu/%s/migrate" % (tnode, vmid),
                          {"target": target, "with-local-disks": 1}), label="migrate " + name, timeout=3600)
    return target


def configure_vm(api, node, vmid, cores=None, memory=None, sockets=None, cpu=None, nets=None,
                 ci=None, tags=None, description=None, onboot=None):
    cfg = {}
    if cores:
        cfg["cores"] = int(cores)
    if sockets:
        cfg["sockets"] = int(sockets)
    if memory:
        cfg["memory"] = int(memory)
    if cpu:
        cfg["cpu"] = cpu
    if nets:
        cur = api.get("/nodes/%s/qemu/%s/config" % (node, vmid)) or {}
        for i, n in enumerate(nets):
            n = dict(n, bridge=resolve_bridge(api, node, n.get("bridge")))
            old = cur.get("net%d" % i) or ""
            m = re.match(r"^(\w+)=([0-9A-Fa-f:]{17})", old)
            mac = m.group(2) if m else None
            cfg["net%d" % i] = nic_string_vm(n, mac=mac, model=(m.group(1) if m else None))
            if n.get("ip"):
                cfg["ipconfig%d" % i] = ipconfig_string(n)
    if ci:
        if ci.get("user"):
            cfg["ciuser"] = ci["user"]
        if ci.get("password"):
            cfg["cipassword"] = ci["password"]
        if ci.get("sshkeys"):
            cfg["sshkeys"] = urllib.parse.quote(ci["sshkeys"].strip() + "\n", safe="")
        if ci.get("nameserver"):
            cfg["nameserver"] = ci["nameserver"]
        if ci.get("searchdomain"):
            cfg["searchdomain"] = ci["searchdomain"]
    dels = []
    if tags:
        cfg["tags"] = tags
    elif tags == "":
        dels.append("tags")  # drop the template's tags
    if description:
        cfg["description"] = description
    elif description == "":
        dels.append("description")
    if onboot is not None:
        cfg["onboot"] = 1 if onboot else 0
    if dels:
        cfg["delete"] = ",".join(dels)
    if cfg:
        api.put("/nodes/%s/qemu/%s/config" % (node, vmid), cfg)


def boot_disk(cfg, kind):
    if kind == "lxc":
        return "rootfs"
    m = re.search(r"order=([^;,\s]+)", cfg.get("boot", ""))
    cands = ([m.group(1)] if m else []) + ["scsi0", "virtio0", "sata0", "ide0"]
    for c in cands:
        v = cfg.get(c)
        if v and "media=cdrom" not in v and "cloudinit" not in v:
            return c
    return None


def grow_disk(api, node, kind, vmid, size_gb, disk=None):
    cfg = api.get("/nodes/%s/%s/%s/config" % (node, kind, vmid)) or {}
    disk = disk or boot_disk(cfg, kind)
    if not disk or disk not in cfg:
        say("    (no disk found to resize)")
        return
    cur = cfg_size_gb(cfg[disk])
    if cur is not None and size_gb <= cur + 0.01:
        return
    r = api.put("/nodes/%s/%s/%s/resize" % (node, kind, vmid), {"disk": disk, "size": "%dG" % size_gb})
    api.wait(r, label="resize")


OSTYPE_ALIASES = {
    "linux": "l26", "windows": "win11", "win": "win11", "win2025": "win11", "win2k25": "win11", "win2022": "win11",
    "win2k22": "win11", "winserver": "win11", "server2022": "win11", "server2025": "win11",
    "win2019": "win10", "win2k19": "win10", "win2016": "win10", "win2k16": "win10", "server2019": "win10",
    "bsd": "other", "freebsd": "other", "pfsense": "other", "opnsense": "other",
}


def create_vm_installer(api, node, vmid, name, iso=None, cores=2, memory=2048, disk=32, storage=None,
                        nets=None, ostype="l26", efi=False, tpm=False, cpu=None, pool=None, tags=None,
                        virtio_iso=None, description=None, onboot=None):
    st = pick_storage(api, node, "images", storage)
    ostype = OSTYPE_ALIASES.get(str(ostype).lower(), ostype)
    win = ostype.startswith("w")
    paravirt = (not win) or bool(virtio_iso)
    bus = "scsi" if paravirt else "sata"
    model = "virtio" if paravirt else "e1000e"
    p = {"vmid": vmid, "name": name, "ostype": ostype, "memory": int(memory), "cores": int(cores),
         "sockets": 1, "cpu": cpu or ("host" if win else cpu_default(api)), "scsihw": "virtio-scsi-single",
         "agent": "enabled=1"}
    p["%s0" % bus] = "%s:%d,discard=on%s" % (st, int(disk), ",iothread=1" if bus == "scsi" else "")
    order = ["%s0" % bus]
    if iso:
        p["ide2"] = "%s,media=cdrom" % resolve_iso(api, node, iso)
        order.append("ide2")
    if virtio_iso:
        p["ide0"] = "%s,media=cdrom" % resolve_iso(api, node, virtio_iso)
    for i, n in enumerate(nets or [{"bridge": "auto"}]):
        n = dict(n, bridge=resolve_bridge(api, node, n.get("bridge")))
        p["net%d" % i] = nic_string_vm(n, model=n.get("model") or model)
    order.append("net0")
    p["boot"] = "order=" + ";".join(order)
    if efi or win:
        p["bios"] = "ovmf"
        p["machine"] = "q35"
        p["efidisk0"] = "%s:1,efitype=4m,pre-enrolled-keys=%d" % (st, 1 if win else 0)
    if tpm or ostype == "win11":
        p["tpmstate0"] = "%s:1,version=v2.0" % st
    if pool:
        p["pool"] = pool
    if tags:
        p["tags"] = tags
    if description:
        p["description"] = description
    if onboot is not None:
        p["onboot"] = 1 if onboot else 0
    say("  creating VM %s (%s) on %s ..." % (name, vmid, node))
    api.wait(api.post("/nodes/%s/qemu" % node, p), label="create " + name)


def create_ct(api, node, vmid, name, ostemplate, cores=1, memory=512, swap=512, disk=8, storage=None,
              nets=None, password=None, sshkeys=None, unprivileged=True, nesting=True, pool=None,
              tags=None, nameserver=None, searchdomain=None, description=None, onboot=None):
    st = pick_storage(api, node, "rootdir", storage)
    p = {"vmid": vmid, "hostname": name, "ostemplate": ostemplate, "cores": int(cores),
         "memory": int(memory), "swap": int(swap), "rootfs": "%s:%d" % (st, int(disk)),
         "unprivileged": 1 if unprivileged else 0}
    if nesting:
        p["features"] = "nesting=1"
    for i, n in enumerate(nets or [{"bridge": "auto", "ip": "dhcp"}]):
        s = "name=eth%d,bridge=%s" % (i, resolve_bridge(api, node, n.get("bridge")))
        if n.get("vlan"):
            s += ",tag=%s" % n["vlan"]
        if n.get("firewall"):
            s += ",firewall=1"
        ip = n.get("ip") or "dhcp"
        if ip == "dhcp":
            s += ",ip=dhcp"
        else:
            s += ",ip=%s" % ip_with_prefix(ip)
            if n.get("gw"):
                s += ",gw=%s" % n["gw"]
        p["net%d" % i] = s
    if password:
        p["password"] = password
    if sshkeys:
        p["ssh-public-keys"] = sshkeys.strip() + "\n"
    for k, v in (("pool", pool), ("tags", tags), ("nameserver", nameserver),
                 ("searchdomain", searchdomain), ("description", description)):
        if v:
            p[k] = v
    if onboot is not None:
        p["onboot"] = 1 if onboot else 0
    say("  creating CT %s (%s) on %s from %s ..." % (name, vmid, node, ostemplate.split("/")[-1]))
    api.wait(api.post("/nodes/%s/lxc" % node, p), label="create CT " + name, timeout=1800)


def guest_status(api, g):
    try:
        return (api.get(gpath(g) + "/status/current") or {}).get("status")
    except PVEError:
        return None


def power(api, g, action, timeout=120, force=False):
    params = {}
    if action == "shutdown":
        params = {"timeout": timeout, "forceStop": 1 if force else 0}
    r = api.post(gpath(g) + "/status/" + action, params)
    api.wait(r, label="%s %s" % (action, g.get("name") or g["vmid"]), timeout=timeout + 120)


def destroy_guest(api, g):
    if guest_status(api, g) == "running":
        say("  stopping %s (%s) ..." % (g.get("name"), g["vmid"]))
        power(api, g, "stop")
    say("  destroying %s (%s) on %s ..." % (g.get("name"), g["vmid"], g["node"]))
    api.wait(api.delete(gpath(g), {"purge": 1, "destroy-unreferenced-disks": 1}),
             label="destroy %s" % (g.get("name") or g["vmid"]))


def guest_ips(api, g, live=True):
    ips = []
    if live and g.get("status", "running") == "running":
        try:
            if g["type"] == "qemu":
                r = api.get(gpath(g) + "/agent/network-get-interfaces") or {}
                for itf in r.get("result", []):
                    if itf.get("name") == "lo":
                        continue
                    for a in itf.get("ip-addresses") or []:
                        if a.get("ip-address-type") == "ipv4" and not a["ip-address"].startswith("127."):
                            ips.append(a["ip-address"])
            else:
                for itf in api.get(gpath(g) + "/interfaces") or []:
                    if itf.get("name") != "lo" and itf.get("inet"):
                        ips.append(itf["inet"].split("/")[0])
        except PVEError:
            pass
    if not ips:
        try:
            cfg = api.get(gpath(g) + "/config") or {}
        except PVEError:
            cfg = {}
        for k in sorted(cfg):
            v = str(cfg[k])
            if (g["type"] == "qemu" and k.startswith("ipconfig")) or (g["type"] == "lxc" and re.match(r"net\d+$", k)):
                m = re.search(r"ip=([\d.]+)", v)
                if m:
                    ips.append(m.group(1) + "*")
                elif "ip=dhcp" in v:
                    ips.append("dhcp?")
    return ips


# --------------------------------------------------------------------------- commands: info
def cmd_status(api, args):
    v = api.version()
    nodes = api.nodes()
    guests = api.guests()
    try:
        cl = api.get("/cluster/status") or []
    except PVEError:
        cl = []
    storages = api.get("/cluster/resources", {"type": "storage"}) or []
    data = {"version": v, "cluster": cl, "nodes": nodes, "guests": guests, "storage": storages}

    def text(d):
        cname = next((c.get("name") for c in cl if c.get("type") == "cluster"), "(standalone)")
        say("Proxmox VE %s  cluster=%s  api=%s  as=%s" % (v.get("version"), cname, api.root, api.cfg["tokenid"]))
        say("")
        table(nodes, [("node", "NODE"), ("status", "STATUS"),
                      (lambda n: "%.0f%%" % (100 * (n.get("cpu") or 0)), "CPU"), ("maxcpu", "CORES"),
                      (lambda n: "%s/%s" % (hb(n.get("mem")), hb(n.get("maxmem"))), "MEM"),
                      (lambda n: "%.1fd" % ((n.get("uptime") or 0) / 86400.0), "UPTIME")])
        say("")
        cmd_ls_print(guests)
        say("")
        table(sorted(storages, key=lambda s: (s["storage"], s["node"])),
              [("storage", "STORAGE"), ("node", "NODE"), ("plugintype", "TYPE"), ("content", "CONTENT"),
               (lambda s: "%s/%s" % (hb(s.get("disk")), hb(s.get("maxdisk"))), "USED"),
               ("status", "STATUS")])
    emit(data, text)


def cmd_ls_print(guests):
    rows = sorted(guests, key=lambda g: int(g["vmid"]))
    table(rows, [("vmid", "VMID"), ("name", "NAME"), (lambda g: kind_label(g["type"]), "TYPE"),
                 ("node", "NODE"), (lambda g: "template" if g.get("template") else g.get("status"), "STATUS"),
                 ("maxcpu", "CPU"), (lambda g: hb(g.get("maxmem")), "MEM"), (lambda g: hb(g.get("maxdisk")), "DISK"),
                 ("pool", "POOL"), ("tags", "TAGS")])


def cmd_ls(api, args):
    gs = api.guests()
    if args.node:
        gs = [g for g in gs if g["node"] == args.node]
    if args.type:
        gs = [g for g in gs if kind_label(g["type"]) == args.type]
    if args.lab:
        gs = [g for g in gs if g.get("pool") == "lab-" + sanitize_name(args.lab)]
    if args.running:
        gs = [g for g in gs if g.get("status") == "running"]
    if args.templates:
        gs = [g for g in gs if g.get("template")]
    emit(gs, cmd_ls_print)


def cmd_nodes(api, args):
    emit(api.nodes(), lambda d: table(d, [("node", "NODE"), ("status", "STATUS"), ("maxcpu", "CORES"),
                                          (lambda n: "%s/%s" % (hb(n.get("mem")), hb(n.get("maxmem"))), "MEM"),
                                          (lambda n: "%.0f%%" % (100 * (n.get("cpu") or 0)), "CPU")]))


def cmd_storage(api, args):
    nodes = [args.node] if args.node else [n["node"] for n in api.online_nodes()]
    rows = []
    for n in nodes:
        for s in api.storages(n):
            s = dict(s, node=n)
            rows.append(s)
            if args.content:
                s["items"] = storage_content(api, n, s["storage"], args.content) \
                    if args.content in (s.get("content") or "") else []

    def text(rows):
        table(rows, [("node", "NODE"), ("storage", "STORAGE"), ("type", "TYPE"), ("content", "CONTENT"),
                     (lambda s: hb(s.get("avail")), "FREE"), (lambda s: hb(s.get("total")), "TOTAL"),
                     (lambda s: "yes" if s.get("shared") else "", "SHARED")])
        if args.content:
            for s in rows:
                for it in s.get("items", []):
                    say("  %-8s %-60s %s" % (s["node"], it["volid"], hb(it.get("size"))))
    emit(rows, text)


def cmd_info(api, args):
    g = find_guest(api, args.id)
    cfg = api.get(gpath(g) + "/config") or {}
    st = api.get(gpath(g) + "/status/current") or {}
    ips = guest_ips(api, dict(g, status=st.get("status")))
    data = {"guest": g, "status": st, "config": cfg, "ips": ips}

    def text(d):
        say("%s %s '%s' on %s - %s   IPs: %s" % (kind_label(g["type"]).upper(), g["vmid"], g.get("name"), g["node"],
                                                 st.get("status"), ", ".join(ips) or "?"))
        for k in sorted(cfg):
            if k in ("digest",):
                continue
            v = cfg[k]
            if k in ("cipassword",):
                v = "********"
            if k == "sshkeys":
                v = urllib.parse.unquote(str(v)).strip()[:60] + "..."
            say("  %-14s %s" % (k, v))
    emit(data, text)


def cmd_tasks(api, args):
    ts = api.get("/cluster/tasks") or []
    if args.errors:
        ts = [t for t in ts if t.get("status") not in (None, "OK", "RUNNING") and t.get("endtime")]
    ts = ts[: args.limit]
    emit(ts, lambda d: table(d, [(lambda t: time.strftime("%m-%d %H:%M:%S", time.localtime(t.get("starttime", 0))), "START"),
                                 ("node", "NODE"), ("type", "TYPE"), ("id", "ID"), ("user", "USER"),
                                 (lambda t: t.get("status") or "running", "STATUS")]))


def cmd_whoami(api, args):
    perms = api.get("/access/permissions", {"path": "/"}) or {}
    root = perms.get("/", {})
    data = {"tokenid": api.cfg["tokenid"], "env": api.cfg["env_path"], "api": api.root,
            "version": api.version().get("version"), "privileges_on_root": sorted(root)}
    emit(data, lambda d: say("%s @ %s (PVE %s), env=%s\n%d privileges on / : %s" % (
        d["tokenid"], d["api"], d["version"], d["env"], len(root), " ".join(sorted(root)))))


def cmd_api(api, args):
    params = parse_kv(args.params)
    method = args.method.upper()
    r = api.request(method, args.path, params)
    if method != "GET" and isinstance(r, str) and r.startswith("UPID:") and args.wait:
        api.wait(r)
    print(json.dumps(r, indent=2, default=str))


# --------------------------------------------------------------------------- commands: guests
def _nets_from_args(args):
    if getattr(args, "net", None):
        nets = [parse_netarg(s) for s in args.net]
    else:
        nets = None
    if nets and getattr(args, "gw", None):
        for n in nets:
            if n.get("ip") not in (None, "dhcp"):
                n.setdefault("gw", args.gw)
                break
    return nets


def refuse_duplicate(api, name):
    dup = [g for g in api.guests() if (g.get("name") or "").lower() == name]
    if dup:
        die("A guest named '%s' already exists (VMID %s on %s). Pick another name - or, if an earlier run was "
            "interrupted, finish that one with `set`/`start`." % (name, dup[0]["vmid"], dup[0]["node"]))


def cmd_vm_create(api, args):
    name = sanitize_name(args.name)
    refuse_duplicate(api, name)
    nets = _nets_from_args(args)
    pw = args.password
    if pw == "auto":
        pw = gen_password()
    sshkey = read_ssh_key(args.ssh_key) if args.ssh_key else None
    vmids = VMIDs(api)
    if args.template:
        want_node = args.node if args.node not in (None, "auto") else None
        tpl = find_vm_template(api, args.template, want_node)
        if tpl and want_node and tpl["node"] != want_node and distro_key(args.template) in CLOUD_IMAGES \
                and not template_storage_shared(api, tpl) and not args.no_build:
            say("Template only exists on %s (local storage) - building a copy on %s once, so clones stay fast."
                % (tpl["node"], want_node))
            tpl = None
        if not tpl:
            if distro_key(args.template) in CLOUD_IMAGES and not args.no_build:
                say("No template for '%s' yet - building one from the official cloud image." % args.template)
                tpl = build_vm_template(api, args.template, node=want_node or args.node)
            else:
                die("No VM template matches '%s'. Templates: %s. Buildable: %s" % (
                    args.template,
                    ", ".join("%s(%s)" % (g.get("name"), g["vmid"]) for g in api.guests() if g.get("template")) or "none",
                    ", ".join(sorted(CLOUD_IMAGES))))
        node = tpl["node"] if args.node in (None, "auto") else pick_node(api, args.node)
        vmid = vmids.take(args.vmid)
        node = clone_vm(api, tpl, name, vmid, node=node, storage=args.storage, full=args.full, pool=args.pool)
        ci = {"user": args.user, "password": pw, "sshkeys": sshkey, "nameserver": args.nameserver,
              "searchdomain": args.searchdomain}
        configure_vm(api, node, vmid, cores=args.cores, memory=mem_mb(args.memory), cpu=args.cpu, nets=nets,
                     ci=ci, tags=args.tags or "", description=args.description or "", onboot=args.onboot)
        if args.disk:
            grow_disk(api, node, "qemu", vmid, disk_gb(args.disk))
    else:
        node = pick_node(api, args.node)
        vmid = vmids.take(args.vmid)
        create_vm_installer(api, node, vmid, name, iso=args.iso, cores=args.cores or 2,
                            memory=mem_mb(args.memory, 2048), disk=disk_gb(args.disk, 32), storage=args.storage,
                            nets=nets, ostype=args.ostype, efi=args.efi, tpm=args.tpm, cpu=args.cpu,
                            pool=args.pool, tags=args.tags, virtio_iso=args.virtio_iso,
                            description=args.description, onboot=args.onboot)
    g = {"vmid": vmid, "node": node, "type": "qemu", "name": name}
    if args.start:
        say("  starting %s ..." % name)
        power(api, g, "start")
    out = {"vmid": vmid, "name": name, "node": node, "started": bool(args.start)}
    if args.template:
        out.update(user=args.user, password=pw, ssh_key=bool(sshkey))
    emit(out, lambda d: say("OK: VM %s '%s' on %s%s%s" % (
        vmid, name, node, " (started)" if args.start else "",
        ("\n  login: %s / %s" % (args.user, pw)) if (args.template and args.user and pw) else "")))


def cmd_vm_clone(api, args):
    src = find_guest(api, args.source, kinds=("qemu",))
    name = sanitize_name(args.name)
    vmid = VMIDs(api).take(args.vmid)
    node = clone_vm(api, src, name, vmid, node=None if args.node in (None, "auto") else args.node,
                    storage=args.storage, full=args.full or not src.get("template"), pool=args.pool)
    if args.start:
        power(api, {"vmid": vmid, "node": node, "type": "qemu", "name": name}, "start")
    emit({"vmid": vmid, "name": name, "node": node}, lambda d: say("OK: cloned to %s '%s' on %s" % (vmid, name, node)))


def cmd_vm_template(api, args):
    g = find_guest(api, args.id, kinds=("qemu",))
    need_yes(args, "convert VM %s '%s' into a template (irreversible)" % (g["vmid"], g.get("name")))
    if guest_status(api, g) == "running":
        die("VM must be stopped first")
    api.wait(api.post(gpath(g) + "/template"))
    say("OK: %s is now a template" % g["vmid"])


def cmd_ct_create(api, args):
    name = sanitize_name(args.name)
    refuse_duplicate(api, name)
    node = pick_node(api, args.node)
    nets = _nets_from_args(args)
    pw = args.password
    if pw == "auto" or (not pw and not args.ssh_key):
        pw = gen_password()
    sshkey = read_ssh_key(args.ssh_key) if args.ssh_key else None
    ost = ensure_ct_template(api, node, args.template)
    vmid = VMIDs(api).take(args.vmid)
    create_ct(api, node, vmid, name, ost, cores=args.cores, memory=mem_mb(args.memory, 512),
              swap=mem_mb(args.swap, 512), disk=disk_gb(args.disk, 8), storage=args.storage, nets=nets,
              password=pw, sshkeys=sshkey, unprivileged=not args.privileged, nesting=not args.no_nesting,
              pool=args.pool, tags=args.tags, nameserver=args.nameserver, searchdomain=args.searchdomain,
              description=args.description, onboot=args.onboot)
    if args.start:
        power(api, {"vmid": vmid, "node": node, "type": "lxc", "name": name}, "start")
    emit({"vmid": vmid, "name": name, "node": node, "root_password": pw},
         lambda d: say("OK: CT %s '%s' on %s%s\n  login: root / %s" % (vmid, name, node,
                                                                        " (started)" if args.start else "", pw)))


def cmd_power(api, args):
    for ident in args.ids:
        g = find_guest(api, ident)
        say("%s %s (%s) ..." % (args.action, g.get("name"), g["vmid"]))
        try:
            power(api, g, args.action, timeout=args.timeout, force=args.force)
            say("  OK")
        except PVEError as e:
            say("  FAILED: %s" % e)


def cmd_set(api, args):
    g = find_guest(api, args.id)
    kv = parse_kv(args.settings)
    for k in list(kv):
        if k == "memory" or k == "swap":
            kv[k] = mem_mb(kv[k])
        elif k == "sshkeys" and g["type"] == "qemu":
            kv[k] = urllib.parse.quote((read_ssh_key(kv[k]) or "") + "\n", safe="")
    if args.delete:
        kv["delete"] = args.delete
    if not kv:
        die("Nothing to set (give key=value pairs, e.g. cores=4 memory=8G net1=virtio,bridge=vmbr1)")
    r = api.put(gpath(g) + "/config", kv)
    api.wait(r)
    pending = None
    try:
        pending = [p for p in (api.get(gpath(g) + "/pending") or []) if "pending" in p or p.get("delete")]
    except PVEError:
        pass
    say("OK: updated %s (%s): %s" % (g.get("name"), g["vmid"], ", ".join(kv)))
    if pending:
        say("  NOTE: some changes are pending until the guest is rebooted: %s"
            % ", ".join(p["key"] for p in pending))


def cmd_resize(api, args):
    g = find_guest(api, args.id)
    size = args.size
    cfg = api.get(gpath(g) + "/config") or {}
    disk = args.disk or boot_disk(cfg, g["type"])
    if not disk:
        die("Could not find a disk to resize; pass --disk")
    if size.startswith("+"):
        r = api.put(gpath(g) + "/resize", {"disk": disk, "size": "+%dG" % disk_gb(size[1:])})
    else:
        r = api.put(gpath(g) + "/resize", {"disk": disk, "size": "%dG" % disk_gb(size)})
    api.wait(r)
    say("OK: %s %s resized to %s" % (g.get("name"), disk, size))


def cmd_delete(api, args):
    gs = [find_guest(api, i) for i in args.ids]
    desc = ", ".join("%s %s '%s' on %s" % (kind_label(g["type"]), g["vmid"], g.get("name"), g["node"]) for g in gs)
    need_yes(args, "permanently DELETE " + desc + " (disks included)")
    for g in gs:
        try:
            destroy_guest(api, g)
            say("  deleted %s" % g["vmid"])
        except PVEError as e:
            say("  FAILED %s: %s" % (g["vmid"], e))


def cmd_ip(api, args):
    out = []
    unknown = {}
    for ident in args.ids:
        g = find_guest(api, ident)
        ips = guest_ips(api, g)
        out.append({"vmid": g["vmid"], "name": g.get("name"), "status": g.get("status"), "ips": ips})
        if args.scan and "dhcp?" in ips:
            cfg = api.get(gpath(g) + "/config") or {}
            m = re.search(r"bridge=([\w.-]+)", str(cfg.get("net0", "")))
            subnet = bridge_subnet(api, g["node"], m.group(1)) if m else None
            if subnet:
                users = [u for u in (cfg.get("ciuser"), "lab", "root") if u]
                unknown.setdefault((subnet, tuple(dict.fromkeys(users))), []).append(out[-1])
    for (subnet, users), rows in unknown.items():
        say("scanning %s for SSH hosts that accept the lab key ..." % subnet)
        found = find_hosts_by_name([r["name"] for r in rows], subnet, users=users)
        for r in rows:
            if r["name"] in found:
                r["ips"] = [found[r["name"]][0] if x == "dhcp?" else x for x in r["ips"]]
                r["ssh_user"] = found[r["name"]][1]
    emit(out, lambda d: [say("%-6s %-20s %-8s %s" % (r["vmid"], r["name"], r["status"], ", ".join(r["ips"]) or "?"))
                         for r in d] and say("(* = configured IP, not yet confirmed by the guest; 'dhcp?' = unknown - add --scan)"))


def cmd_migrate(api, args):
    g = find_guest(api, args.id)
    target = pick_node(api, args.target)
    p = {"target": target}
    running = guest_status(api, g) == "running"
    if g["type"] == "qemu":
        p["with-local-disks"] = 1
        if running:
            p["online"] = 1
    elif running:
        p["restart"] = 1
    say("migrating %s (%s) %s -> %s ..." % (g.get("name"), g["vmid"], g["node"], target))
    api.wait(api.post(gpath(g) + "/migrate", p), label="migrate", timeout=7200)
    say("OK")


def cmd_snap(api, args):
    g = find_guest(api, args.id)
    base = gpath(g) + "/snapshot"
    if args.action == "list":
        snaps = [s for s in (api.get(base) or []) if s.get("name") != "current"]
        emit(snaps, lambda d: table(d, [("name", "NAME"), (lambda s: time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(s["snaptime"])) if s.get("snaptime") else "", "TIME"),
                                         ("description", "DESCRIPTION")]))
        return
    if not args.name:
        die("snapshot name required")
    if args.action == "create":
        p = {"snapname": args.name, "description": args.description}
        if g["type"] == "qemu" and args.vmstate:
            p["vmstate"] = 1
        api.wait(api.post(base, p), label="snapshot")
        say("OK: snapshot '%s' of %s" % (args.name, g.get("name")))
    elif args.action == "rollback":
        need_yes(args, "roll %s back to snapshot '%s' (current state is lost)" % (g.get("name"), args.name))
        api.wait(api.post("%s/%s/rollback" % (base, args.name)), label="rollback")
        say("OK: rolled back")
    elif args.action == "delete":
        need_yes(args, "delete snapshot '%s' of %s" % (args.name, g.get("name")))
        api.wait(api.delete("%s/%s" % (base, args.name)), label="delete snapshot")
        say("OK: deleted snapshot")


# --------------------------------------------------------------------------- commands: templates & ISOs
def cmd_template_list(api, args):
    nodes = [args.node] if args.node else [n["node"] for n in api.online_nodes()]
    vm_tpls = [g for g in api.guests() if g.get("template")]
    ct = {n: list_content(api, n, "vztmpl") for n in nodes}
    data = {"vm_templates": vm_tpls, "ct_templates": ct, "buildable_cloud_images": sorted(CLOUD_IMAGES)}

    def text(d):
        say("VM templates (clone source):")
        table(vm_tpls, [("vmid", "VMID"), ("name", "NAME"), ("node", "NODE"), (lambda g: hb(g.get("maxdisk")), "DISK")])
        say("\nContainer templates (downloaded):")
        for n, v in ct.items():
            for x in v:
                say("  %-8s %s" % (n, x))
        say("\nCloud images buildable with `template build`: " + ", ".join(sorted(CLOUD_IMAGES)))
        say("Container templates downloadable on demand: see `template ct-available`")
    emit(data, text)


def cmd_template_build(api, args):
    t = build_vm_template(api, args.distro, node=args.node, storage=args.storage, vmid=args.vmid,
                          bridge=args.bridge, name=args.name, agent=not args.no_agent)
    emit(t, lambda d: say("OK: template %s (%s) on %s" % (d.get("name"), d["vmid"], d["node"])))


def cmd_template_ct_available(api, args):
    node = pick_node(api, args.node)
    apl = api.get("/nodes/%s/aplinfo" % node) or []
    if args.filter:
        apl = [a for a in apl if args.filter.lower() in a["template"].lower()]
    emit(apl, lambda d: table(sorted(d, key=lambda a: natkey(a["template"])),
                              [("template", "TEMPLATE"), ("section", "SECTION"), ("headline", "DESCRIPTION")]))


def cmd_template_ct_download(api, args):
    nodes = [n["node"] for n in api.online_nodes()] if args.node == "all" else [pick_node(api, args.node)]
    for n in nodes:
        v = ensure_ct_template(api, n, args.spec, args.storage)
        say("OK: %s has %s" % (n, v))


def cmd_iso_list(api, args):
    nodes = [args.node] if args.node else [n["node"] for n in api.online_nodes()]
    rows = []
    for n in nodes:
        for s in api.storages(n):
            if "iso" in (s.get("content") or ""):
                for it in storage_content(api, n, s["storage"], "iso"):
                    rows.append({"node": n, "volid": it["volid"], "size": it.get("size")})
    emit(rows, lambda d: table(d, [("node", "NODE"), ("volid", "VOLID"), (lambda r: hb(r.get("size")), "SIZE")]))


def cmd_iso_download(api, args):
    nodes = [n["node"] for n in api.online_nodes()] if args.node == "all" else [pick_node(api, args.node)]
    fname = args.filename or urllib.parse.urlparse(args.url).path.rsplit("/", 1)[-1]
    if not re.search(r"\.(iso|img)$", fname, re.I):
        die("ISO filename must end in .iso or .img - pass --filename")
    for n in nodes:
        st = pick_storage(api, n, "iso", args.storage)
        p = {"content": "iso", "filename": fname, "url": args.url}
        if args.checksum:
            p["checksum"] = args.checksum
            p["checksum-algorithm"] = args.algorithm
        say("downloading %s -> %s:iso/%s on %s ..." % (args.url, st, fname, n))
        api.wait(api.post("/nodes/%s/storage/%s/download-url" % (n, st), p), label="ISO download", timeout=7200)
        say("OK: %s:iso/%s on %s" % (st, fname, n))


# --------------------------------------------------------------------------- commands: host networking
NET_FIELDS = ("autostart", "bridge_ports", "bridge_vlan_aware", "cidr", "gateway", "comments", "mtu",
              "cidr6", "gateway6", "vlan-id", "vlan-raw-device", "bond_mode", "slaves", "bond-primary",
              "bond_xmit_hash_policy")


def protected_ifaces(api, node):
    ifs = api.get("/nodes/%s/network" % node) or []
    host = api.cfg.get("host")
    prot = set()
    for i in ifs:
        if i.get("gateway"):
            prot.add(i["iface"])
        for k in ("cidr", "address"):
            if i.get(k) and str(i[k]).split("/")[0] == host:
                prot.add(i["iface"])
    for i in ifs:
        if i["iface"] in prot:
            for p in (i.get("bridge_ports") or i.get("slaves") or "").split():
                prot.add(p)
    return prot


def cmd_net_list(api, args):
    nodes = [args.node] if args.node and args.node != "all" else [n["node"] for n in api.online_nodes()]
    rows = []
    for n in nodes:
        full = api.get("/nodes/%s/network" % n, full=True) or {}
        for i in full.get("data") or []:
            rows.append(dict(i, node=n))
        if full.get("changes"):
            rows.append({"node": n, "iface": "(pending)", "comments": "uncommitted changes - run `net apply` or `net revert`"})
    rows.sort(key=lambda r: (r["node"], natkey(r["iface"])))
    emit(rows, lambda d: table(d, [("node", "NODE"), ("iface", "IFACE"), ("type", "TYPE"),
                                   (lambda r: "up" if r.get("active") else ("" if r["iface"] == "(pending)" else "down"), "STATE"),
                                   ("cidr", "CIDR"), ("gateway", "GATEWAY"),
                                   (lambda r: r.get("bridge_ports") or r.get("slaves") or r.get("vlan-raw-device"), "PORTS"),
                                   (lambda r: "yes" if r.get("bridge_vlan_aware") else "", "VLAN-AWARE"),
                                   ("comments", "COMMENT")]))


def guard_foreign_pending(api, nodes, args):
    """Refuse --apply when a node already has staged changes we did not make (they would go live too)."""
    if not getattr(args, "apply", False):
        return
    busy = [n for n in nodes if (api.get("/nodes/%s/network" % n, full=True) or {}).get("changes")]
    if busy:
        die("%s already has staged network changes that --apply would also activate. Review them with "
            "`net pending`, then either `net apply --yes` or `net revert --yes` first." % ", ".join(busy))


def _apply_net(api, nodes):
    for n in nodes:
        say("  applying network config on %s (ifreload) ..." % n)
        api.wait(api.put("/nodes/%s/network" % n), label="network reload " + n)


def _next_bridge(api, nodes):
    used = set()
    for n in nodes:
        used |= {i["iface"] for i in api.get("/nodes/%s/network" % n) or []}
    i = 1
    while "vmbr%d" % i in used:
        i += 1
    return "vmbr%d" % i


def cmd_net_add_bridge(api, args):
    nodes = [n["node"] for n in api.online_nodes()] if args.node in (None, "all") else [pick_node(api, args.node)]
    guard_foreign_pending(api, nodes, args)
    name = args.name if args.name != "auto" else _next_bridge(api, nodes)
    if args.gateway:
        say("WARNING: a second default gateway on a node can break its connectivity.")
    for n in nodes:
        existing = {i["iface"] for i in api.get("/nodes/%s/network" % n) or []}
        if name in existing:
            say("  %s already exists on %s - skipped" % (name, n))
            continue
        p = {"iface": name, "type": "bridge", "autostart": 1, "bridge_ports": args.ports or None,
             "cidr": args.cidr, "gateway": args.gateway, "comments": args.comment,
             "bridge_vlan_aware": 1 if args.vlan_aware else None, "mtu": args.mtu}
        api.post("/nodes/%s/network" % n, p)
        say("  staged bridge %s on %s" % (name, n))
    if args.apply:
        _apply_net(api, nodes)
        say("OK: %s is live on %s" % (name, ", ".join(nodes)))
    else:
        say("Staged only. Run `net apply --node %s` to activate." % (args.node or "all"))


def cmd_net_add_vlan(api, args):
    nodes = [n["node"] for n in api.online_nodes()] if args.node in (None, "all") else [pick_node(api, args.node)]
    guard_foreign_pending(api, nodes, args)
    iface = args.name or "%s.%s" % (args.raw_device, args.tag)
    for n in nodes:
        p = {"iface": iface, "type": "vlan", "autostart": 1, "cidr": args.cidr, "comments": args.comment}
        if not re.match(r"^.+\.\d+$", iface):
            p["vlan-raw-device"] = args.raw_device
            p["vlan-id"] = args.tag
        api.post("/nodes/%s/network" % n, p)
        say("  staged VLAN iface %s on %s" % (iface, n))
    if args.apply:
        _apply_net(api, nodes)
    else:
        say("Staged only. Run `net apply` to activate.")


def cmd_net_set(api, args):
    nodes = [n["node"] for n in api.online_nodes()] if args.node in (None, "all") else [pick_node(api, args.node)]
    guard_foreign_pending(api, nodes, args)
    kv = parse_kv(args.settings)
    for n in nodes:
        prot = protected_ifaces(api, n)
        if args.iface in prot and not args.force:
            die("%s carries %s's management IP/gateway. Changing it can cut off access to the node. "
                "Re-run with --force only if the user explicitly asked for this." % (args.iface, n))
        cur = api.get("/nodes/%s/network/%s" % (n, args.iface)) or {}
        p = {k: cur[k] for k in NET_FIELDS if cur.get(k) not in (None, "")}
        if "cidr" not in p and cur.get("address") and cur.get("netmask"):
            p["cidr"] = str(ipaddress.ip_interface("%s/%s" % (cur["address"], cur["netmask"])))
        p.update(kv)
        p["type"] = cur.get("type")
        if args.delete:
            p["delete"] = args.delete
            for k in args.delete.split(","):
                p.pop(k, None)
        api.put("/nodes/%s/network/%s" % (n, args.iface), p)
        say("  staged change to %s on %s: %s" % (args.iface, n, ", ".join("%s=%s" % x for x in kv.items())))
    if args.apply:
        _apply_net(api, nodes)
    else:
        say("Staged only. Run `net apply` to activate (or `net revert`).")


def cmd_net_delete(api, args):
    nodes = [n["node"] for n in api.online_nodes()] if args.node in (None, "all") else [pick_node(api, args.node)]
    guard_foreign_pending(api, nodes, args)
    for n in nodes:
        if args.iface in protected_ifaces(api, n):
            die("Refusing: %s is the management interface on %s" % (args.iface, n))
    need_yes(args, "delete interface %s on %s%s" % (args.iface, ", ".join(nodes), " and apply" if args.apply else ""))
    for n in nodes:
        try:
            api.delete("/nodes/%s/network/%s" % (n, args.iface))
            say("  staged removal of %s on %s" % (args.iface, n))
        except PVEError as e:
            say("  %s: %s" % (n, e))
    if args.apply:
        _apply_net(api, nodes)


def cmd_net_apply(api, args):
    nodes = [n["node"] for n in api.online_nodes()] if args.node in (None, "all") else [pick_node(api, args.node)]
    need_yes(args, "reload networking on %s (applies all staged interface changes)" % ", ".join(nodes))
    _apply_net(api, nodes)
    say("OK")


def cmd_net_pending(api, args):
    nodes = [n["node"] for n in api.online_nodes()] if args.node in (None, "all") else [pick_node(api, args.node)]
    out = {n: (api.get("/nodes/%s/network" % n, full=True) or {}).get("changes") for n in nodes}
    emit(out, lambda d: [say("== %s\n%s" % (n, c or "(no staged changes)")) for n, c in d.items()])


def cmd_net_revert(api, args):
    nodes = [n["node"] for n in api.online_nodes()] if args.node in (None, "all") else [pick_node(api, args.node)]
    pending = [n for n in nodes if (api.get("/nodes/%s/network" % n, full=True) or {}).get("changes")]
    if not pending:
        say("No staged network changes on %s" % ", ".join(nodes))
        return
    need_yes(args, "discard ALL staged (unapplied) network changes on %s - including any made in the web UI"
             % ", ".join(pending))
    for n in pending:
        api.delete("/nodes/%s/network" % n)
        say("  reverted staged network changes on %s" % n)


# --------------------------------------------------------------------------- commands: SDN
def sdn_apply(api):
    say("  applying SDN configuration ...")
    api.wait(api.put("/cluster/sdn"), label="SDN apply", timeout=600)


def sdn_pending(api):
    for kind in ("zones", "vnets"):
        try:
            if any(x.get("state") for x in api.get("/cluster/sdn/%s" % kind, {"pending": 1}) or []):
                return True
        except PVEError:
            pass
    return False


def cmd_sdn_list(api, args):
    zones = api.get("/cluster/sdn/zones") or []
    vnets = api.get("/cluster/sdn/vnets") or []
    for v in vnets:
        try:
            v["subnets"] = api.get("/cluster/sdn/vnets/%s/subnets" % v["vnet"]) or []
        except PVEError:
            v["subnets"] = []
    data = {"zones": zones, "vnets": vnets}

    def text(d):
        say("Zones:")
        table(zones, [("zone", "ZONE"), ("type", "TYPE"), ("bridge", "BRIDGE"), ("ipam", "IPAM"),
                      ("dhcp", "DHCP"), ("state", "STATE"), ("pending", "PENDING")])
        say("\nVNets (usable as a bridge name on every node in the zone):")
        table(vnets, [("vnet", "VNET"), ("zone", "ZONE"), ("tag", "TAG"), ("alias", "ALIAS"),
                      (lambda v: ", ".join("%s gw %s%s" % (s.get("cidr"), s.get("gateway") or "-",
                                                            " NAT" if s.get("snat") else "") for s in v["subnets"]), "SUBNETS"),
                      ("state", "STATE")])
    emit(data, text)


def cmd_sdn_zone_add(api, args):
    p = {"zone": args.zone, "type": args.type, "ipam": "pve"}
    if args.type in ("vlan", "qinq"):
        if not args.bridge:
            die("--bridge is required for vlan zones (e.g. vmbr0, should be VLAN-aware)")
        p["bridge"] = args.bridge
    if args.type in ("vxlan", "evpn"):
        if not args.peers:
            die("--peers is required for vxlan zones (comma-separated node IPs)")
        p["peers"] = args.peers
    if args.mtu:
        p["mtu"] = args.mtu
    if args.nodes:
        p["nodes"] = args.nodes
    if args.dhcp:
        p["dhcp"] = "dnsmasq"
    api.post("/cluster/sdn/zones", p)
    say("staged zone %s (%s)" % (args.zone, args.type))
    if args.apply:
        sdn_apply(api)


def cmd_sdn_vnet_add(api, args):
    p = {"vnet": args.vnet, "zone": args.zone, "tag": args.tag, "alias": args.alias}
    if args.vlan_aware:
        p["vlanaware"] = 1
    api.post("/cluster/sdn/vnets", p)
    say("staged vnet %s in zone %s" % (args.vnet, args.zone))
    if args.subnet:
        net = ipaddress.ip_network(args.subnet, strict=False)
        sp = {"subnet": str(net), "type": "subnet", "gateway": args.gateway or str(net.network_address + 1)}
        if args.snat:
            sp["snat"] = 1
        if args.dhcp_range:
            sp["dhcp-range"] = ["start-address=%s,end-address=%s" % tuple(args.dhcp_range.split("-", 1))]
        api.post("/cluster/sdn/vnets/%s/subnets" % args.vnet, sp)
        say("staged subnet %s gw %s%s" % (net, sp["gateway"], " with NAT" if args.snat else ""))
    if args.apply:
        sdn_apply(api)


def _delete_vnet(api, vnet):
    try:
        for s in api.get("/cluster/sdn/vnets/%s/subnets" % vnet) or []:
            api.delete("/cluster/sdn/vnets/%s/subnets/%s" % (vnet, s["subnet"]))
    except PVEError as e:
        say("  (subnets of %s: %s)" % (vnet, e))
    api.delete("/cluster/sdn/vnets/%s" % vnet)


def cmd_sdn_vnet_del(api, args):
    users = []
    for g in api.guests():
        try:
            cfg = api.get(gpath(g) + "/config") or {}
        except PVEError:
            continue
        if any(re.search(r"bridge=%s(,|$)" % re.escape(args.vnet), str(v)) for k, v in cfg.items() if k.startswith("net")):
            users.append("%s(%s)" % (g.get("name"), g["vmid"]))
    if users:
        die("vnet %s is still used by: %s" % (args.vnet, ", ".join(users)))
    need_yes(args, "delete SDN vnet %s (and its subnets)" % args.vnet)
    _delete_vnet(api, args.vnet)
    say("staged removal of vnet %s" % args.vnet)
    if args.apply:
        sdn_apply(api)


def cmd_sdn_zone_del(api, args):
    need_yes(args, "delete SDN zone %s" % args.zone)
    api.delete("/cluster/sdn/zones/%s" % args.zone)
    if args.apply:
        sdn_apply(api)


def cmd_sdn_apply(api, args):
    sdn_apply(api)
    say("OK")


# --------------------------------------------------------------------------- labs
def state_path(lab):
    return os.path.join(LABS_DIR, "%s.state.json" % lab)


def load_state(lab):
    p = state_path(lab)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_state(st):
    os.makedirs(LABS_DIR, exist_ok=True)
    p = state_path(st["lab"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, default=str)
    os.replace(tmp, p)


def pool_get(api, poolid):
    try:
        r = api.get("/pools", {"poolid": poolid})
        if isinstance(r, list):
            return r[0] if r else None
    except PVEError:
        pass
    try:
        return api.get("/pools/%s" % poolid)
    except PVEError:
        return None


def pool_set_comment(api, poolid, comment):
    try:
        api.put("/pools", {"poolid": poolid, "comment": comment})
    except PVEError:
        api.put("/pools/%s" % poolid, {"comment": comment})


def pool_delete(api, poolid):
    try:
        api.delete("/pools", {"poolid": poolid})
    except PVEError:
        api.delete("/pools/%s" % poolid)


def pool_meta(comment):
    m = re.search(r"vnets=([\w,]*)", comment or "")
    return {"vnets": [v for v in (m.group(1).split(",") if m else []) if v]}


class IPAlloc:
    def __init__(self, cidr, gateway=None, start=10, rng=None):
        self.net = ipaddress.ip_network(cidr, strict=False)
        self.prefixlen = self.net.prefixlen
        self.used = set()
        if gateway:
            self.used.add(str(gateway))
        if rng:
            a, b = rng.split("-", 1)
            self.next, self.end = int(ipaddress.ip_address(a.strip())), int(ipaddress.ip_address(b.strip()))
        else:
            self.next = int(self.net.network_address) + int(start)
            self.end = int(self.net.broadcast_address) - 1

    def reserve(self, ip):
        self.used.add(str(ip).split("/")[0])

    def take(self):
        while self.next <= self.end:
            a = ipaddress.ip_address(self.next)
            self.next += 1
            if str(a) not in self.used:
                self.used.add(str(a))
                return str(a)
        die("Ran out of addresses in %s" % self.net)


def auto_cidr(api, taken=()):
    used = set(x for x in taken if x)
    try:
        for v in api.get("/cluster/sdn/vnets") or []:
            for s in api.get("/cluster/sdn/vnets/%s/subnets" % v["vnet"]) or []:
                used.add(s.get("cidr"))
    except PVEError:
        pass
    if os.path.isdir(LABS_DIR):
        for f in os.listdir(LABS_DIR):
            if f.endswith(".state.json"):
                try:
                    with open(os.path.join(LABS_DIR, f), encoding="utf-8") as fh:
                        for n in (json.load(fh).get("networks") or {}).values():
                            used.add(n.get("cidr"))
                except Exception:
                    pass
    pool = ipaddress.ip_network(LAB_SUBNETS, strict=False)
    for c in (pool.subnets(new_prefix=24) if pool.prefixlen <= 24 else [pool]):
        if str(c) not in used:
            return str(c)
    die("No free /24 left in %s (set PVE_LAB_SUBNETS or give the network a cidr)" % LAB_SUBNETS)


def alloc_vnet_id(api, lab, extra=()):
    existing = {v["vnet"] for v in api.get("/cluster/sdn/vnets") or []} | set(extra)
    base = re.sub(r"[^a-z0-9]", "", lab.lower()) or "lab"
    if base[0].isdigit():
        base = "l" + base
    base = base[:6]
    for i in range(100):
        cand = ("%s%d" % (base, i))[:8]
        if cand not in existing:
            return cand
    die("could not allocate a vnet id")


def ensure_isolated_net(api, lab, nname, n, taken_ids):
    changed = False
    zone = n.get("zone") or LAB_ZONE
    zones = {z["zone"] for z in api.get("/cluster/sdn/zones") or []}
    if zone not in zones:
        api.post("/cluster/sdn/zones", {"zone": zone, "type": "simple", "ipam": "pve"})
        say("  created SDN zone '%s' (simple)" % zone)
        changed = True
    vnets = {v["vnet"] for v in api.get("/cluster/sdn/vnets") or []}
    vnet = n.get("vnet")
    if not vnet or vnet not in vnets:
        vnet = vnet or alloc_vnet_id(api, lab, taken_ids)
        api.post("/cluster/sdn/vnets", {"vnet": vnet, "zone": zone, "alias": ("lab %s %s" % (lab, nname))[:60]})
        say("  created vnet %s for network '%s'" % (vnet, nname))
        changed = True
    subs = api.get("/cluster/sdn/vnets/%s/subnets" % vnet) or []
    if not any(s.get("cidr") == n["cidr"] for s in subs):
        p = {"subnet": n["cidr"], "type": "subnet", "gateway": n["gateway"]}
        if n.get("nat"):
            p["snat"] = 1
        api.post("/cluster/sdn/vnets/%s/subnets" % vnet, p)
        say("  added subnet %s gw %s%s" % (n["cidr"], n["gateway"], " +NAT" if n.get("nat") else ""))
        changed = True
    return vnet, changed


def load_blueprint(path):
    if not os.path.isfile(path):
        for base in (LABS_DIR, os.path.join(os.path.dirname(HERE), "examples")):
            cand = os.path.join(base, path)
            if os.path.isfile(cand):
                path = cand
                break
        else:
            die("Blueprint not found: %s (also looked in %s)" % (path, LABS_DIR))
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    if path.lower().endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError:
            die("PyYAML is not installed here - write the blueprint as .json instead")
        bp = yaml.safe_load(txt)
    else:
        bp = json.loads(txt)
    if not isinstance(bp, dict):
        die("Blueprint must be an object")
    return bp, os.path.abspath(path)


def expand_machines(bp):
    out = []
    for m in bp.get("machines") or []:
        if "name" not in m:
            die("Every machine needs a name: %s" % m)
        cnt = int(m.get("count", 1))
        for i in range(1, cnt + 1):
            mm = dict(m)
            base = str(m["name"])
            if "{n}" in base:
                nm = base.replace("{n}", str(i))
            elif cnt > 1:
                nm = "%s%d" % (base, i)
            else:
                nm = base
            mm["name"] = sanitize_name(nm)
            mm["_index"] = i
            mm["_count"] = cnt
            out.append(mm)
    names = [m["name"] for m in out]
    dups = sorted(set(n for n in names if names.count(n) > 1))
    if dups:
        die("Duplicate machine names: %s" % ", ".join(dups))
    return out


def lab_plan(api, bp):
    lab = sanitize_name(bp.get("lab") or bp.get("name") or die("Blueprint needs a 'lab' name"), 24)
    pool = "lab-" + lab
    st = load_state(lab) or {}
    defaults = bp.get("defaults") or {}
    dvm = {"template": "ubuntu-24.04", "cores": 2, "memory": 2048, "disk": "20G"}
    dvm.update(defaults.get("vm") or {})
    dct = {"template": "debian-12", "cores": 1, "memory": 512, "swap": 512, "disk": "8G",
           "nesting": True, "unprivileged": True}
    dct.update(defaults.get("ct") or {})
    machines = expand_machines(bp)
    if not machines:
        die("Blueprint has no machines")

    nets = dict(bp.get("networks") or {})
    if bp.get("network"):
        nets.setdefault("default", bp["network"])
    if not nets:
        nets["default"] = {"bridge": "auto", "dhcp": True}

    existing = {}
    pinfo = pool_get(api, pool)
    for mem in (pinfo or {}).get("members", []):
        if mem.get("type") in ("qemu", "lxc"):
            existing[(mem.get("name") or "").lower()] = mem

    # lab node: explicit > previous > node holding the first VM template > most free RAM
    lab_node = bp.get("node") or "auto"
    if lab_node == "auto" and st.get("node"):
        lab_node = st["node"]
    if lab_node == "auto":
        for m in machines:
            if str(m.get("type", "vm")).lower() in ("vm", "qemu") and not m.get("iso"):
                t = find_vm_template(api, m.get("template", dvm["template"]))
                if t:
                    lab_node = t["node"]
                    break
    lab_node = pick_node(api, lab_node)

    netplan = {}
    taken = []
    for nname, spec in nets.items():
        spec = dict(spec or {})
        prev = (st.get("networks") or {}).get(nname, {})
        if spec.get("isolated"):
            cidr = spec.get("cidr")
            if not cidr or cidr == "auto":
                cidr = prev.get("cidr") or auto_cidr(api, taken)
            net = ipaddress.ip_network(cidr, strict=False)
            taken.append(str(net))
            gw = spec.get("gateway") or str(net.network_address + 1)
            netplan[nname] = {"kind": "isolated", "cidr": str(net), "gateway": gw, "nat": spec.get("nat", True),
                              "vnet": prev.get("vnet"), "bridge": prev.get("vnet"), "zone": spec.get("zone", LAB_ZONE),
                              "alloc": IPAlloc(str(net), gw, start=spec.get("start", 10))}
        else:
            e = {"kind": "bridge", "bridge": resolve_bridge(api, lab_node, spec.get("bridge")), "vlan": spec.get("vlan")}
            if spec.get("cidr") and not spec.get("dhcp"):
                e.update(cidr=spec["cidr"], gateway=spec.get("gateway"),
                         alloc=IPAlloc(spec["cidr"], spec.get("gateway"), start=spec.get("start", 10),
                                       rng=spec.get("range")))
            else:
                e["dhcp"] = True
            netplan[nname] = e
    first_net = next(iter(netplan))
    for ms in (st.get("machines") or {}).values():
        for nic in ms.get("nics") or []:
            np_ = netplan.get(nic.get("net"))
            if np_ and np_.get("alloc") and nic.get("ip") not in (None, "dhcp"):
                np_["alloc"].reserve(nic["ip"])

    vmids = VMIDs(api, start=bp.get("vmid_start"))
    plan = []
    for m in machines:
        kind = "ct" if str(m.get("type", "vm")).lower() in ("ct", "lxc", "container") else "vm"
        d = dvm if kind == "vm" else dct
        prev = (st.get("machines") or {}).get(m["name"], {})
        e = {"name": m["name"], "type": kind, "node": pick_node(api, m["node"]) if m.get("node") else lab_node,
             "cores": int(m.get("cores", d["cores"])), "memory": mem_mb(m.get("memory", d["memory"])),
             "disk": disk_gb(m.get("disk", d["disk"])), "tags": m.get("tags"),
             "start": m.get("start", defaults.get("start", True)), "onboot": m.get("onboot", defaults.get("onboot"))}
        if kind == "vm":
            if m.get("iso"):
                e.update(iso=m["iso"], ostype=m.get("ostype", "l26"), efi=m.get("efi", False),
                         tpm=m.get("tpm", False), virtio_iso=m.get("virtio_iso"))
            else:
                e["template"] = str(m.get("template", d["template"]))
        else:
            e.update(template=str(m.get("template", d["template"])), swap=mem_mb(m.get("swap", d["swap"])),
                     nesting=m.get("nesting", d["nesting"]), unprivileged=m.get("unprivileged", d["unprivileged"]))
        refs = m.get("nets") or [first_net]
        nics, gw_set = [], False
        for idx, ref in enumerate(refs):
            r = {"net": ref} if isinstance(ref, str) else dict(ref)
            nname = r.get("net") or first_net
            if nname not in netplan:
                die("Machine %s uses unknown network '%s' (have: %s)" % (m["name"], nname, ", ".join(netplan)))
            np_ = netplan[nname]
            if np_["kind"] == "isolated" and e["node"] != lab_node:
                die("Machine %s is on node %s but isolated lab networks are node-local (lab node %s)"
                    % (m["name"], e["node"], lab_node))
            prev_nics = prev.get("nics") or []
            prev_nic = prev_nics[idx] if idx < len(prev_nics) else None
            ipspec = r.get("ip", m.get("ip") if idx == 0 else None)
            if ipspec in (None, "auto"):
                if prev_nic and prev_nic.get("ip") and prev_nic.get("net") == nname:
                    ip = prev_nic["ip"]
                elif np_.get("alloc"):
                    ip = np_["alloc"].take()
                else:
                    ip = "dhcp"
            elif ipspec == "dhcp":
                ip = "dhcp"
            else:
                ip = ip_add(ipspec, m["_index"] - 1) if m["_count"] > 1 else ipspec
                if np_.get("alloc"):
                    np_["alloc"].reserve(ip)
            if ip != "dhcp":
                ip = ip_with_prefix(ip, np_["alloc"].prefixlen if np_.get("alloc") else 24)
            gw = None
            if ip != "dhcp" and not gw_set:
                gw = r.get("gw") or (np_.get("gateway") if (np_["kind"] == "bridge" or np_.get("nat")) else None)
                gw_set = bool(gw)
            if ip == "dhcp":
                gw_set = True  # DHCP supplies the default route
            nics.append({"net": nname, "ip": ip, "gw": gw, "vlan": r.get("vlan", np_.get("vlan")),
                         "firewall": bool(r.get("firewall", False)), "model": r.get("model")})
        e["nics"] = nics
        ex = existing.get(m["name"])
        if ex:
            e.update(action="exists", vmid=ex.get("vmid"), node=ex.get("node"),
                     step=prev.get("step", "done") if prev else "done")
        else:
            want = m.get("vmid")
            if not want and prev.get("vmid") and int(prev["vmid"]) not in vmids.used:
                want = prev["vmid"]
            e.update(action="create", vmid=vmids.take(want))
        plan.append(e)

    # resolve sources
    for e in plan:
        if e["action"] != "create":
            e["source"] = "-"
            continue
        if e["type"] == "vm" and e.get("iso"):
            e["source"] = "iso " + e["iso"].split("/")[-1]
        elif e["type"] == "vm":
            t = find_vm_template(api, e["template"], e["node"])
            buildable = distro_key(e["template"]) in CLOUD_IMAGES
            if t and (t["node"] == e["node"] or not buildable or template_storage_shared(api, t)):
                e["tpl"] = t
                e["source"] = "clone %s(%s)" % (t.get("name"), t["vmid"])
                if t["node"] != e["node"]:
                    e["source"] += " +migrate"
            elif buildable:
                e["build"] = distro_key(e["template"])
                e["source"] = "build tpl-%s, clone" % e["build"]
            else:
                die("No VM template '%s' and it is not a buildable cloud image (%s)"
                    % (e["template"], ", ".join(sorted(CLOUD_IMAGES))))
        else:
            how, val = find_ct_template(api, e["node"], e["template"])
            e["ostemplate"] = val if how == "have" else None
            e["source"] = ("ct " if how == "have" else "download ct ") + val.split("/")[-1]

    creds_prev = st.get("credentials") or {}
    user = defaults.get("user", "lab")
    password = defaults.get("password", "auto")
    if password == "auto":
        password = creds_prev.get("password") or gen_password()
    sshv = defaults.get("ssh_key", "auto")
    key_file = None
    if sshv == "auto":
        pub, key_file = lab_ssh_key()
    else:
        pub = read_ssh_key(sshv) if sshv else None
    return {"lab": lab, "pool": pool, "node": lab_node, "networks": netplan, "machines": plan, "state": st,
            "defaults": defaults, "creds": {"user": user, "password": password, "ssh_pub": pub,
                                            "ssh_key_file": key_file}}


def print_plan(p):
    say("Lab '%s'   pool=%s   node=%s" % (p["lab"], p["pool"], p["node"]))
    say("Networks:")
    for n, e in p["networks"].items():
        if e["kind"] == "isolated":
            say("  %-10s isolated SDN vnet %s  %s  gw %s  %s" % (
                n, e.get("vnet") or "(new)", e["cidr"], e["gateway"], "NAT->internet" if e.get("nat") else "no internet"))
        else:
            say("  %-10s bridge %s%s  %s" % (n, e["bridge"], (" vlan %s" % e["vlan"]) if e.get("vlan") else "",
                                              "DHCP" if e.get("dhcp") else "static %s" % e.get("cidr")))
    say("Machines:")
    table(p["machines"], [(lambda e: e["action"] if e.get("step", "done") == "done" else "resume", "ACTION"), ("name", "NAME"), ("type", "TYPE"), ("vmid", "VMID"), ("node", "NODE"),
                          ("cores", "CPU"), (lambda e: "%sM" % e["memory"], "MEM"), (lambda e: "%sG" % e["disk"], "DISK"),
                          (lambda e: ", ".join("%s:%s" % (n["net"], n["ip"]) for n in e["nics"]), "NICS"),
                          ("source", "SOURCE")])
    c = p["creds"]
    say("Login: VMs user '%s', CTs user 'root'; password %s; ssh key %s" % (
        c["user"], "(generated)" if not p["defaults"].get("password") else "(from blueprint)",
        relpath(c.get("ssh_key_file")) or ("(from blueprint)" if c.get("ssh_pub") else "none")))


def relpath(p):
    if not p:
        return p
    try:
        return os.path.relpath(p, DATA_DIR).replace("\\", "/")
    except ValueError:
        return p


def lab_apply(api, p, bp_path):
    lab, pool = p["lab"], p["pool"]
    st = p["state"] or {}
    st.update(lab=lab, pool=pool, node=p["node"], blueprint=bp_path, updated=now_iso())
    st.setdefault("created", now_iso())
    st.setdefault("machines", {})
    st.setdefault("networks", {})
    c = p["creds"]
    st["credentials"] = {"vm_user": c["user"], "ct_user": "root", "password": c["password"],
                         "ssh_private_key": relpath(c.get("ssh_key_file"))}
    save_state(st)

    if not pool_get(api, pool):
        api.post("/pools", {"poolid": pool, "comment": "pve.py lab %s" % lab})
        say("  created pool %s" % pool)

    sdn_changed, vnets = False, []
    for nname, n in p["networks"].items():
        if n["kind"] == "isolated":
            vnet, ch = ensure_isolated_net(api, lab, nname, n, vnets)
            n["vnet"] = n["bridge"] = vnet
            vnets.append(vnet)
            sdn_changed |= ch
        st["networks"][nname] = {k: v for k, v in n.items() if k != "alloc"}
    save_state(st)
    if sdn_changed or (vnets and sdn_pending(api)):
        sdn_apply(api)
    pool_set_comment(api, pool, "pve.py lab %s; vnets=%s" % (lab, ",".join(vnets)))

    built = {}
    tags = "lab-%s" % lab
    failures = []
    for e in p["machines"]:
        ms = st["machines"].get(e["name"])
        if e["action"] == "exists" and (not ms or ms.get("step", "done") == "done"):
            say("- %s exists (VMID %s), skipping" % (e["name"], e["vmid"]))
            continue
        check_budget(margin=20, what="before building %s" % e["name"])
        if e["action"] == "create" or not ms:
            ms = {"vmid": e["vmid"], "type": e["type"], "node": e["node"], "nics": e["nics"], "step": "creating"}
            st["machines"][e["name"]] = ms
            save_state(st)
        kind = "qemu" if e["type"] == "vm" else "lxc"
        g = {"vmid": ms["vmid"], "node": ms["node"], "name": e["name"], "type": kind}
        say("- %s (%s %s)%s" % (e["name"], e["type"], ms["vmid"], "" if ms["step"] == "creating" else " resuming at " + ms["step"]))
        nets = [{"bridge": p["networks"][n["net"]]["bridge"], "vlan": n.get("vlan"), "ip": n["ip"], "gw": n.get("gw"),
                 "firewall": n.get("firewall"), "model": n.get("model")} for n in e["nics"]]
        mtags = ";".join([tags] + ([e["tags"]] if isinstance(e.get("tags"), str) else (e.get("tags") or [])))
        try:
            if ms["step"] == "creating":
                if any(int(x["vmid"]) == int(ms["vmid"]) for x in api.guests()):
                    wait_active(api, ms["node"], ms["vmid"], label="create %s" % e["name"])
                elif e["type"] == "vm" and e.get("iso"):
                    create_vm_installer(api, e["node"], ms["vmid"], e["name"], iso=e["iso"], cores=e["cores"],
                                        memory=e["memory"], disk=e["disk"], nets=nets, ostype=e["ostype"],
                                        efi=e["efi"], tpm=e["tpm"], pool=pool, tags=mtags,
                                        virtio_iso=e.get("virtio_iso"), onboot=e.get("onboot"))
                elif e["type"] == "vm":
                    tpl = e.get("tpl") or find_vm_template(api, e.get("template") or "", e["node"])
                    if not tpl:
                        key = e.get("build") or distro_key(e.get("template") or "")
                        if key not in built:
                            built[key] = build_vm_template(api, key, node=e["node"])
                        tpl = built[key]
                    ms["node"] = g["node"] = clone_vm(api, tpl, e["name"], ms["vmid"], node=e["node"], pool=pool)
                else:
                    ost = e.get("ostemplate") or ensure_ct_template(api, e["node"], e["template"])
                    create_ct(api, e["node"], ms["vmid"], e["name"], ost, cores=e["cores"], memory=e["memory"],
                              swap=e["swap"], disk=e["disk"], nets=nets, password=c["password"],
                              sshkeys=c.get("ssh_pub"), unprivileged=e["unprivileged"], nesting=e["nesting"],
                              pool=pool, tags=mtags, nameserver=p["defaults"].get("nameserver"),
                              onboot=e.get("onboot"))
                ms["step"] = "created"
                save_state(st)
            if ms["step"] == "created":
                if e["type"] == "vm" and not e.get("iso"):
                    configure_vm(api, ms["node"], ms["vmid"], cores=e["cores"], memory=e["memory"], nets=nets,
                                 ci={"user": c["user"], "password": c["password"], "sshkeys": c.get("ssh_pub"),
                                     "nameserver": p["defaults"].get("nameserver")},
                                 tags=mtags, description="Lab %s - built by pve.py" % lab, onboot=e.get("onboot"))
                    grow_disk(api, ms["node"], "qemu", ms["vmid"], e["disk"])
                ms["step"] = "configured"
                save_state(st)
            if ms["step"] == "configured":
                if e.get("start", True) and guest_status(api, g) != "running":
                    power(api, g, "start")
                    say("    started")
                ms["step"] = "done"
                save_state(st)
        except (PVEError, SystemExit) as ex:
            msg = str(ex) if isinstance(ex, PVEError) else "aborted (see error above)"
            say("    FAILED: %s" % msg)
            failures.append((e["name"], msg))
    st["updated"] = now_iso()
    save_state(st)
    return failures


def lab_report(api, lab):
    st = load_state(lab) or {}
    names = {int(m["vmid"]): n for n, m in (st.get("machines") or {}).items() if m.get("vmid")}
    pinfo = pool_get(api, "lab-" + lab) or {}
    gidx = {int(g["vmid"]): g for g in api.guests()}
    rows = []
    for mem in pinfo.get("members", []):
        if mem.get("type") not in ("qemu", "lxc"):
            continue
        g = dict(gidx.get(int(mem["vmid"]), mem))
        cur = {}
        try:
            cur = api.get(gpath(g) + "/status/current") or {}
        except PVEError:
            pass
        g["status"] = cur.get("status") or g.get("status")
        g["name"] = names.get(int(g["vmid"])) or mem.get("name") or g.get("name") or cur.get("name")
        rows.append({"name": g.get("name"), "vmid": g["vmid"], "type": kind_label(g["type"]), "node": g["node"],
                     "status": g.get("status"), "ips": ", ".join(guest_ips(api, g))})
    rows.sort(key=lambda r: int(r["vmid"]))
    return {"lab": lab, "pool": "lab-" + lab, "machines": rows, "networks": st.get("networks"),
            "credentials": st.get("credentials"), "state_file": state_path(lab) if st else None}


def print_report(r):
    say("Lab '%s' (pool %s)" % (r["lab"], r["pool"]))
    for n, e in (r.get("networks") or {}).items():
        if e.get("kind") == "isolated":
            say("  net %-10s vnet %s %s gw %s %s" % (n, e.get("vnet"), e.get("cidr"), e.get("gateway"),
                                                     "NAT" if e.get("nat") else ""))
        else:
            say("  net %-10s bridge %s %s" % (n, e.get("bridge"), "DHCP" if e.get("dhcp") else e.get("cidr")))
    table(r["machines"], [("vmid", "VMID"), ("name", "NAME"), ("type", "TYPE"), ("node", "NODE"),
                          ("status", "STATUS"), ("ips", "IPS")])
    c = r.get("credentials") or {}
    if c:
        say("Login: VMs %s / %s   CTs root / %s   ssh key: %s" % (
            c.get("vm_user"), c.get("password"), c.get("password"), c.get("ssh_private_key") or "-"))
    say("(* = configured IP, not yet confirmed by the guest; 'dhcp?' = unknown - try `ip NAME --scan`)")


def cmd_lab_plan(api, args):
    bp, path = load_blueprint(args.file)
    p = lab_plan(api, bp)
    if JSON_OUT:
        out = {k: v for k, v in p.items() if k not in ("networks", "state")}
        out["networks"] = {k: {kk: vv for kk, vv in v.items() if kk != "alloc"} for k, v in p["networks"].items()}
        emit(out)
    else:
        print_plan(p)


def cmd_lab_apply(api, args):
    bp, path = load_blueprint(args.file)
    p = lab_plan(api, bp)
    print_plan(p)
    todo = [e for e in p["machines"] if e["action"] == "create" or e.get("step", "done") != "done"]
    lab = p["lab"]
    if not todo and all(n.get("vnet") or n["kind"] != "isolated" for n in p["networks"].values()):
        say("\nNothing to do - lab is already built.")
        print_report(lab_report(api, lab))
        return
    say("")
    need_yes(args, "build %d machine(s) for lab '%s'" % (len(todo), p["lab"]))
    t0 = time.time()
    failures = lab_apply(api, p, path)
    say("")
    print_report(lab_report(api, p["lab"]))
    if failures:
        say("\n%d machine(s) FAILED: %s" % (len(failures), "; ".join("%s: %s" % f for f in failures)))
        say("Fix the cause and re-run the same `lab apply` - finished machines are skipped.")
        say("=== lab apply finished: FAILED (%ds) ===" % (time.time() - t0))
        sys.exit(1)
    say("=== lab apply finished: OK (%ds) ===" % (time.time() - t0))


def cmd_lab_destroy(api, args):
    lab = sanitize_name(args.lab, 24)
    pool = "lab-" + lab
    pinfo = pool_get(api, pool)
    st = load_state(lab) or {}
    if not pinfo and not st:
        die("No lab '%s' (no pool %s, no state file)" % (lab, pool))
    gidx = {int(g["vmid"]): g for g in api.guests()}
    members = [gidx.get(int(m["vmid"]), m) for m in (pinfo or {}).get("members", []) if m.get("type") in ("qemu", "lxc")]
    seen = {int(m["vmid"]) for m in members}
    for mname, ms in (st.get("machines") or {}).items():  # guests that never made it into the pool
        g = gidx.get(int(ms.get("vmid", -1)))
        if g and int(g["vmid"]) not in seen and (g.get("name") or "").lower() == mname:
            members.append(g)
    vnets = set(pool_meta((pinfo or {}).get("comment")).get("vnets", []))
    vnets |= {n.get("vnet") for n in (st.get("networks") or {}).values() if n.get("kind") == "isolated" and n.get("vnet")}
    desc = "destroy lab '%s': %d guest(s) [%s], SDN vnets [%s], pool %s" % (
        lab, len(members), ", ".join("%s(%s)" % (m.get("name"), m["vmid"]) for m in members),
        ", ".join(sorted(vnets)), pool)
    need_yes(args, desc)
    for g in members:
        try:
            destroy_guest(api, g)
        except PVEError as e:
            say("  FAILED %s: %s" % (g["vmid"], e))
    if vnets:
        existing = {v["vnet"] for v in api.get("/cluster/sdn/vnets") or []}
        for v in sorted(vnets):
            if v in existing:
                try:
                    _delete_vnet(api, v)
                    say("  removed vnet %s" % v)
                except PVEError as e:
                    say("  could not remove vnet %s: %s" % (v, e))
        sdn_apply(api)
    if pinfo:
        try:
            pool_delete(api, pool)
            say("  removed pool %s" % pool)
        except PVEError as e:
            say("  could not remove pool: %s" % e)
    sp = state_path(lab)
    if os.path.isfile(sp):
        os.replace(sp, sp.replace(".state.json", ".destroyed-%s.json" % time.strftime("%Y%m%d-%H%M%S")))
    say("OK: lab '%s' destroyed" % lab)


def cmd_lab_list(api, args):
    pools = [p for p in (api.get("/pools") or []) if p["poolid"].startswith("lab-")]
    gs = api.guests()
    rows = []
    for p in pools:
        mem = [g for g in gs if g.get("pool") == p["poolid"]]
        rows.append({"lab": p["poolid"][4:], "pool": p["poolid"], "guests": len(mem),
                     "running": sum(1 for g in mem if g.get("status") == "running"),
                     "state_file": "yes" if load_state(p["poolid"][4:]) else "no"})
    emit(rows, lambda d: table(d, [("lab", "LAB"), ("guests", "GUESTS"), ("running", "RUNNING"),
                                   ("state_file", "STATE")]))


def cmd_lab_status(api, args):
    r = lab_report(api, sanitize_name(args.lab, 24))
    emit(r, print_report)


def cmd_lab_power(api, args):
    lab = sanitize_name(args.lab, 24)
    pinfo = pool_get(api, "lab-" + lab) or die("No lab '%s'" % lab)
    gidx = {int(g["vmid"]): g for g in api.guests()}
    for m in pinfo.get("members", []):
        if m.get("type") not in ("qemu", "lxc"):
            continue
        g = gidx.get(int(m["vmid"]), m)
        want = args.action
        if (want == "start" and g.get("status") == "running") or (want in ("stop", "shutdown") and g.get("status") == "stopped"):
            continue
        say("%s %s (%s) ..." % (want, g.get("name"), g["vmid"]))
        try:
            power(api, g, want, timeout=args.timeout, force=True)
        except PVEError as e:
            say("  FAILED: %s" % e)
    say("OK")


EXAMPLE_BLUEPRINT = {
    "lab": "demo",
    "node": "auto",
    "defaults": {
        "user": "lab", "password": "auto", "ssh_key": "auto", "nameserver": "1.1.1.1",
        "vm": {"template": "ubuntu-24.04", "cores": 2, "memory": "2G", "disk": "20G"},
        "ct": {"template": "debian-12", "cores": 1, "memory": "512M", "disk": "8G"}
    },
    "networks": {
        "lan": {"isolated": True, "cidr": "auto", "nat": True},
        "home": {"bridge": "auto", "dhcp": True}
    },
    "machines": [
        {"name": "gw", "type": "ct", "template": "alpine", "nets": [{"net": "home", "ip": "dhcp"}, {"net": "lan", "ip": "auto"}]},
        {"name": "web", "type": "ct", "count": 2, "nets": ["lan"]},
        {"name": "db", "type": "vm", "memory": "4G", "disk": "40G", "nets": ["lan"]}
    ]
}


def cmd_lab_example(api, args):
    print(json.dumps(EXAMPLE_BLUEPRINT, indent=2))


# --------------------------------------------------------------------------- bootstrap
def cmd_bootstrap_user(api, args):
    userid = args.user if "@" in args.user else "%s@%s" % (args.user, args.realm)
    q = urllib.parse.quote(userid, safe="")
    users = [u["userid"] for u in api.get("/access/users") or []]
    if userid not in users:
        api.post("/access/users", {"userid": userid, "comment": args.comment, "enable": 1})
        say("created user %s" % userid)
    else:
        say("user %s exists" % userid)
    api.put("/access/acl", {"path": "/", "users": userid, "roles": args.role, "propagate": 1})
    say("granted %s on / to %s" % (args.role, userid))
    tokens = [t["tokenid"] for t in api.get("/access/users/%s/token" % q) or []]
    if args.token in tokens:
        if not args.rotate:
            die("token %s!%s exists and its secret cannot be read back - use --rotate to replace it" % (userid, args.token))
        api.delete("/access/users/%s/token/%s" % (q, args.token))
    r = api.post("/access/users/%s/token/%s" % (q, args.token), {"privsep": 0, "comment": args.comment})
    out = args.out or os.path.join(DATA_DIR, "pve.env")
    if os.path.abspath(out) == os.path.abspath(api.cfg.get("env_path") or "") and not args.rotate_env:
        die("%s is the env file currently in use - pass --out NEWFILE (or --rotate-env to overwrite it)" % out)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Proxmox API token for %s. Created %s\n" % (userid, now_iso()))
        f.write('export PVE_HOST="%s"\nexport PVE_USER="%s"\nexport PVE_TOKEN_ID="%s"\n'
                'export PVE_TOKEN_SECRET="%s"\nexport PVE_VERIFY_SSL="0"\n' % (api.cfg["host"], userid, args.token, r["value"]))
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    say("OK: token %s written to %s" % (r.get("full-tokenid"), out))


TOKEN_HELP = """Create the API token once (pick ONE way):

  Web UI: Datacenter > Permissions > Users > Add  (e.g. claude, realm pve)
          Datacenter > Permissions > Add > User Permission: path /, user claude@pve, role Administrator
          Datacenter > Permissions > API Tokens > Add: user claude@pve, token id agent,
          UNTICK "Privilege Separation", copy the secret (shown once)

  Shell on any node (as root):
          pveum user add claude@pve --comment "AI agent"
          pveum acl modify / --users claude@pve --roles Administrator
          pveum user token add claude@pve agent --privsep 0

  Administrator is needed for host network changes; PVEAdmin is enough for VMs, CTs, pools
  and SDN-less labs. Then put the secret into PVE_TOKEN_SECRET and run:  pve.py doctor"""


def cmd_init(api, args):
    out = args.out or os.path.join(DATA_DIR, "pve.env")
    if os.path.exists(out) and not args.force:
        die("%s already exists (use --force to overwrite)" % out)
    if "!" not in args.token_id or "@" not in args.token_id:
        die("--token-id must look like user@realm!tokenname, e.g. claude@pve!agent")
    secret = sys.stdin.readline().strip() if args.secret_stdin else ""
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Proxmox connection for pve.py (keep this file private). Created %s\n" % now_iso())
        f.write('PVE_HOST="%s"\nPVE_TOKEN_ID="%s"\nPVE_TOKEN_SECRET="%s"\nPVE_VERIFY_SSL="%d"\n'
                % (args.host, args.token_id, secret or "PASTE-TOKEN-SECRET-HERE", 1 if args.verify_ssl else 0))
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    say("Wrote %s" % out)
    if not secret:
        say("Next: paste the token secret into PVE_TOKEN_SECRET in that file.\n")
        say(TOKEN_HELP.replace("claude@pve", args.token_id.split("!")[0]).replace(" agent", " " + args.token_id.split("!")[1]))
    else:
        say("Next: pve.py doctor")


def cmd_doctor(api, args):
    results = []

    def rep_(level, what, detail=""):
        results.append({"level": level, "check": what, "detail": detail})
        say("%-4s %-28s %s" % (level, what, detail))

    say("pve.py %s  python %s  data dir %s" % (VERSION, sys.version.split()[0], DATA_DIR))
    rep_("INFO", "env file", api.cfg.get("env_path") or "(environment variables)")
    try:
        v = api.version()
    except PVEError as e:
        rep_("FAIL", "API reachable + token", str(e))
        say("\nFix: check PVE_HOST, that this machine can reach https://%s:8006, and the token.\n" % api.cfg["host"])
        say(TOKEN_HELP)
        emit(results) if JSON_OUT else None
        sys.exit(1)
    ver = api.ver()
    rep_("OK" if ver >= (8, 0) else "WARN", "Proxmox VE version", "%s%s" % (
        v.get("version"), "" if ver >= (8, 0) else " (8.x+ recommended; SDN labs need 8.1+)"))
    rep_("OK", "API + token", "%s as %s" % (api.root, api.cfg["tokenid"]))
    try:
        privs = set((api.get("/access/permissions", {"path": "/"}) or {}).get("/", {}))
    except PVEError:
        privs = set()
    need = {"VM.Allocate": "create guests", "VM.Clone": "clone templates", "Datastore.AllocateSpace": "disks",
            "Datastore.AllocateTemplate": "download templates/ISOs", "Pool.Allocate": "lab pools",
            "SDN.Allocate": "isolated lab networks", "Sys.Modify": "host networking + downloads",
            "Sys.Audit": "read node/network info", "VM.PowerMgmt": "start/stop"}
    missing = [k for k in need if k not in privs]
    rep_("OK" if not missing else "WARN", "token privileges on /",
         "%d privileges" % len(privs) if not missing else "missing: " + ", ".join("%s (%s)" % (k, need[k]) for k in missing))
    nodes = api.nodes()
    online = [n["node"] for n in nodes if n.get("status") == "online"]
    rep_("OK" if online else "FAIL", "nodes", "%d online: %s%s" % (len(online), ", ".join(online), (
        "; offline: " + ", ".join(n["node"] for n in nodes if n["node"] not in online)) if len(online) < len(nodes) else ""))
    for n in online:
        cont = set()
        for st in api.storages(n):
            if st.get("active", 1):
                cont |= set((st.get("content") or "").split(","))
        miss = [c for c in ("images", "rootdir", "vztmpl", "iso") if c not in cont]
        rep_("OK" if not miss else "WARN", "storage on " + n, ("disks+CTs+templates+ISOs available" if not miss else
                                                             "no storage for: " + ", ".join(miss)) +
             ("" if "import" in cont else "; no 'import' content (enabled automatically on 'local' when needed)"))
        br = default_bridge(api, n)
        sub = bridge_subnet(api, n, br)
        rep_("OK", "default bridge on " + n, "%s %s" % (br, sub or "(no IP on host - DHCP/IP discovery limited)"))
    try:
        zones = api.get("/cluster/sdn/zones") or []
        rep_("OK", "SDN (isolated lab networks)", "available; zones: %s" % (", ".join(z["zone"] for z in zones) or "none yet"))
    except PVEError as e:
        rep_("WARN", "SDN (isolated lab networks)", "not available (%s) - labs can still use bridges" % str(e)[:80])
    tpls = [g for g in api.guests() if g.get("template")]
    rep_("INFO", "VM templates", ", ".join("%s(%s@%s)" % (g.get("name"), g["vmid"], g["node"]) for g in tpls)
         or "none - built automatically on first use")
    for tool, why in (("ssh", "log into guests, bake guest agent"), ("ssh-keygen", "lab SSH key"),
                      ("ping", "ip --scan / agent bake discovery")):
        rep_("OK" if shutil.which(tool) else "WARN", "local tool: " + tool, why if shutil.which(tool) else "missing - " + why)
    try:
        os.makedirs(LABS_DIR, exist_ok=True)
        rep_("OK", "data dir writable", DATA_DIR)
    except OSError as e:
        rep_("FAIL", "data dir writable", "%s (%s) - set PVE_HOME" % (DATA_DIR, e))
    if JSON_OUT:
        emit(results)
    bad = [r for r in results if r["level"] == "FAIL"]
    say("\n%s" % ("All good." if not bad and not [r for r in results if r["level"] == "WARN"]
                  else "%d problem(s), %d warning(s)." % (len(bad), len([r for r in results if r["level"] == "WARN"]))))
    if bad:
        sys.exit(1)


# --------------------------------------------------------------------------- CLI
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    common.add_argument("--env", default=argparse.SUPPRESS, help="env file with PVE_* settings")
    common.add_argument("--budget", type=int, default=argparse.SUPPRESS,
                        help="stop cleanly after N seconds (exit 3, re-run to resume); default $PVE_BUDGET")

    p = argparse.ArgumentParser(prog="pve.py", parents=[common],
                                description="Proxmox VE control toolkit v%s" % VERSION,
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    def sp(parent, name, fn, help_):
        x = parent.add_parser(name, parents=[common], help=help_, description=help_)
        x.set_defaults(fn=fn)
        return x

    sp(sub, "status", cmd_status, "cluster overview: nodes, guests, storage")
    x = sp(sub, "ls", cmd_ls, "list VMs and containers")
    x.add_argument("--node")
    x.add_argument("--type", choices=["vm", "ct"])
    x.add_argument("--lab")
    x.add_argument("--running", action="store_true")
    x.add_argument("--templates", action="store_true")
    sp(sub, "nodes", cmd_nodes, "list nodes")
    x = sp(sub, "storage", cmd_storage, "list storage (optionally its content)")
    x.add_argument("--node")
    x.add_argument("--content", help="also list items: iso|vztmpl|images|import|backup|rootdir")
    x = sp(sub, "info", cmd_info, "show a guest's status + full config")
    x.add_argument("id")
    x = sp(sub, "tasks", cmd_tasks, "recent cluster tasks")
    x.add_argument("--limit", type=int, default=25)
    x.add_argument("--errors", action="store_true")
    sp(sub, "whoami", cmd_whoami, "show the token in use and its privileges")
    x = sp(sub, "api", cmd_api, "raw API call, e.g. api GET /nodes/pve01/status")
    x.add_argument("method")
    x.add_argument("path")
    x.add_argument("params", nargs="*", help="key=value")
    x.add_argument("--wait", action="store_true", help="wait for a returned task")

    # power & generic guest ops
    for act in ("start", "stop", "shutdown", "reboot", "suspend", "resume"):
        x = sp(sub, act, cmd_power, "%s one or more guests (VMID or name)" % act)
        x.add_argument("ids", nargs="+")
        x.add_argument("--timeout", type=int, default=120)
        x.add_argument("--force", action="store_true", help="shutdown: hard stop after timeout")
        x.set_defaults(action=act)
    x = sp(sub, "set", cmd_set, "change guest config: set ID cores=4 memory=8G net1=virtio,bridge=vmbr1 onboot=1")
    x.add_argument("id")
    x.add_argument("settings", nargs="*")
    x.add_argument("--delete", help="comma-separated config keys to remove, e.g. net1,ide2")
    x = sp(sub, "resize", cmd_resize, "grow a disk: resize ID 64G  |  resize ID +10G")
    x.add_argument("id")
    x.add_argument("size")
    x.add_argument("--disk")
    x = sp(sub, "delete", cmd_delete, "destroy guests (needs --yes)")
    x.add_argument("ids", nargs="+")
    x.add_argument("--yes", action="store_true")
    x = sp(sub, "ip", cmd_ip, "show guest IP addresses")
    x.add_argument("ids", nargs="+")
    x.add_argument("--scan", action="store_true",
                   help="for DHCP guests without guest agent: find them by SSH-ing (lab key) into hosts on the bridge's subnet")
    x = sp(sub, "migrate", cmd_migrate, "move a guest to another node")
    x.add_argument("id")
    x.add_argument("target")
    x = sp(sub, "snap", cmd_snap, "snapshots: snap list|create|rollback|delete ID [NAME]")
    x.add_argument("action", choices=["list", "create", "rollback", "delete"])
    x.add_argument("id")
    x.add_argument("name", nargs="?")
    x.add_argument("--description")
    x.add_argument("--vmstate", action="store_true", help="include RAM (VMs)")
    x.add_argument("--yes", action="store_true")

    # vm
    vm = sub.add_parser("vm", help="create/clone VMs").add_subparsers(dest="vmcmd", metavar="ACTION")
    x = sp(vm, "create", cmd_vm_create, "create a VM from a template (cloud-init) or an ISO")
    x.add_argument("name")
    src = x.add_mutually_exclusive_group()
    src.add_argument("--template", help="template name/VMID or distro (ubuntu-24.04, debian-12, ...) - auto-built if missing")
    src.add_argument("--iso", help="installer ISO (volid or part of filename)")
    x.add_argument("--node", default="auto")
    x.add_argument("--vmid", type=int)
    x.add_argument("--cores", type=int)
    x.add_argument("--memory", help="e.g. 4G or 4096")
    x.add_argument("--disk", help="e.g. 32G (template disks are grown to this)")
    x.add_argument("--storage")
    x.add_argument("--net", action="append", help="repeatable: auto | vmbr0 | bridge=vmbr1,ip=10.0.0.5/24,gw=10.0.0.1,vlan=20 (default: management bridge, DHCP)")
    x.add_argument("--gw")
    x.add_argument("--user", default="lab", help="cloud-init user (template VMs)")
    x.add_argument("--password", default="auto", help="cloud-init password; 'auto' generates one")
    x.add_argument("--ssh-key", help="public key, .pub path, or 'auto' (lab key)")
    x.add_argument("--nameserver")
    x.add_argument("--searchdomain")
    x.add_argument("--cpu")
    x.add_argument("--ostype", default="l26", help="l26 (linux) | win11 (Win11/Server 2022/2025) | win10 (Win10/Server 2016/2019) | other (BSD, pfSense)")
    x.add_argument("--efi", action="store_true")
    x.add_argument("--tpm", action="store_true")
    x.add_argument("--virtio-iso", help="second CD with virtio drivers (Windows)")
    x.add_argument("--full", action="store_true", help="full clone instead of linked")
    x.add_argument("--no-build", action="store_true")
    x.add_argument("--pool")
    x.add_argument("--tags")
    x.add_argument("--description")
    x.add_argument("--onboot", type=int, choices=[0, 1])
    x.add_argument("--start", action=argparse.BooleanOptionalAction if hasattr(argparse, "BooleanOptionalAction") else "store_true", default=True)
    x = sp(vm, "clone", cmd_vm_clone, "clone a VM or template")
    x.add_argument("source")
    x.add_argument("name")
    x.add_argument("--vmid", type=int)
    x.add_argument("--node", default="auto")
    x.add_argument("--storage")
    x.add_argument("--full", action="store_true")
    x.add_argument("--pool")
    x.add_argument("--start", action="store_true")
    x = sp(vm, "template", cmd_vm_template, "convert a stopped VM into a template (needs --yes)")
    x.add_argument("id")
    x.add_argument("--yes", action="store_true")

    # ct
    ct = sub.add_parser("ct", help="create containers").add_subparsers(dest="ctcmd", metavar="ACTION")
    x = sp(ct, "create", cmd_ct_create, "create an LXC container (template auto-downloaded)")
    x.add_argument("name")
    x.add_argument("--template", default="debian-12", help="debian-12, ubuntu-24.04, alpine, rockylinux-9 ... or a volid")
    x.add_argument("--node", default="auto")
    x.add_argument("--vmid", type=int)
    x.add_argument("--cores", type=int, default=1)
    x.add_argument("--memory", default="512M")
    x.add_argument("--swap", default="512M")
    x.add_argument("--disk", default="8G")
    x.add_argument("--storage")
    x.add_argument("--net", action="append", help="repeatable: auto | vmbr0 | bridge=vmbr1,ip=10.0.0.5/24,gw=10.0.0.1,vlan=20 (default: management bridge, DHCP)")
    x.add_argument("--gw")
    x.add_argument("--password", default="auto")
    x.add_argument("--ssh-key")
    x.add_argument("--nameserver")
    x.add_argument("--searchdomain")
    x.add_argument("--privileged", action="store_true")
    x.add_argument("--no-nesting", action="store_true")
    x.add_argument("--pool")
    x.add_argument("--tags")
    x.add_argument("--description")
    x.add_argument("--onboot", type=int, choices=[0, 1])
    x.add_argument("--start", action=argparse.BooleanOptionalAction if hasattr(argparse, "BooleanOptionalAction") else "store_true", default=True)

    # templates / iso
    tp = sub.add_parser("template", help="VM/CT templates").add_subparsers(dest="tcmd", metavar="ACTION")
    x = sp(tp, "list", cmd_template_list, "list VM templates, downloaded CT templates, buildable images")
    x.add_argument("--node")
    x = sp(tp, "build", cmd_template_build, "build a cloud-init VM template from an official cloud image")
    x.add_argument("distro", help=", ".join(sorted(CLOUD_IMAGES)))
    x.add_argument("--node", default="auto")
    x.add_argument("--storage")
    x.add_argument("--vmid", type=int)
    x.add_argument("--bridge", default="auto", help="bridge with DHCP+internet for the one-time agent-install boot (auto = management bridge)")
    x.add_argument("--name")
    x.add_argument("--no-agent", action="store_true", help="skip baking in qemu-guest-agent")
    x = sp(tp, "ct-available", cmd_template_ct_available, "list downloadable container templates")
    x.add_argument("--node")
    x.add_argument("--filter")
    x = sp(tp, "ct-download", cmd_template_ct_download, "download a container template")
    x.add_argument("spec")
    x.add_argument("--node", default="auto", help="node name, auto, or all")
    x.add_argument("--storage")
    ip_ = sub.add_parser("iso", help="ISO images").add_subparsers(dest="icmd", metavar="ACTION")
    x = sp(ip_, "list", cmd_iso_list, "list ISOs")
    x.add_argument("--node")
    x = sp(ip_, "download", cmd_iso_download, "download an ISO from a URL onto a node")
    x.add_argument("url")
    x.add_argument("--node", default="auto", help="node name, auto, or all")
    x.add_argument("--storage")
    x.add_argument("--filename")
    x.add_argument("--checksum")
    x.add_argument("--algorithm", default="sha256")

    # host network
    nt = sub.add_parser("net", help="host network (bridges, VLANs)").add_subparsers(dest="ncmd", metavar="ACTION")
    x = sp(nt, "list", cmd_net_list, "list interfaces on nodes")
    x.add_argument("--node")
    x = sp(nt, "add-bridge", cmd_net_add_bridge, "add a Linux bridge (name 'auto' = next free vmbrN)")
    x.add_argument("name")
    x.add_argument("--node", help="node name or all (default all)")
    x.add_argument("--ports", help="physical port(s) to attach, e.g. nic1 (empty = internal-only bridge)")
    x.add_argument("--cidr", help="host IP on this bridge, e.g. 10.10.0.1/24")
    x.add_argument("--gateway")
    x.add_argument("--vlan-aware", action="store_true")
    x.add_argument("--mtu", type=int)
    x.add_argument("--comment")
    x.add_argument("--apply", action="store_true", help="activate immediately (ifreload)")
    x = sp(nt, "add-vlan", cmd_net_add_vlan, "add a VLAN interface, e.g. vmbr0.30")
    x.add_argument("--raw-device", required=True)
    x.add_argument("--tag", type=int, required=True)
    x.add_argument("--name")
    x.add_argument("--node")
    x.add_argument("--cidr")
    x.add_argument("--comment")
    x.add_argument("--apply", action="store_true")
    x = sp(nt, "set", cmd_net_set, "change an interface: net set vmbr1 bridge_vlan_aware=1 comments=lab")
    x.add_argument("iface")
    x.add_argument("settings", nargs="*")
    x.add_argument("--node")
    x.add_argument("--delete")
    x.add_argument("--force", action="store_true", help="allow touching the management interface")
    x.add_argument("--apply", action="store_true")
    x = sp(nt, "delete", cmd_net_delete, "remove an interface (needs --yes)")
    x.add_argument("iface")
    x.add_argument("--node")
    x.add_argument("--apply", action="store_true")
    x.add_argument("--yes", action="store_true")
    x = sp(nt, "apply", cmd_net_apply, "activate staged changes (needs --yes)")
    x.add_argument("--node")
    x.add_argument("--yes", action="store_true")
    x = sp(nt, "revert", cmd_net_revert, "discard staged changes (needs --yes)")
    x.add_argument("--node")
    x.add_argument("--yes", action="store_true")
    x = sp(nt, "pending", cmd_net_pending, "show staged (unapplied) changes as a diff")
    x.add_argument("--node")

    # sdn
    sd = sub.add_parser("sdn", help="SDN zones / vnets / subnets").add_subparsers(dest="scmd", metavar="ACTION")
    sp(sd, "list", cmd_sdn_list, "list zones and vnets")
    x = sp(sd, "zone-add", cmd_sdn_zone_add, "add a zone (id: <=8 lowercase letters/digits)")
    x.add_argument("zone")
    x.add_argument("--type", default="simple", choices=["simple", "vlan", "qinq", "vxlan", "evpn"])
    x.add_argument("--bridge")
    x.add_argument("--peers")
    x.add_argument("--mtu", type=int)
    x.add_argument("--nodes")
    x.add_argument("--dhcp", action="store_true", help="dnsmasq DHCP (package must be installed on nodes)")
    x.add_argument("--apply", action="store_true")
    x = sp(sd, "vnet-add", cmd_sdn_vnet_add, "add a vnet (+ optional subnet with gateway/NAT)")
    x.add_argument("vnet")
    x.add_argument("--zone", required=True)
    x.add_argument("--tag", type=int)
    x.add_argument("--alias")
    x.add_argument("--vlan-aware", action="store_true")
    x.add_argument("--subnet")
    x.add_argument("--gateway")
    x.add_argument("--snat", action="store_true")
    x.add_argument("--dhcp-range", help="start-end")
    x.add_argument("--apply", action="store_true")
    x = sp(sd, "vnet-del", cmd_sdn_vnet_del, "delete a vnet (needs --yes)")
    x.add_argument("vnet")
    x.add_argument("--yes", action="store_true")
    x.add_argument("--apply", action="store_true")
    x = sp(sd, "zone-del", cmd_sdn_zone_del, "delete a zone (needs --yes)")
    x.add_argument("zone")
    x.add_argument("--yes", action="store_true")
    x.add_argument("--apply", action="store_true")
    sp(sd, "apply", cmd_sdn_apply, "apply pending SDN config")

    # labs
    lb = sub.add_parser("lab", help="whole labs from a blueprint").add_subparsers(dest="lcmd", metavar="ACTION")
    x = sp(lb, "plan", cmd_lab_plan, "show what a blueprint would build")
    x.add_argument("file")
    x = sp(lb, "apply", cmd_lab_apply, "build/resume a lab (needs --yes; re-run to resume after exit code 3)")
    x.add_argument("file")
    x.add_argument("--yes", action="store_true")
    x = sp(lb, "destroy", cmd_lab_destroy, "delete every guest + network of a lab (needs --yes)")
    x.add_argument("lab")
    x.add_argument("--yes", action="store_true")
    sp(lb, "list", cmd_lab_list, "list labs")
    x = sp(lb, "status", cmd_lab_status, "machines, IPs and logins of a lab")
    x.add_argument("lab")
    for act in ("start", "stop", "shutdown"):
        x = sp(lb, act, cmd_lab_power, "%s every guest in a lab" % act)
        x.add_argument("lab")
        x.add_argument("--timeout", type=int, default=120)
        x.set_defaults(action=act)
    sp(lb, "example", cmd_lab_example, "print an example blueprint")

    x = sp(sub, "bootstrap-user", cmd_bootstrap_user, "create a PVE user + API token and write an env file")
    x.add_argument("user", help="e.g. claude (realm pve is added)")
    x.add_argument("--realm", default="pve")
    x.add_argument("--token", default="agent")
    x.add_argument("--role", default="Administrator", help="Administrator (needed for host networking) or PVEAdmin")
    x.add_argument("--comment", default="AI agent (pve.py)")
    x.add_argument("--out", help="env file to write (default: $PVE_HOME/pve.env)")
    x.add_argument("--rotate", action="store_true", help="replace an existing token of that name")
    x.add_argument("--rotate-env", action="store_true", help="allow overwriting the env file in use")
    x = sub.add_parser("init", parents=[common], help="write a pve.env connection file (no API call)",
                       description="Write a pve.env connection file and print how to create the API token.")
    x.set_defaults(fn=cmd_init)
    x.add_argument("--host", required=True, help="Proxmox host/IP (port 8006 assumed), any node of the cluster")
    x.add_argument("--token-id", default="claude@pve!agent", help="full token id user@realm!name")
    x.add_argument("--secret-stdin", action="store_true", help="read the token secret from stdin")
    x.add_argument("--verify-ssl", action="store_true")
    x.add_argument("--out", help="default: $PVE_HOME/pve.env (%s)" % os.path.join(DATA_DIR, "pve.env"))
    x.add_argument("--force", action="store_true")
    sp(sub, "doctor", cmd_doctor, "check connectivity, token rights, storage, SDN, local tools")
    return p


def main(argv=None):
    global JSON_OUT, DEADLINE
    parser = build_parser()
    args = parser.parse_args(argv)
    JSON_OUT = bool(getattr(args, "json", False))
    budget = getattr(args, "budget", None) or os.environ.get("PVE_BUDGET")
    if budget and int(budget) > 0:
        DEADLINE = time.time() + int(budget)
    if not getattr(args, "fn", None):
        parser.print_help()
        sys.exit(0)
    if args.fn in (cmd_lab_example, cmd_init):
        return args.fn(None, args)
    api = PVE(resolve_config(getattr(args, "env", None)))
    try:
        args.fn(api, args)
    except Budget as b:
        say("")
        say("PARTIAL: time budget reached (%s)%s." % (b.what, " - that Proxmox task keeps running" if b.upid else ""))
        say("Nothing is lost: re-run the SAME command to continue where it stopped.")
        sys.exit(3)
    except PVEError as e:
        die(e)
    except KeyboardInterrupt:
        die("interrupted (Proxmox tasks already started keep running on the server)", 130)
    except BrokenPipeError:  # output piped into head & co.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)


if __name__ == "__main__":
    main()
