#!/usr/bin/env python3
"""Hurdlebot: optional auto-jump helper for the Hurdle Jumper arcade cabinet (Sneaky Sasquatch).

Runs on your Mac in user space (no admin). It watches a small part of the screen, finds the yellow
hurdle, measures how fast it moves, and clicks the cabinet's red button just before it reaches the
runner. It never clicks anywhere else and never inserts coins: YOU click the coin slot, it plays the round.

  python3 hurdlebot.py                 # play rounds until Ctrl+C (max 3 rounds, 3 minutes by default)
  python3 hurdlebot.py --plays 5       # stop after 5 rounds (this is your coin cap: 5 coins per round)
  python3 hurdlebot.py --calibrate     # look at the screen for 5 s and print what it sees, no clicks
  python3 hurdlebot.py --dry           # detect and log, never click

Stop at any time: Ctrl+C in this window (or close it). Needs Screen Recording and Accessibility for Terminal.
"""
import argparse, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(os.path.expanduser("~"), "CrunchByte", "Peekwing"))

# Where the cabinet screen sits, as fractions of the whole screen (x, y, w, h) at default zoom.
REGION = (0.246, 0.034, 0.508, 0.451)
BUTTON = (0.504, 0.588)       # the big red button, fractions of the screen
GRAB_W = 320                  # capture width of the region, small = fast
BAND = (0.50, 0.95)           # rows (fraction of region height) where hurdles run
SCAN_STEP = 2
LEAD_MIN, LEAD_MAX = 0.05, 0.25   # tuning never leaves this range (seconds before contact)
# Speed model measured from a recording: hurdle speed in a 394 px wide cabinet screen is
# about 174 px/s at the first hurdle and grows about 6 px/s every second after it.
SPEED0, SPEED_GAIN, SPEED_REF_W = 174.0, 6.0, 394.0


def is_yellow(r, g, b):
    return r > 200 and g > 150 and b < 125 and r - b > 105 and g > 0.62 * r


def is_brown(r, g, b):
    return 70 < r < 150 and 40 < g < 100 and 20 < b < 75 and r > g > b


def find_runs(cols, gap=3):
    runs, start, prev = [], None, None
    for x in cols:
        if start is None:
            start = prev = x
        elif x - prev <= gap:
            prev = x
        else:
            runs.append((start, prev))
            start = prev = x
    if start is not None:
        runs.append((start, prev))
    return runs


def analyze(px, w, h):
    """px = RGBX bytes of the region. Returns (runner_right_x or None, [(x0, x1) yellow runs in the lane])."""
    y0, y1 = int(h * BAND[0]), int(h * BAND[1])
    ycols, bcols = set(), set()
    for y in range(y0, y1, SCAN_STEP):
        row = y * w * 4
        for x in range(0, w, SCAN_STEP):
            i = row + x * 4
            r, g, b = px[i], px[i + 1], px[i + 2]
            if is_yellow(r, g, b):
                ycols.add(x)
            elif x < w * 0.5 and is_brown(r, g, b):
                bcols.add(x)
    runner = max(bcols) if len(bcols) >= 4 else None
    return runner, find_runs(sorted(ycols))


