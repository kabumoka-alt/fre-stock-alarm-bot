"""
미국 주식 급등 감지 봇 v8
- 정규장: 상승 상위 50종목 | RSI 50+
- 프리/애프터: 상승 상위 20종목 | 5분 5%+ | RSI 50+ | 거래량 3배+
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# 정규장 조건
REGULAR_TOP_N = 50
REGULAR_RSI = 50

# 프리/애프터 조건
EXTENDED_TOP_N = 20
EXTENDED_PRICE_CHANGE = 5.0   # 5분 내 5%
EXTENDED_RSI = 50
EXTENDED_VOLUME_MULT = 3.0    # 거래량 3배

CHECK_INTERVAL = 60
COOLDOWN_MINUTES = 30

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

last_alert = {}


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=5)
        if resp.status_code != 200:
            print(f"[텔레그램 오류] {resp.text}")
    except Exception as e:
        print(f"[텔레그램 예외] {e}")


def get_market_session():
    """현재 장 세션 반환: 'pre', 'regular', 'after', 'overnight', 'closed'"""
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc + timedelta(hours=-4)
    weekday = now_et.weekday()  # 0=월, 6=일
    et_min = now_et.hour * 60 + now_et.minute

    if weekday == 5:  # 토요일 전체 closed
        return "closed"
    if weekday == 6 and et_min < (20 * 60):  # 일요일 20시 이전 closed
        return "closed"

    if (4 * 60) <= et_min < (9 * 60 + 30):
        return "pre"
    elif (9 * 60 + 30) <= et_min <= (16 * 60):
        return "regular"
    elif (16 * 60) < et_min <= (20 * 60):
        return "after"
    else:
        return "overnight"  # 20:00 ~ 04:00


def get_active_symbols():
    url = "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives"
    params = {"by": "trades", "top": 100}
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if resp.status_code == 200:
            return [d["symbol"] for d in resp.json().get("most_actives", [])]
        print(f"[스크리너 오류] {resp.status_code}")
        return []
    except Exception as e:
        print(f"[스크리너 예외] {e}")
        return []


def get_snapshots(symbols: list):
    url = "https://data.alpaca.markets/v2/stocks/snapshots"
    params = {"symbols": ",".join(symbols), "feed": "iex"}
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        print(f"[스냅샷 오류] {resp.status_code}")
        return {}
    except Exception as e:
        print(f"[스냅샷 예외] {e}")
        return {}


def get_bars(symbol: str, limit: int = 30):
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    params = {"timeframe": "1Min", "limit": limit, "feed": "iex", "sort": "asc"}
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("bars", [])
        return []
    except:
        return []


def calc_rsi(bars: list, period: int = 14):
    if len(bars) < period + 1:
        return None
    closes = [float(b["c"]) for b in bars]
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))


def analyze_symbol(symbol: str, extended: bool = False):
    """종목 분석. extended=True면 프리/애프터 조건 추가"""
    bars = get_bars(symbol, limit=30)
    if not bars or len(bars) < 6:
        return None

    rsi = calc_rsi(bars)
    if rsi is None or rsi < (EXTENDED_RSI if extended else REGULAR_RSI):
        return None

    if extended:
        current_price = float(bars[-1]["c"])
        price_5m_ago = float(bars[-6]["c"])
        price_change_5m = ((current_price - price_5m_ago) / price_5m_ago) * 100

        current_vol = float(bars[-1]["v"])
        avg_vol = sum(float(b["v"]) for b in bars[:-1]) / len(bars[:-1])
        vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0

        price_ok = "✅" if price_change_5m >= EXTENDED_PRICE_CHANGE else "❌"
        vol_ok = "✅" if vol_ratio >= EXTENDED_VOLUME_MULT else "❌"
        print(f"  └ RSI:{rsi:.1f} | 5분:{price_change_5m:+.2f}%{price_ok} | 거래량:{vol_ratio:.1f}x{vol_ok}")

        if price_change_5m < EXTENDED_PRICE_CHANGE or vol_ratio < EXTENDED_VOLUME_MULT:
            return None

        return {"rsi": rsi, "price_change_5m": price_change_5m, "vol_ratio": vol_ratio}

    print(f"  └ RSI:{rsi:.1f}")
    return {"rsi": rsi}


def run_scan(session: str):
    symbols = get_active_symbols()
    if not symbols:
        return

    snapshots = get_snapshots(symbols)
    if not snapshots:
        return

    ranked = []
    for sym, snap in snapshots.items():
        daily = snap.get("dailyBar", {})
        prev = snap.get("prevDailyBar", {})
        if not daily or not prev or not prev.get("c"):
            continue
        change_pct = ((daily["c"] - prev["c"]) / prev["c"]) * 100
        ranked.append({"symbol": sym, "price": daily["c"], "change_pct": change_pct})

    ranked = sorted(ranked, key=lambda x: x["change_pct"], reverse=True)
    is_extended = session in ("pre", "after", "overnight")
    top_n = EXTENDED_TOP_N if is_extended else REGULAR_TOP_N
    top = ranked[:top_n]

    session_label = {"pre": "🌅 프리마켓", "regular": "📈 정규장", "after": "🌙 애프터마켓", "overnight": "🌃 주간거래"}[session]
    print(f"[{session_label}] 상위 {top_n}종목 스캔 | 1위: {top[0]['symbol']} {top[0]['change_pct']:+.2f}%")

    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc + timedelta(hours=9)

    for stock in top:
        sym = stock["symbol"]

        if sym in last_alert:
            elapsed = (now_utc - last_alert[sym]).total_seconds() / 60
            if elapsed < COOLDOWN_MINUTES:
                continue

        result = analyze_symbol(sym, extended=is_extended)
        if result is None:
            continue

        last_alert[sym] = now_utc

        if is_extended:
            message = (
                f"{session_label} <b>급등 신호!</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"📌 종목: <b>{sym}</b>\n"
                f"💰 현재가: ${stock['price']:.2f}\n"
                f"📈 일중 상승률: <b>{stock['change_pct']:+.2f}%</b>\n"
                f"⚡ 5분 상승: <b>{result['price_change_5m']:+.2f}%</b>\n"
                f"📊 RSI: <b>{result['rsi']:.1f}</b>\n"
                f"📦 거래량: <b>{result['vol_ratio']:.1f}x</b>\n"
                f"🇰🇷 한국시간: {now_kst.strftime('%m/%d %H:%M:%S')}"
            )
        else:
            message = (
                f"{session_label} <b>급등 + RSI 신호!</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"📌 종목: <b>{sym}</b>\n"
                f"💰 현재가: ${stock['price']:.2f}\n"
                f"📈 일중 상승률: <b>{stock['change_pct']:+.2f}%</b>\n"
                f"📊 RSI: <b>{result['rsi']:.1f}</b>\n"
                f"🇰🇷 한국시간: {now_kst.strftime('%m/%d %H:%M:%S')}"
            )

        send_telegram(message)
        print(f"[🚀 알림!] {sym} | {stock['change_pct']:+.2f}% | RSI {result['rsi']:.1f}")
        time.sleep(0.5)


def main():
    print("=" * 50)
    print("🚀 급등 감지 봇 v8 시작!")
    print(f"📈 정규장: 상위 {REGULAR_TOP_N}종목 | RSI {REGULAR_RSI}+")
    print(f"🌅 프리/애프터/주간: 상위 {EXTENDED_TOP_N}종목 | 5분 {EXTENDED_PRICE_CHANGE}%+ | RSI {EXTENDED_RSI}+ | 거래량 {EXTENDED_VOLUME_MULT}x+")
    print("=" * 50)

    send_telegram(
        f"🤖 <b>급등 감지 봇 v8 시작!</b>\n"
        f"📈 정규장: 상위 {REGULAR_TOP_N}종목 | RSI {REGULAR_RSI}+\n"
        f"🌅 프리/애프터/주간: 상위 {EXTENDED_TOP_N}종목 | 5분 {EXTENDED_PRICE_CHANGE}%+ | RSI {EXTENDED_RSI}+ | 거래량 {EXTENDED_VOLUME_MULT}x+"
    )

    while True:
        session = get_market_session()
        now_str = datetime.now().strftime('%H:%M:%S')

        if session == "closed":
            print(f"[{now_str}] 휴장 중...")
        else:
            print(f"\n[{now_str}] 세션: {session}")
            run_scan(session)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
