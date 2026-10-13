#!/usr/bin/env bash
# Svitlo bot installer for Ubuntu (DigitalOcean droplet).
# Usage:  curl -fsSL https://raw.githubusercontent.com/EsplateOnej/svitlo-bot/main/install.sh | bash
# Re-run any time to update the bot. Add "-s -- --reconfigure" to re-enter the settings.
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

if [ ! -f "$DATA_DIR/state.json" ]; then
  # continue from the state the GitHub version kept, so nothing is re-announced
  curl -fsSL "$REPO_RAW/state.json?$(date +%s)" -o "$DATA_DIR/state.json" || echo "{}" > "$DATA_DIR/state.json"
fi
chown -R svitlo:svitlo "$DATA_DIR"

if [ ! -f "$ENV_FILE" ] || [ "${1:-}" = "--reconfigure" ]; then
  echo
  echo "==> Настройка (ввод скрыт, вставляй через правый клик или Ctrl+Shift+V)"
  read -rsp "Токен Telegram-бота: " TG_TOKEN </dev/tty; echo
  read -rp  "Email второго аккаунта Marstek (Enter — пропустить): " MS_EMAIL </dev/tty
  MS_PWD=""
  if [ -n "$MS_EMAIL" ]; then read -rsp "Пароль Marstek: " MS_PWD </dev/tty; echo; fi
  read -rp  "EcoFlow Access Key (Enter — пропустить): " EF_AK </dev/tty
  EF_SK=""; EF_SN=""; EF_NAME=""; POWER_SOURCE=""
  if [ -n "$EF_AK" ]; then
    read -rsp "EcoFlow Secret Key: " EF_SK </dev/tty; echo
    read -rp  "Серийный номер EcoFlow (SN): " EF_SN </dev/tty
    read -rp  "Как называть станцию в сообщениях (например Delta 2): " EF_NAME </dev/tty
    read -rp  "EcoFlow подключена к розетке и по ней определять, есть ли свет? (y/n): " YN </dev/tty
    case "$YN" in [yYдД]*) POWER_SOURCE=ecoflow ;; esac
  fi
  read -rsp "Ключ alerts.in.ua для воздушной тревоги (Enter — пропустить): " ALERTS </dev/tty; echo
  umask 077
  cat > "$ENV_FILE" <<EOF
TG_TOKEN=$TG_TOKEN
TG_CHAT_ID=-5549498327
QUEUE=18.1
MARSTEK_EMAIL=$MS_EMAIL
MARSTEK_PASSWORD=$MS_PWD
ECOFLOW_ACCESS_KEY=$EF_AK
ECOFLOW_SECRET_KEY=$EF_SK
ECOFLOW_SN=$EF_SN
ECOFLOW_NAME=$EF_NAME
POWER_SOURCE=$POWER_SOURCE
ALERTS_TOKEN=$ALERTS
STATE_FILE=$DATA_DIR/state.json
EOF
  chmod 600 "$ENV_FILE"
fi

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
systemctl enable --now svitlo-bot >/dev/null 2>&1
systemctl restart svitlo-bot
sleep 5
echo
if systemctl is-active --quiet svitlo-bot; then
  echo "✅ Бот запущен. Отправь /graf в семейный чат."
  echo "   Логи:      journalctl -u svitlo-bot -f"
  echo "   Настройки: sudo nano $ENV_FILE  (потом: sudo systemctl restart svitlo-bot)"
else
  echo "❌ Бот не запустился. Последние строки лога:"
  journalctl -u svitlo-bot -n 30 --no-pager
fi
