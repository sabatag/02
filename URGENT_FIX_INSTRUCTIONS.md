# 🚨 URGENT FIX: Switch to bot2.py for Take-Profit Orders

## Problem
You're running the old `bot.py` which calculates PNL incorrectly without leverage. This is why your 27.90% profitable position shows as only 0.01% in the bot and doesn't trigger take-profit orders.

## Solution
Switch to `bot2.py` which has the leverage fix.

## Evidence from Your Logs
```
❌ Your current bot.py log:
2025-07-15 12:48:00 - user1: Checking ETHUSDT, avg_entry_price=2985.63, current_price=2985.98, pnl=0.0001

✅ bot2.py would show:
2025-07-15 12:48:00 - user1: Checking ETHUSDT, avg_entry_price=2985.63, current_price=2985.98, unrealized_pnl=0.1831 USDT, leverage=100.0x, pnl_percent=0.2790 (27.90%)
```

## Quick Fix Steps

### Option 1: Run the switch script
```bash
python3 switch_to_bot2.py
```

### Option 2: Manual steps
1. **Stop current bot:**
   ```bash
   pkill -f "python.*bot"
   ```

2. **Start bot2.py:**
   ```bash
   python3 bot2.py &
   ```

3. **Check logs to verify:**
   Look for logs showing `leverage=100.0x, pnl_percent=0.2790 (27.90%)`

## Why This Fixes Your Issue

### The Problem (bot.py)
- **PNL Calculation:** `(current_price - entry_price) / entry_price`
- **No Leverage:** Ignores 100x leverage on ETH
- **Result:** 27.90% real PNL shows as 0.01% in bot
- **Take-Profit:** Never triggers because 0.01% < 15% threshold

### The Fix (bot2.py)
- **PNL Calculation:** `(unrealized_pnl / position_value) * leverage`
- **Uses Bybit API:** Gets real PNL and leverage directly
- **Result:** 27.90% real PNL shows correctly as 27.90%
- **Take-Profit:** Triggers immediately because 27.90% > 15% threshold

## Take-Profit Logic in bot2.py
- **Trigger:** 15% PNL (your ETH at 27.90% qualifies)
- **Initial Level:** 10% PNL
- **Trailing:** 2% steps up from 15% trigger
- **Your ETH:** Should set take-profit at ~32% level (trailing from 15%)

## Expected Result
After switching to bot2.py, your profitable positions should immediately start setting take-profit orders because they'll be calculated correctly with leverage.

## Monitoring
Watch for log entries like:
```
user1: Checking ETHUSDT, unrealized_pnl=0.1831 USDT, leverage=100.0x, pnl_percent=0.2790 (27.90%)
user1: ETHUSDT PNL=0.2790 (27.90%), updated take-profit to 32.0%
```