class Bot:
    def __init__(self, grab, click, clock=time.time, lead=0.12, plays=3, tune=True, dry=False, say=print):
        self.grab, self.click, self.clock = grab, click, clock
        self.lead, self.plays, self.tune, self.dry, self.say = lead, plays, tune, dry, say
        self.lead0 = lead
        self.scores = {}
        # try the starting lead, then spiral outward; afterwards stick with the best one
        # (measured from a real run: cleared jumps left the ground -0.10..+0.23 s around contact)
        base = min(LEAD_MAX, max(LEAD_MIN, lead))
        self.candidates = []
        for d in (0, -0.03, 0.03, -0.06, 0.06, -0.09, 0.09):
            c = round(min(LEAD_MAX, max(LEAD_MIN, base + d)), 3)
            if c not in self.candidates:
                self.candidates.append(c)
        self.reset_round()
        self.round_no = 0
        self.runner_x = None

    def reset_round(self):
        self.samples, self.jumped, self.cleared = [], False, 0
        self.seen_moving, self.last_move_t, self.stuck_since = False, None, None
        self.last_seen_t = None
        self.round_t0 = None          # time of the first hurdle seen this round (speed model clock)

    def seed_v(self, t, w):
        """Expected hurdle speed (px/s, negative = leftwards) at time t, scaled to this capture width."""
        age = 0.0 if self.round_t0 is None else max(0.0, t - self.round_t0)
        return -(SPEED0 + SPEED_GAIN * age) * w / SPEED_REF_W

    def fit_v(self):
        s = self.samples[-5:]
        if len(s) < 3:
            return None
        return (s[-1][1] - s[0][1]) / (s[-1][0] - s[0][0] + 1e-9)

    def tick(self):
        """One frame. Returns 'round_over' when the round finished, else None."""
        t0 = self.clock()
        px, w, h = self.grab()
        t = self.clock()
        rx, runs = analyze(px, w, h)
        if rx is not None:
            self.runner_x = rx if self.runner_x is None else self.runner_x * 0.9 + rx * 0.1
        rxr = self.runner_x if self.runner_x is not None else w * 0.2
        ahead = [r for r in runs if r[0] > rxr + 2 and (r[1] - r[0]) < w * 0.25]
        if ahead:
            front = min(ahead, key=lambda r: r[0])[0]
            if self.samples and front > self.samples[-1][1] + 25:   # a new hurdle appeared at the right
                self.samples, self.jumped = [], False
            self.samples.append((t, front))
            self.samples = self.samples[-8:]
            self.last_seen_t = t
            if self.round_t0 is None:
                self.round_t0 = t
            v = self.fit_v()
            moving = v is not None and v < -20
            if moving:
                self.seen_moving, self.last_move_t, self.stuck_since = True, t, None
            vuse = v if moving else self.seed_v(t, w)     # fire on the first sighting using the model speed
            if v is None or moving:
                est = front + vuse * (self.clock() - t)
                t_hit = (est - rxr) / -vuse
                if not self.jumped and t_hit <= self.lead:
                    self.jumped = True
                    if not self.dry:
                        self.click()
                    self.say("jump: hurdle %.0fpx away, %.0f px/s (%s), %.2fs to contact" % (est - rxr, -vuse, "measured" if moving else "model", t_hit))
            elif v is not None and self.seen_moving:
                self.stuck_since = self.stuck_since or t        # hurdle stopped moving: the runner hit it
                if t - self.stuck_since > 0.8:
                    return self.end_round("fault")
        else:
            if self.samples and self.jumped and self.seen_moving:
                self.cleared += 1
                self.say("cleared hurdle %d" % self.cleared)
            self.samples, self.jumped, self.stuck_since = [], False, None
            if self.seen_moving and self.last_seen_t and t - self.last_seen_t > 6:
                return self.end_round("quiet")
        return None

    def end_round(self, why):
        self.round_no += 1
        self.say("round %d over (%s): cleared %d hurdles, lead %.2fs" % (self.round_no, why, self.cleared, self.lead))
        if self.tune:
            self.scores[round(self.lead, 3)] = max(self.cleared, self.scores.get(round(self.lead, 3), 0))
            nxt = [c for c in self.candidates if round(c, 3) not in self.scores]
            self.lead = nxt[0] if nxt else max(self.scores, key=lambda k: (self.scores[k], -abs(k - self.lead0)))
            self.say("scores so far %s -> next lead %.2fs" % (self.scores, self.lead))
        self.reset_round()
        return "round_over"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Auto-jump helper for Hurdle Jumper.")
    ap.add_argument("--plays", type=int, default=3, help="stop after this many rounds (coin cap, 5 coins each)")
    ap.add_argument("--seconds", type=int, default=180, help="hard time limit")
    ap.add_argument("--lead", type=float, default=0.12, help="press this many seconds before contact")
    ap.add_argument("--no-tune", action="store_true", help="keep --lead fixed between rounds")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    if sys.platform != "darwin":
        print("Hurdlebot runs on macOS.")
        return 1
    import peekwing as pw
    cap, inp = pw.FastCapture(), pw.FastInput()
    be = pw.MacBackend()
    sw, sh = be.size

    def grab():
        return cap.pixels(REGION, GRAB_W)

    def click():
        inp.run([{"t": "click", "x": BUTTON[0] * sw, "y": BUTTON[1] * sh}])

    print("Hurdlebot: Ctrl+C stops it. It only clicks the red button. YOU click the coin slot each round.")
    if a.calibrate:
        t0 = time.time()
        while time.time() - t0 < 5:
            px, w, h = grab()
            rx, runs = analyze(px, w, h)
            print("runner right edge x=%s, yellow runs=%s (region %dx%d)" % (rx, runs, w, h))
            time.sleep(0.4)
        return 0
    bot = Bot(lambda: grab(), click, lead=a.lead, plays=a.plays, tune=not a.no_tune, dry=a.dry)
    t0, frames = time.time(), 0
    try:
        while time.time() - t0 < a.seconds and bot.round_no < a.plays:
            bot.tick()
            frames += 1
    except KeyboardInterrupt:
        pass
    print("stopped: %d rounds, %.0f frames/s" % (bot.round_no, frames / max(1e-9, time.time() - t0)))
    be.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
