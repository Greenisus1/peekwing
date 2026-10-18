import json, os, sys, threading, unittest, urllib.request, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import peekwing as pw


class Fake:
    name = "fake"
    size = (1000, 600)
    def __init__(self): self.calls = []
    def permissions(self): return {"screen": True, "accessibility": True}
    n = 0
    def screenshot(self, w): self.calls.append(("shot", w)); return b"\xff\xd8fake"
    def frame(self, w, q, region=None):
        self.last_region = region
        self.n += 1
        return b"\xff\xd8" + (b"A" if self.n % 2 else b"B") * 50   # alternates so every frame counts as changed
    def input(self, ops): self.calls.append(("input", ops))
    def mouse(self, **a): self.calls.append(("mouse", a))
    def type_text(self, t): self.calls.append(("type", t))
    def key(self, k, m): self.calls.append(("key", k, m))
    def notify(self, m): pass
    def cleanup(self): pass


class T(unittest.TestCase):
    def setUp(self):
        self.b = Fake()
        self.st = pw.State(self.b, "123456", 30)
        self.srv = pw.ThreadingHTTPServer(("127.0.0.1", 0), pw.make_handler(self.st))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self): self.srv.shutdown()

    def req(self, path, data=None, pin="123456"):
        r = urllib.request.Request(self.base + path, data=None if data is None else json.dumps(data).encode(),
                                   headers={"X-Pin": pin})
        try:
            with urllib.request.urlopen(r) as x: return x.status, x.read(), x.headers
        except urllib.error.HTTPError as e: return e.code, e.read(), e.headers

    def test_no_pin_no_screen(self):
        self.assertEqual(self.req("/screen", pin="000000")[0], 401)
        self.assertEqual(self.b.calls, [])

    def test_screen_ok_and_size_header(self):
        c, body, h = self.req("/screen?w=800")
        self.assertEqual(c, 200); self.assertEqual(h["X-Screen-W"], "1000"); self.assertTrue(body.startswith(b"\xff\xd8"))

    def test_view_only_blocks_control(self):
        self.assertEqual(self.req("/click", {"x": 1, "y": 1})[0], 403)
        self.assertEqual(self.b.calls, [])

    def test_control_scaling(self):
        self.st.control = True
        self.assertEqual(self.req("/click", {"x": 640, "y": 300, "w": 1280})[0], 200)
        m = self.b.calls[0][1]
        self.assertAlmostEqual(m["x"], 500); self.assertAlmostEqual(m["y"], 234.375)

    def test_bad_mod_rejected(self):
        self.st.control = True
        self.assertEqual(self.req("/key", {"key": "c", "mods": ["rm"]})[0], 400)

    def test_lockout(self):
        for _ in range(5): self.req("/status", pin="bad")
        self.assertTrue(self.st.stop.is_set())
        self.assertEqual(self.req("/status")[0], 429)

    def test_expiry(self):
        self.st.deadline = 0
        self.assertEqual(self.req("/status")[0], 410)

    def test_no_limit_session(self):
        st = pw.State(self.b, "1", 0)
        self.assertFalse(st.expired())
        self.assertEqual(st.deadline, float("inf"))
        st2 = pw.State(self.b, "1", 30)
        self.assertFalse(st2.expired())

    def test_stream_delivers_frames_and_obeys_view(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
        import pwclient, tempfile
        d = tempfile.mkdtemp()
        n, secs = pwclient.stream(self.base, "123456", d, seconds=1.5, w=640, q=40, fps=20)
        self.assertGreaterEqual(n, 8)                       # fake backend is instant, so the rate is the cap
        self.assertTrue(open(os.path.join(d, "f0000.jpg"), "rb").read().startswith(b"\xff\xd8"))
        self.assertEqual(self.req("/stream", pin="nope")[0], 401)

    def test_stream_skips_unchanged_frames(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
        import pwclient, tempfile
        self.b.frame = lambda w, q: b"\xff\xd8same"
        n, secs = pwclient.stream(self.base, "123456", tempfile.mkdtemp(), seconds=2.2, fps=20)
        self.assertLessEqual(n, 4)                          # only ~1 keep-alive frame per second

    def test_input_batch(self):
        self.st.control = True
        ops = [{"t": "move", "x": 640, "y": 400}, {"t": "kdown", "key": "space"}, {"t": "wait", "ms": 80},
               {"t": "kup", "key": "space"}, {"t": "click", "x": 0, "y": 0, "count": 2}]
        self.assertEqual(self.req("/input", {"w": 1280, "ops": ops})[0], 200)
        sent = self.b.calls[-1][1]
        self.assertEqual(sent[0]["x"], 500.0)               # 640 * (1000/1280)... scaled to screen points
        self.assertEqual(sent[1]["code"], 49)
        self.assertEqual(len(sent), 5)

    def test_input_rejects_bad_ops(self):
        self.st.control = True
        for ops in ([{"t": "kdown", "key": "nonsense"}], [{"t": "rm"}], [{"t": "wait", "ms": 1000}] * 6, "x"):
            self.assertEqual(self.req("/input", {"ops": ops})[0], 400, ops)
        self.assertEqual(self.b.calls, [])

    def test_input_needs_control(self):
        self.assertEqual(self.req("/input", {"ops": [{"t": "move", "x": 1, "y": 1}]})[0], 403)

    def test_region_param(self):
        c, body, h = self.req("/screen?region=0.25,0.1,0.5,0.4&w=480")
        self.assertEqual(c, 200); self.assertEqual(self.b.last_region, (0.25, 0.1, 0.5, 0.4))
        self.assertEqual(self.req("/stream?region=0.9,0.9,0.5,0.5")[0], 400)
        self.assertEqual(self.req("/screen?region=0.9,0.9,0.5,0.5")[0], 503)

    def test_public_root_leaks_nothing(self):
        c, body, _ = self.req("/", pin="")
        self.assertEqual(body, b"peekwing up\n")

    def test_browser_frames(self):
        self.tearDown()
        self.b = pw.BrowserBackend(); self.st = pw.State(self.b, "123456", 30)
        self.srv = pw.ThreadingHTTPServer(("127.0.0.1", 0), pw.make_handler(self.st))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.assertEqual(self.req("/screen")[0], 503)
        r = urllib.request.Request(self.base + "/frame", data=b"JPG", headers={"X-Pin": "123456"})
        self.assertEqual(urllib.request.urlopen(r).status, 200)
        self.assertEqual(self.req("/screen")[1], b"JPG")
        self.assertEqual(self.req("/click", {"x": 1, "y": 1})[0], 403)
        self.assertEqual(self.req("/share", pin="")[0], 200)

    def test_persistent_view_and_control_windows(self):
        import tempfile, time
        self.tearDown()
        cfg = tempfile.mktemp()
        json.dump({"token": "tok"}, open(cfg, "w"))
        self.b = Fake(); self.st = pw.State(self.b, "tok", 0, cfg_path=cfg)
        self.srv = pw.ThreadingHTTPServer(("127.0.0.1", 0), pw.make_handler(self.st))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        W = lambda **k: json.dump(dict(token="tok", **k), open(cfg, "w"))
        self.assertEqual(self.req("/status", pin="tok")[0], 200)
        self.assertEqual(self.req("/screen", pin="tok")[0], 403)          # view off by default
        self.assertEqual(self.req("/click", {"x": 1, "y": 1}, pin="tok")[0], 403)
        W(control_until=time.time() + 60)                                  # control alone is not enough
        self.assertEqual(self.req("/click", {"x": 1, "y": 1}, pin="tok")[0], 403)
        W(view_until=time.time() + 60)
        self.assertEqual(self.req("/screen", pin="tok")[0], 200)
        self.assertEqual(self.req("/click", {"x": 1, "y": 1}, pin="tok")[0], 403)  # view on, control still off
        W(view_until=time.time() + 60, control_until=time.time() + 60)
        self.assertEqual(self.req("/click", {"x": 1, "y": 1}, pin="tok")[0], 200)
        W(view_until=time.time() - 1, control_until=time.time() + 60)      # view expired -> all off
        self.assertEqual(self.req("/screen", pin="tok")[0], 403)
        self.assertEqual(self.req("/click", {"x": 1, "y": 1}, pin="tok")[0], 403)
        for _ in range(5): self.req("/status", pin="bad")
        self.assertEqual(self.req("/status", pin="tok")[0], 429)
        self.assertFalse(self.st.stop.is_set())


if __name__ == "__main__":
    unittest.main()
