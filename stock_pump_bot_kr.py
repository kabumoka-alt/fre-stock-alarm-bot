"""
국내주식(KRX) 급등 감지 봇 (모의투자 연동)
──────────────────────────────────────────
스크리닝/시세는 KIS API, 시뮬레이션+실주문(모의투자)도 KIS로 처리.

- 정규장(09:00~15:30 KST)만 스캔
- [v2] 1분봉 3%+ 는 1차 필터, 통과 종목은 가중점수(1분변화율×0.5 + 거래량서지×0.3
  + RSI보너스×0.2)로 재정렬해 상위 종목만 매수 (해외주식봇 v35 스코어링 방식 이식)
- [v2] 국내장 전용 안전필터: 상한가(전일대비 +29%↑) 근접 종목 제외, 우선주 이름 패턴 제외
- [v3] 예산 로직 안정화: LIVE_TRADING 시 가상 캐시(sim_stats) 대신 실계좌 매수가능금액
  기준으로 슬롯당 예산 산정
  → 재시작 후 포지션 복원 시 가상 캐시와 실잔고가 어긋나 발생했던 대량 오매수 방지
- [v4] 당일 매수·당일 매도 원칙: 14:50 이후 신규 매수 중단, 15:15(동시호가 15:20 전)에
  보유종목 전량 정리매매 완료. 예수금은 실계좌 기준이라 봇 재시작과 무관하게 자동으로
  누적(복리)됨.
- [v5] 풀매수 = 예수금 전액 사용(3슬롯 분산, 한 종목 몰빵 아님). 총상한 캡 제거 —
  매수가능금액 전액을 MAX_POSITIONS로 나눠 슬롯 예산 산정.
- [v6] 하루 누적 실현손익이 기준자본 대비 +6%(DAILY_PROFIT_TARGET_PCT)에 도달하면
  그날 신규 매수만 중단(보유종목 매도/손절 감시는 계속). 기준자본은 매일 9:00에
  실계좌 예수금으로 갱신.
- [v7] 익절폭 > 손절폭 구조로 전환: 손절 -2%(net), 순수익 +2.5% 도달 시 트레일링 스탑
  활성화되어 고점 대비 -1.5%p 되밀리면 청산, +8%(net) 도달 시 트레일링 없이 즉시 확정.
  모든 손익 판단은 수수료·세금(왕복 약 0.24%)까지 반영한 순수익률 기준(net_gain_pct).
  분할매도 없음(전량 진입/전량 청산).
- 보유종목 10초 주기 체크 (해외주식봇과 동일한 슬리피지 개선 적용)
- 손절 2회 도달 시 당일 블랙리스트
- [v8-수정] 미체결/유령 포지션 차단: 매수 주문 후 실계좌 잔고를 역조회하여 실제 체결된 수량과 평단가만 장부에 반영.
- [v9-패치] 타임존 불일치 오류 수정 및 포지션 복원 시 트레일링 스탑 초기값(peak_gain) 바인딩 누락 보완.
"""

import os
import re
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

# [v2] 스코어링 도입
MIN_ENTRY_SCORE   = 6.0     # 가중점수 10점 만점 중 최소 통과선 (아래 calc_entry_score 참고)
LIMIT_UP_THRESHOLD = 29.0   # 전일대비 등락률(%) 이 값 이상이면 상한가 근접으로 보고 제외

# [v7] 익절폭 > 손절폭 구조로 전환 + 트레일링 스탑
STOP_LOSS_PCT       = -2.0   # [v7] -3.0 → -2.0, 손절폭을 좁혀서 손익비 개선
TRAIL_ACTIVATE_PCT  = 2.5    # 순수익(수수료 반영) 이 값 이상 찍으면 트레일링 스탑 활성화
TRAIL_GAP_PCT        = 1.5    # 활성화 후 고점 대비 이만큼(%p) 되밀리면 청산 (수익 보존)
SELL_FULL_PCT       = 8.0    # 안전판 하드 상한 — 여기 도달하면 트레일링 기다리지 않고 즉시 확정

MAX_STOP_LOSS_COUNT = 2

