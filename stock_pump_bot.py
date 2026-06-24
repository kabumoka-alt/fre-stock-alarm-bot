"""
미국 주식 급등 감지 봇 v18 (정규장 전용 + 시뮬레이션 + 매매일지)
- 정규장(09:30~16:00 ET)만 스캔
- 1분봉 5%+ & RSI 50+ & 거래량 2x+ 조건 충족 시 진입
- OBV 방향 참고 표시 (필터 아님)
- 매도 타이밍: +7% 1차(절반), +15% 전량, -10% 손절
- [v14] 손절 블랙리스트: 당일 손절 종목 재진입 완전 차단
- [v14] 거래량 조건 추가: 최근 5분봉 평균 대비 2배 이상일 때만 진입
- [v16] 텔레그램 알림 최소화: 매시 정각 중간 일지 / 장마감 최종 일지만 수신
- [v19] 매매일지 보유 종목에 현재가/수익률 표시 (API 조회)
- [v18] 횡보 청산: 매수 후 10분 경과 & 진입가 ±3% 이내 시 전량 청산
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta

ALPACA_API_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

# 정규장 조건
REGULAR_TOP_N        = 10
REGULAR_RSI          = 50
PRICE_CHANGE_1M      = 5.0
VOLUME_SURGE_RATIO   = 2.0   # [v14] 최근 5분봉 평균 대비 현재 거래량 배율 기준

CHECK_INTERVAL        = 60
COOLDOWN_MINUTES      = 30
SELL_COOLDOWN_MINUTES = 60

# 매도 타이밍 임계값
SELL_PARTIAL_PCT = 7.0
SELL_FULL_PCT    = 15.0
STOP_LOSS_PCT    = -10.0

# [v18] 횡보 청산 조건
SIDEWAYS_MINUTES = 10      # 매수 후 이 시간(분) 동안
SIDEWAYS_PCT     = 3.0     # 진입가 대비 ±이 범위 이내면 횡보로 판단 → 전량 청산

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

entry_prices = {}
last_alert   = {}

# ──────────────────────────────────────────
# 시뮬레이션 상태
# ──────────────────────────────────────────
SIM_INITIAL_CASH = 100.0

sim_positions: dict = {}
# { sym: {"entry": float, "qty": int, "partial_done": bool} }

sim_stats = {
    "initial_cash": SIM_INITIAL_CASH,
    "cash":         SIM_INITIAL_CASH,
    "total_pnl":    0.0,
    "trades":       0,
    "wins":         0,
    "losses":       0,
}

# [v14] 당일 손절 블랙리스트: 손절된 종목 → 당일 재진입 금지
blacklisted_today: set = set()

# 오늘 거래 일지: [{"sym", "action", "qty", "price", "pnl", "pnl_pct", "time_kst"}]
trade_log: list = []

# 매시 정각 / 장마감 전송 추적
last_hourly_report_et: int = -1
market_close_sent: bool    = False


# ──────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────

def naver_link(sym: str) -> str:
    return f'<a href="https://m.stock.naver.com/worldstock/stock/{sym}/total">{sym}</a>'


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML",
        }, timeout=5)
        if resp.status_code != 200:
            print(f"[텔레그램 오류] {resp.text}")
    except Exception as e:
        print(f"[텔레그램 예외] {e}")


def get_et_now():
    return datetime.now(timezone.utc) + timedelta(hours=-4)


def is_regular_session() -> bool:
    now_et = get_et_now()
    et_min = now_et.hour * 60 + now_et.minute
    if now_et.weekday() >= 5:
        return False
    return (9 * 60 + 30) <= et_min <= (16 * 60)


# ──────────────────────────────────────────
# 보유 종목 현황 블록
# ──────────────────────────────────────────

def holdings_block(current_prices: dict = None) -> str:
    """
    current_prices: {sym: float} — 매매일지 생성 시 API 조회한 현재가.
    None이면 진입가만 표시 (기존 동작 유지).
    """
    if not sim_positions:
        return "📭 <b>보유 종목:</b> 없음"
    lines = ["📦 <b>보유 종목:</b>"]
    for sym, pos in sim_positions.items():
        status = "1차완료" if pos["partial_done"] else "전량보유"
        if current_prices and sym in current_prices:
            cur   = current_prices[sym]
            pnl_pct = ((cur - pos["entry"]) / pos["entry"]) * 100
            pnl_amt = (cur - pos["entry"]) * pos["qty"]
            icon  = "📈" if pnl_pct >= 0 else "📉"
            lines.append(
                f"  • {naver_link(sym)} {pos['qty']}주 @ ${pos['entry']:.2f} [{status}]\n"
                f"    {icon} 현재 ${cur:.2f} ({pnl_pct:+.2f}%, {pnl_amt:+.2f}$)"
            )
        else:
            lines.append(
                f"  • {naver_link(sym)} {pos['qty']}주 @ ${pos['entry']:.2f} [{status}]"
            )
    return "\n".join(lines)


# ──────────────────────────────────────────
# 블랙리스트 현황 블록
# ──────────────────────────────────────────

def blacklist_block() -> str:
    if not blacklisted_today:
        return ""
    syms = ", ".join(sorted(blacklisted_today))
    return f"🚫 <b>당일 블랙리스트:</b> {syms}"


# ──────────────────────────────────────────
# 매매일지 빌더
# ──────────────────────────────────────────

def build_trade_report(title: str) -> str:
    now_kst          = datetime.now(timezone.utc) + timedelta(hours=9)
    total_return_pct = (sim_stats["total_pnl"] / sim_stats["initial_cash"]) * 100
    win_rate         = (
        sim_stats["wins"] / sim_stats["trades"] * 100
        if sim_stats["trades"] > 0 else 0.0
    )

    # [v19] 보유 종목 현재가 조회
    current_prices = {}
    if sim_positions:
        snaps = get_snapshots(list(sim_positions.keys()))
        for sym, snap in snaps.items():
            price, _ = get_live_price(snap)
            if price:
                current_prices[sym] = float(price)

    lines = [
        f"📋 <b>{title}</b>",
        f"🇰🇷 {now_kst.strftime('%m/%d %H:%M')} KST",
        f"━━━━━━━━━━━━━━",
    ]

    if trade_log:
        lines.append("📝 <b>거래 내역:</b>")
        for t in trade_log:
            icon = "📥" if t["action"] == "BUY" else ("📈" if t["pnl"] >= 0 else "📉")
            if t["action"] == "BUY":
                lines.append(
                    f"  {icon} {t['time_kst']} {t['sym']} {t['qty']}주 매수 @ ${t['price']:.2f}"
                )
            else:
                lines.append(
                    f"  {icon} {t['time_kst']} {t['sym']} {t['qty']}주 {t['reason']} "
                    f"@ ${t['price']:.2f} ({t['pnl']:+.2f}$, {t['pnl_pct']:+.2f}%)"
                )
    else:
        lines.append("📝 거래 내역: 없음")

    lines.append("━━━━━━━━━━━━━━")
    lines.append(holdings_block(current_prices))

    bl = blacklist_block()
    if bl:
        lines.append(bl)

    lines.append("━━━━━━━━━━━━━━")

    pnl_sign = "+" if sim_stats["total_pnl"] >= 0 else ""
    lines += [
        f"💵 예수금: <b>${sim_stats['cash']:.2f}</b>",
        f"💰 누적 손익: <b>{pnl_sign}{sim_stats['total_pnl']:.2f}$</b> "
        f"(<b>{total_return_pct:+.2f}%</b>)",
        f"🏆 {sim_stats['wins']}승 {sim_stats['losses']}패 "
        f"(승률 {win_rate:.0f}%) | 총 {sim_stats['trades']}거래",
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────
# 시뮬레이션 헬퍼
# ──────────────────────────────────────────

def sim_open(sym: str, price: float) -> bool:
    """매수 신호 → 2주 매수 기록 + 예수금 차감."""
    if sym in sim_positions:
        return False
    # [v14] 블랙리스트 종목 재진입 차단
    if sym in blacklisted_today:
        print(f"  [시뮬 매수 차단] {sym} — 당일 블랙리스트")
        return False
    cost = price * 2
    if sim_stats["cash"] < cost:
        print(f"  [시뮬 매수 불가] {sym} | 필요: ${cost:.2f} | 예수금: ${sim_stats['cash']:.2f}")
        return False
    sim_stats["cash"] -= cost
    sim_positions[sym] = {"entry": price, "qty": 2, "partial_done": False}
    now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
    trade_log.append({
        "action": "BUY", "sym": sym, "qty": 2, "price": price,
        "pnl": 0.0, "pnl_pct": 0.0, "reason": "매수",
        "time_kst": now_kst.strftime("%H:%M"),
    })
    print(f"  [시뮬 매수] {sym} 2주 @ ${price:.2f} | 잔여 예수금: ${sim_stats['cash']:.2f}")
    return True


def sim_close(sym: str, exit_price: float, reason: str, qty: int = None) -> str:
    """
    포지션 청산.
    qty=None 이면 전량 청산.
    손절(-4%) 시 블랙리스트 등록.
    반환값: 텔레그램 시뮬 요약 문자열.
    """
    pos = sim_positions.get(sym)
    if not pos:
        return ""

    entry_price = pos["entry"]   # 청산 전에 미리 저장
    close_qty   = qty if qty is not None else pos["qty"]
    pnl         = (exit_price - entry_price) * close_qty
    pnl_pct     = ((exit_price - entry_price) / entry_price) * 100

    sim_stats["cash"] += exit_price * close_qty
    pos["qty"] -= close_qty

    if pos["qty"] <= 0:
        del sim_positions[sym]
        sim_stats["total_pnl"] += pnl
        sim_stats["trades"]    += 1
        if pnl >= 0:
            sim_stats["wins"]   += 1
        else:
            sim_stats["losses"] += 1
    else:
        pos["partial_done"]     = True
        sim_stats["total_pnl"] += pnl

    # [v14] 손절 시 당일 블랙리스트 등록
    if "손절" in reason:
        blacklisted_today.add(sym)
        print(f"  [블랙리스트 등록] {sym} — 당일 재진입 금지")

    now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
    trade_log.append({
        "action": "SELL", "sym": sym, "qty": close_qty, "price": exit_price,
        "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason,
        "time_kst": now_kst.strftime("%H:%M"),
    })

    win_rate         = (
        sim_stats["wins"] / sim_stats["trades"] * 100
        if sim_stats["trades"] > 0 else 0.0
    )
    total_return_pct = (sim_stats["total_pnl"] / sim_stats["initial_cash"]) * 100

    bl_note = f"\n🚫 {sym} 당일 블랙리스트 등록" if "손절" in reason else ""

    summary = (
        f"\n\n💹 <b>[시뮬레이션]</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📤 청산: {reason} | {close_qty}주 @ ${exit_price:.2f}\n"
        f"📥 진입가: ${entry_price:.2f}\n"
        f"{'📈' if pnl >= 0 else '📉'} 건별 손익: "
        f"<b>{'+' if pnl >= 0 else ''}{pnl:.2f}$ ({pnl_pct:+.2f}%)</b>\n"
        f"💵 예수금: <b>${sim_stats['cash']:.2f}</b>\n"
        f"💰 누적 손익: <b>{'+' if sim_stats['total_pnl'] >= 0 else ''}"
        f"{sim_stats['total_pnl']:.2f}$</b> (<b>{total_return_pct:+.2f}%</b>)\n"
        f"🏆 {sim_stats['wins']}승 {sim_stats['losses']}패 "
        f"(승률 {win_rate:.0f}%) | 총 {sim_stats['trades']}거래\n"
        f"{holdings_block()}"
        f"{bl_note}"
    )
    return summary


# ──────────────────────────────────────────
# Alpaca API
# ──────────────────────────────────────────

def get_active_symbols():
    url    = "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives"
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
    url    = "https://data.alpaca.markets/v2/stocks/snapshots"
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
    url    = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
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


# ──────────────────────────────────────────
# 지표 계산
# ──────────────────────────────────────────

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
    if len(bars) < 3:
        return "-"
    obv, obv_list = 0, []
    for i, bar in enumerate(bars):
        if i == 0:
            obv_list.append(obv)
            continue
        close      = float(bar["c"])
        prev_close = float(bars[i - 1]["c"])
        vol        = float(bar["v"])
        if close > prev_close:
            obv += vol
        elif close < prev_close:
            obv -= vol
        obv_list.append(obv)
    recent  = obv_list[-5:]
    rising  = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
    falling = len(recent) - 1 - rising
    if rising  >= 3:
        return "📈상승"
    elif falling >= 3:
        return "📉하락"
    else:
        return "➡️횡보"


def calc_volume_surge(bars: list) -> tuple[float, bool]:
    """
    [v14] 거래량 급등 체크.
    최근 20봉 평균 거래량 대비 마지막 봉 거래량 비율 계산.
    반환: (배율, 조건충족여부)
    """
    if len(bars) < 6:
        return 0.0, False
    # 마지막 봉 제외한 최근 20봉(또는 가능한 봉) 평균
    history_bars = bars[:-1][-20:]
    if not history_bars:
        return 0.0, False
    avg_vol = sum(float(b["v"]) for b in history_bars) / len(history_bars)
    if avg_vol <= 0:
        return 0.0, False
    cur_vol = float(bars[-1]["v"])
    ratio   = cur_vol / avg_vol
    return ratio, ratio >= VOLUME_SURGE_RATIO


def calc_atr(bars: list, period: int = 14) -> float:
    """
    [v17] ATR (Average True Range) 계산.
    True Range = max(고-저, |고-전일종가|, |저-전일종가|)
    반환: ATR 값 (데이터 부족 시 0.0)
    """
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        high      = float(bars[i]["h"])
        low       = float(bars[i]["l"])
        prev_close = float(bars[i - 1]["c"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period


def get_live_price(snap: dict):
    lt     = snap.get("latestTrade", {})
    mb     = snap.get("minuteBar",   {})
    db     = snap.get("dailyBar",    {})
    price  = lt.get("p") or mb.get("c") or db.get("c")
    source = "호가" if lt.get("p") else ("1분봉" if mb.get("c") else "종가")
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
            "symbol":       sym,
            "price":        current_price,
            "price_source": price_source,
            "prev_close":   prev_close,
            "change_pct":   change_pct,
            "snap":         snap,
        })
    return sorted(ranked, key=lambda x: x["change_pct"], reverse=True)


# ──────────────────────────────────────────
# 매도 타이밍 체크
# ──────────────────────────────────────────

def check_sell_timing(sym: str, current_price: float, price_source: str):
    if sym not in entry_prices:
        return
    entry       = entry_prices[sym]
    entry_price = entry["entry"]
    now_utc     = datetime.now(timezone.utc)
    now_kst     = now_utc + timedelta(hours=9)
    gain_pct    = ((current_price - entry_price) / entry_price) * 100
    ticker_link = naver_link(sym)

    def cooldown_ok(key):
        last = entry.get(key)
        if last is None:
            return True
        return (now_utc - last).total_seconds() / 60 >= SELL_COOLDOWN_MINUTES

    # ── 횡보 청산 (매수 후 10분 경과 & ±3% 이내) ──
    elapsed_min = (now_utc - entry["time"]).total_seconds() / 60
    if elapsed_min >= SIDEWAYS_MINUTES and not entry.get("sideways_done"):
        if 0 <= gain_pct <= SIDEWAYS_PCT:
            entry["sideways_done"] = True
            if sym in sim_positions:
                sim_close(sym, current_price, "횡보청산", qty=None)
            print(f"[➡️ 횡보청산] {sym} | ${entry_price:.2f} → ${current_price:.2f} ({gain_pct:+.2f}%) | {elapsed_min:.0f}분 경과")
            return

    # ── 손절 ──
    if gain_pct <= STOP_LOSS_PCT:
        if cooldown_ok("stop"):
            entry["stop"] = now_utc
            if sym in sim_positions:
                sim_close(sym, current_price, "손절(-10%)", qty=None)
            print(f"[🔴 손절] {sym} | ${entry_price:.2f} → ${current_price:.2f} ({gain_pct:+.2f}%)")
        return

    # ── +15% 전량 매도 ──
    if gain_pct >= SELL_FULL_PCT:
        if cooldown_ok("alert2"):
            entry["alert2"] = now_utc
            if sym in sim_positions:
                sim_close(sym, current_price, "+15% 전량", qty=None)
            print(f"[🟢 전량매도] {sym} | ${entry_price:.2f} → ${current_price:.2f} ({gain_pct:+.2f}%)")
        return

    # ── +7% 1차 매도 ──
    if gain_pct >= SELL_PARTIAL_PCT:
        if cooldown_ok("alert1"):
            entry["alert1"] = now_utc
            pos = sim_positions.get(sym)
            if pos and not pos.get("partial_done"):
                sim_close(sym, current_price, "+7% 1차(절반)", qty=1)
            print(f"[🟡 1차매도] {sym} | ${entry_price:.2f} → ${current_price:.2f} ({gain_pct:+.2f}%)")


# ──────────────────────────────────────────
# 종목 분석
# ──────────────────────────────────────────

def analyze_regular(sym: str, snap: dict):
    bars = get_bars(sym)
    if not bars or len(bars) < 6:
        print(f"  └ 데이터 부족: {len(bars) if bars else 0}개")
        return None

    latest_price, _ = get_live_price(snap)
    current_price   = latest_price or float(bars[-1]["c"])
    price_1m_ago    = float(bars[-2]["c"])
    if price_1m_ago <= 0:
        return None

    price_change_1m    = ((current_price - price_1m_ago) / price_1m_ago) * 100
    rsi                = calc_rsi(bars)
    if rsi is None:
        return None

    # [v14] 거래량 급등 체크
    vol_ratio, vol_ok  = calc_volume_surge(bars)
    obv_label          = calc_obv(bars)

    # [v17] ATR 계산
    atr = calc_atr(bars)

    price_ok_str = "✅" if price_change_1m >= PRICE_CHANGE_1M else "❌"
    vol_ok_str   = "✅" if vol_ok else "❌"
    print(
        f"  └ RSI:{rsi:.1f} | 1분:{price_change_1m:+.2f}%{price_ok_str} "
        f"| 거래량:{vol_ratio:.1f}x{vol_ok_str} | ATR:{atr:.3f} | OBV:{obv_label}"
    )

    # 진입 조건: 1분 상승 + 거래량 급등
    if price_change_1m < PRICE_CHANGE_1M or not vol_ok:
        return None

    return {
        "rsi":             rsi,
        "price_change_1m": price_change_1m,
        "obv_label":       obv_label,
        "vol_ratio":       vol_ratio,
        "atr":             atr,
    }


# ──────────────────────────────────────────
# 정기 리포트 (매시 정각 / 장마감)
# ──────────────────────────────────────────

def check_scheduled_reports():
    global last_hourly_report_et, market_close_sent

    now_et  = get_et_now()
    et_hour = now_et.hour
    et_min  = now_et.minute
    weekday = now_et.weekday()

    if weekday >= 5:
        return

    # ── 장 종료 최종 일지 (16:00~16:02 ET, 1회) ──
    if et_hour == 16 and et_min <= 2 and not market_close_sent:
        market_close_sent = True
        print("[📊 장마감 최종 매매일지 전송]")
        send_telegram(build_trade_report("🔔 장 종료 최종 매매일지"))
        return

    # 장중(09:30~16:00)에만 정각 리포트
    if not ((9 * 60 + 30) <= (et_hour * 60 + et_min) <= 16 * 60):
        return

    # ── 매시 정각 중간 일지 (XX:00~XX:02, 1회/시) ──
    if et_min <= 2 and et_hour != last_hourly_report_et and et_hour >= 10:
        last_hourly_report_et = et_hour
        now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
        title   = f"🕐 {et_hour}:00 ET ({now_kst.strftime('%H:%M')} KST) 중간 매매일지"
        print(f"[📊 정각 매매일지 전송] {et_hour}:00 ET")
        send_telegram(build_trade_report(title))


# ──────────────────────────────────────────
# 메인 스캔
# ──────────────────────────────────────────

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

    snap_map = {s["symbol"]: s for s in ranked}
    for sym in list(entry_prices.keys()):
        if sym in snap_map:
            stock = snap_map[sym]
            check_sell_timing(sym, stock["price"], stock["price_source"])

    top = ranked[:REGULAR_TOP_N]
    print(f"[정규장] 상위 {REGULAR_TOP_N}종목 | 1위: {top[0]['symbol']} {top[0]['change_pct']:+.2f}%")
    print(f"  {holdings_block().replace(chr(10), ' | ')}")
    if blacklisted_today:
        print(f"  🚫 블랙리스트: {', '.join(sorted(blacklisted_today))}")

    # [v17] 상위 10종목 ATR 계산 후 높은 순으로 재정렬
    top_with_atr = []
    for stock in top:
        sym = stock["symbol"]
        if sym in blacklisted_today:
            continue
        bars = get_bars(sym)
        atr  = calc_atr(bars) if bars else 0.0
        top_with_atr.append({**stock, "_atr": atr})

    top_with_atr.sort(key=lambda x: x["_atr"], reverse=True)
    print(f"  [ATR 재정렬] " + " | ".join(
        f"{s['symbol']}({s['_atr']:.3f})" for s in top_with_atr[:5]
    ))

    for stock in top_with_atr:
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
            "entry": stock["price"], "time": now_utc,
            "alert1": None, "alert2": None, "stop": None,
            "sideways_done": False,
        }

        bought      = sim_open(sym, stock["price"])
        ticker_link = naver_link(sym)

        print(
            f"[🚀 감지] {sym} | {stock['change_pct']:+.2f}% | RSI {result['rsi']:.1f} "
            f"| 거래량 {result['vol_ratio']:.1f}x | ATR {result['atr']:.3f} | 진입가 ${stock['price']:.2f}"
        )
        time.sleep(0.5)


# ──────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────

def main():
    global market_close_sent

    print("=" * 60)
    print("🚀 급등 감지 봇 v19 (정규장 전용 + 시뮬 + 매매일지) 시작!")
    print(f"📈 정규장: 상위 {REGULAR_TOP_N}종목 | 1분 {PRICE_CHANGE_1M}%+ | RSI {REGULAR_RSI}+")
    print(f"📦 거래량 조건: 평균 대비 {VOLUME_SURGE_RATIO}x 이상")
    print(f"🎯 매도: +{SELL_PARTIAL_PCT}% 1차 | +{SELL_FULL_PCT}% 전량 | {STOP_LOSS_PCT}% 손절")
    print(f"➡️  횡보청산: {SIDEWAYS_MINUTES}분 경과 & 0~+{SIDEWAYS_PCT}% 이내")
    print(f"🚫 손절 시 당일 블랙리스트 등록 (재진입 차단)")
    print("=" * 60)

    send_telegram(
        f"🤖 <b>급등 감지 봇 v19 시작!</b>\n"
        f"📈 1분 {PRICE_CHANGE_1M}%+ | RSI {REGULAR_RSI}+ | 거래량 {VOLUME_SURGE_RATIO}x+\n"
        f"📊 상위 {REGULAR_TOP_N}종목 → ATR 높은 순 재정렬 후 진입\n"
        f"🚫 손절(-10%) 종목 당일 재진입 차단 (블랙리스트)\n"
        f"💹 텔레그램: 매시 정각 일지 / 장마감 최종 일지만 수신"
    )

    while True:
        now_str = datetime.now().strftime('%H:%M:%S')
        now_et  = get_et_now()

        # 날짜 바뀌면 당일 플래그 리셋
        if now_et.hour == 9 and now_et.minute < 30:
            if market_close_sent:
                market_close_sent = False
                print("[리셋] 장마감 플래그 초기화")
            if blacklisted_today:
                blacklisted_today.clear()
                print("[리셋] 블랙리스트 초기화 (새 장 시작)")

        check_scheduled_reports()

        if not is_regular_session():
            print(f"[{now_str}] 정규장 외 시간 — 대기 중...")
        else:
            print(f"\n[{now_str}] 정규장 스캔 시작")
            run_scan()

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
