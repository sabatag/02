#!/usr/bin/env python3
"""
Script to help switch from bot.py to bot2.py with leverage fixes
"""

import subprocess
import sys
import os

def stop_current_bot():
    """Stop any running bot processes"""
    try:
        # Kill any running bot processes
        subprocess.run(["pkill", "-f", "python.*bot"], check=False)
        print("✓ Stopped any running bot processes")
    except Exception as e:
        print(f"Error stopping bot: {e}")

def start_bot2():
    """Start bot2.py with leverage fixes"""
    try:
        if not os.path.exists("bot2.py"):
            print("❌ bot2.py not found!")
            return False
            
        print("🚀 Starting bot2.py with leverage fixes...")
        
        # Start bot2.py in background
        process = subprocess.Popen([
            sys.executable, "bot2.py"
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        print(f"✓ Started bot2.py with PID: {process.pid}")
        print("\nThe bot2.py includes these fixes:")
        print("• Uses real PNL from Bybit API instead of local calculation")
        print("• Multiplies PNL by leverage (100x for ETH, 10x for DOGE, etc.)")
        print("• Enhanced logging shows: unrealized_pnl, leverage, pnl_percent")
        print("• Take-profit triggers at 15% PNL, sets at 10% with 2% trailing")
        
        return True
        
    except Exception as e:
        print(f"❌ Error starting bot2.py: {e}")
        return False

def show_differences():
    """Show key differences between bot.py and bot2.py"""
    print("\n" + "="*60)
    print("KEY DIFFERENCES BETWEEN bot.py AND bot2.py")
    print("="*60)
    
    print("\n🔴 OLD bot.py (what you're using now):")
    print("• PNL = (current_price - entry_price) / entry_price")
    print("• No leverage consideration")
    print("• Log: 'pnl=0.0001' (tiny values)")
    print("• Result: 27.90% real PNL shows as 0.01% in bot")
    
    print("\n🟢 NEW bot2.py (what you should use):")
    print("• PNL = (unrealized_pnl / position_value) * leverage")
    print("• Uses real PNL from Bybit API")
    print("• Log: 'unrealized_pnl=0.1831 USDT, leverage=100.0x, pnl_percent=0.2790 (27.90%)'")
    print("• Result: 27.90% real PNL correctly triggers take-profit")
    
    print("\n📊 EXAMPLE WITH YOUR ETH POSITION:")
    print("• Bybit UI: +27.90% PNL")
    print("• bot.py shows: pnl=0.0001 (0.01%) - NO take-profit")
    print("• bot2.py shows: pnl_percent=0.2790 (27.90%) - TRIGGERS take-profit")

if __name__ == "__main__":
    print("🔧 Bot Switch Helper")
    print("="*30)
    
    show_differences()
    
    print("\n" + "="*60)
    print("SWITCHING TO bot2.py")
    print("="*60)
    
    # Stop current bot
    stop_current_bot()
    
    # Start bot2.py
    if start_bot2():
        print("\n✅ SUCCESS! bot2.py is now running with leverage fixes.")
        print("Your take-profit orders should now work correctly!")
        print("\nTo monitor the bot:")
        print("• Check logs for 'leverage=100.0x, pnl_percent=0.2790 (27.90%)'")
        print("• Take-profit should trigger at 15% PNL")
        print("• Orders will be set at 10% PNL level with 2% trailing")
    else:
        print("\n❌ FAILED to start bot2.py. Please check the error above.")