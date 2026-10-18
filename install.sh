#!/bin/sh
# Peekwing installer: user space only, no admin, no sudo.
set -e
D="$HOME/CrunchByte/Peekwing"
mkdir -p "$D"
cd "$D"
R="https://raw.githubusercontent.com/Greenisus1/peekwing/main"
curl -fsSL -o peekwing.py "$R/peekwing.py"
if [ ! -x cloudflared ]; then
  if [ -x "$HOME/CrunchByte/PiRemote/cloudflared" ]; then
    cp "$HOME/CrunchByte/PiRemote/cloudflared" cloudflared
  else
    case "$(uname -m)" in arm64) A=arm64;; *) A=amd64;; esac
    curl -fsSL -o cf.tgz "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-$A.tgz"
    tar -xzf cf.tgz cloudflared
    rm -f cf.tgz
    chmod +x cloudflared
  fi
fi
cat > Peekwing.command <<'EOC'
#!/bin/sh
cd "$HOME/CrunchByte/Peekwing"
exec python3 peekwing.py "$@"
EOC
chmod +x Peekwing.command
cat > peekwing <<'EOC'
#!/bin/sh
exec python3 "$HOME/CrunchByte/Peekwing/peekwing.py" "$@"
EOC
chmod +x peekwing
echo
echo "Installed in $D"
echo "Start:  ~/CrunchByte/Peekwing/Peekwing.command"
echo "Check permissions first:  python3 ~/CrunchByte/Peekwing/peekwing.py --check"
echo "Always-on mode:  ~/CrunchByte/Peekwing/peekwing install"
