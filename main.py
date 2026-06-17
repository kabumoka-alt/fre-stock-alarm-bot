import os
import requests
import time
from datetime import datetime

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")

def is_market_open():
    now = datetime.utcnow()
    hour = now.hour

    # 🇺🇸 단순 필터 (정확한 버전)
    # 정규장: UTC 14:30 ~ 21:00 (대략)
    return 14 <= hour <= 21

def send(msg):
    try:
        requests.get(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg}
        )
    except:
        pass

def get_symbols():
    url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_KEY}"
    r = requests.get(url).json()
    if not isinstance(r, list):
        return []
    return [x["symbol"] for x in r[:300]]

def get_change(symbol):
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=5).json()

        c = r.get("c")
        pc = r.get("pc")

        if not c or not pc or pc == 0:
            return None

        return ((c - pc) / pc) * 100

    except:
        return None

print("🚀 TOP100 SCANNER STARTED (MARKET FILTER ON)")

while True:
    try:
        # 🚨 정규장 아닐 때 스킵
        if not is_market_open():
            print("⛔ MARKET CLOSED")
            time.sleep(300)
            continue

        symbols = get_symbols()
        results = []

        for s in symbols:
            ch = get_change(s)
            if ch is None:
                continue
            results.append((s, ch))

        top100 = sorted(results, key=lambda x: x[1], reverse=True)[:100]

        for s, ch in top100[:5]:
            print(s, ch)

            if ch >= 3:
                send(f"🚀 정규장 TOP100\n{s}\n+{ch:.2f}%")

        time.sleep(60)

    except Exception as e:
        print("error:", e)
        time.sleep(10)
