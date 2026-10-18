# Peekwing

Share your Mac screen with a helper, and optionally let them click and type. No admin rights to install, no router or firewall changes, nothing listening on your network. For everyone.

Built with AI assistance from my design. MIT license.

## Install (Mac, no admin)

    curl -fsSL -o /tmp/pw.sh https://raw.githubusercontent.com/Greenisus1/peekwing/main/install.sh && sh /tmp/pw.sh

## Use

    ~/CrunchByte/Peekwing/Peekwing.command

It prints an address and a 6-digit PIN (and copies them to your clipboard). Send both to your helper. Control (click and type) starts off.

- `c` + Enter: turn control (click and type) on or off. Off by default; only you can switch it on, from this window.
- `q` + Enter or Ctrl+C: stop everything. Closing the Terminal window also stops it.
- Duration: it asks how long to share when it starts (Enter = 30 minutes, `0` = no limit until you quit). Or pass `--minutes N` (`--minutes 0` = no limit). The banner shows the choice.
- 5 wrong PINs shut it down.
- Nothing is recorded or saved. Screenshots are made on demand and deleted right away.
- A notification and the open Terminal window show that sharing is on. Every helper action is printed there.

## Always-on mode (optional)

    ~/CrunchByte/Peekwing/peekwing install

Adds a login item (a user LaunchAgent in ~/Library/LaunchAgents, no admin) so the service starts when you log in. It runs only while your Mac is awake and you are logged in, uses an outbound tunnel only, and checks a long secret token stored only on your Mac (file mode 600).

**Everything is off until you switch it on, every time.** Viewing and control are timed windows that you start from Terminal and that end by themselves.

| Command | What it does |
|---|---|
| `peekwing view on [minutes]` | let your helper see the screen for a while (default 30, max 240), then it switches off by itself |
| `peekwing control on [minutes]` | also allow click and type for a while; only works while view is on |
| `peekwing view off` / `control off` | switch off right now (`view off` also ends control) |
| `peekwing token` | show and copy the secret (`token new` replaces it, old one stops working) |
| `peekwing status` | running or not, view and control state, current address, log path |
| `peekwing pause` / `resume` | stop or restart the whole service |
| `peekwing uninstall` | stop, remove the login item, erase token and files |

You get a macOS notification whenever a helper starts viewing or controlling after being idle, and the log lists every action (no screen contents, no secret). Wrong token 5 times locks it for 10 minutes. Without a Cloudflare named tunnel the address changes each time the service restarts; set `tunnel_token` and `hostname` in `config.json` for a stable one.

## macOS permissions (you tap these yourself)

macOS blocks screen capture and control until you allow them. In System Settings > Privacy & Security:

1. **Screen Recording**: turn on Terminal. Needed to see the screen.
2. **Accessibility**: turn on Terminal. Needed only for control.

macOS may ask for an administrator password (or Touch ID) when you flip these. Enter it yourself into macOS, never anywhere else. Quit and reopen Terminal after allowing. `python3 peekwing.py --check` shows what is on.

On a school or work managed Mac these switches can be locked.

## View-only without Terminal permissions

    python3 ~/CrunchByte/Peekwing/peekwing.py --browser

Open the printed `/share` link in your browser and pick a screen or window to share. Control is not available in this mode. Your browser may itself need the Screen Recording permission.

## Speed

Frames are grabbed in-process (CoreGraphics/ImageIO), which is much faster than the old `screencapture` path; if that is unavailable it falls back automatically and says so in the Terminal log. Measure your Mac: `python3 ~/CrunchByte/Peekwing/peekwing.py --bench`.

- `GET /stream?w=800&q=45&fps=10` is a live MJPEG stream (w 320-1600, q 10-90, fps 1-30). Unchanged screens send only a keep-alive frame per second.
- `POST /input {"w":1280,"ops":[...]}` runs up to 60 mouse/key ops in one call: `move, mdown, mup, click, drag, scroll, kdown, kup, wait` (keys by name such as `space`, `left`, `w`; waits max 1000 ms each, 5 s total). Needs control on.
- `tools/pwclient.py` records a stream to image files and prints the real frames per second.

## Helper API (for the person or agent on the other side)

Send the PIN in an `X-Pin` header.

- `GET /status`, `GET /screen?w=1280` (JPEG)
- `POST /click {"x":640,"y":360,"w":1280,"button":"left","count":1}` (x, y are in the screenshot's pixel space; `w` is the screenshot width)
- `POST /move`, `/drag {x,y,x2,y2}`, `/scroll {dy}`, `/type {"text":"hi"}`, `/key {"key":"c","mods":["cmd"]}`

## Tests

    python3 -m unittest tests/test_peekwing.py

The HTTP, PIN, lockout, expiry, view-only and coordinate logic are tested with a fake screen. The macOS capture and input calls (screencapture, sips, osascript) need a real Mac.

## Optional: Hurdle Jumper auto-jump (extras/hurdlebot.py)

A separate helper for the Hurdle Jumper arcade cabinet in Sneaky Sasquatch. It watches the cabinet screen, measures how fast the yellow hurdle is moving (the speed grows during a round), and clicks the red button just before contact. It only ever clicks the red button, never inserts coins (you click the coin slot) and stops with Ctrl+C.

    python3 ~/CrunchByte/Peekwing/extras/hurdlebot.py --calibrate   # prints what it sees, no clicks
    python3 ~/CrunchByte/Peekwing/extras/hurdlebot.py --dry         # detects and logs, no clicks
    python3 ~/CrunchByte/Peekwing/extras/hurdlebot.py --plays 3     # plays up to 3 rounds (3 coins' worth x 5), then stops

`--lead 0.30` is how many seconds before contact it presses. Between rounds it tries nearby values and keeps the best one (`--no-tune` to turn that off). Needs Screen Recording and Accessibility for Terminal.
The screen layout numbers (where the cabinet screen and button are) are estimates from screenshots at default zoom, so run `--calibrate` first.
