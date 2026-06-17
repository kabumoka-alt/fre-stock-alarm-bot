import os
import requests
import time
from datetime import datetime

# ======================
# 🔥 설정
# ======================
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")

SYMBOLS = ["AAPL", "TSLA", "NVDA", "AMD", "META", "AMZN"]

SCAN_INTERVAL = 60

# ======================
# 📩 텔레그램
# ======================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": msg}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

# ======================
# 🕒 MARKET CHECK (단순 UTC)
# ======================
def is_market_open():
    now = datetime.utcnow()

    if now.weekday() >= 5:
        return False

    # 미국장 대략 시간
    if 13 <= now.hour < 20:
        return True

    return False

# ======================
# 📡 가격 가져오기
# ======================
def get_price(symbol):
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_KEY}"
    r = requests.get(url).json()
    return r["c"]

# ======================
# 🚀 MAIN
# ======================
def run():
    send_telegram("🚀 30% PUMP SCANNER STARTED")

    base_price = {}  # 기준 가격 저장

    while True:
        try:
            if not is_market_open():
                print("⛔ MARKET CLOSED")
                time.sleep(30)
                continue

            print("📊 SCANNING...")

            for symbol in SYMBOLS:
                price = get_price(symbol)

                # 🔥 기준값 없으면 세팅
                if symbol not in base_price:
                    base_price[symbol] = price

                change = (price - base_price[symbol]) / base_price[symbol] * 100

                print(symbol, price, f"{change:.2f}%")

                # 🚨 30% 급등 알림
                if change >= 30:
                    send_telegram(
                        f"🚀🚀 {symbol} +{change:.2f}% PUMP ALERT!"
                    )

                    # 🔁 기준값 업데이트 (중복 알림 방지)
                    base_price[symbol] = price

                # 🔻 급락하면 기준도 갱신 (리셋 방지)
                if change <= -10:
                    base_price[symbol] = price

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            print("ERROR:", e)
            time.sleep(10)

# ======================
# ▶ 실행
# ======================
if __name__ == "__main__":
    run()
