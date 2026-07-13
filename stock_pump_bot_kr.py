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

⚠️ 국내 공휴일(설/추석 등) 캘린더 체크는 아직 없음 — 요일만 판단.
   필요 시 한국거래소 휴장일 API나 고정 리스트 추가 필요.
⚠️ KIS 시세 API 응답 필드명은 실제 실행 결과로 재확인 필요.
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
#      (등락률 상위를 쫓는 모멘텀 전략은 승률이 낮게 나오기 쉬워서, 손익비를 반대로
#       가져가야 승률 40% 안팎에서도 기대값이 플러스가 됨 — 계산 근거는 아래 주석 참고)
STOP_LOSS_PCT       = -2.0   # [v7] -3.0 → -2.0, 손절폭을 좁혀서 손익비 개선
TRAIL_ACTIVATE_PCT  = 2.5    # 순수익(수수료 반영) 이 값 이상 찍으면 트레일링 스탑 활성화
TRAIL_GAP_PCT        = 1.5    # 활성화 후 고점 대비 이만큼(%p) 되밀리면 청산 (수익 보존)
SELL_FULL_PCT       = 8.0    # 안전판 하드 상한 — 여기 도달하면 트레일링 기다리지 않고 즉시 확정
# 손익분기 승률(수수료 반영 전 근사) = 손절/(익절+손절) = 2/(TRAIL_GAP 기준이라 상황별이지만)
# 대략 2/(4+2)=33% 안팎 → 모멘텀 전략치고 넉넉한 마진

MAX_STOP_LOSS_COUNT = 2

# [v7] 수수료·세금 반영 (2026.1.1~ 세율 기준, 실제 요율은 증권사 등급/이벤트에 따라 다를 수 있음)
#      매도 시 증권거래세 0.20%(코스피 0.05%+농특세 0.15% / 코스닥 0.20%, 동일 총율)
#      + 매수/매도 수수료(온라인 기준 약 0.015%씩) + 유관기관제비용(약 0.0037%씩)
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

# [v6] 종목당 익절과는 별개로, 하루 누적 수익률이 이 값에 도달하면 그날 신규 매수만 중단
#      (보유종목 감시/매도는 계속 정상 진행). 해외봇의 "일일 수익목표 도달 시 halt"와 동일 개념.
DAILY_PROFIT_TARGET_PCT = 6.0

SIM_INITIAL_CASH = 1_000_000   # 원 단위 가상 예수금 (LIVE_TRADING=false 순수 시뮬레이션 전용)

# [v3] 예산 로직 안정화: LIVE_TRADING일 때는 가상 캐시 대신 실계좌 매수가능금액을 기준으로 사용
# [v5] 총상한 캡 제거 — 예수금(매수가능금액) 전액을 MAX_POSITIONS(3) 슬롯으로 나눠서 씀.
#      (한 종목 몰빵이 아니라 "현금을 놀리지 않는다"는 의미의 풀매수)

# [v4] 당일 매수·당일 매도 원칙 + 동시호가(15:20~) 이전 정리매매
BUY_CUTOFF_HOUR, BUY_CUTOFF_MINUTE   = 14, 50   # 이 시각 이후 신규 매수(스캔) 중단 → 청산 전 정리 시간 확보
LIQUIDATION_HOUR, LIQUIDATION_MINUTE = 15, 15   # 동시호가 시작(15:20) 전, 연속거래 중에 전량 청산 완료

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

# [v6] 일일 누적 수익목표 추적 (매일 9:00에 리셋)
daily_start_balance: float = 0.0   # 그날 시작 시점 실계좌 예수금 (기준 자본)
daily_realized_pnl: float = 0.0    # 그날 실현손익 누적
daily_target_hit: bool = False     # 목표 도달 시 True → 신규 매수 중단


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
        # ── 매도 가드: 실제 잔고 확인 (유령 포지션 방지) ──
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


