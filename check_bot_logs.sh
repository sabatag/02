#!/bin/bash

echo "📊 ПРОВЕРКА РАБОТЫ BOT2.PY"
echo "=========================="

LOG_FILE="/var/www/phyton/bot/botttss/logs.txt"

# Проверить, что бот запущен
if pgrep -f "python.*bot2.py" > /dev/null; then
    echo "✅ bot2.py запущен (PID: $(pgrep -f 'python.*bot2.py'))"
else
    echo "❌ bot2.py не запущен"
    if pgrep -f "python.*bot.py" > /dev/null; then
        echo "⚠️  Запущен старый bot.py (PID: $(pgrep -f 'python.*bot.py'))"
        echo "Нужно переключиться на bot2.py!"
    fi
fi

echo ""

# Проверить логи
if [ -f "$LOG_FILE" ]; then
    echo "📝 Анализ логов за последние 2 минуты:"
    echo "======================================"
    
    # Найти строки с правильным форматом (leverage)
    CORRECT_LOGS=$(tail -50 "$LOG_FILE" | grep -c "unrealized_pnl.*USDT.*leverage.*pnl_percent")
    
    # Найти строки со старым форматом (без leverage)
    OLD_LOGS=$(tail -50 "$LOG_FILE" | grep -c "entry_price.*current_price.*pnl=" | grep -v "unrealized_pnl")
    
    echo "✅ Правильные логи (с leverage): $CORRECT_LOGS"
    echo "❌ Старые логи (без leverage): $OLD_LOGS"
    
    if [ $CORRECT_LOGS -gt 0 ]; then
        echo ""
        echo "🎉 ОТЛИЧНО! Найдены правильные логи с leverage:"
        tail -50 "$LOG_FILE" | grep "unrealized_pnl.*USDT.*leverage.*pnl_percent" | tail -3
        
        # Проверить take-profit ордера
        TP_ORDERS=$(tail -50 "$LOG_FILE" | grep -c "Set take-profit order")
        if [ $TP_ORDERS -gt 0 ]; then
            echo ""
            echo "🚀 TAKE-PROFIT ОРДЕРА ВЫСТАВЛЕНЫ: $TP_ORDERS"
            tail -50 "$LOG_FILE" | grep "Set take-profit order" | tail -3
        else
            echo ""
            echo "⏳ Take-profit ордера пока не выставлены"
            echo "Возможные причины:"
            echo "- PNL < 15% (нужно дождаться)"
            echo "- Позиции еще не проверены"
        fi
        
    elif [ $OLD_LOGS -gt 0 ]; then
        echo ""
        echo "❌ ПРОБЛЕМА! Все еще используется старый формат:"
        tail -50 "$LOG_FILE" | grep "entry_price.*current_price.*pnl=" | grep -v "unrealized_pnl" | tail -3
        echo ""
        echo "🔧 РЕШЕНИЕ: Запустите ./switch_to_correct_bot.sh"
        
    else
        echo ""
        echo "🔍 Логи не найдены. Возможные причины:"
        echo "- Бот только что запущен"
        echo "- Нет активных позиций"
        echo "- Проблемы с API"
    fi
    
else
    echo "❌ Файл логов не найден: $LOG_FILE"
fi

echo ""
echo "📈 Мониторинг логов в реальном времени:"
echo "======================================="
echo "Для мониторинга запустите: tail -f $LOG_FILE"
echo "Для остановки: pkill -f 'python.*bot'"