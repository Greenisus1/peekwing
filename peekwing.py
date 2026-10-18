#!/usr/bin/env python3
"""Peekwing - share your Mac screen (and optionally let a helper click and type)
without admin rights. Outbound tunnel only, PIN protected, off until you start it.

Run:  python3 peekwing.py          (Ctrl+C or q + Enter stops everything)
"""
import argparse, hashlib, hmac, json, os, re, secrets, shutil, subprocess, sys
import tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "1.0.3"
MAX_BAD_PINS = 5


def word(v):
    return {True: "on", False: "OFF", None: "unknown"}[v]
DEFAULT_W = 1280

KEYCODES = {"return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
            "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125, "up": 126,
            "home": 115, "end": 119, "pageup": 116, "pagedown": 121}
MODS = {"command": "command down", "cmd": "command down", "shift": "shift down",
        "option": "option down", "alt": "option down", "control": "control down", "ctrl": "control down"}

MOUSE_JXA = r"""
ObjC.import('CoreGraphics');
function run(argv) {
  var a = JSON.parse(argv[0]);
  var p = {x: a.x, y: a.y};
  function post(t, b, n) {
    var e = $.CGEventCreateMouseEvent(null, t, p, b);
    if (n) { $.CGEventSetIntegerValueField(e, $.kCGMouseEventClickState, n); }
    $.CGEventPost($.kCGHIDEventTap, e);
  }
  if (a.op === 'move') { post($.kCGEventMouseMoved, 0, 0); }
  else if (a.op === 'click') {
    var right = a.button === 'right';
    var d = right ? $.kCGEventRightMouseDown : $.kCGEventLeftMouseDown;
    var u = right ? $.kCGEventRightMouseUp : $.kCGEventLeftMouseUp;
    var b = right ? $.kCGMouseButtonRight : $.kCGMouseButtonLeft;
    post($.kCGEventMouseMoved, 0, 0);
    for (var i = 1; i <= (a.count || 1); i++) { post(d, b, i); post(u, b, i); }
  }
  else if (a.op === 'drag') {
    post($.kCGEventMouseMoved, 0, 0);
    post($.kCGEventLeftMouseDown, $.kCGMouseButtonLeft, 1);
    p = {x: a.x2, y: a.y2};
    post($.kCGEventLeftMouseDragged, $.kCGMouseButtonLeft, 1);
    post($.kCGEventLeftMouseUp, $.kCGMouseButtonLeft, 1);
  }
  else if (a.op === 'scroll') {
    var e = $.CGEventCreateScrollWheelEvent2(null, $.kCGScrollEventUnitLine, 1, a.dy, 0, 0);
    $.CGEventPost($.kCGHIDEventTap, e);
  }
  return 'ok';
}
"""

PERM_JXA = r"""
ObjC.import('CoreGraphics'); ObjC.import('ApplicationServices');
function run() {
  return JSON.stringify({screen: !!$.CGPreflightScreenCaptureAccess(), accessibility: !!$.AXIsProcessTrusted()});
}
"""


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 20), **kw)


