"""
미국 주식 급등 감지 봇 v30 (정규장 전용 + 시뮬레이션 + 매매일지)
- 정규장(09:30~16:00 ET)만 스캔
- 1분봉 3%+ 조건 충족 시 진입 (거래량은 참고용 표시만)
- OBV 방향 참고 표시 (필터 아님)
- 매도 타이밍: +7% 1차(절반), +15% 전량, -10% 손절
- [v16] 텔레그램 알림 최소화: 매시 정각 중간 일지 / 장마감 최종 일지만 수신
- [v17] ATR 기반 변동성 정렬: 상위 30종목 중 ATR 높은 순으로 재정렬 후 진입
- [v18] 횡보 청산: 매수 후 10분 경과 & +3~+7% 구간 시 전량 청산
- [v19] 매매일지 보유 종목에 현재가/수익률 표시 (API 조회)
- [v21] 스크리너 변경: most-actives(거래횟수) → movers(상승률 기준)
- [v25] 저가주 필터: $1 미만 종목 진입 제외
- [v30] 예수금 배분 방식 변경: 30% 고정 → 남은 슬롯 균등 분배 (예수금 ÷ 남은 슬롯)
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
REGULAR_TOP_N        = 30
REGULAR_RSI          = 50
PRICE_CHANGE_1M      = 3.0
VOLUME_SURGE_RATIO   = 1.5   # 최근 봉 평균 대비 현재 거래량 배율 기준
MIN_PRICE            = 1.0   # [v25] 저가주 필터: $1 미만 종목 진입 제외

CHECK_INTERVAL        = 60
COOLDOWN_MINUTES      = 30
SELL_COOLDOWN_MINUTES = 60
MAX_BUY_PER_SCAN      = 3    # [v24] 스캔 1회당 신규 매수 최대 종목 수
MAX_POSITIONS         = 7    # [v29] 동시 보유 최대 종목 수

# 매도 타이밍 임계값
SELL_PARTIAL_PCT = 7.0
SELL_FULL_PCT    = 15.0
STOP_LOSS_PCT    = -10.0

# [v18] 횡보 청산 조건
SIDEWAYS_MINUTES = 10
SIDEWAYS_MIN_PCT = 3.0     # [v20] 횡보 구간 하한
SIDEWAYS_MAX_PCT = 7.0     # [v20] 횡보 구간 상한 (+3~+7% 이내면 횡보 청산)

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
# [v30] 예수금 배분: 남은 슬롯(MAX_POSITIONS - 현재보유) 균등 분배 방식으로 전환

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

# [v23] 종목당 손절 횟수 제한으로 전환
# stop_loss_count[sym] = 당일 손절 횟수, MAX_STOP_LOSS_COUNT 도달 시 당일 블랙리스트
stop_loss_count: dict = {}
MAX_STOP_LOSS_COUNT = 2   # 2회까지는 재진입 허용, 3회째 손절부터 당일 차단

# 손절 횟수가 MAX_STOP_LOSS_COUNT 이상 도달한 종목 (실질 블랙리스트)
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


def _send_telegram_chunk(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML",
        }, timeout=5)
        if resp.status_code != 200:
            print(f"[텔레그램 오류] {resp.text}")
    except Exception as e:
        print(f"[텔레그램 예외] {e}")


def send_telegram(message: str):
    """텔레그램 전송. 4096자 초과 시 줄 단위로 나눠 여러 메시지로 전송."""
    TELEGRAM_MAX = 4000   # 안전 여유 (실제 한도 4096)
    if len(message) <= TELEGRAM_MAX:
        _send_telegram_chunk(message)
        return

    # 줄 단위로 잘라서 청크 구성
    lines = message.split("\n")
    chunk = ""
    for line in lines:
        # 한 줄 자체가 너무 길면 강제로 잘라 전송
        if len(line) > TELEGRAM_MAX:
            if chunk:
                _send_telegram_chunk(chunk)
                chunk = ""
            for i in range(0, len(line), TELEGRAM_MAX):
                _send_telegram_chunk(line[i:i + TELEGRAM_MAX])
            continue
        # 현재 청크에 이 줄을 더하면 한도 초과 → 지금까지 청크 전송 후 새로 시작
        if len(chunk) + len(line) + 1 > TELEGRAM_MAX:
            _send_telegram_chunk(chunk)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        _send_telegram_chunk(chunk)


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
    syms = ", ".join(
        f"{sym}({stop_loss_count.get(sym, 0)}회)" for sym in sorted(blacklisted_today)
    )
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
    """매수 신호 → 남은 슬롯 균등 분배 방식으로 매수 (예수금 ÷ 남은 슬롯)."""
    if sym in sim_positions:
        return False
    if sym in blacklisted_today:
        print(f"  [시뮬 매수 차단] {sym} — 당일 블랙리스트")
        return False
    # [v29] 동시 보유 종목 수 제한
    if len(sim_positions) >= MAX_POSITIONS:
        print(f"  [시뮬 매수 불가] {sym} — 보유 종목 {len(sim_positions)}개 (최대 {MAX_POSITIONS}개)")
        return False
    # [v30] 남은 슬롯에 예수금 균등 분배
    remaining_slots = MAX_POSITIONS - len(sim_positions)
    budget          = sim_stats["cash"] / remaining_slots
    qty             = int(budget // price)
    if qty < 1:
        print(f"  [시뮬 매수 불가] {sym} | 예수금 부족 (슬롯예산={budget:.2f}, 1주={price:.2f})")
        return False
    cost = price * qty
    sim_stats["cash"] -= cost
    sim_positions[sym] = {"entry": price, "qty": qty, "partial_done": False}
    now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
    trade_log.append({
        "action": "BUY", "sym": sym, "qty": qty, "price": price,
        "pnl": 0.0, "pnl_pct": 0.0, "reason": "매수",
        "time_kst": now_kst.strftime("%H:%M"),
    })
    print(f"  [시뮬 매수] {sym} {qty}주 @ ${price:.2f} (슬롯예산={cost:.2f}, 남은슬롯 {remaining_slots}) | 잔여: ${sim_stats['cash']:.2f}")
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

    # [v23] 손절 시 카운트 증가, 허용 횟수(MAX_STOP_LOSS_COUNT) 도달 시에만 블랙리스트 등록
    if "손절" in reason:
        stop_loss_count[sym] = stop_loss_count.get(sym, 0) + 1
        cnt = stop_loss_count[sym]
        if cnt > MAX_STOP_LOSS_COUNT:
            blacklisted_today.add(sym)
            print(f"  [블랙리스트 등록] {sym} — 손절 {cnt}회 누적, 당일 재진입 금지")
        else:
            remaining = MAX_STOP_LOSS_COUNT - cnt
            print(f"  [손절 카운트] {sym} — {cnt}회째 (재진입 {remaining}회 더 허용)")

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

def is_warrant(sym: str) -> bool:
    """
    [v27] 워런트/유닛 등 파생 티커 판별.
    - '.WS' 접미사 (예: TE.WS)
    - 'W'로 끝나는 5글자 이상 티커 (예: EVLVW, AUROW, AFRIW)
    - '.U', '.UN' 유닛, '.R' 라이트 등
    """
    s = sym.upper()
    if any(suffix in s for suffix in (".WS", ".WT", ".U", ".UN", ".RT", ".R")):
        return True
    # W로 끝나는 5글자 이상 티커는 워런트일 가능성 높음
    if len(s) >= 5 and s.endswith("W") and "." not in s:
        return True
    return False


def get_active_symbols():
    # [v21] most-actives(거래횟수) → movers(상승률 기준)으로 변경
    url    = "https://data.alpaca.markets/v1beta1/screener/stocks/movers"
    params = {"top": 50}
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if resp.status_code == 200:
            data    = resp.json()
            gainers = data.get("gainers", [])
            # [v27] 워런트/유닛 제외
            symbols  = [d["symbol"] for d in gainers if not is_warrant(d["symbol"])]
            excluded = [d["symbol"] for d in gainers if is_warrant(d["symbol"])]
            if excluded:
                print(f"  [워런트 제외] {excluded}")
            return symbols
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
    거래량 급등 체크 — 최근 5봉 합산 vs 그 이전 봉 평균×5 비교 (참고용 표시).
    봉 수가 적으면 가용 데이터로 계산. 반환: (배율, 조건충족여부)
    """
    if len(bars) < 6:
        return 0.0, False
    recent_5   = bars[-5:]           # 최근 5봉
    history    = bars[:-5]           # 그 이전 전체 (최대 20봉으로 제한)
    history    = history[-20:]
    if not history:
        return 0.0, False
    avg_vol_per_bar = sum(float(b["v"]) for b in history) / len(history)
    if avg_vol_per_bar <= 0:
        return 0.0, False
    recent_vol  = sum(float(b["v"]) for b in recent_5)
    baseline    = avg_vol_per_bar * 5   # 이전 평균을 5봉 기준으로 환산
    ratio       = recent_vol / baseline
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

    # ── 횡보 청산 (매수 후 10분 경과 & +3~+7% 미만) ──
    elapsed_min = (now_utc - entry["time"]).total_seconds() / 60
    if elapsed_min >= SIDEWAYS_MINUTES and not entry.get("sideways_done"):
        if SIDEWAYS_MIN_PCT <= gain_pct < SIDEWAYS_MAX_PCT:
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
                half = max(1, pos["qty"] // 2)
                sim_close(sym, current_price, "+7% 1차(절반)", qty=half)
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

    # [v25] 저가주 필터: $1 미만 제외
    if current_price < MIN_PRICE:
        print(f"  └ 저가주 제외: ${current_price:.2f} < ${MIN_PRICE}")
        return None

    price_1m_ago    = float(bars[-2]["c"])
    if price_1m_ago <= 0:
        return None

    price_change_1m    = ((current_price - price_1m_ago) / price_1m_ago) * 100
    rsi                = calc_rsi(bars)   # None일 수 있음 (봉 부족), 참고용 표시만

    # 거래량/OBV/ATR (모두 참고용)
    vol_ratio, vol_ok  = calc_volume_surge(bars)
    obv_label          = calc_obv(bars)
    atr                = calc_atr(bars)

    rsi_disp     = f"{rsi:.1f}" if rsi is not None else "N/A"
    price_ok_str = "✅" if price_change_1m >= PRICE_CHANGE_1M else "❌"
    vol_ok_str   = "✅" if vol_ok else "❌"
    print(
        f"  └ RSI:{rsi_disp} | 1분:{price_change_1m:+.2f}%{price_ok_str} "
        f"| 거래량:{vol_ratio:.1f}x{vol_ok_str} | ATR:{atr:.3f} | OBV:{obv_label}"
    )

    # 진입 조건: 1분 상승만
    if price_change_1m < PRICE_CHANGE_1M:
        return None

    return {
        "rsi":             rsi if rsi is not None else 0.0,
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

        # [v25] 보유 종목 전량 현재가로 강제 청산
        if sim_positions:
            held = list(sim_positions.keys())
            snaps = get_snapshots(held)
            for sym in held:
                snap = snaps.get(sym, {})
                price, _ = get_live_price(snap)
                if price:
                    sim_close(sym, float(price), "장마감 강제청산", qty=None)
                    print(f"  [장마감 강제청산] {sym} @ ${float(price):.2f}")

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

    # ── [v24] 보유 종목 독립 가격 체크 ──
    # snap_map에 없는 보유 종목도 별도 스냅샷 조회하여 손절/매도 체크
    held_syms_not_in_scan = [
        sym for sym in list(entry_prices.keys()) if sym not in snap_map
    ]
    if held_syms_not_in_scan:
        extra_snaps = get_snapshots(held_syms_not_in_scan)
        for sym, snap in extra_snaps.items():
            price, source = get_live_price(snap)
            if price:
                check_sell_timing(sym, float(price), source)
        print(f"  [보유종목 독립체크] {held_syms_not_in_scan} — {len(held_syms_not_in_scan)}개")

    # 스캔 대상에 있는 보유 종목 체크
    for sym in list(entry_prices.keys()):
        if sym in snap_map:
            stock = snap_map[sym]
            check_sell_timing(sym, stock["price"], stock["price_source"])

    top = ranked[:REGULAR_TOP_N]
    print(f"[정규장] 상위 {REGULAR_TOP_N}종목 | 1위: {top[0]['symbol']} {top[0]['change_pct']:+.2f}%")
    print(f"  {holdings_block().replace(chr(10), ' | ')}")
    if blacklisted_today:
        print(f"  🚫 블랙리스트: {', '.join(sorted(blacklisted_today))}")

    # ATR 계산 후 높은 순으로 재정렬
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

    # ── [v24] 스캔당 최대 MAX_BUY_PER_SCAN 종목만 신규 매수 ──
    bought_this_scan = 0

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

        # 매수 제한 체크
        if bought_this_scan >= MAX_BUY_PER_SCAN:
            print(f"  [매수 제한] {sym} — 이번 스캔 {MAX_BUY_PER_SCAN}종목 초과, 스킵")
            continue

        bought = sim_open(sym, stock["price"])
        if bought:
            bought_this_scan += 1

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
    print("🚀 급등 감지 봇 v30 (정규장 전용 + 시뮬 + 매매일지) 시작!")
    print(f"📈 정규장: 상위 {REGULAR_TOP_N}종목 | 1분 {PRICE_CHANGE_1M}%+ | ${MIN_PRICE}+ 종목만")
    print(f"🎯 매도: +{SELL_PARTIAL_PCT}% 1차 | +{SELL_FULL_PCT}% 전량 | {STOP_LOSS_PCT}% 손절")
    print(f"➡️  횡보청산: {SIDEWAYS_MINUTES}분 경과 & +{SIDEWAYS_MIN_PCT}~+{SIDEWAYS_MAX_PCT}% 구간")
    print(f"📦 동시 보유 최대 {MAX_POSITIONS}종목 | 스캔당 최대 {MAX_BUY_PER_SCAN}종목")
    print(f"🔔 장마감 보유 종목 전량 강제 청산")
    print(f"🚫 손절 {MAX_STOP_LOSS_COUNT}회 도달 시 당일 블랙리스트 등록 (그 전까진 재진입 허용)")
    print("=" * 60)

    send_telegram(
        f"🤖 <b>급등 감지 봇 v30 시작!</b>\n"
        f"📈 1분 {PRICE_CHANGE_1M}%+ | ${MIN_PRICE}+ 종목만\n"
        f"📦 동시 보유 최대 {MAX_POSITIONS}종목 | 스캔당 최대 {MAX_BUY_PER_SCAN}종목\n"
        f"📊 상승률 상위 {REGULAR_TOP_N}종목 → ATR 높은 순 재정렬 후 진입\n"
        f"🔔 장마감 보유 종목 전량 강제 청산\n"
        f"🚫 손절 {MAX_STOP_LOSS_COUNT}회 도달 시 당일 차단 (그 전까진 재진입 허용)\n"
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
            if blacklisted_today or stop_loss_count:
                blacklisted_today.clear()
                stop_loss_count.clear()
                print("[리셋] 블랙리스트 및 손절 카운트 초기화 (새 장 시작)")

        check_scheduled_reports()

        if not is_regular_session():
            print(f"[{now_str}] 정규장 외 시간 — 대기 중...")
        else:
            print(f"\n[{now_str}] 정규장 스캔 시작")
            run_scan()

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