def calc_entry_score(price_change_1m: float, vol_ratio: float, rsi) -> float:
    """
    [v2] 해외주식봇 v35 스타일 가중점수 (10점 만점).
    - 1분 변화율 (0.5 가중): 10%p까지 선형, 그 이상은 캡
    - 거래량서지 (0.3 가중): 5배까지 선형, 그 이상은 캡
    - RSI (0.2 가중): 과매수(70+)일수록 가산 — 필터가 아니라 모멘텀 강도로 취급
    RSI/거래량은 진입을 막는 조건이 아니라 우선순위를 정하는 데만 쓰임.
    """
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
    """우선주 이름 패턴(우, 1우, 2우B, 3우C 등) 감지 → 국내장 전용 제외 필터."""
    return bool(re.search(r"\d*우[A-Z]?$", name or ""))


# ETN/ETF/ETP/레버리지·인버스 등 파생상품 제외 키워드.
# 이런 상품은 순수 급등 모멘텀이 아니라 기초지수 추종이라 1분 급등/RSI가 왜곡되고
# (거래량 0.0x인데 RSI 100 같은 비정상 점수), 유동성도 들쭉날쭉해 스캘핑 대상 부적합.
_DERIVATIVE_KEYWORDS = (
    "ETN", "ETF", "ETP", "레버리지", "인버스", "TR ETN", "선물", "옵션",
    "KODEX", "TIGER", "RISE", "PLUS", "SOL ", "ACE ", "KBSTAR", "ARIRANG",
    "히어로즈", "TIMEFOLIO", "마이티", "WOORI", "KOSEF", "파워", "TOP10",
)


def is_derivative_product(name: str, code: str = "") -> bool:
    """ETN/ETF/ETP 등 파생상품 여부 판별 → 스캘핑 대상에서 제외."""
    nm = (name or "").upper()
    if any(kw.upper() in nm for kw in _DERIVATIVE_KEYWORDS):
        return True
    # 종목코드가 알파벳으로 시작하면(예: Q530138) ETN 계열 → 제외
    if code and not code[0].isdigit():
        return True
    return False


# ──────────────────────────────────────────
# 시뮬레이션 + KIS 실주문
# ──────────────────────────────────────────

