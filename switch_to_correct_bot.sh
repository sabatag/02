#!/bin/bash

echo "🔄 ПЕРЕКЛЮЧЕНИЕ НА ИСПРАВЛЕННЫЙ BOT2.PY"
echo "====================================="

# Показать текущие процессы
echo "📊 Текущие процессы Python:"
ps aux | grep python | grep bot | head -5

echo ""

# Остановить все боты
echo "⏹️  Остановка всех ботов..."
pkill -f "python.*bot"
sleep 3

# Проверить, что остановлены
REMAINING=$(ps aux | grep python | grep bot | grep -v grep | wc -l)
if [ $REMAINING -eq 0 ]; then
    echo "✅ Все боты остановлены"
else
    echo "⚠️  Остались процессы: $REMAINING"
    ps aux | grep python | grep bot | grep -v grep
fi

echo ""

# Показать различия между файлами
echo "📋 СРАВНЕНИЕ ФАЙЛОВ:"
echo "bot.py размер: $(wc -l < bot.py) строк"
echo "bot2.py размер: $(wc -l < bot2.py) строк"

echo ""

# Проверить ключевые различия
echo "🔍 ПРОВЕРКА КЛЮЧЕВЫХ РАЗЛИЧИЙ:"

if grep -q "unrealized_pnl.*leverage.*pnl_percent" bot2.py; then
    echo "✅ bot2.py содержит правильный расчет PNL с leverage"
else
    echo "❌ bot2.py не содержит правильный расчет PNL"
fi

if grep -q 'pnl={pnl:.4f}' bot.py; then
    echo "⚠️  bot.py использует старый расчет PNL (без leverage)"
else
    echo "✅ bot.py не использует старый расчет"
fi

echo ""

# Запустить правильный бот
echo "🚀 ЗАПУСК ПРАВИЛЬНОГО БОТА (bot2.py):"
echo "Запускаем bot2.py с leverage поддержкой..."

python3 bot2.py &
PYTHON_PID=$!
sleep 3

# Проверить запуск
if ps -p $PYTHON_PID > /dev/null; then
    echo "✅ bot2.py успешно запущен (PID: $PYTHON_PID)"
    echo "📝 Логи: /var/www/phyton/bot/botttss/logs.txt"
    echo ""
    echo "🔍 Мониторинг логов (первые 10 секунд):"
    echo "======================================"
    timeout 10 tail -f /var/www/phyton/bot/botttss/logs.txt | head -20
else
    echo "❌ Не удалось запустить bot2.py"
    exit 1
fi

echo ""
echo "✅ ПЕРЕКЛЮЧЕНИЕ ЗАВЕРШЕНО!"
echo "Теперь ваш бот использует правильный расчет PNL с leverage"
echo ""
echo "📊 Ожидаемый результат:"
echo "- Логи с 'unrealized_pnl', 'leverage=100.0x', 'pnl_percent=0.2790 (27.90%)'"
echo "- Автоматическая установка take-profit при PNL > 15%"
echo "- Правильный расчет: (unrealized_pnl / position_value) * leverage"