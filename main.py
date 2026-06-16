import os
import requests
import time

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")

def send(msg):
    try:
        requests.get(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg}
        )
    except:
        pass

# 📌 전체 US 종목 리스트 가져오기
def get_all_symbols():
    url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_KEY}"
    r = requests.get(url).json()

    if not isinstance(r, list):
        return []

    return [x["symbol"] for x in r if x.get("symbol")][:300]  # 안정용 제한

# 📌 상승률 계산
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

print("🚀 TOP100 SCANNER STARTED")

while True:
    try:
        symbols = get_all_symbols()

        results = []

        # 📊 전체 상승률 계산
        for s in symbols:
            ch = get_change(s)

            if ch is None:
                continue

            results.append((s, ch))

        # 🔥 상승률 정렬 → TOP100
        top100 = sorted(results, key=lambda x: x[1], reverse=True)[:100]

        print("TOP CALCULATED")

        # 🚨 상위 5개만 알림 (스팸 방지)
        for s, ch in top100[:5]:
            print(s, ch)

            if ch >= 3:
                send(f"🚀 TOP100 급등\n{s}\n+{ch:.2f}%")

        time.sleep(60)

    except Exception as e:
        print("error:", e)
        time.sleep(10)
