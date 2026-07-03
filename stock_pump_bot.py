"""
미국 주식 급등 감지 봇 v36 (정규장 전용 + 시뮬레이션 + 매매일지)
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
- [v31] 개장 변동성 구간(09:30~10:30 ET) 공격 모드: 진입 2%, 1차 +9%, 전량 +20%, 손절 -6%
- [v32] 초저가주($3 미만) 보유 시 15초 주기 빠른 가격 체크 (스캔 사이 갭 하락 대응)
- [v33] ETF/펀드/레버리지 상품 진입 제외 (종목명 키워드 필터)
- [v33] 일간 +30% 급등주 눌림목 재진입: 고점 -15% 조정 후 1분 +1.5% 반등 시 진입
- [v34] 본전 스탑: 1차매도 후 남은 물량은 본전(0%) 이탈 시 청산 (이긴 거래의 손실 전환 방지)
- [v34] 유령 포지션 버그 수정: 전량 청산 시 entry_prices도 함께 삭제
- [v34] 서머타임 자동 대응: UTC-4 하드코딩 → zoneinfo America/New_York
- [v34] 진입 필수조건 추가: 분당 거래대금 $50k 이상 + 최신 1분봉 2분 이내(낡은 봉 차단)
- [v34] 시뮬 슬리피지 반영: 매수/매도 체결가에 불리한 방향으로 0.2~0.5% 적용
- [v34] trade_log/알림기록 일일 초기화 (매매일지 무한 누적 방지)
- [v35] ★ 낡은 봉 필터 오작동 수정: 종목 92% 차단 → 매매 0건 문제 해결
        · 현재가/1분변동을 스냅샷 실시간체결가(latestTrade) 기준으로 계산 (봉은 보조)
        · 신선도 판단을 '봉 나이' → '마지막 체결 시각(latestTrade.t)'으로 교체
- [v35] 정렬 기준 변경: ATR 순 → (1분상승률×0.7 + 거래량비×0.3) 가중 점수
- [v35] 손절 카운트 주석/코드 정합 (3회째 차단 명시)
- [v35] 슬리피지 가격대별 차등 현실화 ($1미만 5% / $1~3 2% / $3~10 1% / $10+ 0.3%)
- [v36] 개장 안정화 구간: 09:30~09:35 ET 신규 진입 금지 (첫 5분 고변동 회피, 보유 매도는 정상)
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ET_TZ = ZoneInfo("America/New_York")   # [v34] 서머타임 자동 반영

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

# [v32] 초저가주 빠른 체크: 보유 종목 중 저가주는 스캔 사이에도 짧은 주기로 가격 확인
# (저가주는 유동성이 얇아 1분 사이 -10% → -30% 갭 하락이 실제로 발생했음: TC 사례)
FAST_CHECK_PRICE      = 3.0   # 이 가격 미만 보유 종목은 빠른 체크 대상
FAST_CHECK_INTERVAL   = 15    # 빠른 체크 주기 (초)

# [v33] ETF/펀드 제외 필터 (종목명 기반, Alpaca 자산정보 조회 + 캐시)
# 주의: "SHARES"는 일반 ADR(American Depositary Shares)까지 걸러버리므로 제외
ETF_NAME_KEYWORDS = ("ETF", "ETN", "FUND", "TRUST", "INDEX", "2X", "3X",
                     "BULL", "BEAR", "LEVERAGED", "INVERSE", "PROSHARES", "DIREXION")

# [v34] 진입 필수조건: 분당 거래대금
MIN_DOLLAR_VOL_1M   = 50_000   # 최근 1분봉 거래대금 $50k 미만이면 진입 금지 (유동성 스파이크 차단)

# [v35] 신선도 판단: 마지막 '체결' 시각 기준 (봉 나이가 아님)
# 저유동성 종목은 봉이 몇 시간 전일 수 있으나, 지금 실제 거래되면 latestTrade는 최신임
MAX_TRADE_AGE_SEC   = 90       # 마지막 체결이 90초 이상 전이면 '죽은 종목'으로 진입 금지

# [v35] 시뮬 슬리피지 가격대별 차등 (급등주 현실 반영: 저가일수록 호가 벌어짐)
def slippage_pct_for(price: float) -> float:
    if price < 1.0:   return 5.0   # $1 미만: 극단적 슬리피지
    if price < 3.0:   return 2.0   # $1~3
    if price < 10.0:  return 1.0   # $3~10
    return 0.3                     # $10+

# [v34] 본전 스탑: 1차매도 후 남은 물량의 손절선을 본전으로 상향
BREAKEVEN_STOP_PCT  = 0.0

# [v33] 일간 급등주 눌림목 재진입
PULLBACK_MIN_DAY_GAIN   = 30.0   # 감시 등록 기준: 일간 등락률 30% 이상
PULLBACK_DROP_PCT       = 15.0   # 장중 고점 대비 15% 이상 조정 시 재진입 후보
PULLBACK_BOUNCE_1M      = 1.5    # 조정 후 1분봉 +1.5% 반등 시 진입
PULLBACK_WATCH_MAX      = 10     # 감시 종목 최대 수
COOLDOWN_MINUTES      = 30
SELL_COOLDOWN_MINUTES = 60
MAX_BUY_PER_SCAN      = 3    # [v24] 스캔 1회당 신규 매수 최대 종목 수
MAX_POSITIONS         = 7    # [v29] 동시 보유 최대 종목 수

# 매도 타이밍 임계값 (평소)
SELL_PARTIAL_PCT = 7.0
SELL_FULL_PCT    = 15.0
STOP_LOSS_PCT    = -10.0

# [v31] 개장 변동성 구간(09:30~10:30 ET) 공격 모드 파라미터
AGGRESSIVE_START_MIN        = 9 * 60 + 30   # 09:30 ET
AGGRESSIVE_END_MIN          = 10 * 60 + 30  # 10:30 ET
AGGRESSIVE_PRICE_CHANGE_1M  = 2.0    # 진입 조건 완화: 3.0% → 2.0%
AGGRESSIVE_SELL_PARTIAL_PCT = 9.0    # 1차 매도 목표 상향: 7.0% → 9.0%
AGGRESSIVE_SELL_FULL_PCT    = 20.0   # 전량 매도 목표 상향: 15.0% → 20.0%
AGGRESSIVE_STOP_LOSS_PCT    = -6.0   # 손절 타이트하게: -10.0% → -6.0%

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
MAX_STOP_LOSS_COUNT = 2   # [v35] cnt > 2, 즉 3회째 손절부터 당일 차단 (1·2회는 재진입 허용)

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
    # [v34] 서머타임(EDT/EST) 자동 반영
    return datetime.now(ET_TZ)


_market_open_cache = {"date": None, "value": False}

def is_market_holiday_or_closed() -> bool:
    """
    [v37] Alpaca Clock API로 미국 공휴일/휴장일 여부 확인.
    같은 날짜에 대해 결과를 캐싱하여 API 호출을 최소화한다.
    API 호출 실패 시에는 휴장으로 단정하지 않고 False를 반환하여
    기존 요일 판단 로직만 적용되도록 한다.
    """
    now_et = get_et_now()
    today_str = now_et.strftime("%Y-%m-%d")

    if _market_open_cache["date"] == today_str:
        return not _market_open_cache["value"]

    try:
        resp = requests.get(
            "https://api.alpaca.markets/v2/calendar",
            headers={
                "APCA-API-KEY-ID": ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            },
            params={"start": today_str, "end": today_str},
            timeout=5,
        )
        data = resp.json()
        is_open_today = len(data) > 0   # 해당 날짜가 캘린더에 있으면 개장일
        _market_open_cache["date"] = today_str
        _market_open_cache["value"] = is_open_today
        return not is_open_today
    except Exception as e:
        print(f"[캘린더 조회 오류] {e} → 요일 기준으로만 판단")
        return False


def is_regular_session() -> bool:
    now_et = get_et_now()
    et_min = now_et.hour * 60 + now_et.minute
    if now_et.weekday() >= 5:
        return False
    if is_market_holiday_or_closed():
        return False
    return (9 * 60 + 30) <= et_min <= (16 * 60)


def is_aggressive_window() -> bool:
    """개장 직후 변동성 구간(09:30~10:30 ET) 여부."""
    now_et = get_et_now()
    et_min = now_et.hour * 60 + now_et.minute
    if now_et.weekday() >= 5:
        return False
    if is_market_holiday_or_closed():
        return False
    return AGGRESSIVE_START_MIN <= et_min <= AGGRESSIVE_END_MIN


ENTRY_BLOCK_UNTIL_MIN = 9 * 60 + 35   # [v36] 09:35 ET까지 신규 진입 금지

def is_entry_allowed() -> bool:
    """[v36] 신규 진입 허용 여부. 개장 직후 5분(09:30~09:35)은 고변동이라 진입 금지."""
    now_et = get_et_now()
    et_min = now_et.hour * 60 + now_et.minute
    if now_et.weekday() >= 5:
        return False
    if is_market_holiday_or_closed():
        return False
    return et_min >= ENTRY_BLOCK_UNTIL_MIN


def get_active_thresholds() -> dict:
    """현재 시간대(공격모드/평소)에 맞는 진입·매도 임계값 반환."""
    if is_aggressive_window():
        return {
            "price_change_1m": AGGRESSIVE_PRICE_CHANGE_1M,
            "sell_partial_pct": AGGRESSIVE_SELL_PARTIAL_PCT,
            "sell_full_pct": AGGRESSIVE_SELL_FULL_PCT,
            "stop_loss_pct": AGGRESSIVE_STOP_LOSS_PCT,
            "mode": "🔥공격",
        }
    return {
        "price_change_1m": PRICE_CHANGE_1M,
        "sell_partial_pct": SELL_PARTIAL_PCT,
        "sell_full_pct": SELL_FULL_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
        "mode": "평시",
    }


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

def apply_slippage(price: float, side: str) -> float:
    """[v35] 관측가에 불리한 방향으로 가격대별 슬리피지 적용. side: 'buy'|'sell'"""
    pct = slippage_pct_for(price)
    factor = 1 + pct / 100 if side == "buy" else 1 - pct / 100
    return price * factor


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
    # [v34] 매수 체결가에 슬리피지 반영 (관측가보다 불리하게)
    fill_price = apply_slippage(price, "buy")
    # [v30] 남은 슬롯에 예수금 균등 분배
    remaining_slots = MAX_POSITIONS - len(sim_positions)
    budget          = sim_stats["cash"] / remaining_slots
    qty             = int(budget // fill_price)
    if qty < 1:
        print(f"  [시뮬 매수 불가] {sym} | 예수금 부족 (슬롯예산={budget:.2f}, 1주={fill_price:.2f})")
        return False
    cost = fill_price * qty
    sim_stats["cash"] -= cost
    sim_positions[sym] = {"entry": fill_price, "qty": qty, "partial_done": False}
    # entry_prices의 진입가도 체결가 기준으로 동기화
    if sym in entry_prices:
        entry_prices[sym]["entry"] = fill_price
    now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
    trade_log.append({
        "action": "BUY", "sym": sym, "qty": qty, "price": fill_price,
        "pnl": 0.0, "pnl_pct": 0.0, "reason": "매수",
        "time_kst": now_kst.strftime("%H:%M"),
    })
    print(f"  [시뮬 매수] {sym} {qty}주 @ ${fill_price:.2f} (관측가 ${price:.2f}+슬리피지, 남은슬롯 {remaining_slots}) | 잔여: ${sim_stats['cash']:.2f}")
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

    # [v34] 매도 체결가에 슬리피지 반영 (관측가보다 불리하게)
    exit_price  = apply_slippage(exit_price, "sell")

    entry_price = pos["entry"]   # 청산 전에 미리 저장
    close_qty   = qty if qty is not None else pos["qty"]
    pnl         = (exit_price - entry_price) * close_qty
    pnl_pct     = ((exit_price - entry_price) / entry_price) * 100

    sim_stats["cash"] += exit_price * close_qty
    pos["qty"] -= close_qty

    if pos["qty"] <= 0:
        del sim_positions[sym]
        entry_prices.pop(sym, None)   # [v34] 유령 포지션 버그 수정: 추적 정보도 함께 삭제
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
# [v32] 실계좌 자동매매 — 주문 실행
# ──────────────────────────────────────────

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


_asset_name_cache = {}   # sym -> 종목명 (ETF 판별용, 하루 단위로 충분히 유효)

def is_etf(sym: str) -> bool:
    """
    [v33] ETF/펀드/레버리지 상품 판별 (종목명 키워드 기반).
    Alpaca 자산정보 API로 종목명을 조회해 캐시하고, 이름에 ETF성 키워드가 있으면 제외.
    조회 실패 시 False (일반 종목으로 간주).
    """
    if sym in _asset_name_cache:
        name = _asset_name_cache[sym]
    else:
        try:
            resp = requests.get(
                f"https://api.alpaca.markets/v2/assets/{sym}",
                headers=HEADERS, timeout=10,
            )
            name = resp.json().get("name", "") if resp.status_code == 200 else ""
        except Exception:
            name = ""
        _asset_name_cache[sym] = name
    upper = name.upper()
    return any(kw in upper for kw in ETF_NAME_KEYWORDS)


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
    source = "체결" if lt.get("p") else ("1분봉" if mb.get("c") else "종가")
    return price, source


def latest_trade_age_sec(snap: dict) -> float:
    """[v35] 스냅샷의 마지막 체결(latestTrade.t) 경과 시간(초). 없으면 큰 값."""
    lt = snap.get("latestTrade", {})
    t  = lt.get("t")
    if not t:
        # 체결 정보 없으면 minuteBar 시각으로 폴백
        mb = snap.get("minuteBar", {})
        t  = mb.get("t")
    if not t:
        return 999999.0
    try:
        ts = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return 999999.0


def snap_1m_change(snap: dict, bars: list):
    """
    [v35] 1분 변동률 계산. 현재가는 스냅샷 실시간체결가 우선.
    비교 기준(1분 전 가격)은:
      1) minuteBar.o (현재 진행중 분봉 시가) 가 있으면 사용 — 봉 나이와 무관하게 최신
      2) 없으면 봉 데이터 bars[-2].c 로 폴백
    반환: (current_price, price_1m_ago, change_pct) 또는 None
    """
    lt = snap.get("latestTrade", {})
    mb = snap.get("minuteBar", {})
    current = lt.get("p") or mb.get("c")
    if not current:
        return None
    current = float(current)

    ref = None
    # minuteBar 시가: 지금 진행 중인 1분봉의 시작가 → 실시간성 보장
    if mb.get("o"):
        ref = float(mb["o"])
    elif bars and len(bars) >= 1:
        # 현재가는 스냅샷(실시간)에서 오므로, 비교 기준은 '직전 완성봉 종가'(=실질 1분 전)
        ref = float(bars[-1]["c"])
    if not ref or ref <= 0:
        return None

    change = ((current - ref) / ref) * 100
    return current, ref, change


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

    # [v31] 진입 당시 기록해둔 시간대별(공격모드/평시) 매도 임계값 사용
    th               = entry.get("thresholds") or get_active_thresholds()
    sell_partial_pct = th["sell_partial_pct"]
    sell_full_pct    = th["sell_full_pct"]
    stop_loss_pct    = th["stop_loss_pct"]

    # [v34] 본전 스탑: 1차매도가 나갔으면 남은 물량 손절선을 본전으로 상향
    breakeven_armed = entry.get("breakeven_armed", False)
    if breakeven_armed:
        stop_loss_pct = BREAKEVEN_STOP_PCT

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

    # ── 손절 / 본전스탑 ──
    if gain_pct <= stop_loss_pct:
        if cooldown_ok("stop"):
            entry["stop"] = now_utc
            reason = "본전스탑" if breakeven_armed else f"손절({stop_loss_pct:.0f}%)"
            if sym in sim_positions:
                sim_close(sym, current_price, reason, qty=None)
            icon = "⚖️" if breakeven_armed else "🔴"
            print(f"[{icon} {reason}] {sym} | ${entry_price:.2f} → ${current_price:.2f} ({gain_pct:+.2f}%)")
        return

    # ── 전량 매도 ──
    if gain_pct >= sell_full_pct:
        if cooldown_ok("alert2"):
            entry["alert2"] = now_utc
            if sym in sim_positions:
                sim_close(sym, current_price, f"+{sell_full_pct:.0f}% 전량", qty=None)
            print(f"[🟢 전량매도] {sym} | ${entry_price:.2f} → ${current_price:.2f} ({gain_pct:+.2f}%)")
        return

    # ── 1차 매도(절반) ──
    if gain_pct >= sell_partial_pct:
        if cooldown_ok("alert1"):
            entry["alert1"] = now_utc
            pos = sim_positions.get(sym)
            if pos and not pos.get("partial_done"):
                half = max(1, pos["qty"] // 2)
                sim_close(sym, current_price, f"+{sell_partial_pct:.0f}% 1차(절반)", qty=half)
                entry["breakeven_armed"] = True   # [v34] 남은 물량은 본전 이탈 시 청산
                print(f"  [⚖️ 본전스탑 활성] {sym} — 남은 물량 손절선 → 진입가")
            print(f"[🟡 1차매도] {sym} | ${entry_price:.2f} → ${current_price:.2f} ({gain_pct:+.2f}%)")


# ──────────────────────────────────────────
# 종목 분석
# ──────────────────────────────────────────

def last_bar_age_sec(bars: list) -> float:
    """[v34] 마지막 1분봉의 경과 시간(초). 파싱 실패 시 매우 큰 값."""
    try:
        t = bars[-1]["t"].replace("Z", "+00:00")
        bar_time = datetime.fromisoformat(t)
        return (datetime.now(timezone.utc) - bar_time).total_seconds()
    except Exception:
        return 999999.0


def analyze_regular(sym: str, snap: dict):
    # [v35] 신선도: 마지막 '체결' 시각 기준 (봉 나이 아님) — 지금 실제 거래되는 종목만
    trade_age = latest_trade_age_sec(snap)
    if trade_age > MAX_TRADE_AGE_SEC:
        print(f"  └ 거래 정지 제외: 마지막 체결 {trade_age:.0f}초 전 (기준 {MAX_TRADE_AGE_SEC}초)")
        return None

    bars = get_bars(sym)   # 지표(거래량/OBV/RSI)용 보조 데이터. 없어도 진입 가능
    has_bars = bool(bars and len(bars) >= 6)

    # [v35] 1분 변동률: 스냅샷 실시간체결가 우선 (봉이 낡아도 정확)
    ch = snap_1m_change(snap, bars if has_bars else [])
    if ch is None:
        print(f"  └ 가격 계산 불가 (스냅샷/봉 모두 부족)")
        return None
    current_price, price_1m_ago, price_change_1m = ch

    # [v25] 저가주 필터: $1 미만 제외
    if current_price < MIN_PRICE:
        print(f"  └ 저가주 제외: ${current_price:.2f} < ${MIN_PRICE}")
        return None

    # 지표 (모두 참고/보조용)
    rsi       = calc_rsi(bars)       if has_bars else None
    vol_ratio, vol_ok = calc_volume_surge(bars) if has_bars else (0.0, False)
    obv_label = calc_obv(bars)       if has_bars else "-"
    atr       = calc_atr(bars)       if has_bars else 0.0

    # [v31] 시간대별(공격모드/평시) 진입 임계값
    th       = get_active_thresholds()
    entry_th = th["price_change_1m"]

    rsi_disp     = f"{rsi:.1f}" if rsi is not None else "N/A"
    price_ok_str = "✅" if price_change_1m >= entry_th else "❌"
    print(
        f"  └ [{th['mode']}] 1분:{price_change_1m:+.2f}%(기준{entry_th}%){price_ok_str} "
        f"| RSI:{rsi_disp} | 거래량:{vol_ratio:.1f}x | ATR:{atr:.3f} | OBV:{obv_label} | 체결{trade_age:.0f}s前"
    )

    # 진입 조건: 1분 상승 (시간대별 임계값)
    if price_change_1m < entry_th:
        return None

    # [v34→35] 분당 거래대금 필수조건 (봉 있을 때만 체크; 없으면 통과)
    if has_bars:
        recent_dv = max(float(b["v"]) * float(b["c"]) for b in bars[-3:])
        if recent_dv < MIN_DOLLAR_VOL_1M:
            print(f"  └ 거래대금 부족: 분당 ${recent_dv:,.0f} < ${MIN_DOLLAR_VOL_1M:,} — 진입 금지")
            return None

    return {
        "rsi":             rsi if rsi is not None else 0.0,
        "price_change_1m": price_change_1m,
        "obv_label":       obv_label,
        "vol_ratio":       vol_ratio,
        "atr":             atr,
        "current_price":   current_price,   # [v35] 스냅샷 기준 현재가 (진입가로 사용)
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
    if is_market_holiday_or_closed():
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

        entry_prices.clear()   # [v34] 알림 전용 추적 포함 전체 정리

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

def fast_check_low_priced():
    """
    [v32] 보유 종목 중 초저가주(FAST_CHECK_PRICE 미만)만 골라 가격을 확인하고
    손절/익절 타이밍을 체크한다. 메인 스캔(60초) 사이의 갭 하락을 잡기 위함.
    반환: 체크한 종목 수
    """
    low_syms = [
        sym for sym, e in entry_prices.items()
        if e["entry"] < FAST_CHECK_PRICE and sym in sim_positions
    ]
    if not low_syms:
        return 0
    snaps = get_snapshots(low_syms)
    checked = 0
    for sym, snap in snaps.items():
        price, source = get_live_price(snap)
        if price:
            check_sell_timing(sym, float(price), source)
            checked += 1
    return checked


# [v33] 일간 급등주 눌림목 감시: sym -> {"high": 장중 최고 관측가, "day_gain": 등록 시 등락률}
pullback_watch = {}

def update_pullback_watch(ranked: list):
    """일간 +30% 이상 급등 종목을 감시 목록에 등록/갱신 (ETF·워런트·저가주 제외)."""
    for stock in ranked:
        sym = stock["symbol"]
        if stock["change_pct"] < PULLBACK_MIN_DAY_GAIN:
            continue
        if stock["price"] < MIN_PRICE or sym in blacklisted_today:
            continue
        if is_warrant(sym) or is_etf(sym):
            continue
        if sym in pullback_watch:
            # 장중 고점 갱신
            if stock["price"] > pullback_watch[sym]["high"]:
                pullback_watch[sym]["high"] = stock["price"]
        elif len(pullback_watch) < PULLBACK_WATCH_MAX:
            pullback_watch[sym] = {"high": stock["price"], "day_gain": stock["change_pct"]}
            print(f"  [👀 눌림감시 등록] {sym} (일간 {stock['change_pct']:+.1f}%, 고점 ${stock['price']:.2f})")


def check_pullback_entries(snap_map: dict, now_utc, bought_this_scan: int) -> int:
    """
    감시 종목 중 '고점 대비 -15% 이상 조정 후 1분봉 +1.5% 반등' 시 재진입.
    반환: 이번 스캔 누적 매수 수
    """
    for sym in list(pullback_watch.keys()):
        if bought_this_scan >= MAX_BUY_PER_SCAN:
            break
        if sym in entry_prices or sym in blacklisted_today:
            continue
        if sym in last_alert:
            elapsed = (now_utc - last_alert[sym]).total_seconds() / 60
            if elapsed < COOLDOWN_MINUTES:
                continue

        stock = snap_map.get(sym)
        if stock:
            price = stock["price"]
            snap_for_age = stock["snap"]
        else:
            snaps = get_snapshots([sym])
            snap  = snaps.get(sym)
            if not snap:
                continue
            price, _ = get_live_price(snap)
            if not price:
                continue
            snap_for_age = snap

        # [v35] 거래 정지 종목 제외 (마지막 체결 시각 기준)
        if latest_trade_age_sec(snap_for_age) > MAX_TRADE_AGE_SEC:
            continue

        watch = pullback_watch[sym]
        if price > watch["high"]:
            watch["high"] = price
            continue

        drop_pct = ((watch["high"] - price) / watch["high"]) * 100
        if drop_pct < PULLBACK_DROP_PCT:
            continue

        # 조정 확인됨 → 1분봉 반등 체크 (스냅샷 minuteBar 우선)
        bars = get_bars(sym)
        ch = snap_1m_change(snap_for_age, bars if (bars and len(bars) >= 2) else [])
        if ch is None:
            continue
        _, _, bounce = ch
        if bounce < PULLBACK_BOUNCE_1M:
            continue

        # 재진입!
        last_alert[sym] = now_utc
        entry_prices[sym] = {
            "entry": price, "time": now_utc,
            "alert1": None, "alert2": None, "stop": None,
            "sideways_done": False,
            "thresholds": get_active_thresholds(),
        }
        if sim_open(sym, price):
            bought_this_scan += 1
            print(
                f"[🎯 눌림재진입] {sym} | 고점 ${watch['high']:.2f} 대비 -{drop_pct:.1f}% 조정 후 "
                f"1분 +{bounce:.2f}% 반등 | 진입가 ${price:.2f}"
            )
        del pullback_watch[sym]   # 진입했으면 감시 해제
    return bought_this_scan


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
    mode_now = get_active_thresholds()["mode"]
    print(f"[정규장/{mode_now}] 상위 {REGULAR_TOP_N}종목 | 1위: {top[0]['symbol']} {top[0]['change_pct']:+.2f}%")
    print(f"  {holdings_block().replace(chr(10), ' | ')}")
    if blacklisted_today:
        print(f"  🚫 블랙리스트: {', '.join(sorted(blacklisted_today))}")

    # [v35] 가중 점수(1분상승률×0.7 + 거래량비×0.3)로 재정렬 — 급등 초입 우선
    scored = []
    etf_excluded = []
    for stock in top:
        sym = stock["symbol"]
        if sym in blacklisted_today:
            continue
        if is_etf(sym):                     # [v33] ETF/펀드/레버리지 제외
            etf_excluded.append(sym)
            continue
        # 스냅샷 기준 1분 변동 + 거래량비 (봉은 보조)
        bars = get_bars(sym)
        ch = snap_1m_change(stock["snap"], bars if (bars and len(bars) >= 2) else [])
        pc_1m = ch[2] if ch else 0.0
        vr, _ = calc_volume_surge(bars) if (bars and len(bars) >= 6) else (0.0, False)
        score = pc_1m * 0.7 + min(vr, 5.0) * 0.3
        scored.append({**stock, "_score": score, "_pc1m": pc_1m, "_vr": vr})
    if etf_excluded:
        print(f"  [ETF 제외] {', '.join(etf_excluded)}")

    scored.sort(key=lambda x: x["_score"], reverse=True)
    top_with_atr = scored   # 이후 루프 호환용 이름 유지
    print(f"  [점수 재정렬] " + " | ".join(
        f"{s['symbol']}({s['_score']:.1f}|{s['_pc1m']:+.1f}%,{s['_vr']:.1f}x)" for s in scored[:5]
    ))

    # ── [v24] 스캔당 최대 MAX_BUY_PER_SCAN 종목만 신규 매수 ──
    bought_this_scan = 0

    # [v36] 개장 직후 5분은 신규 진입 금지 (보유 종목 매도는 위에서 이미 처리됨)
    if not is_entry_allowed():
        print("  [개장 안정화 구간] 09:35 ET 전 — 신규 진입 보류 (매도는 정상)")
        # 눌림감시 목록은 계속 갱신해두되, 재진입 매수는 하지 않음
        update_pullback_watch(ranked)
        return

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

        # [v35] 진입가는 스냅샷 기준 현재가 (봉 종가 아님)
        entry_px = result.get("current_price", stock["price"])

        # 매수 제한 체크 (한도 초과면 entry_prices에 남기지 않음)
        if bought_this_scan >= MAX_BUY_PER_SCAN:
            print(f"  [매수 제한] {sym} — 이번 스캔 {MAX_BUY_PER_SCAN}종목 초과, 스킵")
            continue

        last_alert[sym] = now_utc
        entry_prices[sym] = {
            "entry": entry_px, "time": now_utc,
            "alert1": None, "alert2": None, "stop": None,
            "sideways_done": False,
            "thresholds": get_active_thresholds(),   # [v31] 진입 당시 시간대 기준 고정
        }

        bought = sim_open(sym, entry_px)
        if bought:
            bought_this_scan += 1
        else:
            entry_prices.pop(sym, None)   # 매수 실패 시 추적정보 정리

        print(
            f"[🚀 감지] {sym} | 1분{result['price_change_1m']:+.2f}% | RSI {result['rsi']:.1f} "
            f"| 거래량 {result['vol_ratio']:.1f}x | 진입가 ${entry_px:.2f}"
        )
        time.sleep(0.5)

    # ── [v33] 일간 급등주 눌림목 감시/재진입 ──
    update_pullback_watch(ranked)
    if pullback_watch:
        print(f"  [눌림감시 중] " + " | ".join(
            f"{s}(고점${w['high']:.2f})" for s, w in pullback_watch.items()
        ))
    bought_this_scan = check_pullback_entries(snap_map, now_utc, bought_this_scan)


# ──────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────

def main():
    global market_close_sent

    print("=" * 60)
    print("🚀 급등 감지 봇 v36 (정규장 전용 + 시뮬 + 매매일지) 시작!")
    print(f"📈 정규장: 상위 {REGULAR_TOP_N}종목 | 1분 {PRICE_CHANGE_1M}%+ | ${MIN_PRICE}+ 종목만")
    print(f"🎯 매도: +{SELL_PARTIAL_PCT}% 1차 | +{SELL_FULL_PCT}% 전량 | {STOP_LOSS_PCT}% 손절")
    print(
        f"🔥 공격모드(09:30~10:30 ET): 1분 {AGGRESSIVE_PRICE_CHANGE_1M}%+ | "
        f"+{AGGRESSIVE_SELL_PARTIAL_PCT}% 1차 | +{AGGRESSIVE_SELL_FULL_PCT}% 전량 | {AGGRESSIVE_STOP_LOSS_PCT}% 손절"
    )
    print(f"➡️  횡보청산: {SIDEWAYS_MINUTES}분 경과 & +{SIDEWAYS_MIN_PCT}~+{SIDEWAYS_MAX_PCT}% 구간")
    print(f"📦 동시 보유 최대 {MAX_POSITIONS}종목 | 스캔당 최대 {MAX_BUY_PER_SCAN}종목")
    print(f"🔔 장마감 보유 종목 전량 강제 청산")
    print(f"🚫 손절 {MAX_STOP_LOSS_COUNT}회 도달 시 당일 블랙리스트 등록 (그 전까진 재진입 허용)")
    print("=" * 60)

    send_telegram(
        f"🤖 <b>급등 감지 봇 v36 시작!</b>\n"
        f"📈 평시 1분 {PRICE_CHANGE_1M}%+ | ${MIN_PRICE}+ 종목만\n"
        f"🔥 공격모드(09:30~10:30 ET): 1분 {AGGRESSIVE_PRICE_CHANGE_1M}%+ | "
        f"+{AGGRESSIVE_SELL_PARTIAL_PCT}% 1차 | +{AGGRESSIVE_SELL_FULL_PCT}% 전량 | {AGGRESSIVE_STOP_LOSS_PCT}% 손절\n"
        f"📦 동시 보유 최대 {MAX_POSITIONS}종목 | 스캔당 최대 {MAX_BUY_PER_SCAN}종목\n"
        f"📊 상승률 상위 {REGULAR_TOP_N}종목 → 점수(1분상승×0.7+거래량×0.3) 순 진입\n"
        f"🔔 장마감 보유 종목 전량 강제 청산\n"
        f"🚫 손절 {MAX_STOP_LOSS_COUNT}회 도달 시 당일 차단 (그 전까진 재진입 허용)\n"
        f"⚖️ 1차매도 후 본전스탑 | 💧 분당 거래대금 ${MIN_DOLLAR_VOL_1M//1000}k+ 필수\n"
        f"🧾 시뮬 체결가 슬리피지 반영 ($1미만 5% / $1~3 2% / $3~10 1% / $10+ 0.3%)\n"
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
            if blacklisted_today or stop_loss_count or trade_log or last_alert:
                blacklisted_today.clear()
                stop_loss_count.clear()
                pullback_watch.clear()   # [v33] 눌림감시 목록도 새 장마다 초기화
                trade_log.clear()        # [v34] 매매일지 일일 초기화 (무한 누적 방지)
                last_alert.clear()       # [v34] 쿨다운 기록 초기화
                # 포지션 없는 잔여 추적 정보 정리 (장마감 청산 실패 대비)
                for s in [s for s in entry_prices if s not in sim_positions]:
                    del entry_prices[s]
                print("[리셋] 블랙리스트/손절카운트/눌림감시/일지/쿨다운 초기화 (새 장 시작)")

        check_scheduled_reports()

        if not is_regular_session():
            print(f"[{now_str}] 정규장 외 시간 — 대기 중...")
            time.sleep(CHECK_INTERVAL)
        else:
            print(f"\n[{now_str}] 정규장 스캔 시작")
            run_scan()

            # [v32] 다음 스캔까지 60초 대기하는 동안, 초저가주 보유 시 15초마다 가격 체크
            waited = 0
            while waited < CHECK_INTERVAL:
                time.sleep(FAST_CHECK_INTERVAL)
                waited += FAST_CHECK_INTERVAL
                if waited >= CHECK_INTERVAL:
                    break
                n = fast_check_low_priced()
                if n:
                    print(f"  [⚡빠른체크] 초저가주 {n}종목 가격 확인")


if __name__ == "__main__":
    main()
