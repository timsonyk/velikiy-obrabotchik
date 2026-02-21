#!/bin/bash
# Скрипт деплоя на сервер

SERVER="root@SERVER_IP"
REMOTE_DIR="/root/obrabotchik-dite"

echo "1. Убиваю локальный бот (если есть)..."
pkill -9 -f "python.*bot.py" 2>/dev/null || true

echo "2. Копирую файлы на сервер..."
scp bot.py config.py image_processor.py "$SERVER:$REMOTE_DIR/"

echo "3. Перезапускаю бот на сервере..."
ssh "$SERVER" "systemctl restart obrabotchik-bot"

echo "4. Проверяю статус..."
sleep 3
ssh "$SERVER" "systemctl status obrabotchik-bot | head -10"

echo ""
echo "Готово!"
