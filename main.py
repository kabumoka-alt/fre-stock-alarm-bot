import os
import requests
import time
from datetime import datetime, timezone

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")

SCAN_INTERVAL = 120    # 스캔 주기 (초)
ALERT_COOLDOWN = 600   # 같은 종목 재알림 대기 시간 (초)
MAX_SYMBOLS = 80       # API 한도 내에서 처리할 최대 종목 수


def is_market_open():
    now = datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    if h < 13 or h > 20:
        return False
    if h == 13 and m < 30:
        return False
    if h == 20 and m > 0:
        return False
    return True


def send(msg):
    try:
        requests.get(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            params={"chat_id": CHAT_ID, "text": msg},
            timeout=5
        )
    except Exception as e:
        print(f"텔레그램 오류: {e}")


def get_symbols():
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/stock/symbol",
            params={"exchange": "US", "token": FINNHUB_KEY},
            timeout=10
        ).json()
        if not isinstance(r, list):
            return []
        # ETF, 워런트, 유닛 제외 — 보통주만
        return [
            x["symbol"] for x in r
            if x.get("type") == "Common Stock" and "." not in x["symbol"]
        ][:MAX_SYMBOLS]
    except Exception as e:
        print(f"종목 로딩 오류: {e}")
        return []


def get_quote(symbol):
    """(현재가, 등락률) 반환, 실패 시 None"""
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/quote",
            params={"symbol": symbol, "token": FINNHUB_KEY},
            timeout=5
        ).json()
        price = r.get("c")
        prev = r.get("pc")
        if not price or not prev or prev == 0:
            return None
        return price, ((price - prev) / prev) * 100
    except:
        return None


def get_volume_ratio(symbol):
    """현재 5분봉 거래량 / 직전 19개 평균 반환"""
    try:
        to_ts = int(time.time())
        from_ts = to_ts - (20 * 5 * 60)  # 5분봉 20개치 범위
        r = requests.get(
            "https://finnhub.io/api/v1/stock/candle",
            params={
                "symbol": symbol,
                "resolution": "5",
                "from": from_ts,   # ✅ 수정: count → from/to
                "to": to_ts,
                "token": FINNHUB_KEY
            },
            timeout=5
        ).json()
        volumes = r.get("v", [])
        if len(volumes) < 6:
            return None
        avg = sum(volumes[:-1]) / len(volumes[:-1])
        return (volumes[-1] / avg) if avg > 0 else 0
    except:
        return None


def get_rsi(symbol):
    """5분봉 RSI(14) 최신값 반환"""
    try:
        to_ts = int(time.time())
        from_ts = to_ts - (60 * 5 * 60)  # RSI 계산에 충분한 기간
        r = requests.get(
            "https://finnhub.io/api/v1/indicator",
            params={
                "symbol": symbol,
                "resolution": "5",
                "from": from_ts,
                "to": to_ts,
                "indicator": "rsi",
                "timeperiod": 14,
                "token": FINNHUB_KEY
            },
            timeout=5
        ).json()
        values = r.get("rsi", [])
        return values[-1] if values else None
    except:
        return None


def score_pump(change, volume_ratio, rsi):
    score = 0

    if 1 <= change <= 3:
        score += 30
    elif 3 < change <= 6:
        score += 20
    elif change > 8:
        score -= 30  # 이미 과열

    if volume_ratio >= 4:
        score += 30
    elif volume_ratio >= 3:
        score += 25
    elif volume_ratio >= 2:
        score += 15

    if rsi is None:
        pass  # ✅ 수정: 데이터 없으면 중립 처리 (감점 제거)
    elif 50 <= rsi <= 60:
        score += 25
    elif 60 < rsi <= 70:
        score += 20
    elif 70 < rsi <= 80:
        score += 5
    else:
        score -= 10

    return max(0, min(score, 100))


# 종목별 마지막 알림 시각 (Unix timestamp)
alert_times: dict[str, float] = {}

print("🚀 급등 스캐너 시작")

while True:
    try:
        if not is_market_open():
            print("⛔ 장 마감 — 5분 대기")
            time.sleep(300)
            continue

        symbols = get_symbols()
        print(f"📊 {len(symbols)}개 종목 스캔 중")

        results = []
        now = time.time()

        for s in symbols:
            time.sleep(0.7)  # ✅ 수정: 분당 ~85호출로 한도 준수

            quote = get_quote(s)
            if not quote:
                continue
            price, change = quote

            # 등락률 사전 필터 — 조건 미달 시 캔들/RSI 호출 생략
            if not (0.5 <= change <= 12):
                continue

            vol_ratio = get_volume_ratio(s)
            if vol_ratio is None or vol_ratio < 1.5:
                continue

            rsi = get_rsi(s)
            prob = score_pump(change, vol_ratio, rsi)

            rsi_str = f"{rsi:.1f}" if rsi else "N/A"
            print(f"{s:6s} | {change:+.2f}% | 거래량 {vol_ratio:.2f}x | RSI {rsi_str} | 점수 {prob}")

            if prob >= 75:
                results.append((s, price, change, vol_ratio, rsi, prob))

        results.sort(key=lambda x: x[5], reverse=True)

        for s, price, change, vol, rsi, prob in results[:5]:
            last_alert = alert_times.get(s, 0)
            if now - last_alert < ALERT_COOLDOWN:
                continue  # ✅ 수정: 10분 이내 재알림 방지

            rsi_str = f"{rsi:.1f}" if rsi else "N/A"
            msg = (
                f"🚨 10분 급등 신호\n"
                f"종목: {s}\n"
                f"현재가: ${price:.2f}\n"
                f"등락: +{change:.2f}%\n"
                f"RSI: {rsi_str}\n"
                f"거래량: {vol:.2f}x\n"
                f"🔥 점수: {prob}/100"
            )
            print(msg)
            send(msg)
            alert_times[s] = now  # ✅ 수정: 알림 시각 기록

        print(f"✅ 스캔 완료 — {len(results)}개 감지. {SCAN_INTERVAL}초 후 재스캔\n")
        time.sleep(SCAN_INTERVAL)

    except Exception as e:
        print(f"루프 오류: {e}")
        time.sleep(15)