# [v7] 수수료·세금 반영
BUY_COST_RATE_PCT  = 0.015 + 0.0037          # 매수 시 비용률(%): 수수료 + 유관기관제비용
SELL_COST_RATE_PCT = 0.015 + 0.20 + 0.0037   # 매도 시 비용률(%): 수수료 + 거래세 + 유관기관제비용


def net_entry_cost(price: float) -> float:
    """매수 1주당 실제 지불금액(수수료 포함)."""
    return price * (1 + BUY_COST_RATE_PCT / 100)


def net_exit_proceeds(price: float) -> float:
    """매도 1주당 실제 수령금액(수수료·세금 차감)."""
    return price * (1 - SELL_COST_RATE_PCT / 100)


def net_gain_pct(entry_price: float, current_price: float) -> float:
    """수수료·세금까지 반영한 순수익률(%). 매도 타이밍 판단은 전부 이 값 기준."""
    cost = net_entry_cost(entry_price)
    proceeds = net_exit_proceeds(current_price)
    if cost <= 0:
        return 0.0
    return (proceeds - cost) / cost * 100


# [v6] 일일 누적 수익목표 추적
DAILY_PROFIT_TARGET_PCT = 6.0

SIM_INITIAL_CASH = 1_000_000   # 원 단위 가상 예수금 (LIVE_TRADING=false 순수 시뮬레이션 전용)

# [v4] 당일 매수·당일 매도 원칙 + 동시호가(15:20~) 이전 정리매매
BUY_CUTOFF_HOUR, BUY_CUTOFF_MINUTE   = 14, 50   # 이 시각 이후 신규 매수(스캔) 중단
LIQUIDATION_HOUR, LIQUIDATION_MINUTE = 15, 15   # 동시호가 시작 전 정리매매 전량 청산 완료

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

daily_start_balance: float = 0.0   # 그날 시작 시점 실계좌 예수금 (기준 자본)
daily_realized_pnl: float = 0.0    # 그날 실현손익 누적
daily_target_hit: bool = False     # 목표 도달 시 True


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
        if side == "sell":
            _sellable = kis.get_kr_sellable_qty(code)
            if _sellable <= 0:
                print(f"  [매도 스킵] {code} 실잔고 0주 (장부 불일치)")
                sim_positions.pop(code, None)
                return {"rt_cd": "-1", "msg1": "실잔고 없음 - 매도 스킵"}
            if qty > _sellable:
                print(f"  [매도 수량 축소] {code} {qty}주 → {_sellable}주 (실잔고)")
                qty = _sellable
        result = kis.place_domestic_order(code, qty, price, side)
        notify_kis_order("매수" if side == "buy" else "매도", code, qty, price, result)
        return result
    except Exception as e:
        print(f"[KIS 국내주문 예외] {code} {side} → {e}")
        send_telegram(f"⚠️ [KIS 국내 모의투자] {code} {side} 주문 예외: {e}")
        return {"rt_cd": "-1", "msg1": str(e)}


# ──────────────────────────────────────────
# 지표 계산
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


def calc_entry_score(price_change_1m: float, vol_ratio: float, rsi) -> float:
    change_score = min(max(price_change_1m, 0.0), 10.0)
    volume_score = min(max(vol_ratio, 0.0), 5.0) * 2.0
    if rsi is None:
        rsi_score = 5.0
    elif rsi >= 70:
        rsi_score = 10.0
    elif rsi >= 50:
        rsi_score = 7.0
    elif rsi >= 40:
        rsi_score = 4.0
    else:
        rsi_score = 0.0
    return change_score * 0.5 + volume_score * 0.3 + rsi_score * 0.2


def is_preferred_stock(name: str) -> bool:
    return bool(re.search(r"\d*우[A-Z]?$", name or ""))


# ──────────────────────────────────────────
# 시뮬레이션 + KIS 실주문
# ──────────────────────────────────────────

