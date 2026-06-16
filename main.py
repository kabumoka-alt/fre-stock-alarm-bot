import os
import requests
import time

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 🔥 테스트용 워치리스트 (TOP100 전 단계)
WATCHLIST = ["AAPL", "TSLA", "NVDA", "AMD", "META", "PLTR", "MSFT"]

def send(msg):
    try:
        requests.get(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception as e:
        print("send error:", e)

def get_price(symbol):
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token=demo"
        return requests.get(url).json()
    except:
        return {}

print("BOT STARTED")

while True:
    try:
        for s in WATCHLIST:

            data = get_price(s)

            current = data.get("c")
            prev = data.get("pc")

            # 🚨 방어 코드 (0 나누기 / None 방지)
            if not current or not prev or prev == 0:
                continue

            change = ((current - prev) / prev) * 100

            print(s, change)

            # 🚀 테스트 기준 (나중에 30%로 변경)
            if change >= 3:
                send(f"🚀 상승 감지\n{s}\n+{change:.2f}%")

        time.sleep(60)

    except Exception as e:
        print("loop error:", e)
        time.sleep(10)
