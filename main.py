import os
import logging
import time
import sys

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

def check_environment():
    """Перевірка змінних середовища"""
    logging.info("🔍 Checking environment variables...")
    
    required_vars = ['TELEGRAM_API_KEY', 'API_KEY', 'SECRET_KEY']
    all_ok = True
    
    for var in required_vars:
        value = os.environ.get(var)
        if value:
            # Показуємо тільки перші 5 символів для безпеки
            masked_value = value[:5] + "..." if len(value) > 5 else "***"
            logging.info(f"✅ {var}: Present ({masked_value})")
        else:
            logging.error(f"❌ {var}: MISSING!")
            all_ok = False
    
    return all_ok

def main():
    logging.info("🚀 Starting Bitcoin Scalping Bot...")
    logging.info("📊 Timeframe: 1 minute")
    logging.info("📈 MACD: 12, 26, 9")
    
    # Перевірка змінних середовища
    if not check_environment():
        logging.error("❌ Cannot start bot: Missing environment variables")
        logging.info("💡 Add these variables in Railway: TELEGRAM_API_KEY, API_KEY, SECRET_KEY")
        return
    
    logging.info("✅ All checks passed!")
    logging.info("🤖 Bot is starting...")
    
    # Імітація роботи бота
    counter = 0
    try:
        while True:
            counter += 1
            logging.info(f"📈 Bot running... Check #{counter}")
            time.sleep(30)  # Чекаємо 30 секунд
            
    except KeyboardInterrupt:
        logging.info("👋 Bot stopped by user")
    except Exception as e:
        logging.error(f"⚠️ Bot crashed: {e}")

if __name__ == "__main__":
    main()
