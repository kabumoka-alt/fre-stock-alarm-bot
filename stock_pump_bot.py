"""
미국 주식 급등 감지 봇 v9
- 주간거래: 일중 상승률 27%+ 알림
- 프리/정규/애프터: 5분봉 5%+ & RSI 50+ (실시간 호가 기준)
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta

ALPACA_API_KEY = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# 주간거래 조건
OVERNIGHT_TOP_N = 20
OVERNIGHT_CHANGE = 27.0       # 일중 27%+

# 정규장 조건
REGULAR_TOP_N = 50
REGULAR_RSI = 50

# 프리/애프터 조건
EXTENDED_TOP_N = 20
EXTENDED_PRICE_CHANGE = 5.0   # 5분 5%+
EXTENDED_RSI = 50
EXTENDED_VOLUME_MULT = 1.5

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
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc + timedelta(hours=-4)  # 서머타임 UTC-4
    weekday = now_et.weekday()
    et_min = now_et.hour * 60 + now_et.minute

    if weekday == 5:
        return "closed"
    if weekday == 6 and et_min < (20 * 60):
        return "closed"

    if (4 * 60) <= et_min < (9 * 60 + 30):
        return "pre"
    elif (9 * 60 + 30) <= et_min <= (16 * 60):
        return "regular"
    elif (16 * 60) < et_min <= (20 * 60):
        return "after"
    else:
        return "overnight"


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
    params = {"symbols": ",".join(symbols)}
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
    """1분봉 데이터 - feed 없이 최선 데이터 요청"""
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    params = {"timeframe": "1Min", "limit": limit, "sort": "asc"}
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if resp.status_code == 200:
            bars = resp.json().get("bars", [])
            if bars:
                return bars
        # 실패시 iex로 재시도
        params["feed"] = "iex"
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


def get_live_price(snap: dict):
    """실시간 호가 우선 반환"""
    latest_trade = snap.get("latestTrade", {})
    minute_bar = snap.get("minuteBar", {})
    daily_bar = snap.get("dailyBar", {})
    price = latest_trade.get("p") or minute_bar.get("c") or daily_bar.get("c")
    source = "호가" if latest_trade.get("p") else ("1분봉" if minute_bar.get("c") else "종가")
    return price, source


def build_ranked(snapshots: dict):
    """스냅샷으로 상승률 랭킹 생성 - 실시간 호가 기준"""
    ranked = []
    for sym, snap in snapshots.items():
        prev = snap.get("prevDailyBar", {})
        if not prev or not prev.get("c"):
            continue

        current_price, price_source = get_live_price(snap)
        if not current_price:
            continue

        prev_close = prev["c"]
        change_pct = ((current_price - prev_close) / prev_close) * 100
        ranked.append({
            "symbol": sym,
            "price": current_price,
            "price_source": price_source,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "snap": snap
        })

    return sorted(ranked, key=lambda x: x["change_pct"], reverse=True)


def analyze_regular(sym: str, snap: dict):
    """정규장: 5분봉 5%+ & RSI 50+"""
    bars = get_bars(sym)
    if not bars or len(bars) < 6:
        print(f"  └ 데이터 부족: {len(bars) if bars else 0}개")
        return None

    # 실시간 호가로 현재가 오버라이드
    latest_price, _ = get_live_price(snap)
    current_price = latest_price or float(bars[-1]["c"])
    price_5m_ago = float(bars[-6]["c"])

    if price_5m_ago <= 0:
        return None

    price_change_5m = ((current_price - price_5m_ago) / price_5m_ago) * 100
    rsi = calc_rsi(bars)

    if rsi is None:
        return None

    price_ok = "✅" if price_change_5m >= EXTENDED_PRICE_CHANGE else "❌"
    print(f"  └ RSI:{rsi:.1f} | 5분:{price_change_5m:+.2f}%{price_ok}")

    if price_change_5m < EXTENDED_PRICE_CHANGE or rsi < REGULAR_RSI:
        return None

    return {"rsi": rsi, "price_change_5m": price_change_5m}


def analyze_extended(sym: str, snap: dict, check_volume: bool = True):
    """프리/애프터: 5분봉 5%+ & RSI 50+ & 거래량 1.5x+"""
    bars = get_bars(sym)
    if not bars or len(bars) < 6:
        print(f"  └ 데이터 부족: {len(bars) if bars else 0}개")
        return None

    latest_price, _ = get_live_price(snap)
    current_price = latest_price or float(bars[-1]["c"])
    price_5m_ago = float(bars[-6]["c"])

    if price_5m_ago <= 0:
        return None

    price_change_5m = ((current_price - price_5m_ago) / price_5m_ago) * 100
    rsi = calc_rsi(bars)

    if rsi is None:
        return None

    current_vol = float(bars[-1]["v"])
    avg_vol = sum(float(b["v"]) for b in bars[:-1]) / len(bars[:-1]) if len(bars) > 1 else 0
    vol_ratio = current_vol / avg_vol if avg_vol > 0 else 0

    price_ok = "✅" if price_change_5m >= EXTENDED_PRICE_CHANGE else "❌"
    vol_ok = "✅" if vol_ratio >= EXTENDED_VOLUME_MULT else "❌"
    print(f"  └ RSI:{rsi:.1f} | 5분:{price_change_5m:+.2f}%{price_ok} | 거래량:{vol_ratio:.1f}x{vol_ok}")

    if price_change_5m < EXTENDED_PRICE_CHANGE or rsi < EXTENDED_RSI:
        return None
    if check_volume and vol_ratio < EXTENDED_VOLUME_MULT:
        return None

    return {"rsi": rsi, "price_change_5m": price_change_5m, "vol_ratio": vol_ratio}


def run_scan(session: str):
    symbols = get_active_symbols()
    if not symbols:
        return

    snapshots = get_snapshots(symbols)
    if not snapshots:
        return

    ranked = build_ranked(snapshots)
    if not ranked:
        return

    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc + timedelta(hours=9)

    session_label = {
        "pre": "🌅 프리마켓",
        "regular": "📈 정규장",
        "after": "🌙 애프터마켓",
        "overnight": "🌃 주간거래"
    }[session]

    if session == "overnight":
        # 주간거래: 일중 27%+ 즉시 알림
        top = ranked[:OVERNIGHT_TOP_N]
        print(f"[{session_label}] 상위 {OVERNIGHT_TOP_N}종목 | 1위: {top[0]['symbol']} {top[0]['change_pct']:+.2f}%")

        for stock in top:
            sym = stock["symbol"]
            if stock["change_pct"] < OVERNIGHT_CHANGE:
                break  # 정렬돼 있으므로 이하 전부 미달

            if sym in last_alert:
                elapsed = (now_utc - last_alert[sym]).total_seconds() / 60
                if elapsed < COOLDOWN_MINUTES:
                    continue

            last_alert[sym] = now_utc
            message = (
                f"{session_label} <b>급등 신호!</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"📌 종목: <b>{sym}</b>\n"
                f"💰 현재가({stock['price_source']}): <b>${stock['price']:.2f}</b>\n"
                f"📉 전일종가: ${stock['prev_close']:.2f}\n"
                f"📈 일중 상승률: <b>{stock['change_pct']:+.2f}%</b>\n"
                f"🇰🇷 한국시간: {now_kst.strftime('%m/%d %H:%M:%S')}"
            )
            send_telegram(message)
            print(f"[🚀 알림!] {sym} | {stock['change_pct']:+.2f}%")

    else:
        # 프리/정규/애프터: 5분봉 & RSI 체크
        top_n = REGULAR_TOP_N if session == "regular" else EXTENDED_TOP_N
        top = ranked[:top_n]
        print(f"[{session_label}] 상위 {top_n}종목 | 1위: {top[0]['symbol']} {top[0]['change_pct']:+.2f}%")

        for stock in top:
            sym = stock["symbol"]

            if sym in last_alert:
                elapsed = (now_utc - last_alert[sym]).total_seconds() / 60
                if elapsed < COOLDOWN_MINUTES:
                    continue

            print(f"  [{sym}] 분석 중...")

            if session == "regular":
                result = analyze_regular(sym, stock["snap"])
            else:
                result = analyze_extended(sym, stock["snap"])

            if result is None:
                continue

            last_alert[sym] = now_utc
            rsi_str = f"{result['rsi']:.1f}" if result.get('rsi') else "N/A"
            vol_str = f"{result['vol_ratio']:.1f}x" if result.get('vol_ratio') else "-"

            message = (
                f"{session_label} <b>급등 신호!</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"📌 종목: <b>{sym}</b>\n"
                f"💰 현재가({stock['price_source']}): <b>${stock['price']:.2f}</b>\n"
                f"📉 전일종가: ${stock['prev_close']:.2f}\n"
                f"📈 일중 상승률: <b>{stock['change_pct']:+.2f}%</b>\n"
                f"⚡ 5분 상승: <b>{result['price_change_5m']:+.2f}%</b>\n"
                f"📊 RSI: <b>{rsi_str}</b>\n"
                f"📦 거래량: <b>{vol_str}</b>\n"
                f"🇰🇷 한국시간: {now_kst.strftime('%m/%d %H:%M:%S')}"
            )
            send_telegram(message)
            print(f"[🚀 알림!] {sym} | {stock['change_pct']:+.2f}% | RSI {rsi_str}")
            time.sleep(0.5)


def main():
    print("=" * 50)
    print("🚀 급등 감지 봇 v9 시작!")
    print(f"🌃 주간거래: 상위 {OVERNIGHT_TOP_N}종목 | 일중 {OVERNIGHT_CHANGE}%+")
    print(f"📈 정규장: 상위 {REGULAR_TOP_N}종목 | 5분 {EXTENDED_PRICE_CHANGE}%+ | RSI {REGULAR_RSI}+")
    print(f"🌅 프리/애프터: 상위 {EXTENDED_TOP_N}종목 | 5분 {EXTENDED_PRICE_CHANGE}%+ | RSI {EXTENDED_RSI}+ | 거래량 {EXTENDED_VOLUME_MULT}x+")
    print("=" * 50)

    send_telegram(
        f"🤖 <b>급등 감지 봇 v9 시작!</b>\n"
        f"🌃 주간: 일중 {OVERNIGHT_CHANGE}%+\n"
        f"📈 정규장: 5분 {EXTENDED_PRICE_CHANGE}%+ | RSI {REGULAR_RSI}+\n"
        f"🌅 프리/애프터: 5분 {EXTENDED_PRICE_CHANGE}%+ | RSI {EXTENDED_RSI}+ | 거래량 {EXTENDED_VOLUME_MULT}x+"
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
