#!/bin/bash

echo "🐛 ОТЛАДКА БОТА С LEVERAGE"
echo "========================="

# Остановить текущий бот
echo "⏹️  Остановка текущего бота..."
pkill -f "python.*bot"
sleep 2

# Запустить отладочную версию
echo "🚀 Запуск отладочной версии..."
python3 bot2_debug.py &
PYTHON_PID=$!
sleep 3

echo "✅ Отладочный бот запущен (PID: $PYTHON_PID)"
echo "📝 Файл логов: /var/www/phyton/bot/botttss/logs_debug.txt"
echo ""
echo "🔍 Отслеживание логов (нажмите Ctrl+C для выхода):"
echo "=================================================="

# Показать логи
tail -f /var/www/phyton/bot/botttss/logs_debug.txt