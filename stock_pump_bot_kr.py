"""
국내주식(KRX) 급등 감지 봇 (모의투자 연동)
──────────────────────────────────────────
해외주식봇(v37) 조건을 그대로 국내주식(KRX)에 적용.
스크리닝/시세는 KIS API, 시뮬레이션+실주문(모의투자)도 KIS로 처리.

- 정규장(09:00~15:30 KST)만 스캔
- 1분봉 3%+ 조건 충족 시 진입
- 매도 타이밍: +7% 1차(절반), +15% 전량, -10% 손절
- 보유종목 10초 주기 체크 (해외주식봇과 동일한 슬리피지 개선 적용)
- 손절 2회 도달 시 당일 블랙리스트

⚠️ 국내 공휴일(설/추석 등) 캘린더 체크는 아직 없음 — 요일만 판단.
   필요 시 한국거래소 휴장일 API나 고정 리스트 추가 필요.
⚠️ KIS 시세 API 응답 필드명은 실제 실행 결과로 재확인 필요.
"""

import os
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import kis_client as kis

KST = ZoneInfo("Asia/Seoul")

TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

# ── LIVE_TRADING: true일 때만 KIS 모의투자 실주문 함께 실행 ──
LIVE_TRADING = os.environ.get("LIVE_TRADING", "false").lower() == "true"
if LIVE_TRADING and not kis.USE_MOCK:
    raise RuntimeError("⚠️ KIS_USE_MOCK=false 상태에서 이 스위치를 켜는 것은 위험합니다.")

# ── 조건 (해외주식봇과 동일) ──
TOP_N               = 30
PRICE_CHANGE_1M      = 3.0
MIN_PRICE            = 1000      # 국내주식 저가주 필터 (원)
CHECK_INTERVAL        = 60
POSITION_CHECK_INTERVAL = 10
COOLDOWN_MINUTES      = 30
SELL_COOLDOWN_MINUTES = 60
MAX_BUY_PER_SCAN      = 3
MAX_POSITIONS         = 3        # v38 집중투자 기준

SELL_PARTIAL_PCT = 7.0
SELL_FULL_PCT    = 15.0
STOP_LOSS_PCT    = -10.0

MAX_STOP_LOSS_COUNT = 2

SIM_INITIAL_CASH = 1_000_000   # 원 단위 가상 예수금 (100만원)

entry_prices: dict = {}
last_alert: dict = {}
sim_positions: dict = {}
sim_stats = {
    "initial_cash": SIM_INITIAL_CASH,
    "cash": SIM_INITIAL_CASH,
    "total_pnl": 0.0,
    "trades": 0, "wins": 0, "losses": 0,
}
stop_loss_count: dict = {}
blacklisted_today: set = set()
trade_log: list = []
last_hourly_report: int = -1
market_close_sent = False


# ──────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────

def get_kst_now():
    return datetime.now(KST)


def is_krx_regular_session() -> bool:
    now = get_kst_now()
    if now.weekday() >= 5:
        return False
    et_min = now.hour * 60 + now.minute
    return (9 * 60) <= et_min <= (15 * 60 + 30)


def _send_telegram_chunk(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=5)
        if resp.status_code != 200:
            print(f"[텔레그램 오류] {resp.text}")
    except Exception as e:
        print(f"[텔레그램 예외] {e}")


def send_telegram(message: str):
    TELEGRAM_MAX = 4000
    if len(message) <= TELEGRAM_MAX:
        _send_telegram_chunk(message)
        return
    lines = message.split("\n")
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > TELEGRAM_MAX:
            _send_telegram_chunk(chunk)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        _send_telegram_chunk(chunk)


def notify_kis_order(action: str, code: str, qty: int, price: int, result: dict):
    rt_cd = result.get("rt_cd")
    msg1  = result.get("msg1", "")
    if rt_cd == "0":
        send_telegram(f"✅ [KIS 국내 모의투자] {code} {action} {qty}주 @ {price:,}원 성공\n{msg1}")
    else:
        send_telegram(f"⚠️ [KIS 국내 모의투자] {code} {action} {qty}주 @ {price:,}원 실패 (rt_cd={rt_cd})\n{msg1}")


def place_kis_order_safe(code: str, qty: int, price: int, side: str):
    try:
        result = kis.place_domestic_order(code, qty, price, side)
        notify_kis_order("매수" if side == "buy" else "매도", code, qty, price, result)
        return result
    except Exception as e:
        print(f"[KIS 국내주문 예외] {code} {side} → {e}")
        send_telegram(f"⚠️ [KIS 국내 모의투자] {code} {side} 주문 예외: {e}")
        return {"rt_cd": "-1", "msg1": str(e)}


