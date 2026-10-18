import json, os, sys, threading, unittest, urllib.request, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import peekwing as pw


class Fake:
    name = "fake"
    size = (1000, 600)
    def __init__(self): self.calls = []
    def permissions(self): return {"screen": True, "accessibility": True}
    def screenshot(self, w): self.calls.append(("shot", w)); return b"\xff\xd8fake"
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
        self.assertEqual(c, 200); self.assertEqual(h["X-Screen-W"], "1000"); self.assertEqual(self.b.calls[0], ("shot", 800))

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
