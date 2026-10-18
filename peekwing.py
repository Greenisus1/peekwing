#!/usr/bin/env python3
"""Peekwing - share your Mac screen (and optionally let a helper click and type)
without admin rights. Outbound tunnel only, PIN protected, off until you start it.

Run:  python3 peekwing.py          (Ctrl+C or q + Enter stops everything)
"""
import argparse, hashlib, hmac, json, os, re, secrets, shutil, subprocess, sys
import tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "1.0.6"
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


LETTERS = {"a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
           "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
           "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
           "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
           ".": 47, "`": 50}
KEYCODES.update(LETTERS)
KEYCODES.update({"shift": 56, "control": 59, "ctrl": 59, "option": 58, "alt": 58, "command": 55, "cmd": 55})

INPUT_JXA = r"""
ObjC.import('CoreGraphics'); ObjC.import('unistd');
function run(argv) {
  var ops = JSON.parse(argv[0]);
  function mouse(t, x, y, b, n) {
    var e = $.CGEventCreateMouseEvent(null, t, {x: x, y: y}, b);
    if (n) { $.CGEventSetIntegerValueField(e, $.kCGMouseEventClickState, n); }
    $.CGEventPost($.kCGHIDEventTap, e);
  }
  for (var i = 0; i < ops.length; i++) {
    var o = ops[i], right = o.button === 'right';
    var D = right ? $.kCGEventRightMouseDown : $.kCGEventLeftMouseDown;
    var U = right ? $.kCGEventRightMouseUp : $.kCGEventLeftMouseUp;
    var B = right ? $.kCGMouseButtonRight : $.kCGMouseButtonLeft;
    if (o.t === 'move') { mouse($.kCGEventMouseMoved, o.x, o.y, 0, 0); }
    else if (o.t === 'mdown') { mouse($.kCGEventMouseMoved, o.x, o.y, 0, 0); mouse(D, o.x, o.y, B, 1); }
    else if (o.t === 'mup') { mouse(U, o.x, o.y, B, 1); }
    else if (o.t === 'click') {
      mouse($.kCGEventMouseMoved, o.x, o.y, 0, 0);
      for (var k = 1; k <= (o.count || 1); k++) { mouse(D, o.x, o.y, B, k); mouse(U, o.x, o.y, B, k); }
    }
    else if (o.t === 'drag') {
      mouse($.kCGEventMouseMoved, o.x, o.y, 0, 0); mouse($.kCGEventLeftMouseDown, o.x, o.y, $.kCGMouseButtonLeft, 1);
      mouse($.kCGEventLeftMouseDragged, o.x2, o.y2, $.kCGMouseButtonLeft, 1);
      mouse($.kCGEventLeftMouseUp, o.x2, o.y2, $.kCGMouseButtonLeft, 1);
    }
    else if (o.t === 'scroll') {
      var e = $.CGEventCreateScrollWheelEvent2(null, $.kCGScrollEventUnitLine, 1, o.dy, 0, 0);
      $.CGEventPost($.kCGHIDEventTap, e);
    }
    else if (o.t === 'kdown' || o.t === 'kup') {
      var ke = $.CGEventCreateKeyboardEvent(null, o.code, o.t === 'kdown');
      $.CGEventPost($.kCGHIDEventTap, ke);
    }
    else if (o.t === 'wait') { $.usleep(o.ms * 1000); }
  }
  return 'ok';
}
"""


def build_input_ops(ops, k):
    """Validate and translate helper ops (screenshot-space coords) into screen-point ops."""
    out, wait_total = [], 0
    if not isinstance(ops, list) or len(ops) > 60:
        raise ValueError("ops must be a list of at most 60")
    for o in ops:
        t = o.get("t")
        if t in ("move", "mdown", "mup", "click", "drag"):
            m = {"t": t, "x": float(o.get("x", 0)) * k, "y": float(o.get("y", 0)) * k,
                 "button": "right" if o.get("button") == "right" else "left"}
            if t == "click":
                m["count"] = max(1, min(3, int(o.get("count", 1))))
            if t == "drag":
                m["x2"] = float(o.get("x2", 0)) * k
                m["y2"] = float(o.get("y2", 0)) * k
        elif t == "scroll":
            m = {"t": t, "dy": max(-50, min(50, int(o.get("dy", 0))))}
        elif t in ("kdown", "kup"):
            key = str(o.get("key", "")).lower()
            if key not in KEYCODES:
                raise ValueError("unknown key: " + key[:20])
            m = {"t": t, "code": KEYCODES[key]}
        elif t == "wait":
            ms = max(0, min(1000, int(o.get("ms", 0))))
            wait_total += ms
            if wait_total > 5000:
                raise ValueError("too much waiting")
            m = {"t": t, "ms": ms}
        else:
            raise ValueError("unknown op: " + str(t)[:20])
        out.append(m)
    return out


