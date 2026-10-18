import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "extras"))
import hurdlebot as hb

W, H = 320, 180
RUNNER_R = 64


class Sim:
    """Tiny fake cabinet: hurdles run left at an increasing speed; a click makes the runner airborne."""
    def __init__(self, v0=140, acc=25, period=1.8, air=(0.08, 0.60), decoy_until=1.0, latency=0.03):
        self.t, self.v0, self.acc, self.period, self.air, self.decoy_until, self.lat = 0.0, v0, acc, period, air, decoy_until, latency
        self.spawn = 1.2
        self.jump_at = None
        self.frozen_since = None
        self.cleared = self.faults = 0
        self.counted = False
        self.clicks = []
        self.base = bytearray()
        for y in range(H):
            col = (110, 170, 230) if y < H * 0.45 else (60, 160, 70)
            self.base += (bytes(col) + b"\x00") * W

    def x(self, dt):
        return 300 - self.v0 * dt - 0.5 * self.acc * dt * dt

    def resolve(self):
        dt = self.t - self.spawn
        if dt < 0:
            return
        if self.frozen_since is None and self.x(dt) <= RUNNER_R:
            if self.jump_at is not None and self.jump_at + self.air[0] <= self.t <= self.jump_at + self.air[1]:
                self.cleared += 1
                self.spawn = self.t + self.period            # hurdle passed, next one later
                self.jump_at = None
            else:
                self.faults += 1
                self.frozen_since = self.t
        if self.frozen_since is not None and self.t - self.frozen_since > 1.6:
            self.frozen_since = None
            self.spawn = self.t + 2.0                          # new round after a pause
            self.v0, self.acc = 140, 25

    def click(self):
        self.clicks.append(self.t)
        self.jump_at = self.t

    def frame(self):
        self.resolve()
        px = bytearray(self.base)
        def rect(x0, x1, y0, y1, col):
            x0, x1 = max(0, int(x0)), min(W, int(x1))
            if x1 <= x0:
                return
            row = bytes(col) * 0 + b"".join(bytes(col) + b"\x00" for _ in range(x1 - x0))
            for y in range(int(y0), int(y1)):
                px[(y * W + x0) * 4:(y * W + x1) * 4] = row
        rect(40, RUNNER_R + 1, H * 0.55, H * 0.90, (105, 70, 45))
        if self.t < self.decoy_until:                          # static yellow title text far ahead
            rect(150, 230, H * 0.65, H * 0.72, (240, 225, 90))
        dt = self.t - self.spawn
        if dt >= 0:
            hx = max(self.x(dt), RUNNER_R + 3) if self.frozen_since is None else RUNNER_R + 3
            rect(hx, hx + 14, H * 0.58, H * 0.66, (240, 195, 50))   # plank
            rect(hx + 5, hx + 9, H * 0.66, H * 0.80, (35, 35, 40))  # post
        self.t += self.lat
        return bytes(px), W, H


def run(sim, lead, rounds=3, tune=False, seconds=60):
    bot = hb.Bot(sim.frame, sim.click, clock=lambda: sim.t, lead=lead, plays=rounds, tune=tune, say=lambda *a: None)
    while sim.t < seconds and bot.round_no < rounds:
        bot.tick()
    return bot


class T(unittest.TestCase):
    def test_clears_hurdles_at_increasing_speed(self):
        sim = Sim()
        bot = run(sim, 0.30, rounds=1, seconds=14)
        self.assertGreaterEqual(sim.cleared, 4)
        self.assertEqual(sim.faults, 0, "clicks at %s" % sim.clicks)

    def test_fast_start_speed(self):
        sim = Sim(v0=300, acc=40)
        run(sim, 0.30, rounds=1, seconds=10)
        self.assertGreaterEqual(sim.cleared, 3)
        self.assertEqual(sim.faults, 0)

    def test_one_click_per_hurdle_and_none_for_title_text(self):
        sim = Sim()
        run(sim, 0.30, rounds=1, seconds=8)
        self.assertEqual(len(sim.clicks), sim.cleared)
        self.assertTrue(all(c > 1.2 for c in sim.clicks))

    def test_dry_mode_never_clicks(self):
        sim = Sim()
        bot = hb.Bot(sim.frame, sim.click, clock=lambda: sim.t, lead=0.3, dry=True, say=lambda *a: None)
        while sim.t < 6:
            bot.tick()
        self.assertEqual(sim.clicks, [])

    def test_tuning_recovers_from_a_bad_start(self):
        sim = Sim()
        bot = run(sim, 0.70, rounds=4, tune=True, seconds=40)    # 0.70 s early misses the 0.08-0.60 s air window
        self.assertEqual(bot.scores.get(0.7), 0)                  # first round at the bad lead faulted at once
        self.assertLessEqual(bot.lead, 0.62)                           # then it moved to a working lead
        self.assertGreaterEqual(sim.cleared, 3)                   # and kept clearing hurdles

    def test_defaults_and_tuning_bounds(self):
        bot = hb.Bot(lambda: None, lambda: None, say=lambda *a: None)
        self.assertAlmostEqual(bot.lead, 0.12)
        self.assertTrue(all(hb.LEAD_MIN <= c <= hb.LEAD_MAX for c in bot.candidates))
        self.assertEqual(bot.candidates[0], 0.12)
        self.assertEqual(len(bot.candidates), len(set(bot.candidates)))
        steps = sorted(set(round(abs(c - 0.12), 3) for c in bot.candidates))
        self.assertEqual(steps, [0.0, 0.03, 0.06, 0.07, 0.09])   # -0.09 clamps to the 0.05 floor

    def test_speed_seed_grows_and_scales(self):
        bot = hb.Bot(lambda: None, lambda: None, say=lambda *a: None)
        bot.round_t0 = 0.0
        self.assertAlmostEqual(bot.seed_v(0.0, 394), -174.0)
        self.assertAlmostEqual(bot.seed_v(10.0, 394), -234.0)
        self.assertAlmostEqual(bot.seed_v(0.0, 320), -174.0 * 320 / 394)

    def test_default_lead_clears_and_fires_without_measured_speed(self):
        sim = Sim(period=1.2)
        run(sim, 0.12, rounds=1, seconds=14)
        self.assertGreaterEqual(sim.cleared, 4)
        self.assertEqual(sim.faults, 0, "clicks at %s" % sim.clicks)

    def test_fault_ends_round(self):
        sim = Sim()
        bot = run(sim, 0.9, rounds=1, seconds=30)
        self.assertEqual(bot.round_no, 1)
        self.assertGreaterEqual(sim.faults, 1)


if __name__ == "__main__":
    unittest.main()
