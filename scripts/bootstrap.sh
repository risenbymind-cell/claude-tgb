#!/usr/bin/env bash
#
# One command from a fresh Ubuntu/Debian server to a running bot.
#
#   curl -fsSL https://raw.githubusercontent.com/<you>/claude-tgb/main/scripts/bootstrap.sh | sudo bash
#
# or, having already cloned:
#
#   sudo ./scripts/bootstrap.sh
#
# It installs dependencies, creates a service user, runs the setup wizard for
# the three values only you can supply, verifies everything with the preflight,
# and installs both systemd units. It is safe to re-run: existing config and
# data are left alone.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/directionalbot}"
APP_USER="${APP_USER:-directionalbot}"
REPO="${REPO:-https://github.com/risenbymind-cell/claude-tgb.git}"
BRANCH="${BRANCH:-main}"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo."

bold $'\nDirectionalBot bootstrap'
echo "  target: $APP_DIR"
echo

# ---------------------------------------------------------------- packages --
bold "1. System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates >/dev/null
ok "python3 $(python3 -V 2>&1 | awk '{print $2}'), git, venv"

# ------------------------------------------------------------------- user ---
bold $'\n2. Service user'
if id "$APP_USER" &>/dev/null; then
  ok "$APP_USER already exists"
else
  useradd --system --create-home --home-dir "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
  ok "created $APP_USER (no login shell)"
fi

# ------------------------------------------------------------------- code ---
bold $'\n3. Code'
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  git -C "$APP_DIR" checkout --quiet "$BRANCH"
  git -C "$APP_DIR" pull --quiet --ff-only origin "$BRANCH" || warn "could not fast-forward; leaving as-is"
  ok "updated $APP_DIR"
elif [ -f "$(dirname "$0")/../kbot/__main__.py" ]; then
  SRC="$(cd "$(dirname "$0")/.." && pwd)"
  if [ "$SRC" != "$APP_DIR" ]; then
    mkdir -p "$APP_DIR"
    cp -r "$SRC/." "$APP_DIR/"
    ok "copied from $SRC"
  else
    ok "already in place"
  fi
else
  git clone --quiet --branch "$BRANCH" "$REPO" "$APP_DIR"
  ok "cloned $REPO"
fi

# --------------------------------------------------------------- venv ------
bold $'\n4. Python environment'
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
ok "dependencies installed"

mkdir -p "$APP_DIR/data" "$APP_DIR/secrets"
chmod 700 "$APP_DIR/secrets"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ------------------------------------------------------------------ config --
bold $'\n5. Configuration'
if [ -f "$APP_DIR/.env" ]; then
  ok ".env already exists — keeping it"
else
  echo "  The wizard needs three things only you can supply:"
  echo "    · a Telegram bot token   (https://t.me/BotFather)"
  echo "    · your Telegram user ID  (https://t.me/userinfobot)"
  echo "    · production or demo"
  echo "  Everything else is generated."
  echo
  ( cd "$APP_DIR" && "$APP_DIR/.venv/bin/python" -m kbot.tools setup )
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

# --------------------------------------------------------------- preflight --
bold $'\n6. Preflight'
set +e
( cd "$APP_DIR" && sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m kbot.tools doctor )
DOCTOR=$?
set -e
[ "$DOCTOR" -le 0 ] || warn "preflight reported problems (exit $DOCTOR) — fix them before starting"

# ----------------------------------------------------------------- systemd --
bold $'\n7. Services'
for unit in directionalbot directionalbot-recorder; do
  src="$APP_DIR/deploy/$unit.service"
  [ -f "$src" ] || { warn "$unit.service not found, skipping"; continue; }
  sed -e "s#/opt/directionalbot#$APP_DIR#g" \
      -e "s#User=directionalbot#User=$APP_USER#" \
      -e "s#Group=directionalbot#Group=$APP_USER#" \
      "$src" > "/etc/systemd/system/$unit.service"
  ok "installed $unit.service"
done
systemctl daemon-reload

if [ "$DOCTOR" -eq 0 ]; then
  systemctl enable --now directionalbot >/dev/null 2>&1 && ok "bot started"
  systemctl enable --now directionalbot-recorder >/dev/null 2>&1 && ok "recorder started"
else
  systemctl enable directionalbot >/dev/null 2>&1
  systemctl enable directionalbot-recorder >/dev/null 2>&1
  warn "services enabled but not started — preflight had problems"
fi

# -------------------------------------------------------------------- done --
bold $'\nDone.'
cat <<EOF

  Status      systemctl status directionalbot
  Logs        journalctl -u directionalbot -f
  Recorder    journalctl -u directionalbot-recorder -f

  Now message your bot on Telegram:

    /start
    /genkeys lifetime 1      mint yourself a key
    /redeem <that key>       activate it

  You start in paper mode. Nothing reaches Kalshi until you connect an API
  key with /connect and switch to Live.

  The recorder is already collecting. In about two weeks:

    cd $APP_DIR && .venv/bin/python -m kbot.research calibrate
    .venv/bin/python -m kbot.research replay --strategy drift

  \033[1mBack up $APP_DIR/.env\033[0m — the MASTER_KEY in it is the only thing
  that can decrypt your users' stored Kalshi credentials.

EOF