class FastInput:
    """Mouse and keyboard events posted in-process (microseconds) instead of spawning osascript."""
    LD, LU, RD, RU, MV, LDRAG = 1, 2, 3, 4, 5, 6

    def __init__(self):
        import ctypes as C
        self.C = C
        P, D, U32, I32, I64 = C.c_void_p, C.c_double, C.c_uint32, C.c_int32, C.c_int64

        class Pt(C.Structure):
            _fields_ = [("x", D), ("y", D)]
        self.Pt = Pt
        cg = C.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.cg, self.cf = cg, cf
        cg.CGEventCreateMouseEvent.restype = P
        cg.CGEventCreateMouseEvent.argtypes = [P, U32, Pt, U32]
        cg.CGEventSetIntegerValueField.restype = None
        cg.CGEventSetIntegerValueField.argtypes = [P, U32, I64]
        cg.CGEventPost.restype = None
        cg.CGEventPost.argtypes = [U32, P]
        cg.CGEventCreateKeyboardEvent.restype = P
        cg.CGEventCreateKeyboardEvent.argtypes = [P, C.c_uint16, C.c_bool]
        cg.CGEventCreateScrollWheelEvent2.restype = P
        cg.CGEventCreateScrollWheelEvent2.argtypes = [P, U32, U32, I32, I32, I32]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [P]

    def _post(self, e):
        if not e:
            raise RuntimeError("event not created")
        self.cg.CGEventPost(0, e)  # kCGHIDEventTap
        self.cf.CFRelease(e)

    def _mouse(self, t, x, y, b, n=0):
        e = self.cg.CGEventCreateMouseEvent(None, t, self.Pt(x, y), b)
        if n:
            self.cg.CGEventSetIntegerValueField(e, 1, n)  # kCGMouseEventClickState
        self._post(e)

    def run(self, ops):
        for o in ops:
            t = o["t"]
            right = o.get("button") == "right"
            d, u, b = (self.RD, self.RU, 1) if right else (self.LD, self.LU, 0)
            if t == "move":
                self._mouse(self.MV, o["x"], o["y"], 0)
            elif t == "mdown":
                self._mouse(self.MV, o["x"], o["y"], 0)
                self._mouse(d, o["x"], o["y"], b, 1)
            elif t == "mup":
                self._mouse(u, o["x"], o["y"], b, 1)
            elif t == "click":
                self._mouse(self.MV, o["x"], o["y"], 0)
                for i in range(1, o.get("count", 1) + 1):
                    self._mouse(d, o["x"], o["y"], b, i)
                    self._mouse(u, o["x"], o["y"], b, i)
            elif t == "drag":
                self._mouse(self.MV, o["x"], o["y"], 0)
                self._mouse(self.LD, o["x"], o["y"], 0, 1)
                self._mouse(self.LDRAG, o["x2"], o["y2"], 0, 1)
                self._mouse(self.LU, o["x2"], o["y2"], 0, 1)
            elif t == "scroll":
                self._post(self.cg.CGEventCreateScrollWheelEvent2(None, 1, 1, int(o["dy"]), 0, 0))
            elif t in ("kdown", "kup"):
                self._post(self.cg.CGEventCreateKeyboardEvent(None, o["code"], t == "kdown"))
            elif t == "wait":
                time.sleep(o["ms"] / 1000.0)


