#!/usr/bin/env python3
"""Helper-side client for Peekwing (stdlib only).

  pwclient.py URL PIN stream OUTDIR [seconds] [w] [q] [fps]   save frames from the live stream
  pwclient.py URL PIN shot OUT.jpg [w]                         one frame
  pwclient.py URL PIN input '[{"t":"click","x":100,"y":100}]'  batched input (control must be on)
"""
import json, os, sys, time, urllib.request


def req(url, pin, path, data=None):
    r = urllib.request.Request(url + path, data=None if data is None else json.dumps(data).encode(),
                               headers={"X-Pin": pin})
    return urllib.request.urlopen(r, timeout=30)


def stream(url, pin, outdir, seconds=10, w=800, q=45, fps=10):
    os.makedirs(outdir, exist_ok=True)
    resp = req(url, pin, "/stream?w=%d&q=%d&fps=%d" % (w, q, fps))
    t0, n = time.time(), 0
    while time.time() - t0 < seconds:
        length = None
        while True:  # part headers
            line = resp.readline()
            if not line:
                return n, time.time() - t0
            line = line.strip()
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
            if line == b"" and length is not None:
                break
        data = resp.read(length)
        resp.readline()
        with open(os.path.join(outdir, "f%04d.jpg" % n), "wb") as f:
            f.write(data)
        n += 1
    resp.close()
    return n, time.time() - t0


if __name__ == "__main__":
    url, pin, cmd = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
    if cmd == "stream":
        a = sys.argv[5:]
        n, secs = stream(url, pin, sys.argv[4], *[int(x) for x in a])
        print("%d frames in %.1fs = %.1f fps" % (n, secs, n / secs))
    elif cmd == "shot":
        w = sys.argv[5] if len(sys.argv) > 5 else "1280"
        open(sys.argv[4], "wb").write(req(url, pin, "/screen?w=" + w).read())
    elif cmd == "input":
        print(req(url, pin, "/input", {"w": 1280, "ops": json.loads(sys.argv[4])}).read().decode())
