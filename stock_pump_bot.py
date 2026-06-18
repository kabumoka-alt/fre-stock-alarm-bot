"""
미국 주식 급등 감지 봇 v11 (정규장 전용)
- 정규장(09:30~16:00 ET)만 스캔
- 5분봉 5%+ & RSI 50+ 조건
- OBV 방향 참고 표시 (필터 아님)
- 매도 타이밍: +7% 1차, +15% 전량, -4% 손절
- 매도 후에도 모니터링 유지 (재진입 대응)
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
PRICE_CHANGE_5M = 5.0

CHECK_INTERVAL = 60
COOLDOWN_MINUTES = 30

# 매도 알림 쿨다운
SELL_COOLDOWN_MINUTES = 60

# 매도 타이밍 임계값
SELL_PARTIAL_PCT = 7.0    # +7% 1차 매도
SELL_FULL_PCT = 15.0      # +15% 전량 매도
STOP_LOSS_PCT = -4.0      # -4% 손절

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

entry_prices = {}
last_alert = {}


def naver_link(sym: str) -> str:
    return f'<a href="https://m.stock.naver.com/worldstock/stock/{sym}/total">{sym}</a>'


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


def is_regular_session() -> bool:
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc + timedelta(hours=-4)
    weekday = now_et.weekday()
    et_min = now_et.hour * 60 + now_et.minute

    if weekday >= 5:
        return False
    return (9 * 60 + 30) <= et_min <= (16 * 60)


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
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    params = {"timeframe": "1Min", "limit": limit, "sort": "asc"}
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if resp.status_code == 200:
            bars = resp.json().get("bars", [])
            if bars:
                return bars
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


def calc_obv(bars: list) -> str:
    """OBV 방향 판단 (참고용, 필터 아님) — 최근 5봉 기준"""
    if len(bars) < 3:
        return "-"

    obv = 0
    obv_list = []
    for i, bar in enumerate(bars):
        if i == 0:
            obv_list.append(obv)
            continue
        close = float(bar["c"])
        prev_close = float(bars[i - 1]["c"])
        vol = float(bar["v"])
        if close > prev_close:
            obv += vol
        elif close < prev_close:
            obv -= vol
        obv_list.append(obv)

    recent = obv_list[-5:]
    rising = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
    falling = len(recent) - 1 - rising

    if rising >= 3:
        return "📈상승"
    elif falling >= 3:
        return "📉하락"
    else:
        return "➡️횡보"


def get_live_price(snap: dict):
    latest_trade = snap.get("latestTrade", {})
    minute_bar = snap.get("minuteBar", {})
    daily_bar = snap.get("dailyBar", {})
    price = latest_trade.get("p") or minute_bar.get("c") or daily_bar.get("c")
    source = "호가" if latest_trade.get("p") else ("1분봉" if minute_bar.get("c") else "종가")
    return price, source


def build_ranked(snapshots: dict):
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


def check_sell_timing(sym: str, current_price: float, price_source: str):
    if sym not in entry_prices:
        return

    entry = entry_prices[sym]
    entry_price = entry["entry"]
    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc + timedelta(hours=9)
    gain_pct = ((current_price - entry_price) / entry_price) * 100
    ticker_link = naver_link(sym)

    def cooldown_ok(key):
        last = entry.get(key)
        if last is None:
            return True
        return (now_utc - last).total_seconds() / 60 >= SELL_COOLDOWN_MINUTES

    if gain_pct <= STOP_LOSS_PCT:
        if cooldown_ok("stop"):
            entry["stop"] = now_utc
            send_telegram(
                f"🔴 <b>손절 타이밍!</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"📌 종목: <b>{ticker_link}</b>\n"
                f"💰 현재가({price_source}): <b>${current_price:.2f}</b>\n"
                f"📥 진입가: ${entry_price:.2f}\n"
                f"📉 수익률: <b>{gain_pct:+.2f}%</b>\n"
                f"⚠️ -4% 손절 구간 진입\n"
                f"🇰🇷 한국시간: {now_kst.strftime('%m/%d %H:%M:%S')}"
            )
            print(f"[🔴 손절] {sym} | ${entry_price:.2f} → ${current_price:.2f} ({gain_pct:+.2f}%)")
        return

    if gain_pct >= SELL_FULL_PCT:
        if cooldown_ok("alert2"):
            entry["alert2"] = now_utc
            send_telegram(
                f"🟢 <b>전량 매도 타이밍!</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"📌 종목: <b>{ticker_link}</b>\n"
                f"💰 현재가({price_source}): <b>${current_price:.2f}</b>\n"
                f"📥 진입가: ${entry_price:.2f}\n"
                f"📈 수익률: <b>{gain_pct:+.2f}%</b>\n"
                f"✅ +15% 전량 매도 구간\n"
                f"🇰🇷 한국시간: {now_kst.strftime('%m/%d %H:%M:%S')}"
            )
            print(f"[🟢 전량매도] {sym} | ${entry_price:.2f} → ${current_price:.2f} ({gain_pct:+.2f}%)")
        return

    if gain_pct >= SELL_PARTIAL_PCT:
        if cooldown_ok("alert1"):
            entry["alert1"] = now_utc
            send_telegram(
                f"🟡 <b>1차 매도 타이밍!</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"📌 종목: <b>{ticker_link}</b>\n"
                f"💰 현재가({price_source}): <b>${current_price:.2f}</b>\n"
                f"📥 진입가: ${entry_price:.2f}\n"
                f"📈 수익률: <b>{gain_pct:+.2f}%</b>\n"
                f"💡 +7% → 절반 매도 후 나머지 홀드\n"
                f"🇰🇷 한국시간: {now_kst.strftime('%m/%d %H:%M:%S')}"
            )
            print(f"[🟡 1차매도] {sym} | ${entry_price:.2f} → ${current_price:.2f} ({gain_pct:+.2f}%)")


def analyze_regular(sym: str, snap: dict):
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

    obv_label = calc_obv(bars)
    price_ok = "✅" if price_change_5m >= PRICE_CHANGE_5M else "❌"
    print(f"  └ RSI:{rsi:.1f} | 5분:{price_change_5m:+.2f}%{price_ok} | OBV:{obv_label}")

    if price_change_5m < PRICE_CHANGE_5M or rsi < REGULAR_RSI:
        return None

    return {"rsi": rsi, "price_change_5m": price_change_5m, "obv_label": obv_label}


def run_scan():
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

    # 매도 타이밍 체크
    snap_map = {s["symbol"]: s for s in ranked}
    for sym in list(entry_prices.keys()):
        if sym in snap_map:
            stock = snap_map[sym]
            check_sell_timing(sym, stock["price"], stock["price_source"])

    top = ranked[:REGULAR_TOP_N]
    print(f"[정규장] 상위 {REGULAR_TOP_N}종목 | 1위: {top[0]['symbol']} {top[0]['change_pct']:+.2f}%")

    for stock in top:
        sym = stock["symbol"]

        if sym in last_alert:
            elapsed = (now_utc - last_alert[sym]).total_seconds() / 60
            if elapsed < COOLDOWN_MINUTES:
                continue

        print(f"  [{sym}] 분석 중...")
        result = analyze_regular(sym, stock["snap"])
        if result is None:
            continue

        last_alert[sym] = now_utc
        entry_prices[sym] = {
            "entry": stock["price"],
            "time": now_utc,
            "alert1": None,
            "alert2": None,
            "stop": None
        }

        rsi_str = f"{result['rsi']:.1f}"
        obv_str = result["obv_label"]
        ticker_link = naver_link(sym)

        message = (
            f"📈 정규장 <b>급등 신호!</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"📌 종목: <b>{ticker_link}</b>\n"
            f"💰 현재가({stock['price_source']}): <b>${stock['price']:.2f}</b>\n"
            f"📉 전일종가: ${stock['prev_close']:.2f}\n"
            f"📈 일중 상승률: <b>{stock['change_pct']:+.2f}%</b>\n"
            f"⚡ 5분 상승: <b>{result['price_change_5m']:+.2f}%</b>\n"
            f"📊 RSI: <b>{rsi_str}</b> | OBV: <b>{obv_str}</b>\n"
            f"📥 진입가 기록: ${stock['price']:.2f}\n"
            f"🎯 매도선: +7%(${stock['price']*1.07:.2f}) / +15%(${stock['price']*1.15:.2f}) | 손절: -4%(${stock['price']*0.96:.2f})\n"
            f"🇰🇷 한국시간: {now_kst.strftime('%m/%d %H:%M:%S')}"
        )
        send_telegram(message)
        print(f"[🚀 알림!] {sym} | {stock['change_pct']:+.2f}% | RSI {rsi_str} | OBV {obv_str} | 진입가 ${stock['price']:.2f}")
        time.sleep(0.5)


def main():
    print("=" * 50)
    print("🚀 급등 감지 봇 v11 (정규장 전용) 시작!")
    print(f"📈 정규장: 상위 {REGULAR_TOP_N}종목 | 5분 {PRICE_CHANGE_5M}%+ | RSI {REGULAR_RSI}+ | OBV 참고")
    print(f"🎯 매도: +{SELL_PARTIAL_PCT}% 1차 | +{SELL_FULL_PCT}% 전량 | {STOP_LOSS_PCT}% 손절")
    print("=" * 50)

    send_telegram(
        f"🤖 <b>급등 감지 봇 v11 (정규장 전용) 시작!</b>\n"
        f"📈 정규장: 5분 {PRICE_CHANGE_5M}%+ | RSI {REGULAR_RSI}+ | OBV 참고표시\n"
        f"🎯 매도알림: +{SELL_PARTIAL_PCT}% 1차 / +{SELL_FULL_PCT}% 전량 / {STOP_LOSS_PCT}% 손절"
    )

    while True:
        now_str = datetime.now().strftime('%H:%M:%S')
        if not is_regular_session():
            print(f"[{now_str}] 정규장 외 시간 — 대기 중...")
        else:
            print(f"\n[{now_str}] 정규장 스캔 시작")
            run_scan()
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
