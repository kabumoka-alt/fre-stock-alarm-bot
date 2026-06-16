import os
import requests
import time
import pandas as pd
from ta.momentum import RSIIndicator

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY")

def send(msg):
    try:
        requests.get(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg}
        )
    except Exception as e:
        print("send error:", e)

# 🔥 미국 종목 일부 가져오기 (TOP 스캔용)
def get_symbols():
    url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_KEY}"
    data = requests.get(url).json()

    if not isinstance(data, list):
        return []

    return [d["symbol"] for d in data[:200]]  # 과부하 방지

# 📊 가격 + 상승률
def get_change(symbol):
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_KEY}"
        q = requests.get(url).json()

        c = q.get("c")
        pc = q.get("pc")

        if not c or not pc or pc == 0:
            return None

        change = ((c - pc) / pc) * 100
        return change, c

    except:
        return None

# 📈 RSI
def get_rsi(symbol):
    try:
        url = f"https://finnhub.io/api/v1/stock/candle?symbol={symbol}&resolution=15&count=50&token={FINNHUB_KEY}"
        data = requests.get(url).json()

        if "c" not in data or len(data["c"]) < 10:
            return None

        close = pd.Series(data["c"])
        rsi = RSIIndicator(close).rsi()

        if rsi.empty:
            return None

        return rsi.iloc[-1]

    except:
        return None

while True:
    try:
        symbols = get_symbols()

        for s in symbols:
            result = get_change(s)

            if not result:
                continue

            change, price = result

            # 🚀 1차 필터 (급등만)
            if change < 20:
                continue

            rsi = get_rsi(s)

            if rsi is None:
                continue

            # 🎯 최종 조건
            if change >= 30 and 50 <= rsi <= 70:
                send(
                    f"🚨 TOP100 급등\n"
                    f"{s}\n"
                    f"+{change:.2f}%\n"
                    f"RSI {rsi:.1f}"
                )

        time.sleep(60)

    except Exception as e:
        print("loop error:", e)
        time.sleep(10)
