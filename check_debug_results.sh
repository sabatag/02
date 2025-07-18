#!/bin/bash

echo "📊 ПРОВЕРКА РЕЗУЛЬТАТОВ ОТЛАДКИ"
echo "==============================="

LOG_FILE="/var/www/phyton/bot/botttss/logs_debug.txt"

if [ ! -f "$LOG_FILE" ]; then
    echo "❌ Файл логов не найден: $LOG_FILE"
    echo "Запустите сначала: ./debug_bot.sh"
    exit 1
fi

echo "📂 Файл логов: $LOG_FILE"
echo "📅 Размер файла: $(stat -c%s "$LOG_FILE") байт"
echo "🕒 Последнее изменение: $(stat -c%y "$LOG_FILE")"
echo ""

# Проверка успешного запуска
if grep -q "Starting DEBUG bot monitoring" "$LOG_FILE"; then
    echo "✅ Бот успешно запущен"
else
    echo "❌ Бот не запущен или ошибка при запуске"
    echo "Последние строки логов:"
    tail -10 "$LOG_FILE"
    exit 1
fi

# Проверка пользователей
USER_COUNT=$(grep -c "Initialized API session" "$LOG_FILE")
echo "👥 Загружено пользователей: $USER_COUNT"

# Проверка позиций
if grep -q "Got .* positions from API" "$LOG_FILE"; then
    echo "✅ Позиции получены от API"
    API_POSITIONS=$(grep "Got .* positions from API" "$LOG_FILE" | tail -1 | sed 's/.*Got \([0-9]*\) positions.*/\1/')
    echo "📊 Позиций в API: $API_POSITIONS"
else
    echo "❌ Не удалось получить позиции от API"
fi

# Проверка ошибок
ERROR_COUNT=$(grep -c "CRITICAL ERROR" "$LOG_FILE")
if [ $ERROR_COUNT -gt 0 ]; then
    echo "❌ Найдено критических ошибок: $ERROR_COUNT"
    echo "Последние ошибки:"
    grep "CRITICAL ERROR" "$LOG_FILE" | tail -3
    echo ""
    echo "Трассировка ошибок:"
    grep -A 10 "Traceback:" "$LOG_FILE" | tail -15
else
    echo "✅ Критических ошибок не найдено"
fi

# Проверка правильного формата логов
if grep -q "unrealized_pnl.*USDT.*leverage.*pnl_percent" "$LOG_FILE"; then
    echo "✅ Найдены правильные логи с leverage"
    echo "Примеры:"
    grep "unrealized_pnl.*USDT.*leverage.*pnl_percent" "$LOG_FILE" | tail -3
else
    echo "❌ Не найдены правильные логи с leverage"
    echo "Возможные причины:"
    echo "- Ошибка в коде"
    echo "- Нет активных позиций"
    echo "- Проблемы с API"
fi

# Проверка тейк-профит ордеров
if grep -q "Set take-profit order" "$LOG_FILE"; then
    echo "✅ Тейк-профит ордера выставлены"
    TP_COUNT=$(grep -c "Set take-profit order" "$LOG_FILE")
    echo "📈 Выставлено тейк-профит ордеров: $TP_COUNT"
    echo "Примеры:"
    grep "Set take-profit order" "$LOG_FILE" | tail -3
else
    echo "❌ Тейк-профит ордера не выставлены"
    echo "Возможные причины:"
    echo "- PNL < 15% (триггер)"
    echo "- Ошибка в коде"
    echo "- Нет активных позиций"
fi

echo ""
echo "🔍 Для просмотра полных логов: tail -f $LOG_FILE"
echo "⏹️  Для остановки бота: pkill -f 'python.*bot'"