#!/usr/bin/env bash
# Auto-deploy PNL bot from kofman88/adjuster.
#
# Usage:
#   BOT_TOKEN='...' bash deploy.sh           # pulls main
#   BOT_TOKEN='...' BRANCH=foo bash deploy.sh # pulls a specific branch
#
# Safe to run while a different bot (e.g. MAIN_BOT) is running on the same
# server — the kill step matches by working directory ($HOME/pnlbot), not by
# process name.

set -e

PNLBOT_DIR="${PNLBOT_DIR:-$HOME/pnlbot}"
BRANCH="${BRANCH:-main}"
TMP_DIR="/tmp/adjuster_deploy_$$"

if [ -z "${BOT_TOKEN:-}" ]; then
  echo "ERROR: BOT_TOKEN env var is required." >&2
  echo "Usage: BOT_TOKEN='...' bash deploy.sh" >&2
  exit 1
fi

echo "=== PNL bot deploy ==="
echo "Target dir : $PNLBOT_DIR"
echo "Source     : kofman88/adjuster @ $BRANCH"
echo

# 1. Stop only the PNL bot (cwd-matched, leaves any other bot alone).
echo "→ Stopping current PNL bot…"
killed=0
for pid in $(pgrep -f "python3 main.py" 2>/dev/null || true); do
  cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
  if [ "$cwd" = "$PNLBOT_DIR" ]; then
    kill "$pid" 2>/dev/null && killed=$((killed + 1))
  fi
done
echo "  killed $killed instance(s) with cwd=$PNLBOT_DIR"
sleep 2

# 2. Backup runtime data so a botched copy doesn't lose user records.
echo "→ Backing up access.json + referrals.json…"
backup_dir="$PNLBOT_DIR/_data_backup"
mkdir -p "$backup_dir"
[ -f "$PNLBOT_DIR/access.json"    ] && cp "$PNLBOT_DIR/access.json"    "$backup_dir/"
[ -f "$PNLBOT_DIR/referrals.json" ] && cp "$PNLBOT_DIR/referrals.json" "$backup_dir/"

# 3. Fresh shallow clone.
echo "→ Cloning kofman88/adjuster:$BRANCH…"
rm -rf "$TMP_DIR"
git clone --depth 1 -b "$BRANCH" \
  https://github.com/kofman88/adjuster.git "$TMP_DIR" >/dev/null

SRC="$TMP_DIR/PNLGENERATOR/tg_trade_bot"
if [ ! -f "$SRC/main.py" ]; then
  echo "ERROR: $SRC/main.py missing — repo layout changed?" >&2
  exit 1
fi

# 4. Overlay bot files onto $PNLBOT_DIR (no rsync dependency).
echo "→ Copying bot files…"
mkdir -p "$PNLBOT_DIR/utils" "$PNLBOT_DIR/configs" \
         "$PNLBOT_DIR/assets/bingx" "$PNLBOT_DIR/assets/bybit" \
         "$PNLBOT_DIR/fonts"

# Always-overwrite (the *bot code*).
cp "$SRC/main.py"             "$PNLBOT_DIR/main.py"
cp "$SRC/access.py"           "$PNLBOT_DIR/access.py"
cp "$SRC/i18n.py"             "$PNLBOT_DIR/i18n.py"
cp "$SRC/utils/draw_text.py"  "$PNLBOT_DIR/utils/draw_text.py"
cp "$SRC/utils/pnl_card.py"   "$PNLBOT_DIR/utils/pnl_card.py"
cp "$SRC/configs/__init__.py" "$PNLBOT_DIR/configs/__init__.py"
cp "$SRC/configs/fonts.py"    "$PNLBOT_DIR/configs/fonts.py"
cp "$SRC/configs/layout.py"   "$PNLBOT_DIR/configs/layout.py"
[ -f "$SRC/calibrate.py" ] && cp "$SRC/calibrate.py" "$PNLBOT_DIR/calibrate.py" || true

# Add-only (assets/fonts) — keep any local customisations.
for ext in png jpg JPG; do
  for f in "$SRC/assets/bingx/"*.$ext; do
    [ -e "$f" ] && cp -n "$f" "$PNLBOT_DIR/assets/bingx/" 2>/dev/null || true
  done
  for f in "$SRC/assets/bybit/"*.$ext; do
    [ -e "$f" ] && cp -n "$f" "$PNLBOT_DIR/assets/bybit/" 2>/dev/null || true
  done
done
for ext in ttf otf; do
  for f in "$SRC/fonts/"*.$ext; do
    [ -e "$f" ] && cp -n "$f" "$PNLBOT_DIR/fonts/" 2>/dev/null || true
  done
done

# 5. Update Python deps (quiet if already satisfied).
echo "→ pip install requirements…"
pip3 install -q -r "$TMP_DIR/PNLGENERATOR/requirements.txt" || \
  echo "  warning: pip install failed; continuing with existing deps"

# 6. Restart bot.
echo "→ Starting bot…"
cd "$PNLBOT_DIR"
BOT_TOKEN="$BOT_TOKEN" nohup python3 main.py > bot.log 2>&1 &
new_pid=$!
echo "$new_pid" > bot.pid

# 7. Verify it didn't die immediately and isn't conflicted.
sleep 3
if ! kill -0 "$new_pid" 2>/dev/null; then
  echo
  echo "FAILED — bot died on startup. Tail of bot.log:"
  tail -30 bot.log
  exit 1
fi
if grep -q "TelegramConflictError" bot.log 2>/dev/null; then
  echo
  echo "WARN: TelegramConflictError — another instance is using the same BOT_TOKEN"
  tail -20 bot.log
  exit 1
fi

echo
echo "=== Deploy OK ==="
echo "PID: $new_pid"
echo
tail -10 bot.log

# 8. Cleanup tmp clone.
rm -rf "$TMP_DIR"
