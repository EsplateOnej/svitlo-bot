#!/usr/bin/env bash
# Svitlo bot installer for Ubuntu (DigitalOcean droplet).
# Usage:  curl -fsSL https://raw.githubusercontent.com/EsplateOnej/svitlo-bot/main/install.sh | bash
# Re-run any time to update the bot (settings are kept).
# Settings are changed with:  svitlo-set NAME 'value'
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/EsplateOnej/svitlo-bot/main"
ENV_FILE=/etc/svitlo-bot.env
APP_DIR=/opt/svitlo-bot
DATA_DIR=/var/lib/svitlo-bot

if [ "$(id -u)" -ne 0 ]; then echo "Запусти от root (или через sudo)"; exit 1; fi

echo "==> Устанавливаю зависимости..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 tzdata curl ca-certificates >/dev/null

id svitlo >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin svitlo
mkdir -p "$APP_DIR" "$DATA_DIR"

echo "==> Скачиваю бота..."
curl -fsSL "$REPO_RAW/bot.py?$(date +%s)" -o "$APP_DIR/bot.py"
python3 -m py_compile "$APP_DIR/bot.py"

if [ ! -s "$DATA_DIR/state.json" ]; then
  # continue from the state the GitHub version kept, so nothing is re-announced
  curl -fsSL "$REPO_RAW/state.json?$(date +%s)" -o "$DATA_DIR/state.json" || echo "{}" > "$DATA_DIR/state.json"
fi
chown -R svitlo:svitlo "$DATA_DIR"

if [ ! -f "$ENV_FILE" ]; then
  umask 077
  cat > "$ENV_FILE" <<EOF
TG_TOKEN=
TG_CHAT_ID=-5549498327
QUEUE=18.1
MARSTEK_EMAIL=
MARSTEK_PASSWORD=
ECOFLOW_ACCESS_KEY=
ECOFLOW_SECRET_KEY=
ECOFLOW_SN=
ECOFLOW_NAME=EcoFlow
POWER_SOURCE=
ALERTS_TOKEN=
STATE_FILE=$DATA_DIR/state.json
EOF
fi
chmod 600 "$ENV_FILE"

# helper: svitlo-set NAME 'value'  -> saves the setting and restarts the bot
cat > /usr/local/bin/svitlo-set <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
ENV_FILE=/etc/svitlo-bot.env
if [ $# -ne 2 ]; then echo "Использование: svitlo-set ИМЯ 'значение'"; exit 1; fi
name="$1"; value="$2"
case "$name" in *[!A-Z0-9_]*|"") echo "Неверное имя: $name"; exit 1;; esac
tmp=$(mktemp)
grep -v "^${name}=" "$ENV_FILE" > "$tmp" || true
esc=${value//\\/\\\\}; esc=${esc//\"/\\\"}
printf '%s="%s"\n' "$name" "$esc" >> "$tmp"
cat "$tmp" > "$ENV_FILE"; rm -f "$tmp"
echo "✅ $name сохранён"
if grep -Eq '^TG_TOKEN="?[^" ]' "$ENV_FILE"; then
  systemctl restart svitlo-bot
  sleep 4
  if systemctl is-active --quiet svitlo-bot; then echo "✅ Бот работает"
  else echo "❌ Бот не запустился:"; journalctl -u svitlo-bot -n 15 --no-pager; fi
fi
EOF
chmod 755 /usr/local/bin/svitlo-set

cat > /etc/systemd/system/svitlo-bot.service <<EOF
[Unit]
Description=Svitlo Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$ENV_FILE
User=svitlo
ExecStart=/usr/bin/python3 -u $APP_DIR/bot.py --serve
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable svitlo-bot >/dev/null 2>&1
echo
if grep -Eq '^TG_TOKEN="?[^" ]' "$ENV_FILE"; then
  systemctl restart svitlo-bot
  sleep 5
  if systemctl is-active --quiet svitlo-bot; then echo "✅ Бот обновлён и работает."
  else echo "❌ Бот не запустился:"; journalctl -u svitlo-bot -n 30 --no-pager; fi
else
  echo "✅ Установка завершена. Теперь задай токен бота командой:"
  echo "   svitlo-set TG_TOKEN 'сюда_токен'"
fi