class MacBackend:
    name = "macos"

    def __init__(self):
        self.work = tempfile.mkdtemp(prefix="peekwing-")
        self.mouse_js = os.path.join(self.work, "mouse.js")
        with open(self.mouse_js, "w") as f:
            f.write(MOUSE_JXA)
        r = sh(["osascript", "-l", "JavaScript", "-e",
                "ObjC.import('CoreGraphics');var b=$.CGDisplayBounds($.CGMainDisplayID());b.size.width+','+b.size.height"])
        try:
            w, h = r.stdout.strip().split(",")
            self.size = (int(float(w)), int(float(h)))
        except Exception:
            self.size = (1440, 900)

    def permissions(self):
        """Ask macOS directly through ctypes. None means the check itself could not run."""
        out = {"screen": None, "accessibility": None}
        try:
            import ctypes
            cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            out["screen"] = bool(cg.CGPreflightScreenCaptureAccess())
        except Exception:
            pass
        try:
            import ctypes
            ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            ax.AXIsProcessTrusted.restype = ctypes.c_bool
            out["accessibility"] = bool(ax.AXIsProcessTrusted())
        except Exception:
            pass
        return out

    def screenshot(self, w):
        raw = os.path.join(self.work, "raw.jpg")
        out = os.path.join(self.work, "out.jpg")
        try:
            sh(["screencapture", "-x", "-m", "-t", "jpg", raw])
            sh(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "60",
                "--resampleWidth", str(w), raw, "--out", out])
            with open(out, "rb") as f:
                return f.read()
        finally:
            for p in (raw, out):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def mouse(self, **a):
        sh(["osascript", "-l", "JavaScript", self.mouse_js, json.dumps(a)])

    def type_text(self, text):
        # one character at a time with a short delay so fast typing cannot drop spaces
        script = ['on run argv', 'tell application "System Events"',
                  'repeat with ch in (characters of (item 1 of argv))', 'keystroke (contents of ch)',
                  'delay 0.04', 'end repeat', 'end tell', 'end run']
        cmd = ["osascript"]
        for line in script:
            cmd += ["-e", line]
        sh(cmd + [text], timeout=max(20, int(len(text) * 0.2) + 10))

    def key(self, key, mods):
        using = ""
        if mods:
            using = " using {" + ", ".join(MODS[m] for m in mods) + "}"
        if key.lower() in KEYCODES:
            script = 'tell application "System Events" to key code %d%s' % (KEYCODES[key.lower()], using)
        else:
            script = 'tell application "System Events" to keystroke (item 1 of argv)%s' % using
            sh(["osascript", "-e", "on run argv", "-e", script, "-e", "end run", key[:1]])
            return
        sh(["osascript", "-e", script])

    def notify(self, msg):
        sh(["osascript", "-e", 'display notification "%s" with title "Peekwing"' % msg])

    def cleanup(self):
        shutil.rmtree(self.work, ignore_errors=True)


SHARE_PAGE = """<!doctype html><meta charset=utf-8><title>Peekwing share</title>
<body style="font-family:sans-serif;max-width:32em;margin:3em auto">
<h2>Peekwing browser share (view only)</h2>
<p>Click Start, then pick the screen to share. Close this tab to stop.</p>
<button id=b style="font-size:1.2em;padding:.5em 1em">Start sharing</button> <p id=m></p>
<script>
const pin=new URLSearchParams(location.search).get('pin')||'';
document.getElementById('b').onclick=async()=>{
 const s=await navigator.mediaDevices.getDisplayMedia({video:true,audio:false});
 const v=document.createElement('video');v.srcObject=s;await v.play();
 const c=document.createElement('canvas');let n=0;
 s.getVideoTracks()[0].onended=()=>{document.getElementById('m').textContent='Stopped.';clearInterval(t)};
 const t=setInterval(()=>{const w=1280;c.width=w;c.height=Math.round(w*v.videoHeight/v.videoWidth);
  c.getContext('2d').drawImage(v,0,0,c.width,c.height);
  c.toBlob(b=>fetch('/frame',{method:'POST',headers:{'X-Pin':pin},body:b}),'image/jpeg',0.6);
  document.getElementById('m').textContent='Sharing... frames sent: '+(++n);},1500);};
</script>"""


class BrowserBackend:
    """View-only: frames come from the browser share page, nothing is captured by Terminal."""
    name = "browser"
    size = (1280, 720)

    def __init__(self):
        self.frame = None

    def permissions(self):
        return {"screen": "browser", "accessibility": "n/a"}

    def screenshot(self, w):
        if not self.frame:
            raise RuntimeError("no frame yet - start sharing in the browser page")
        return self.frame

    def mouse(self, **a): raise RuntimeError("view-only")
    type_text = mouse
    def key(self, *a): raise RuntimeError("view-only")
    def notify(self, m): pass
    def cleanup(self): self.frame = None