def sim_open(code: str, name: str, price: float) -> bool:
    if code in sim_positions or code in blacklisted_today:
        return False
    if len(sim_positions) >= MAX_POSITIONS:
        return False
    remaining = max(MAX_POSITIONS - len(sim_positions), 1)

    if LIVE_TRADING:
        real_buyable = kis.get_domestic_buyable_amount(code, int(price))
        if real_buyable <= 0:
            print(f"  [매수 불가] {code} KIS 매수가능금액 조회 실패/0")
            return False
        budget = real_buyable / remaining
    else:
        budget = sim_stats["cash"] / remaining

    qty = int(budget // net_entry_cost(price))
    if qty < 1:
        print(f"  [매수 불가] {code} 예수금 부족 (예산={budget:.0f}, 1주={price:.0f})")
        return False

    # ── LIVE_TRADING 여부에 따른 분기 및 실잔고 체결 검증 처리 ──
    if LIVE_TRADING:
        print(f"  [🚀 실전 매수 주문 발송] {code}({name}) {qty}주 @ {price:.0f}원")
        result = place_kis_order_safe(code, qty, int(price), "buy")
        
        if result.get("rt_cd") != "0":
            print(f"  [❌ 매수 주문 접수 실패] {code} 장부 반영 스킵")
            return False
        
        print(f"  [⏳ 체결 대기] KIS 잔고 반영을 위해 2초간 대기합니다...")
        time.sleep(2.0)
        
        real_qty = kis.get_kr_sellable_qty(code)
        if real_qty <= 0:
            print(f"  [⚠️ 미체결 알림] {code} 주문은 접수되었으나 현재 잔고가 0주입니다. 장부 등록 제외 (다음 주기 재시도)")
            return False
        
        if real_qty < qty:
            print(f"  [⚠️ 부분체결 감지] 요청 {qty}주 -> 실제 체결 {real_qty}주로 장부 축소 반영")
            qty = real_qty
            
        try:
            bal = kis.get_domestic_balance()
            for h in bal.get("output1", []):
                if h.get("pdno") == code:
                    avg_pchs = float(h.get("pchs_avg_pric", 0) or 0)
                    if avg_pchs > 0:
                        price = avg_pchs
                        break
        except Exception as e:
            print(f"  [평단가 갱신 실패] {e}, 기존 스냅샷 가격({price}) 사용")

    # ── 최종 장부 및 가상 캐시 반영 (실제 체결 확정 수량 기준) ──
    cost = net_entry_cost(price) * qty
    sim_stats["cash"] -= cost
    
    sim_positions[code] = {"entry": price, "qty": qty, "name": name}
    entry_prices[code] = {"entry": price, "time": get_kst_now(), "alert1": None, "alert2": None, "stop": None, "peak_gain": 0.0}
    
    now_kst = get_kst_now()
    trade_log.append({
        "action": "BUY", "sym": code, "name": name, "qty": qty, "price": price,
        "pnl": 0.0, "pnl_pct": 0.0, "reason": "매수체결완료", "time_kst": now_kst.strftime("%H:%M")
    })
    print(f"  [장부 반영 완료] {code}({name}) {qty}주 @ {price:.0f}원 반영 | 잔여 가상캐시: {sim_stats['cash']:.0f}원")
    return True


def sim_close(code: str, exit_price: float, reason: str, qty: int = None):
    global daily_realized_pnl, daily_target_hit
    pos = sim_positions.get(code)
    if not pos:
        return
    entry_price = pos["entry"]
    close_qty = qty if qty is not None else pos["qty"]
    entry_cost_ps = net_entry_cost(entry_price)
    exit_proceeds_ps = net_exit_proceeds(exit_price)
    pnl = (exit_proceeds_ps - entry_cost_ps) * close_qty
    pnl_pct = (exit_proceeds_ps - entry_cost_ps) / entry_cost_ps * 100

    sim_stats["cash"] += exit_proceeds_ps * close_qty
    pos["qty"] -= close_qty
    if pos["qty"] <= 0:
        del sim_positions[code]
        entry_prices.pop(code, None)
        sim_stats["total_pnl"] += pnl
        sim_stats["trades"] += 1
        sim_stats["wins" if pnl >= 0 else "losses"] += 1
    else:
        sim_stats["total_pnl"] += pnl

    daily_realized_pnl += pnl
    if not daily_target_hit and daily_start_balance > 0:
        daily_ret_pct = daily_realized_pnl / daily_start_balance * 100
        if daily_ret_pct >= DAILY_PROFIT_TARGET_PCT:
            daily_target_hit = True
            print(f"[🎯 일일 목표 도달] 누적 {daily_ret_pct:+.2f}% ≥ {DAILY_PROFIT_TARGET_PCT}% — 오늘 신규 매수 중단")
            send_telegram(
                f"🎯 <b>일일 목표수익률 {DAILY_PROFIT_TARGET_PCT}% 도달!</b>\n"
                f"오늘 실현손익 {daily_realized_pnl:+,.0f}원 ({daily_ret_pct:+.2f}%)\n"
                f"오늘은 신규 매수를 중단합니다 (보유종목 매도/손절 감시는 계속 진행)."
            )

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
    now = get_kst_now()   # [v9-패치] 타임존 오류 원치적 차단을 위해 KST 객체로 통일
    gain_pct = net_gain_pct(entry_price, current_price)

    peak = entry.get("peak_gain")
    if peak is None or gain_pct > peak:
        entry["peak_gain"] = gain_pct
        peak = gain_pct

    def cooldown_ok(key):
        last = entry.get(key)
        return last is None or (now - last).total_seconds() / 60 >= SELL_COOLDOWN_MINUTES

    if gain_pct <= STOP_LOSS_PCT:
        entry["stop"] = now
        sim_close(code, current_price, f"손절({STOP_LOSS_PCT:.0f}%)")
        print(f"[🔴 손절] {code} {entry_price:.0f}→{current_price:.0f} (순 {gain_pct:+.2f}%)")
        return

    if gain_pct >= SELL_FULL_PCT:
        sim_close(code, current_price, f"+{SELL_FULL_PCT:.0f}% 상한 익절")
        print(f"[🟢 상한 익절] {code} {entry_price:.0f}→{current_price:.0f} (순 {gain_pct:+.2f}%)")
        return

    if peak >= TRAIL_ACTIVATE_PCT and (peak - gain_pct) >= TRAIL_GAP_PCT:
        if cooldown_ok("alert2"):
            entry["alert2"] = now
            sim_close(code, current_price, f"트레일링(고점{peak:+.1f}%→{gain_pct:+.1f}%)")
            print(f"[🟡 트레일링 청산] {code} 고점{peak:+.2f}% → 현재{gain_pct:+.2f}%")


def monitor_positions():
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

    candidates = []
    for stock in ranked:
        code = stock["code"]
        if not code or stock["price"] < MIN_PRICE:
            continue
        if code in blacklisted_today or code in sim_positions:
            continue
        if is_preferred_stock(stock.get("name", "")):
            continue
        if stock.get("change_pct", 0) >= LIMIT_UP_THRESHOLD:
            continue
        if code in last_alert:
            elapsed = (get_kst_now() - last_alert[code]).total_seconds() / 60
            if elapsed < COOLDOWN_MINUTES:
                continue

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
        score = calc_entry_score(price_change_1m, vol_ratio, rsi)
        print(f"[🚀 후보] {code}({stock['name']}) 1분{price_change_1m:+.2f}% RSI{rsi} 거래량{vol_ratio:.1f}x 점수{score:.1f} 가격{stock['price']:.0f}원")

        if score < MIN_ENTRY_SCORE:
            continue

        candidates.append({"code": code, "name": stock["name"], "price": stock["price"], "score": score})
        time.sleep(0.3)

    candidates.sort(key=lambda c: c["score"], reverse=True)

    bought_this_scan = 0
    for c in candidates:
        if bought_this_scan >= MAX_BUY_PER_SCAN:
            break
        code = c["code"]
        last_alert[code] = get_kst_now()
        
        # entry_prices 가등록을 제거하고 sim_open 성공 시점으로 이관하여 오지포지션(유령) 생성 제어
        if sim_open(code, c["name"], c["price"]):
            bought_this_scan += 1
            print(f"  [✅ 매수 확정] {code}({c['name']}) 점수{c['score']:.1f}")
        else:
            print(f"  [❌ 매수 제외] {code}({c['name']}) 주문 실패 또는 미체결")


def _get_real_deposit_safe():
    try:
        bal = kis.get_domestic_balance()
        output2 = bal.get("output2")
        row = output2[0] if isinstance(output2, list) and output2 else (output2 or {})
        return float(row.get("dnca_tot_amt", 0) or 0)
    except Exception as e:
        print(f"[실계좌 예수금 조회 실패] {e}")
        return None


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

    _closed = [t for t in trade_log if t.get("reason") != "매수"]
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
                lines.append(f"  ⚖️ 손익비: {