# ──────────────────────────────────────────
# 지표 계산 (해외주식봇과 동일 로직 재사용)
# ──────────────────────────────────────────

def calc_rsi(bars, period=14):
    if len(bars) < period + 1:
        return None
    closes = [b["c"] for b in bars]
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0)); losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))


def calc_volume_surge(bars):
    if len(bars) < 6:
        return 0.0, False
    recent_5 = bars[-5:]
    history = bars[:-5][-20:]
    if not history:
        return 0.0, False
    avg_vol = sum(b["v"] for b in history) / len(history)
    if avg_vol <= 0:
        return 0.0, False
    ratio = sum(b["v"] for b in recent_5) / (avg_vol * 5)
    return ratio, ratio >= 1.5


# ──────────────────────────────────────────
# 시뮬레이션 + KIS 실주문
# ──────────────────────────────────────────

def sim_open(code: str, name: str, price: float) -> bool:
    if code in sim_positions or code in blacklisted_today:
        return False
    if len(sim_positions) >= MAX_POSITIONS:
        return False
    remaining = MAX_POSITIONS - len(sim_positions)
    budget = sim_stats["cash"] / remaining

    if LIVE_TRADING:
        real_buyable = kis.get_domestic_buyable_amount(code, int(price))
        if real_buyable <= 0:
            print(f"  [매수 불가] {code} KIS 매수가능금액 조회 실패/0")
            return False
        budget = min(budget, real_buyable / remaining)

    qty = int(budget // price)
    if qty < 1:
        print(f"  [매수 불가] {code} 예수금 부족 (예산={budget:.0f}, 1주={price:.0f})")
        return False

    cost = price * qty
    sim_stats["cash"] -= cost
    sim_positions[code] = {"entry": price, "qty": qty, "partial_done": False, "name": name}
    now_kst = get_kst_now()
    trade_log.append({"action": "BUY", "sym": code, "name": name, "qty": qty, "price": price,
                       "pnl": 0.0, "pnl_pct": 0.0, "reason": "매수", "time_kst": now_kst.strftime("%H:%M")})
    print(f"  [시뮬 매수] {code}({name}) {qty}주 @ {price:.0f}원 | 잔여: {sim_stats['cash']:.0f}원")

    if LIVE_TRADING:
        place_kis_order_safe(code, qty, int(price), "buy")
    return True


def sim_close(code: str, exit_price: float, reason: str, qty: int = None):
    pos = sim_positions.get(code)
    if not pos:
        return
    entry_price = pos["entry"]
    close_qty = qty if qty is not None else pos["qty"]
    pnl = (exit_price - entry_price) * close_qty
    pnl_pct = ((exit_price - entry_price) / entry_price) * 100

    sim_stats["cash"] += exit_price * close_qty
    pos["qty"] -= close_qty
    if pos["qty"] <= 0:
        del sim_positions[code]
        entry_prices.pop(code, None)
        sim_stats["total_pnl"] += pnl
        sim_stats["trades"] += 1
        sim_stats["wins" if pnl >= 0 else "losses"] += 1
    else:
        pos["partial_done"] = True
        sim_stats["total_pnl"] += pnl

    if "손절" in reason:
        stop_loss_count[code] = stop_loss_count.get(code, 0) + 1
        if stop_loss_count[code] > MAX_STOP_LOSS_COUNT:
            blacklisted_today.add(code)

    now_kst = get_kst_now()
    trade_log.append({"action": "SELL", "sym": code, "name": pos.get("name", code), "qty": close_qty,
                       "price": exit_price, "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason,
                       "time_kst": now_kst.strftime("%H:%M")})

    if LIVE_TRADING:
        place_kis_order_safe(code, close_qty, int(exit_price), "sell")


# ──────────────────────────────────────────
# 매도 타이밍
# ──────────────────────────────────────────

def check_sell_timing(code: str, current_price: float):
    if code not in entry_prices:
        return
    entry = entry_prices[code]
    entry_price = entry["entry"]
    now = datetime.now(timezone_utc())
    gain_pct = ((current_price - entry_price) / entry_price) * 100

    def cooldown_ok(key):
        last = entry.get(key)
        return last is None or (now - last).total_seconds() / 60 >= SELL_COOLDOWN_MINUTES

    if gain_pct <= STOP_LOSS_PCT:
        if cooldown_ok("stop"):
            entry["stop"] = now
            sim_close(code, current_price, f"손절({STOP_LOSS_PCT:.0f}%)")
            print(f"[🔴 손절] {code} {entry_price:.0f}→{current_price:.0f} ({gain_pct:+.2f}%)")
        return
    if gain_pct >= SELL_FULL_PCT:
        if cooldown_ok("alert2"):
            entry["alert2"] = now
            sim_close(code, current_price, f"+{SELL_FULL_PCT:.0f}% 전량")
        return
    if gain_pct >= SELL_PARTIAL_PCT:
        if cooldown_ok("alert1"):
            entry["alert1"] = now
            pos = sim_positions.get(code)
            if pos and not pos.get("partial_done"):
                half = max(1, pos["qty"] // 2)
                sim_close(code, current_price, f"+{SELL_PARTIAL_PCT:.0f}% 1차(절반)")


def timezone_utc():
    from datetime import timezone
    return timezone.utc


def monitor_positions():
    """보유종목 전량, 스캔과 분리된 짧은 주기로 체크 (해외주식봇 개선사항 반영)."""
    if not entry_prices:
        return
    for code in list(entry_prices.keys()):
        price = kis.get_domestic_current_price(code)
        if price:
            check_sell_timing(code, price)


# ──────────────────────────────────────────
# 스캔
# ──────────────────────────────────────────

def run_scan():
    ranked = kis.get_domestic_ranking(top=TOP_N)
    if not ranked:
        print("  [순위조회 실패/빈 결과]")
        return

    bought_this_scan = 0
    for stock in ranked:
        code = stock["code"]
        if not code or stock["price"] < MIN_PRICE:
            continue
        if code in blacklisted_today or code in sim_positions:
            continue
        if code in last_alert:
            elapsed = (get_kst_now() - last_alert[code]).total_seconds() / 60
            if elapsed < COOLDOWN_MINUTES:
                continue
        if bought_this_scan >= MAX_BUY_PER_SCAN:
            break

        bars = kis.get_domestic_minute_bars(code)
        if len(bars) < 6:
            continue
        price_1m_ago = bars[-2]["c"]
        if price_1m_ago <= 0:
            continue
        price_change_1m = ((stock["price"] - price_1m_ago) / price_1m_ago) * 100
        if price_change_1m < PRICE_CHANGE_1M:
            continue

        rsi = calc_rsi(bars)
        vol_ratio, _ = calc_volume_surge(bars)
        print(f"[🚀 감지] {code}({stock['name']}) 1분{price_change_1m:+.2f}% RSI{rsi} 거래량{vol_ratio:.1f}x 가격{stock['price']:.0f}원")

        last_alert[code] = get_kst_now()
        entry_prices[code] = {"entry": stock["price"], "time": get_kst_now(), "alert1": None, "alert2": None, "stop": None}
        if sim_open(code, stock["name"], stock["price"]):
            bought_this_scan += 1
        else:
            entry_prices.pop(code, None)
        time.sleep(0.3)


def build_report(title: str) -> str:
    win_rate = sim_stats["wins"] / sim_stats["trades"] * 100 if sim_stats["trades"] else 0.0
    total_return = sim_stats["total_pnl"] / sim_stats["initial_cash"] * 100
    lines = [f"📋 <b>{title}</b>", f"🇰🇷 {get_kst_now().strftime('%m/%d %H:%M')} KST", "━━━━━━━━━━━━━━"]
    if trade_log:
        for t in trade_log[-30:]:
            icon = "📥" if t["action"] == "BUY" else ("📈" if t["pnl"] >= 0 else "📉")
            if t["action"] == "BUY":
                lines.append(f"  {icon} {t['time_kst']} {t['sym']}({t.get('name','')}) {t['qty']}주 매수 @ {t['price']:.0f}원")
            else:
                lines.append(f"  {icon} {t['time_kst']} {t['sym']} {t['qty']}주 {t['reason']} @ {t['price']:.0f}원 ({t['pnl']:+.0f}원, {t['pnl_pct']:+.2f}%)")
    else:
        lines.append("거래 내역 없음")
    lines.append("━━━━━━━━━━━━━━")
    # ── 종목별 요약 (청산 거래만) ──
    _closed = [t for t in daily_trades if t.get("reason") != "매수"]
    if _closed:
        _by = {}
        for t in _closed:
            s = _by.setdefault(t["sym"], {"pnl": 0.0, "n": 0})
            s["pnl"] += t.get("pnl", 0.0); s["n"] += 1
        lines.append("📊 <b>종목별 요약</b>")
        for sym, s in sorted(_by.items(), key=lambda kv: kv[1]["pnl"], reverse=True):
            ic = "🔴" if s["pnl"] > 0 else ("🔵" if s["pnl"] < 0 else "⚪")
            lines.append(f"  {ic} {sym}: {s['pnl']:+,.0f}원 ({s['n']}건)")
        _best = max(_closed, key=lambda t: t.get("pnl", 0.0))
        _worst = min(_closed, key=lambda t: t.get("pnl", 0.0))
        lines.append(f"  🏆 베스트: {_best['sym']} {_best.get('pnl',0):+,.0f}원")
        lines.append(f"  📉 워스트: {_worst['sym']} {_worst.get('pnl',0):+,.0f}원")
        _w = [t["pnl"] for t in _closed if t.get("pnl",0) > 0]
        _l = [t["pnl"] for t in _closed if t.get("pnl",0) < 0]
        if _w and _l:
            aw = sum(_w)/len(_w); al = abs(sum(_l)/len(_l))
            if al > 0:
                lines.append(f"  ⚖️ 손익비: {aw/al:.2f} (평균익 {aw:+,.0f}원 / 평균손 -{al:,.0f}원)")
        lines.append("━" * 14)

    lines.append(f"💵 예수금: {sim_stats['cash']:,.0f}원")
    lines.append(f"💰 누적손익: {sim_stats['total_pnl']:+,.0f}원 ({total_return:+.2f}%)")
    lines.append(f"🏆 {sim_stats['wins']}승 {sim_stats['losses']}패 (승률 {win_rate:.0f}%)")
    return "\n".join(lines)


def main():
    global market_close_sent
    print("=" * 60)
    print("🚀 국내주식(KRX) 급등 감지 봇 시작!")
    print(f"🔌 LIVE_TRADING: {'ON (KIS 국내 모의투자 연동)' if LIVE_TRADING else 'OFF (시뮬만)'}")
    print(f"📈 1분 {PRICE_CHANGE_1M}%+ | {MIN_PRICE:,}원+ 종목만 | 상위 {TOP_N}종목")
    print(f"🎯 매도: +{SELL_PARTIAL_PCT}% 1차 | +{SELL_FULL_PCT}% 전량 | {STOP_LOSS_PCT}% 손절")
    print(f"⚡ 보유종목 {POSITION_CHECK_INTERVAL}초 주기 체크")
    print("=" * 60)

    send_telegram(
        f"🤖 <b>국내주식(KRX) 급등 감지 봇 시작!</b>\n"
        f"🔌 LIVE_TRADING: <b>{'ON' if LIVE_TRADING else 'OFF (시뮬만)'}</b>\n"
        f"📈 1분 {PRICE_CHANGE_1M}%+ | {MIN_PRICE:,}원+ 종목만\n"
        f"🎯 +{SELL_PARTIAL_PCT}% 1차 | +{SELL_FULL_PCT}% 전량 | {STOP_LOSS_PCT}% 손절"
    )

    last_scan_time = 0.0
    while True:
        now_str = get_kst_now().strftime("%H:%M:%S")

        if get_kst_now().hour == 9 and get_kst_now().minute == 0:
            if blacklisted_today or stop_loss_count or trade_log or last_alert:
                blacklisted_today.clear(); stop_loss_count.clear()
                trade_log.clear(); last_alert.clear()
                market_close_sent = False

        if not is_krx_regular_session():
            print(f"[{now_str}] 정규장 외 시간 — 대기 중...")
            time.sleep(POSITION_CHECK_INTERVAL)
            continue

        if get_kst_now().hour == 15 and get_kst_now().minute >= 30 and not market_close_sent:
            market_close_sent = True
            for code in list(sim_positions.keys()):
                price = kis.get_domestic_current_price(code)
                if price:
                    sim_close(code, price, "장마감 강제청산")
            send_telegram(build_report("🔔 장 종료 최종 매매일지"))

        monitor_positions()

        now_mono = time.monotonic()
        if now_mono - last_scan_time >= CHECK_INTERVAL:
            last_scan_time = now_mono
            print(f"\n[{now_str}] 정규장 스캔 시작")
            run_scan()

        time.sleep(POSITION_CHECK_INTERVAL)


if __name__ == "__main__":
    main()