class State:
    def __init__(self, backend, pin, minutes, cfg_path=None):
        self.backend = backend
        self.pin = pin
        self.cfg_path = cfg_path
        self._control = False
        self.persistent = cfg_path is not None
        self.deadline = time.time() + minutes * 60 if minutes else float("inf")  # 0 = no limit
        self.bad = 0
        self.locked_until = 0
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.last_seen = None

    def cfg(self):
        try:
            with open(self.cfg_path) as f:
                return json.load(f)
        except Exception:
            return {}

    @property
    def view(self):
        if self.persistent:
            return time.time() < float(self.cfg().get("view_until", 0))
        return True

    @property
    def control(self):
        if self.persistent:
            # control needs view on too, and each is a timed window the owner starts locally
            return self.view and time.time() < float(self.cfg().get("control_until", 0))
        return self._control

    @control.setter
    def control(self, v):
        self._control = v

    def expired(self):
        return (not self.persistent) and time.time() >= self.deadline


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def make_handler(st):
    class H(BaseHTTPRequestHandler):
        server_version = "Peekwing"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, str(v))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self):
            if st.expired():
                self._send(410, {"error": "session expired"})
                st.stop.set()
                return False
            q = parse_qs(urlparse(self.path).query)
            pin = self.headers.get("X-Pin") or self.headers.get("X-Token") or (q.get("pin") or [""])[0]
            with st.lock:
                if st.persistent and time.time() < st.locked_until:
                    self._send(429, {"error": "locked"})
                    return False
                if not st.persistent and st.bad >= MAX_BAD_PINS:
                    self._send(429, {"error": "locked"})
                    return False
                ok = hmac.compare_digest(pin.encode(), st.pin.encode())
                if not ok:
                    st.bad += 1
                    log("wrong credential attempt %d" % st.bad)
                    if st.persistent and st.bad >= MAX_BAD_PINS:
                        st.locked_until = time.time() + 600
                        st.bad = 0
                        log("too many wrong attempts - locked for 10 minutes")
                    elif st.bad >= MAX_BAD_PINS:
                        log("too many wrong PINs - shutting down")
                        st.stop.set()
                    self._send(401, {"error": "bad credential"})
                    return False
                st.bad = 0
            return True

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                return self._send(200, "peekwing up\n", "text/plain")
            if u.path == "/share" and st.backend.name == "browser" and self.client_address[0] == "127.0.0.1" \
                    and not self.headers.get("Cf-Connecting-Ip"):
                return self._send(200, SHARE_PAGE, "text/html")
            if not self._auth():
                return
            q = parse_qs(u.query)
            if u.path == "/status":
                return self._send(200, {"version": VERSION, "view": st.view, "control": st.control,
                                        "screen": list(st.backend.size),
                                        "seconds_left": None if st.deadline == float("inf") else int(st.deadline - time.time()),
                                        "permissions": st.backend.permissions()})
            if u.path == "/screen":
                if not st.view:
                    return self._send(403, {"error": "view is off: the owner has not switched it on"})
                w = max(320, min(3000, int((q.get("w") or [DEFAULT_W])[0])))
                try:
                    data = st.backend.screenshot(w)
                except Exception as e:
                    return self._send(503, {"error": str(e)[:200]})
                self._announce("screen")
                return self._send(200, data, "image/jpeg", {"X-Screen-W": st.backend.size[0],
                                                           "X-Screen-H": st.backend.size[1]})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return
            if urlparse(self.path).path == "/frame" and st.backend.name == "browser":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 3000000:
                    return self._send(413, {"error": "too big"})
                st.backend.frame = self.rfile.read(n)
                st.backend.size = (1280, 720)
                return self._send(200, {"ok": True})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                a = json.loads(self.rfile.read(min(n, 100000)) or b"{}")
            except Exception:
                return self._send(400, {"error": "bad json"})
            op = urlparse(self.path).path.strip("/")
            if op not in ("click", "move", "drag", "scroll", "type", "key"):
                return self._send(404, {"error": "not found"})
            if not st.view:
                return self._send(403, {"error": "view is off: the owner has not switched it on"})
            if not st.control:
                log("control request blocked (view-only). Type c + Enter to allow control.")
                return self._send(403, {"error": "view-only: the owner has not enabled control"})
            ref = float(a.get("w") or DEFAULT_W)
            k = st.backend.size[0] / ref
            try:
                if op in ("click", "move", "drag", "scroll"):
                    m = {"op": op, "x": float(a.get("x", 0)) * k, "y": float(a.get("y", 0)) * k}
                    if op == "click":
                        m["button"] = "right" if a.get("button") == "right" else "left"
                        m["count"] = max(1, min(3, int(a.get("count", 1))))
                    if op == "drag":
                        m["x2"] = float(a.get("x2", 0)) * k
                        m["y2"] = float(a.get("y2", 0)) * k
                    if op == "scroll":
                        m["dy"] = max(-50, min(50, int(a.get("dy", 0))))
                    st.backend.mouse(**m)
                    self._announce(op)
                elif op == "type":
                    text = str(a.get("text", ""))[:2000]
                    st.backend.type_text(text)
                    self._announce("type %d chars" % len(text))
                else:
                    key = str(a.get("key", ""))
                    mods = [str(x).lower() for x in a.get("mods", [])]
                    if not key or any(x not in MODS for x in mods):
                        return self._send(400, {"error": "bad key or mods"})
                    st.backend.key(key, mods)
                    self._announce("key %s%s" % ("+".join(mods) + "+" if mods else "", key))
            except Exception as e:
                return self._send(500, {"error": str(e)[:200]})
            self._send(200, {"ok": True})

        def _announce(self, what):
            now = time.time()
            if what != "screen" or not st.last_seen or now - st.last_seen > 15:
                log("helper: " + what)
            if what != "screen" and (not st.last_seen or now - st.last_seen > 300):
                try:
                    st.backend.notify("Your helper is controlling your screen.")
                except Exception:
                    pass
                st.last_seen = now
            if what == "screen":
                if not st.last_seen or now - st.last_seen > 300:
                    try:
                        st.backend.notify("Your helper is viewing your screen.")
                    except Exception:
                        pass
                st.last_seen = now

    return H