def sim_open(code: str, name: str, price: float) -> bool:
    if code in sim_positions or code in blacklisted_today:
        return False
    if len(sim_positions) >= MAX_POSITIONS:
        return False
    remaining = max(MAX_POSITIONS - len(sim_positions), 1)  # 방어적 최소값 (0/음수 나눗셈 방지)

    if LIVE_TRADING:
        # [v10] 예산 = min(슬롯예산, 실제 남은 현금).
        #   기존 get_domestic_buyable_amount()는 미체결 주문이 예수금을 잡고 있으면
        #   0을 반환해 매수가 막히거나, 반대로 직전주문 미반영 시 과다 산정됐다.
        #   이제 잔고조회의 예수금(dnca_tot_amt)에서 금일 매수금액합계(pchs_amt_smtl_amt)를
        #   직접 빼서 "실제 지금 쓸 수 있는 현금"을 계산한다. 미체결·부분체결이
        #   pchs_amt_smtl_amt에 반영되므로 예수금 초과 중복매수가 원천 차단된다.
        if daily_start_balance <= 0:
            print(f"  [매수 불가] {code} 기준자본 미확정 (일일 기준자본 0)")
            return False
        try:
            _bal = kis.get_domestic_balance()
            _o2 = (_bal.get("output2") or [{}])[0]
            _deposit = float(_o2.get("dnca_tot_amt", 0) or 0)          # 예수금
            _already_bought = float(_o2.get("pchs_amt_smtl_amt", 0) or 0)  # 금일 매수금액합계
        except Exception as e:
            print(f"  [매수 불가] {code} 잔고조회 실패({e})")
            return False
        real_cash = _deposit - _already_bought   # 실제 남은 주문가능 현금(근사)
        if real_cash < price:
            print(f"  [매수 불가] {code} 잔여현금 부족 (예수금 {_deposit:,.0f} - 매수 {_already_bought:,.0f} = {real_cash:,.0f}, 1주 {price:,.0f})")
            return False
        slot_budget = daily_start_balance / MAX_POSITIONS
        budget = min(slot_budget, real_cash)
    else:
        budget = sim_stats["cash"] / remaining

    req_qty = int(budget // net_entry_cost(price))  # 요청 수량 (비용률 반영)
    if req_qty < 1:
        print(f"  [매수 불가] {code} 예수금 부족 (예산={budget:.0f}, 1주={price:.0f})")
        return False

    # ── 실주문 먼저 넣고, 실제 체결수량을 확인해서 그걸 장부에 기록 ──
    filled_qty = req_qty
    if LIVE_TRADING:
        prev_holding = kis.get_kr_holding_qty(code)  # 주문 전 보유(보통 0, 재진입 대비)
        result = place_kis_order_safe(code, req_qty, int(price), "buy")
        if result.get("rt_cd") != "0":
            print(f"  [매수 실패] {code} 주문 거부 → 장부 미기록 (rt_cd={result.get('rt_cd')})")
            return False
        # 체결 반영까지 잠깐 대기 후 실보유수량 조회 (요청수량 대신 실체결 사용)
        time.sleep(1.5)
        now_holding = kis.get_kr_holding_qty(code)
        filled_qty = now_holding - prev_holding
        if filled_qty < 1:
            # 접수는 됐으나 아직 체결 미확인 — 한 번 더 확인
            time.sleep(1.5)
            filled_qty = kis.get_kr_holding_qty(code) - prev_holding
        if filled_qty < 1:
            print(f"  [매수 미체결] {code} 주문했으나 체결수량 0 확인 → 장부 미기록")
            return False
        if filled_qty != req_qty:
            print(f"  [부분 체결] {code} 요청 {req_qty}주 → 실체결 {filled_qty}주 (장부에 실체결 반영)")

    cost = net_entry_cost(price) * filled_qty
    sim_stats["cash"] -= cost
    sim_positions[code] = {"entry": price, "qty": filled_qty, "name": name}
    now_kst = get_kst_now()
    trade_log.append({"action": "BUY", "sym": code, "name": name, "qty": filled_qty, "price": price,
                       "pnl": 0.0, "pnl_pct": 0.0, "reason": "매수", "time_kst": now_kst.strftime("%H:%M")})
    print(f"  [매수 완료] {code}({name}) {filled_qty}주 @ {price:.0f}원 | 잔여(추정): {sim_stats['cash']:.0f}원")
    return True


def sim_close(code: str, exit_price: float, reason: str, qty: int = None):
    global daily_realized_pnl, daily_target_hit
    pos = sim_positions.get(code)
    if not pos:
        return
    entry_price = pos["entry"]
    close_qty = qty if qty is not None else pos["qty"]

    # ── 실주문 먼저 실행하고, 실제 매도 체결수량을 확인해서 손익을 계산 ──
    #    (기존엔 장부 수량으로 손익을 먼저 계산한 뒤 주문을 넣어, 매도 가드가
    #     실잔고 기준으로 수량을 축소해도 손익/장부에 반영되지 않는 문제가 있었음)
    if LIVE_TRADING:
        prev_holding = kis.get_kr_holding_qty(code)
        if prev_holding <= 0:
            print(f"  [매도 스킵] {code} 실보유 0주 (장부 불일치) → 장부에서 제거")
            sim_positions.pop(code, None)
            entry_prices.pop(code, None)
            return
        close_qty = min(close_qty, prev_holding)
        result = place_kis_order_safe(code, close_qty, int(exit_price), "sell")
        if result.get("rt_cd") != "0":
            print(f"  [매도 실패] {code} 주문 거부 (rt_cd={result.get('rt_cd')}) → 장부 유지, 다음 주기 재시도")
            return
        time.sleep(1.5)
        now_holding = kis.get_kr_holding_qty(code)
        sold_qty = prev_holding - now_holding
        if sold_qty < 1:
            time.sleep(1.5)
            now_holding = kis.get_kr_holding_qty(code)
            sold_qty = prev_holding - now_holding
        if sold_qty < 1:
            print(f"  [매도 미체결] {code} 주문했으나 체결 0 확인 → 장부 유지, 다음 주기 재시도")
            return
        close_qty = sold_qty  # 실제 팔린 수량으로 손익 계산

    entry_cost_ps = net_entry_cost(entry_price)      # 1주당 실제 매수원가(수수료 포함)
    exit_proceeds_ps = net_exit_proceeds(exit_price)  # 1주당 실제 매도수령액(수수료·세금 차감)
    pnl = (exit_proceeds_ps - entry_cost_ps) * close_qty          # [v7] 수수료·세금 반영 순손익
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

    # [v6] 일일 누적 수익목표 체크 (실현손익 기준)
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


# ──────────────────────────────────────────
# 매도 타이밍
# ──────────────────────────────────────────

def check_sell_timing(code: str, current_price: float):
    if code not in entry_prices:
        return
    entry = entry_prices[code]
    entry_price = entry["entry"]
    now = datetime.now(timezone_utc())
    gain_pct = net_gain_pct(entry_price, current_price)  # [v7] 수수료·세금 반영 순수익률 기준

    # 고점 갱신 (트레일링 스탑 기준선)
    peak = entry.get("peak_gain")
    if peak is None or gain_pct > peak:
        entry["peak_gain"] = gain_pct
        peak = gain_pct

    def cooldown_ok(key):
        last = entry.get(key)
        return last is None or (now - last).total_seconds() / 60 >= SELL_COOLDOWN_MINUTES

    # 1) 손절 (최우선, 쿨다운 없이 즉시)
    if gain_pct <= STOP_LOSS_PCT:
        entry["stop"] = now
        sim_close(code, current_price, f"손절({STOP_LOSS_PCT:.0f}%)")
        print(f"[🔴 손절] {code} {entry_price:.0f}→{current_price:.0f} (순 {gain_pct:+.2f}%)")
        return

    # 2) 하드 상한 익절 (트레일링 기다리지 않고 즉시 확정)
    if gain_pct >= SELL_FULL_PCT:
        sim_close(code, current_price, f"+{SELL_FULL_PCT:.0f}% 상한 익절")
        print(f"[🟢 상한 익절] {code} {entry_price:.0f}→{current_price:.0f} (순 {gain_pct:+.2f}%)")
        return

    # 3) 트레일링 스탑: 활성화 구간 진입 후 고점 대비 되밀리면 청산 (수익 보존)
    if peak >= TRAIL_ACTIVATE_PCT and (peak - gain_pct) >= TRAIL_GAP_PCT:
        if cooldown_ok("alert2"):
            entry["alert2"] = now
            sim_close(code, current_price, f"트레일링(고점{peak:+.1f}%→{gain_pct:+.1f}%)")
            print(f"[🟡 트레일링 청산] {code} 고점{peak:+.2f}% → 현재{gain_pct:+.2f}%")


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

    # ── 1단계: 기본 필터 통과 종목의 점수 계산 (즉시 매수하지 않고 후보로만 수집) ──
    candidates = []
    for stock in ranked:
        code = stock["code"]
        if not code or stock["price"] < MIN_PRICE:
            continue
        if code in blacklisted_today or code in sim_positions:
            continue
        if is_preferred_stock(stock.get("name", "")):
            continue  # [v2] 우선주 제외
        if is_derivative_product(stock.get("name", ""), code):
            continue  # [v9] ETN/ETF/레버리지 등 파생상품 제외 (모멘텀 왜곡·유동성 문제)
        if stock.get("change_pct", 0) >= LIMIT_UP_THRESHOLD:
            continue  # [v2] 상한가 근접 제외 (더 못 먹고 급락 리스크만 남은 구간)
        if code in last_alert:
            elapsed = (get_kst_now() - last_alert[code]).total_seconds() / 60
            if elapsed < COOLDOWN_MINUTES:
                continue

        bars = kis.get_domestic_minute_bars(code)
        if len(bars) < 6:
            continue  # 분봉 부족 = 거래정지/신규상장 등, 자연히 제외됨
        price_1m_ago = bars[-2]["c"]
        if price_1m_ago <= 0:
            continue
        price_change_1m = ((stock["price"] - price_1m_ago) / price_1m_ago) * 100
        if price_change_1m < PRICE_CHANGE_1M:
            continue  # 1분 급등은 여전히 최소 진입선 (0/1 필터)

        rsi = calc_rsi(bars)
        vol_ratio, _ = calc_volume_surge(bars)
        score = calc_entry_score(price_change_1m, vol_ratio, rsi)
        print(f"[🚀 후보] {code}({stock['name']}) 1분{price_change_1m:+.2f}% RSI{rsi} 거래량{vol_ratio:.1f}x 점수{score:.1f} 가격{stock['price']:.0f}원")

        if score < MIN_ENTRY_SCORE:
            continue  # [v2] 점수 미달은 후보에서 제외

        candidates.append({"code": code, "name": stock["name"], "price": stock["price"], "score": score})
        time.sleep(0.3)

    # ── 2단계: 점수 높은 순으로 정렬 후 슬롯 수만큼만 매수 ──
    candidates.sort(key=lambda c: c["score"], reverse=True)

    bought_this_scan = 0
    for c in candidates:
        if bought_this_scan >= MAX_BUY_PER_SCAN:
            break
        code = c["code"]
        last_alert[code] = get_kst_now()
        entry_prices[code] = {"entry": c["price"], "time": get_kst_now(), "alert1": None, "alert2": None, "stop": None}
        if sim_open(code, c["name"], c["price"]):
            bought_this_scan += 1
            print(f"  [✅ 매수] {code}({c['name']}) 점수{c['score']:.1f}")
        else:
            entry_prices.pop(code, None)


def _get_real_deposit_safe():
    """
    [v3] 실계좌 예수금 조회 (리포트 표시용). 실패해도 봇 로직에 영향 없도록 예외를 삼킨다.
    ⚠️ output2 응답 필드명(dnca_tot_amt)은 국내 잔고조회 관례 기준 — 실제 응답으로 재확인 필요.
    """
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
    # ── 종목별 요약 (청산 거래만) ──
    _closed = [t for t in trade_log if t.get("reason") != "매수"]  # [v6 버그수정] daily_trades(미정의) → trade_log
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

    lines.append(f"💵 예수금(시뮬 추정, 참고용): {sim_stats['cash']:,.0f}원")
    if LIVE_TRADING:
        real_cash = _get_real_deposit_safe()
        if real_cash is not None:
            lines.append(f"🏦 실계좌 예수금: {real_cash:,.0f}원")
    lines.append(f"💰 누적손익: {sim_stats['total_pnl']:+,.0f}원 ({total_return:+.2f}%)")
    if daily_start_balance > 0:
        _daily_ret = daily_realized_pnl / daily_start_balance * 100
        _flag = "✅ 달성" if daily_target_hit else "진행중"
        lines.append(f"📅 오늘 누적: {daily_realized_pnl:+,.0f}원 ({_daily_ret:+.2f}% / 목표 +{DAILY_PROFIT_TARGET_PCT}%, {_flag})")
    lines.append(f"🏆 {sim_stats['wins']}승 {sim_stats['losses']}패 (승률 {win_rate:.0f}%)")
    return "\n".join(lines)


def restore_positions_from_account():
    """봇 시작 시 실계좌 잔고를 읽어 감시 대상(entry_prices/sim_positions) 복원.
    [v3] sim_stats["cash"]는 여기서 조정하지 않는다 — 이제 LIVE_TRADING 예산 계산은
    sim_stats["cash"]가 아니라 실계좌 매수가능금액을 매번 직접 조회해 쓰므로,
    복원 시점에 가상 캐시를 맞추지 않아도 예산 계산이 틀어지지 않는다.
    (sim_stats["cash"]는 리포트의 참고용 P&L 추정치로만 사용됨)
    """
    try:
        bal = kis.get_domestic_balance()
    except Exception as e:
        print(f"[포지션 복원 실패] {e}")
        return
    restored = 0
    skipped_ghost = 0
    for h in bal.get("output1", []):
        code = h.get("pdno")
        qty = int(h.get("hldg_qty", 0) or 0)
        avg = float(h.get("pchs_avg_pric", 0) or 0)
        name = h.get("prdt_name", "")
        if not code or qty <= 0 or avg <= 0:
            continue
        if code in entry_prices:
            continue

        # [v8] 유령 포지션 방지: 잔고조회(hldg_qty)엔 찍혀도 실제 매도가능수량이 0이면
        #      감시 등록 자체를 하지 않는다. (모의계좌 리셋 등으로 잔고API와 실제
        #      sellable 수량이 어긋나면, 등록 후 sim_close에서 가짜 손절 거래가
        #      trade_log/리포트에 섞여 들어가는 문제가 있었음 → 원천 차단)
        try:
            sellable = kis.get_kr_sellable_qty(code)
        except Exception as e:
            print(f"[포지션 복원] {code} 매도가능수량 조회 실패({e}) - 안전하게 건너뜀")
            continue
        if sellable <= 0:
            print(f"[포지션 복원 스킵] {code} 잔고 {qty}주 표시되지만 매도가능 0주 (유령 포지션 추정)")
            skipped_ghost += 1
            continue
        qty = min(qty, sellable)

        entry_prices[code] = {"entry": avg, "time": get_kst_now(), "alert1": None, "alert2": None, "stop": None}
        sim_positions[code] = {"entry": avg, "qty": qty, "name": name}
        restored += 1
    if restored:
        print(f"[포지션 복원] 실계좌 {restored}종목 감시 등록 완료")
        send_telegram(f"🔄 [국장] 실계좌 {restored}종목 감시 복원 완료 (손절/익절 감시 시작)")
    if skipped_ghost:
        print(f"[포지션 복원] 유령 포지션 {skipped_ghost}종목 감시 제외")
        send_telegram(f"⚠️ [국장] 잔고API 표시 vs 실제 매도가능 불일치 {skipped_ghost}종목 감시 제외 (유령 포지션 추정)")


def main():
    global market_close_sent, daily_start_balance, daily_realized_pnl, daily_target_hit
    print("=" * 60)
    print("🚀 국내주식(KRX) 급등 감지 봇 시작! (v4)")
    print(f"🔌 LIVE_TRADING: {'ON (KIS 국내 모의투자 연동)' if LIVE_TRADING else 'OFF (시뮬만)'}")
    print(f"📈 1분 {PRICE_CHANGE_1M}%+ (1차필터) → 가중점수 {MIN_ENTRY_SCORE}점+ 만 매수 | {MIN_PRICE:,}원+ | 상위 {TOP_N}종목")
    print(f"🛡️ 상한가({LIMIT_UP_THRESHOLD}%+) 근접·우선주 제외")
    print(f"💰 예산: 매수가능금액 전액을 {MAX_POSITIONS}슬롯 분산 (누적, 몰빵 아님)")
    print(f"🎯 매도: 손절 {STOP_LOSS_PCT}%(net) | 트레일링 활성 +{TRAIL_ACTIVATE_PCT}%→고점대비 -{TRAIL_GAP_PCT}%p 청산 | 상한 +{SELL_FULL_PCT}%(net)")
    print(f"📅 일일 누적목표: +{DAILY_PROFIT_TARGET_PCT}% 도달 시 그날 신규 매수 중단 (보유종목 감시는 계속)")
    print(f"⏰ 매수 마감 {BUY_CUTOFF_HOUR}:{BUY_CUTOFF_MINUTE:02d} | 정리매매(전량청산) {LIQUIDATION_HOUR}:{LIQUIDATION_MINUTE:02d} (동시호가 전, 당일 매수·당일 매도)")
    print(f"⚡ 보유종목 {POSITION_CHECK_INTERVAL}초 주기 체크")
    print("=" * 60)

    send_telegram(
        f"🤖 <b>국내주식(KRX) 급등 감지 봇 시작! (v4)</b>\n"
        f"🔌 LIVE_TRADING: <b>{'ON' if LIVE_TRADING else 'OFF (시뮬만)'}</b>\n"
        f"📈 1분 {PRICE_CHANGE_1M}%+ 1차필터 → 가중점수 {MIN_ENTRY_SCORE}점+ 매수 | {MIN_PRICE:,}원+ 종목만\n"
        f"🛡️ 상한가 근접·우선주 제외\n"
        f"💰 예산: 매수가능금액 전액을 {MAX_POSITIONS}슬롯 분산 (누적, 몰빵 아님)\n"
        f"🎯 손절 {STOP_LOSS_PCT}%(net) | 트레일링 +{TRAIL_ACTIVATE_PCT}%→-{TRAIL_GAP_PCT}%p | 상한 +{SELL_FULL_PCT}%(net)\n"
        f"📅 일일 누적목표 +{DAILY_PROFIT_TARGET_PCT}% 도달 시 신규매수 중단\n"
        f"⏰ 매수마감 {BUY_CUTOFF_HOUR}:{BUY_CUTOFF_MINUTE:02d} | 정리매매 {LIQUIDATION_HOUR}:{LIQUIDATION_MINUTE:02d}"
    )

    last_scan_time = 0.0
    restore_positions_from_account()

    # [v6] 봇이 장중에 (재)시작되는 경우도 있으므로, 9:00 정각 리셋과 별개로
    # 시작 시점에도 기준자본이 비어있으면 한 번 채워둔다.
    if daily_start_balance <= 0 and LIVE_TRADING:
        daily_start_balance = _get_real_deposit_safe() or 0.0
        print(f"[일일 기준자본] {daily_start_balance:,.0f}원 (목표 +{DAILY_PROFIT_TARGET_PCT}%)")

    while True:
        try:
            now_str = get_kst_now().strftime("%H:%M:%S")

            if get_kst_now().hour == 9 and get_kst_now().minute == 0:
                if blacklisted_today or stop_loss_count or trade_log or last_alert or daily_target_hit:
                    blacklisted_today.clear(); stop_loss_count.clear()
                    trade_log.clear(); last_alert.clear()
                    market_close_sent = False
                    daily_realized_pnl = 0.0
                    daily_target_hit = False
                    daily_start_balance = _get_real_deposit_safe() or 0.0
                    print(f"[일일 리셋] 기준자본 {daily_start_balance:,.0f}원 (목표 +{DAILY_PROFIT_TARGET_PCT}%)")

            if not is_krx_regular_session():
                print(f"[{now_str}] 정규장 외 시간 — 대기 중...")
                time.sleep(POSITION_CHECK_INTERVAL)
                continue

            if get_kst_now().hour == LIQUIDATION_HOUR and get_kst_now().minute >= LIQUIDATION_MINUTE and not market_close_sent:
                market_close_sent = True
                for code in list(sim_positions.keys()):
                    price = kis.get_domestic_current_price(code)
                    if price:
                        sim_close(code, price, "정리매매(동시호가 전 강제청산)")
                send_telegram(build_report("🔔 정리매매 완료 최종 매매일지"))

            monitor_positions()

            now_kst = get_kst_now()
            buy_window_open = (now_kst.hour, now_kst.minute) < (BUY_CUTOFF_HOUR, BUY_CUTOFF_MINUTE) and not daily_target_hit

            now_mono = time.monotonic()
            if now_mono - last_scan_time >= CHECK_INTERVAL:
                last_scan_time = now_mono
                if buy_window_open:
                    print(f"\n[{now_str}] 정규장 스캔 시작")
                    run_scan()
                elif daily_target_hit:
                    print(f"[{now_str}] 일일 목표수익 도달 — 신규 스캔 생략, 보유종목 청산 대기만 진행")
                else:
                    print(f"[{now_str}] 매수 마감 시간({BUY_CUTOFF_HOUR}:{BUY_CUTOFF_MINUTE:02d} 이후) — 신규 스캔 생략, 보유종목 청산 대기만 진행")

            time.sleep(POSITION_CHECK_INTERVAL)


        except Exception as _loop_e:
            print(f"[\ub8e8\ud504 \uc624\ub958] {_loop_e}")
            import traceback; traceback.print_exc()
            time.sleep(POSITION_CHECK_INTERVAL)
if __name__ == "__main__":
    main()