class FastCapture:
    """In-process screen grab + scale + JPEG with CoreGraphics/ImageIO through ctypes (no process spawn)."""

    def __init__(self):
        import ctypes as C
        self.C = C
        P, S, U32, D, I32, L = C.c_void_p, C.c_size_t, C.c_uint32, C.c_double, C.c_int32, C.c_long

        class Pt(C.Structure):
            _fields_ = [("x", D), ("y", D)]

        class Sz(C.Structure):
            _fields_ = [("w", D), ("h", D)]

        class Rc(C.Structure):
            _fields_ = [("o", Pt), ("s", Sz)]
        self.Rc, self.Pt, self.Sz = Rc, Pt, Sz
        cg = C.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        io = C.CDLL("/System/Library/Frameworks/ImageIO.framework/ImageIO")
        self.cg, self.cf, self.io = cg, cf, io

        def sig(f, res, *args):
            f.restype = res
            f.argtypes = list(args)
        sig(cg.CGMainDisplayID, U32)
        sig(cg.CGDisplayCreateImage, P, U32)
        sig(cg.CGImageCreateWithImageInRect, P, P, Rc)
        sig(cg.CGImageGetWidth, S, P)
        sig(cg.CGImageGetHeight, S, P)
        sig(cg.CGColorSpaceCreateDeviceRGB, P)
        sig(cg.CGBitmapContextCreate, P, P, S, S, S, S, P, U32)
        sig(cg.CGContextSetInterpolationQuality, None, P, I32)
        sig(cg.CGContextDrawImage, None, P, Rc, P)
        sig(cg.CGBitmapContextCreateImage, P, P)
        sig(cg.CGContextRelease, None, P)
        sig(cg.CGImageRelease, None, P)
        sig(cg.CGColorSpaceRelease, None, P)
        sig(cf.CFDataCreateMutable, P, P, L)
        sig(cf.CFDataGetLength, L, P)
        sig(cf.CFDataGetBytePtr, P, P)
        sig(cf.CFRelease, None, P)
        sig(cf.CFStringCreateWithCString, P, P, C.c_char_p, U32)
        sig(cf.CFNumberCreate, P, P, I32, P)
        sig(cf.CFDictionaryCreate, P, P, C.POINTER(P), C.POINTER(P), L, P, P)
        sig(io.CGImageDestinationCreateWithData, P, P, P, S, P)
        sig(io.CGImageDestinationAddImage, None, P, P, P)
        sig(io.CGImageDestinationFinalize, C.c_bool, P)
        self.key_cb = C.addressof(C.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        self.val_cb = C.addressof(C.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        self.q_key = P.in_dll(io, "kImageDestinationLossyCompressionQuality").value if False else \
            P.in_dll(io, "kCGImageDestinationLossyCompressionQuality").value
        self.jpeg = cf.CFStringCreateWithCString(None, b"public.jpeg", 0x08000100)
        self.cs = cg.CGColorSpaceCreateDeviceRGB()
        self.display = cg.CGMainDisplayID()
        self.opts = {}

    def pixels(self, region, w):
        """Raw RGBX pixels of a screen region (fractions x,y,w,h) scaled to width w: (bytes, w, h)."""
        C, cg = self.C, self.cg
        full = cg.CGDisplayCreateImage(self.display)
        if not full:
            raise RuntimeError("CGDisplayCreateImage returned nothing")
        img = full
        ctx = None
        try:
            fw, fh = cg.CGImageGetWidth(full), cg.CGImageGetHeight(full)
            rx, ry, rw, rh = region
            img = cg.CGImageCreateWithImageInRect(full, self.Rc(self.Pt(rx * fw, ry * fh), self.Sz(rw * fw, rh * fh)))
            iw, ih = cg.CGImageGetWidth(img), cg.CGImageGetHeight(img)
            ow, oh = int(w), max(1, int(round(ih * w / float(iw))))
            buf = (C.c_ubyte * (ow * oh * 4))()
            ctx = cg.CGBitmapContextCreate(C.addressof(buf), ow, oh, 8, ow * 4, self.cs, 5)
            cg.CGContextSetInterpolationQuality(ctx, 1)
            cg.CGContextDrawImage(ctx, self.Rc(self.Pt(0, 0), self.Sz(ow, oh)), img)
            return bytes(buf), ow, oh
        finally:
            if ctx:
                cg.CGContextRelease(ctx)
            if img != full:
                cg.CGImageRelease(img)
            cg.CGImageRelease(full)

    def _props(self, q):
        C = self.C
        if q not in self.opts:
            val = C.c_double(q / 100.0)
            num = self.cf.CFNumberCreate(None, 13, C.byref(val))  # kCFNumberDoubleType
            keys = (C.c_void_p * 1)(self.q_key)
            vals = (C.c_void_p * 1)(num)
            self.opts[q] = self.cf.CFDictionaryCreate(None, keys, vals, 1, self.key_cb, self.val_cb)
        return self.opts[q]

    def frame(self, w, q, region=None):
        C, cg, cf, io = self.C, self.cg, self.cf, self.io
        full = cg.CGDisplayCreateImage(self.display)
        if not full:
            raise RuntimeError("CGDisplayCreateImage returned nothing")
        img = full
        ctx = out = data = dest = None
        try:
            if region:  # fractions of the screen: x, y, w, h
                fw, fh = cg.CGImageGetWidth(full), cg.CGImageGetHeight(full)
                rx, ry, rw, rh = region
                crop = cg.CGImageCreateWithImageInRect(
                    full, self.Rc(self.Pt(rx * fw, ry * fh), self.Sz(max(8, rw * fw), max(8, rh * fh))))
                if not crop:
                    raise RuntimeError("crop failed")
                img = crop
            iw, ih = cg.CGImageGetWidth(img), cg.CGImageGetHeight(img)
            ow, oh = int(w), max(1, int(round(ih * w / float(iw))))
            ctx = cg.CGBitmapContextCreate(None, ow, oh, 8, 0, self.cs, 5)  # NoneSkipLast
            if not ctx:
                raise RuntimeError("no bitmap context")
            cg.CGContextSetInterpolationQuality(ctx, 2)  # low = fast
            rc = self.Rc(self.Pt(0, 0), self.Sz(ow, oh))
            cg.CGContextDrawImage(ctx, rc, img)
            out = cg.CGBitmapContextCreateImage(ctx)
            data = cf.CFDataCreateMutable(None, 0)
            dest = io.CGImageDestinationCreateWithData(data, self.jpeg, 1, None)
            io.CGImageDestinationAddImage(dest, out, self._props(q))
            if not io.CGImageDestinationFinalize(dest):
                raise RuntimeError("jpeg encode failed")
            b = C.string_at(cf.CFDataGetBytePtr(data), cf.CFDataGetLength(data))
            if b[:2] != b"\xff\xd8":
                raise RuntimeError("bad jpeg")
            return b
        finally:
            for ref in (dest, data):
                if ref:
                    cf.CFRelease(ref)
            if out:
                cg.CGImageRelease(out)
            if ctx:
                cg.CGContextRelease(ctx)
            if img != full:
                cg.CGImageRelease(img)
            cg.CGImageRelease(full)


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 20), **kw)


