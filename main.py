import os
import requests
import time
from datetime import datetime, timezone

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")

# -----------------------------
# ⛔ 미국장 시간 체크 (UTC)
# -----------------------------
def is_market_open():
    now = datetime.now(timezone.utc)
    hour = now.hour
    minute = now.minute

    if hour < 13 or hour > 20:
        return False
    if hour == 13 and minute < 30:
        return False
    if hour == 20 and minute > 0:
        return False

    return True


# -----------------------------
# 📩 텔레그램 알림
# -----------------------------
def send(msg):
    try:
        requests.get(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg}
        )
    except:
        pass


# -----------------------------
# 📊 종목 리스트
# -----------------------------
def get_symbols():
    url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_KEY}"
    r = requests.get(url).json()

    if not isinstance(r, list):
        return []

    return [x["symbol"] for x in r[:200]]  # 속도 위해 200개 제한


# -----------------------------
# 📈 가격 + 등락률
# -----------------------------
def get_price_change(symbol):
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=5).json()

        price = r.get("c")
        prev = r.get("pc")

        if not price or not prev or prev == 0:
            return None

        change = ((price - prev) / prev) * 100

        return price, change

    except:
        return None


# -----------------------------
# 📊 거래량 (5분 캔들 기반)
# -----------------------------
def get_volume_ratio(symbol):
    try:
        url = f"https://finnhub.io/api/v1/stock/candle?symbol={symbol}&resolution=5&count=20&token={FINNHUB_KEY}"
        r = requests.get(url, timeout=5).json()

        volumes = r.get("v", [])
        if len(volumes) < 5:
            return None

        avg_vol = sum(volumes[:-1]) / len(volumes[:-1])
        current_vol = volumes[-1]

        if avg_vol == 0:
            return 0

        return current_vol / avg_vol

    except:
        return None


# -----------------------------
# 🎯 급등 직전 점수 시스템
# -----------------------------
def calc_score(change, volume_ratio):
    score = 0

    # 📈 초기 상승 구간
    if 1 <= change <= 6:
        score += 2

    # 💣 거래량 폭발
    if volume_ratio >= 3:
        score += 3
    elif volume_ratio >= 2:
        score += 2

    return score


# -----------------------------
# 🚀 실행 시작
# -----------------------------
print("🚀 TOP SCANNER STARTED (MOMENTUM MODE)")

sent = set()

while True:
    try:
        if not is_market_open():
            print("⛔ MARKET CLOSED")
            time.sleep(300)
            continue

        symbols = get_symbols()
        results = []

        for s in symbols:
            price_data = get_price_change(s)
            vol_ratio = get_volume_ratio(s)

            if not price_data or vol_ratio is None:
                continue

            price, change = price_data

            score = calc_score(change, vol_ratio)

            if score >= 4:
                results.append((s, price, change, vol_ratio, score))

        # 🔥 점수 높은 순 정렬
        results.sort(key=lambda x: x[4], reverse=True)

        print(f"FOUND: {len(results)} candidates")

        # 🚨 상위 알림
        for s, price, change, vol, score in results[:10]:

            if s in sent:
                continue

            msg = (
                f"🚨 급등 직전 포착\n"
                f"{s}\n"
                f"현재가: ${price:.2f}\n"
                f"등락: +{change:.2f}%\n"
                f"거래량: {vol:.2f}x\n"
                f"점수: {score}"
            )

            print(msg)
            send(msg)

            sent.add(s)

        time.sleep(60)

    except Exception as e:
        print("error:", e)
        time.sleep(10)