def find_cloudflared():
    home = os.path.expanduser("~")
    for p in (home + "/CrunchByte/Peekwing/cloudflared", home + "/CrunchByte/PiRemote/cloudflared"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("cloudflared")


def start_tunnel(port):
    cf = find_cloudflared()
    if not cf:
        raise RuntimeError("cloudflared not found - run the install line again")
    p = subprocess.Popen([cf, "tunnel", "--url", "http://127.0.0.1:%d" % port, "--no-autoupdate"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    found = {}

    def reader():
        for line in p.stderr:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and "url" not in found:
                found["url"] = m.group(0)
    threading.Thread(target=reader, daemon=True).start()
    t0 = time.time()
    while time.time() - t0 < 45 and "url" not in found and p.poll() is None:
        time.sleep(0.3)
    if "url" not in found:
        p.terminate()
        raise RuntimeError("tunnel did not come up (Wi-Fi may block it). Try another network.")
    return p, found["url"]


def console(st):
    if not sys.stdin.isatty():
        return
    while not st.stop.is_set():
        try:
            line = sys.stdin.readline()
        except Exception:
            return
        if not line:
            return
        c = line.strip().lower()
        if c == "c":
            st.control = not st.control
            log("CONTROL IS NOW " + ("ON - helper can click and type" if st.control else "OFF - view only"))
        elif c == "q":
            st.stop.set()
        elif c:
            print("  c = toggle control   q = quit")


# ---------------------------------------------------------------- always-on mode
LABEL = "com.crunchbyte.peekwing"
HOME = os.path.expanduser("~")
DIR = HOME + "/CrunchByte/Peekwing"
CFG = DIR + "/config.json"
PLIST = HOME + "/Library/LaunchAgents/" + LABEL + ".plist"
LOGF = DIR + "/peekwing.log"


def load_cfg():
    try:
        with open(CFG) as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(c):
    os.makedirs(DIR, exist_ok=True)
    fd = os.open(CFG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(c, f)
    os.chmod(CFG, 0o600)


def launchctl(*a):
    return subprocess.run(["launchctl"] + list(a), capture_output=True, text=True)


def uid():
    return str(os.getuid())


def cmd_install(args):
    c = load_cfg()
    if "token" not in c:
        c["token"] = secrets.token_urlsafe(32)
    c.pop("control", None)
    save_cfg(c)
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>%s</string>
<key>ProgramArguments</key><array><string>%s</string><string>%s</string><string>daemon</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>30</integer>
<key>StandardOutPath</key><string>%s</string><key>StandardErrorPath</key><string>%s</string>
</dict></plist>
""" % (LABEL, sys.executable, os.path.abspath(__file__), LOGF, LOGF)
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "w") as f:
        f.write(plist)
    launchctl("bootout", "gui/" + uid() + "/" + LABEL)
    r = launchctl("bootstrap", "gui/" + uid(), PLIST)
    print("Always-on Peekwing installed (starts at login, runs only while you are logged in and the Mac is awake)." if r.returncode == 0
          else "launchctl said: " + (r.stderr or r.stdout).strip())
    print("Everything is OFF until you switch it on:  peekwing view on [minutes]   then, only if needed:  peekwing control on [minutes]")
    print("Next: python3 %s token   (shows the secret to give your helper through a private link)" % os.path.abspath(__file__))
    return 0


def cmd_uninstall(args):
    launchctl("bootout", "gui/" + uid() + "/" + LABEL)
    for p in (PLIST,):
        try:
            os.remove(p)
        except OSError:
            pass
    shutil.rmtree(DIR, ignore_errors=True)
    print("Peekwing removed: service stopped, login item deleted, token and files erased.")
    return 0


def cmd_pause(args):
    launchctl("bootout", "gui/" + uid() + "/" + LABEL)
    print("Paused. Nobody can reach your screen. Resume with:  peekwing.py resume")
    return 0


def cmd_resume(args):
    r = launchctl("bootstrap", "gui/" + uid(), PLIST)
    print("Resumed." if r.returncode == 0 else (r.stderr or r.stdout).strip())
    return 0


def _window(key, label, args):
    c = load_cfg()
    mode = args[0] if args else "status"
    if mode == "on":
        mins = max(1, min(240, int(args[1]))) if len(args) > 1 else 30
        c[key] = time.time() + mins * 60
        save_cfg(c)
        print("%s is ON for %d minutes, then switches off by itself. Turn off now: %s off" % (label, mins, label.lower()))
    elif mode == "off":
        c[key] = 0
        if key == "view_until":
            c["control_until"] = 0
        save_cfg(c)
        print("%s is off." % label)
    else:
        left = int(c.get(key, 0) - time.time())
        print("%s: %s" % (label, ("ON, %ds left" % left) if left > 0 else "off"))
    return 0


def cmd_view(args):
    return _window("view_until", "View", args)


def cmd_control(args):
    return _window("control_until", "Control", args)


def cmd_token(args):
    c = load_cfg()
    if args and args[0] == "new":
        c["token"] = secrets.token_urlsafe(32)
        save_cfg(c)
        print("New token created; the old one no longer works.")
    t = c.get("token")
    if not t:
        print("Not installed yet. Run: python3 peekwing.py install")
        return 1
    print("Token (keep private):", t)
    try:
        subprocess.run(["pbcopy"], input=t, text=True, timeout=5)
        print("Copied to your clipboard. Paste it only into the private link your helper sends you.")
    except Exception:
        pass
    return 0


def cmd_status(args):
    c = load_cfg()
    r = launchctl("print", "gui/" + uid() + "/" + LABEL)
    print("service running:", "yes" if r.returncode == 0 and "state = running" in r.stdout else "no (paused or not installed)")
    for k, n in (("view_until", "view"), ("control_until", "control")):
        left = int(c.get(k, 0) - time.time())
        print("%s:" % n, ("ON, %ds left" % left) if left > 0 else "off")
    print("address:", c.get("last_url", "(starts after login)"))
    print("log:", LOGF)
    return 0


def daemon(args, backend=None):
    c = load_cfg()
    if "token" not in c:
        print("no token; run install")
        return 1
    try:
        if os.path.getsize(LOGF) > 1000000:
            open(LOGF, "w").close()
    except OSError:
        pass
    backend = backend or MacBackend()
    st = State(backend, c["token"], 0, cfg_path=CFG)
    st.pin = c["token"]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(st))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cf = find_cloudflared()
    if not cf:
        print("cloudflared missing")
        return 2
    if c.get("tunnel_token") and c.get("hostname"):
        # named tunnel on your own Cloudflare account: stable address, ingress set in the dashboard to this port
        cmd = [cf, "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:%d" % port, "run", "--token", c["tunnel_token"]]
        url = "https://" + c["hostname"]
        tun = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            tun, url = start_tunnel(port)
        except Exception as e:
            print("tunnel failed:", e)
            return 2
    c = load_cfg()
    c["last_url"] = url
    save_cfg(c)
    log("always-on up at %s (view and control off until the owner turns them on)" % url)
    try:
        backend.notify("Peekwing is running, view and control are OFF until you turn them on.")
    except Exception:
        pass
    try:
        while tun.poll() is None:
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    srv.shutdown()
    return 3  # launchd restarts us, with a fresh tunnel


def main(argv=None, backend=None):
    cmds = {"install": cmd_install, "uninstall": cmd_uninstall, "pause": cmd_pause, "resume": cmd_resume,
            "control": cmd_control, "view": cmd_view, "token": cmd_token, "status": cmd_status,
            "daemon": lambda a: daemon(a, backend)}
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in cmds:
        return cmds[argv[0]](argv[1:])
    ap = argparse.ArgumentParser(description="Peekwing: share your screen without admin.")
    ap.add_argument("--minutes", type=int, default=None,
                    help="auto-stop after this many minutes; 0 = no limit until you quit. Asked at start if omitted.")
    ap.add_argument("--local", action="store_true", help="no tunnel, localhost only (testing)")
    ap.add_argument("--browser", action="store_true",
                    help="view-only fallback: share one screen/window from a browser tab, no Terminal permissions")
    ap.add_argument("--check", action="store_true", help="only show permission status and exit")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args(argv)
    if backend is None:
        if args.browser:
            backend = BrowserBackend()
        elif sys.platform != "darwin":
            print("Peekwing runs on macOS.")
            return 1
        else:
            backend = MacBackend()
    perms = backend.permissions()
    if args.check:
        print("Screen Recording: %s   Accessibility: %s" % (word(perms.get("screen")), word(perms.get("accessibility"))))
        backend.cleanup()
        return 0
    minutes = args.minutes
    if minutes is None:
        minutes = 30
        if sys.stdin.isatty():
            try:
                ans = input("How many minutes should sharing last? (Enter = 30, 0 = no limit until you quit): ").strip()
                minutes = int(ans) if ans else 30
            except (ValueError, EOFError):
                print("Not a number, using 30.")
    minutes = max(0, minutes)
    pin = "%06d" % secrets.randbelow(1000000)
    st = State(backend, pin, minutes)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(st))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tun, url = None, "http://127.0.0.1:%d" % port
    try:
        if not args.local:
            tun, url = start_tunnel(port)
    except Exception as e:
        print("Could not start:", e)
        srv.shutdown()
        backend.cleanup()
        return 2
    bar = "=" * 56
    print("\n" + bar)
    print("  PEEKWING IS SHARING YOUR SCREEN (view only)")
    print("  Address: " + url)
    print("  PIN:     " + pin)
    if backend.name == "browser":
        print("  Open this in your browser to start sharing:  http://127.0.0.1:%d/share?pin=%s" % (port, pin))
    if minutes:
        print("  DURATION: stops by itself in %d minutes. Or press Ctrl+C, or type q + Enter." % minutes)
    else:
        print("  DURATION: NO TIME LIMIT. Stays on until you press Ctrl+C or type q + Enter.")
    print("  Type c + Enter to let your helper click and type. Off now.")
    print("  Screen Recording: %s   Accessibility: %s" % (word(perms.get("screen")), word(perms.get("accessibility"))))
    if perms.get("screen") is False:
        print("  -> Screen Recording is off. System Settings > Privacy & Security > Screen Recording > turn on Terminal, then restart Terminal.")
    if perms.get("accessibility") is False:
        print("  -> Accessibility is off (needed only for control). Privacy & Security > Accessibility > turn on Terminal.")
    print(bar + "\n", flush=True)
    try:
        subprocess.run(["pbcopy"], input="%s PIN %s" % (url, pin), text=True, timeout=5)
        print("  (Address + PIN copied - paste it into your message.)\n")
    except Exception:
        pass
    try:
        backend.notify("Screen sharing is ON. Stop it in Terminal.")
    except Exception:
        pass
    threading.Thread(target=console, args=(st,), daemon=True).start()
    try:
        while not st.stop.is_set() and not st.expired():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    log("stopping")
    srv.shutdown()
    if tun:
        tun.terminate()
    backend.cleanup()
    try:
        backend.notify("Screen sharing stopped.")
    except Exception:
        pass
    print("Peekwing stopped. Nothing was saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