class MacBackend:
    name = "macos"

    def __init__(self):
        self.work = tempfile.mkdtemp(prefix="peekwing-")
        self.mouse_js = os.path.join(self.work, "mouse.js")
        with open(self.mouse_js, "w") as f:
            f.write(MOUSE_JXA)
        self.input_js = os.path.join(self.work, "input.js")
        with open(self.input_js, "w") as f:
            f.write(INPUT_JXA)
        self.fast = None
        self.fast_failed = False
        self.fast_in = None
        self.input_failed = False
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

    def frame(self, w, q=60, region=None):
        """Fast path first (in-process, many fps); falls back to screencapture+sips (2-3 fps)."""
        if not self.fast_failed:
            try:
                if self.fast is None:
                    self.fast = FastCapture()
                return self.fast.frame(w, q, region)
            except Exception as e:
                self.fast_failed = True
                log("fast capture unavailable (%s) - using the slower screencapture path" % str(e)[:80])
        return self.screenshot(w, q, region)

    def input(self, ops):
        if not self.input_failed:
            try:
                if self.fast_in is None:
                    self.fast_in = FastInput()
                return self.fast_in.run(ops)
            except Exception as e:
                self.input_failed = True
                log("fast input unavailable (%s) - using osascript" % str(e)[:80])
        sh(["osascript", "-l", "JavaScript", self.input_js, json.dumps(ops)], timeout=15)

    def screenshot(self, w, q=60, region=None):
        raw = os.path.join(self.work, "raw.jpg")
        out = os.path.join(self.work, "out.jpg")
        try:
            cmd = ["screencapture", "-x", "-m", "-t", "jpg"]
            if region:
                sw, sh_ = self.size
                cmd += ["-R", "%d,%d,%d,%d" % (region[0] * sw, region[1] * sh_, max(8, region[2] * sw), max(8, region[3] * sh_))]
            sh(cmd + [raw])
            sh(["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(int(q)),
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
        self.latest = None

    def permissions(self):
        return {"screen": "browser", "accessibility": "n/a"}

    def screenshot(self, w):
        if not self.latest:
            raise RuntimeError("no frame yet - start sharing in the browser page")
        return self.latest

    def mouse(self, **a): raise RuntimeError("view-only")
    type_text = mouse
    def key(self, *a): raise RuntimeError("view-only")
    def notify(self, m): pass
    def cleanup(self): self.latest = None


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
        protocol_version = "HTTP/1.1"

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

        @staticmethod
        def _region(q):
            r = (q.get("region") or [""])[0]
            if not r:
                return None
            x, y, w, h = [max(0.0, min(1.0, float(v))) for v in r.split(",")]
            if x + w > 1.0001 or y + h > 1.0001 or w < 0.01 or h < 0.01:
                raise ValueError("region must be fractions x,y,w,h inside the screen")
            return (x, y, w, h)

        def _grab(self, w, q, region=None):
            fn = getattr(st.backend, "frame", None)
            if not fn:
                return st.backend.screenshot(w)
            return fn(w, q, region) if region else fn(w, q)

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
                qual = max(10, min(95, int((q.get("q") or [60])[0])))
                try:
                    data = self._grab(w, qual, self._region(q))
                except Exception as e:
                    return self._send(503, {"error": str(e)[:200]})
                self._announce("screen")
                return self._send(200, data, "image/jpeg", {"X-Screen-W": st.backend.size[0],
                                                           "X-Screen-H": st.backend.size[1]})
            if u.path == "/stream":
                if not st.view:
                    return self._send(403, {"error": "view is off: the owner has not switched it on"})
                return self._stream(q)
            self._send(404, {"error": "not found"})

        def _stream(self, q):
            w = max(320, min(1600, int((q.get("w") or [800])[0])))
            qual = max(10, min(90, int((q.get("q") or [45])[0])))
            fps = max(1, min(60, int((q.get("fps") or [10])[0])))
            try:
                region = self._region(q)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=pwframe")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self._announce("stream %dpx q%d up to %dfps%s" % (w, qual, fps, " (region)" if region else ""))
            last, sent_at = None, 0.0
            try:
                while not st.stop.is_set() and not st.expired() and st.view:
                    t0 = time.time()
                    data = self._grab(w, qual, region)
                    h = hashlib.md5(data).digest()
                    if h != last or t0 - sent_at > 1.0:  # unchanged screen: send only a keep-alive frame per second
                        self.wfile.write(b"--pwframe\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(data)
                                         + data + b"\r\n")
                        self.wfile.flush()
                        last, sent_at = h, t0
                    time.sleep(max(0.0, 1.0 / fps - (time.time() - t0)))
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            except Exception as e:
                log("stream stopped: " + str(e)[:80])
            log("stream ended")

        def do_POST(self):
            if not self._auth():
                return
            if urlparse(self.path).path == "/frame" and st.backend.name == "browser":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 3000000:
                    return self._send(413, {"error": "too big"})
                st.backend.latest = self.rfile.read(n)
                st.backend.size = (1280, 720)
                return self._send(200, {"ok": True})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                a = json.loads(self.rfile.read(min(n, 100000)) or b"{}")
            except Exception:
                return self._send(400, {"error": "bad json"})
            op = urlparse(self.path).path.strip("/")
            if op not in ("click", "move", "drag", "scroll", "type", "key", "input"):
                return self._send(404, {"error": "not found"})
            if not st.view:
                return self._send(403, {"error": "view is off: the owner has not switched it on"})
            if not st.control:
                log("control request blocked (view-only). Type c + Enter to allow control.")
                return self._send(403, {"error": "view-only: the owner has not enabled control"})
            ref = float(a.get("w") or DEFAULT_W)
            k = st.backend.size[0] / ref
            try:
                if op == "input":
                    ops = build_input_ops(a.get("ops"), k)
                    st.backend.input(ops)
                    self._announce("input batch of %d" % len(ops))
                elif op in ("click", "move", "drag", "scroll"):
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
            except ValueError as e:
                return self._send(400, {"error": str(e)[:200]})
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
    ap.add_argument("--bench", action="store_true", help="measure capture speed on this Mac and exit")
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
    if args.bench:
        for w, q, rg in ((640, 40, None), (800, 45, None), (1280, 60, None), (480, 40, (0.25, 0.1, 0.5, 0.4))):
            t0, n, size = time.time(), 0, 0
            try:
                while time.time() - t0 < 3:
                    size += len(backend.frame(w, q, rg) if rg else backend.frame(w, q))
                    n += 1
            except Exception as e:
                print("width %d: failed: %s" % (w, e))
                continue
            dt = time.time() - t0
            print("width %4d q%d%s: %.1f frames/s, %d KB/frame (%s path)" % (
                w, q, " small region" if rg else "", n / dt, size // max(n, 1) // 1024, "slow" if getattr(backend, "fast_failed", False) else "fast"))
        backend.cleanup()
        return 0
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
