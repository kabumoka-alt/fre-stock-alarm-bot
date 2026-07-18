# -*- coding: utf-8 -*-
"""
surge_scalper_us.py
미장 급등주 초입 포착 → +5% 익절 스캘핑 v34 (KIS 모의투자, 해외주식)

구조
  - 스크리닝/시세: Alpaca (기존 미국봇과 동일한 소스)
  - 체결/잔고    : 기존 kis_client.py 의 해외주식 함수 재활용 (중복 구현 없음)
      · kis.place_order(symbol, qty, price, side, session="regular")
      · kis.get_overseas_balance()
      · kis.get_buyable_amount(symbol, price)
  - 개장/휴장/DST : Alpaca clock API 로 판단 (하드코딩 없음)

전략 (실제 해외 매매내역 542건 분석 반영)
  - 님 이긴 거래 중앙값이 +5.3% → 익절 +5%
  - 님 진 거래 중앙값이 -14%(손절 안 함)가 유일한 손실 원인 → 손절 -3% 강제
  - 당일청산 74% → 시간청산 30분 + 장마감 전 전량청산
  - 승률: $1~5 양호 / $5~20 28%로 최악 → 진입 $1.0~$5.0 로 집중
  - 전부 마이크로캡 데이트레이딩 → 유동성 필터 필수

⚠️⚠️ 배포 전 확인 2가지
  1) EC2의 kis_client 해외 매도가 VTTT1001U + SLL_TYPE "00" 로 "고쳐진" 버전인지 확인.
     (업로드해준 파일은 옛 VTTT1006U 라 그대로면 매도가 전부 실패함)
  2) 같은 KIS 모의계좌에서 기존 미국봇과 동시에 돌리지 말 것 (현금/포지션 충돌).
     → 오늘은 기존 미국봇 stop 하고 이것만.

⚠️ 주문·잔고·매수가능금액 조회에는 Alpaca 자산정보에서 확인한
   NASD/NYSE/AMEX 거래소코드를 동일하게 전달함.

[v34]
  - 매수 주문 접수와 체결을 분리하고, 주문 전후 실제 잔고 증가분만 장부에 기록.
  - 부분체결은 체결분만 기록하고 미체결은 포지션을 만들지 않음.
  - 스캔 시작 시 총예산을 한 번만 확정하고 실제 체결금액만 남은 예산에서 차감.
"""

import os
import json
import time
import logging
import math
import atexit
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# KIS 해외주식 체결은 기존 공용 모듈 그대로 재활용
import kis_client as kis
from trade_logger import TradeLogger

# ─────────────────────────────────────────────
# 전략 파라미터 (매매내역 분석 기반 — 내일 돌려보고 여기부터 튜닝)
# ─────────────────────────────────────────────
MAX_POSITIONS       = 3
BUDGET_PER_POSITION = 200.0        # 종목당 상한 예산(USD) — 매수가능액과 min 처리
ENTRY_MIN_CHANGE    = 5.0          # 진입 최소 등락률(%, 전일종가 대비)
ENTRY_MAX_CHANGE    = 12.0         # 진입 최대 등락률(%)
MIN_PRICE           = 1.0          # $1 미만 제외(호가/LULD 정밀도 이슈 + 승률 애매)
MAX_PRICE           = 20.0         # $20 초과 제외 (과거 $5~20 승률은 낮으니 로그 보고 조정)
MIN_5MIN_DOLLAR_VOL = 5_000.0      # 최근 5분 거래대금 하한(USD) — IEX 저평가 감안(2026-07-10 JLHL 사례로 확정)
MOMENTUM_1M_MIN_PCT = 3.0          # 티어A/B/C 공통: 최근 1분봉 상승폭 최소기준(v31 이식, 2026-07-15 저승률 데이터로 도입)
TAKE_PROFIT         = 5.0          # (미사용, 초입은 부분익절+트레일링으로 대체됨 — 하위호환 위해 남겨둠)
STOP_LOSS           = -3.0         # (미사용, 아래 EARLY_STOP_LOSS_V2로 대체됨)
TIME_EXIT_MIN       = 30           # (미사용, 초입은 시간청산 없이 부분익절+트레일링으로 청산)

# ── v31(Railway 봇) 이식: 초입 — 부분익절 + 트레일링(고점추적) ──
EARLY_PARTIAL_PCT    = 7.0         # 이 수익률에서 절반 익절
EARLY_TRAIL_ACTIVATE = 6.0         # 고점이 이 이상 찍히면 트레일링 활성화
EARLY_TRAIL_GAP      = 4.0         # 고점 대비 이만큼(%p) 빠지면 트레일링청산
EARLY_STOP_LOSS_V2   = -8.0        # 손절(기존 STOP_LOSS -3.0 대체)

# ── 티어A — 부분익절 + 트레일링(초입보다 넓게, 손절은 -3% 유지) ──
TIER_A_PARTIAL_PCT    = 10.0
TIER_A_TRAIL_ACTIVATE = 10.0
TIER_A_TRAIL_GAP      = 6.0

# ── 일일 리스크 게이트 (전체 계좌 공통, v31 이식) ──
DAILY_PROFIT_LOCK_PCT = 5.0        # 당일 실현손익 +5% 도달 시 신규매수 중단
DAILY_LOSS_LIMIT_PCT  = -4.0       # 당일 실현손익 -4% 도달 시 신규매수 중단

SCREEN_INTERVAL     = 60           # 스크리닝 주기(초)
MONITOR_INTERVAL    = 10           # 보유 종목 감시 주기(초)
PRICE_STALE_SEC     = 90           # 감시 가격이 이보다 오래되면 STALE 경고
PRICE_EMERGENCY_STALE_SEC = 300    # 이 이상 지연되면 emergency stale
PRICE_WARNING_INTERVAL = 300       # 동일 폴백/지연 경고 최소 반복 간격(초)
BUY_FILL_CHECK_RETRIES = 5         # 주문 후 실제 잔고 반영 확인 횟수
BUY_FILL_CHECK_INTERVAL = 2.0      # 잔고 재조회 간격(초)
MOVERS_TOP          = 50           # Alpaca 급등 상위 몇 개까지 볼지
MOST_ACTIVES_TOP    = 100          # 거래량 상위 몇 개를 초입 후보 풀로 볼지
RECENT_MOVE_MIN     = 3.0          # 최근 5분 최소 상승폭(%) — "지금 움직이는 중"만 통과
VOL_SURGE_MULT      = 3.0          # 초입: 최근 분당거래량 / 직전평균 배수
# ── 추격 = 3개 티어로 자본 분할 (같은 급등 후보풀, 서로 다른 필터/청산/예산) ──
CHASE_ENABLED         = True
CHASE_MIN_CHANGE      = 20.0       # 추격 후보 최소 등락률(%)
CHASE_MAX_CHANGE      = 80.0       # 추격 후보 최대 등락률(%) — NVVE류(+91~106%) 슬리피지 사례로 하향

# 티어A: 급상승 상위, "하락 중만 아니면" 매수 (거래량 조건 없음) — 예수금 30%
TIER_A_BUDGET_PCT     = 0.30
TIER_A_MAX_POS        = 1
TIER_A_TP             = 20.0
TIER_A_SL             = -3.0

# 티어B: 기존 추격 로직 유지, 거래량 조건만 대폭 완화 — 예수금 60%
TIER_B_BUDGET_PCT     = 0.60
TIER_B_MAX_POS        = 2
TIER_B_TP             = 3.0
TIER_B_SL             = -2.0
TIER_B_TIME_EXIT_MIN  = 15

# 티어C: 급상승 "1위"만, 시장가(대용 공격적 지정가), 트레일링 스톱 — 예수금 10%
TIER_C_BUDGET_PCT        = 0.10
TIER_C_MAX_POS           = 1
TIER_C_INITIAL_SL        = -10.0   # 고점 형성 전(진입가 대비) 안전판 손절
TIER_C_TRAIL_PCT         = 15.0    # 고점 대비 이만큼 빠지면 트레일링 청산
TIER_C_MARKET_BUFFER_PCT = 5.0     # 시장가 대용: 매수 +5%/매도 -5% 공격적 지정가
EXIT_SELL_BUFFER_PCT     = 3.0     # 초입/티어A/B 전량매도(손절·트레일링 등)도 공격적 지정가로 즉시체결 (체결지연 슬리피지 방지)
EXIT_MAX_RETRIES         = 5       # 손절/익절 청산 주문의 유한 재시도 상한
EXIT_RATE_LIMIT_MAX_RETRIES = 10   # 주문 시도에서 제외할 연속 KIS rate limit 상한
EXIT_RETRY_INTERVAL      = 2.0     # KIS 1초 throttle보다 긴 안전 간격
EXIT_MAX_REPRICE_PCT     = 10.0    # 최초 기준가 대비 매도 지정가 하한
# TIER_C는 시간청산 없음 — 트레일링 하나로만 청산

# 티어별 종목당 상한 예산(안전판, 실제는 이 값과 tier예산비율×매수가능액 중 작은 쪽)
TIER_MAX_BUDGET_PER_TRADE = 500.0
DEEP_CHECK_MAX      = 10           # 한 스캔에서 분봉까지 볼 후보 최대 수
CLOSE_BUFFER_MIN    = 15           # 장 마감 N분 전엔 신규진입 중단 + 전량청산

VERSION = "v34"

# ── Alpaca ──
ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_TRADE_BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_DATA_BASE  = "https://data.alpaca.markets"
ALPACA_HDR = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = os.path.expanduser("~/surge_scalper_us_positions.json")
PENDING_BUYS_FILE = os.path.expanduser("~/surge_scalper_us_pending_buys.json")
DAILY_STATE_FILE = os.path.expanduser("~/surge_scalper_us_daily_state.json")
TRADE_ANALYTICS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "data", "trade_analytics.db")
BLACKLIST  = {}   # {key: 블랙리스트 등록시각} — 매도 후 REENTRY_COOLDOWN_MIN 지나면 자동 해제
REENTRY_COOLDOWN_MIN = 60   # 같은 종목(같은 티어) 재진입 금지 시간(분)
PENDING_BUYS: dict = {}
_PRICE_LOG_STATE: dict = {}
_EXIT_IN_PROGRESS: set = set()
_EXIT_ALERT_STATE: dict = {}
_EXIT_OPEN_ORDERS: dict = {}


def is_blacklisted(key: str) -> bool:
    """쿨다운이 지났으면 자동으로 블랙리스트에서 빼주고 False 반환."""
    ts = BLACKLIST.get(key)
    if ts is None:
        return False
    if (datetime.now() - ts).total_seconds() / 60 >= REENTRY_COOLDOWN_MIN:
        del BLACKLIST[key]
        return False
    return True


# ── 일일 리스크 게이트 상태 (v31 이식) — 개장 시 main()에서 리셋 ──
daily_realized_pnl_usd = 0.0
daily_start_capital = None   # None이면 계산 불가 상태 → 신규매수 차단(buys_allowed 참고)
# [fix] 위 두 값은 메모리에만 있으면 장중 프로세스 재시작(크래시→systemd 재기동) 시
#       "장 개장" 이벤트로 오인되어 0으로 리셋되고, 그 순간 당일 손실/수익 한도 게이트가
#       무력화된다. 그래서 날짜와 함께 파일에 남겨 재시작 시 "같은 거래일이면 복원"할 수 있게 한다.
daily_state_date = None          # 위 값들이 어느 거래일(UTC) 것인지
daily_session_start_iso = None   # 그 거래일의 세션 시작 시각(ISO) — 일지 집계 기준


def load_daily_state() -> dict:
    if os.path.exists(DAILY_STATE_FILE):
        try:
            with open(DAILY_STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            log.warning("일일 상태 파일 파싱 실패 — 새로 시작")
    return {}


def save_daily_state():
    data = {
        "date": daily_state_date,
        "daily_realized_pnl_usd": daily_realized_pnl_usd,
        "daily_start_capital": daily_start_capital,
        "session_start": daily_session_start_iso,
    }
    try:
        with open(DAILY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("일일 상태 저장 실패: %s", e)


def add_realized_pnl(qty_sold: int, entry_price: float, exit_price: float):
    """매도(전체/부분) 성공 시 실현손익을 일일 누계에 더한다."""
    global daily_realized_pnl_usd
    daily_realized_pnl_usd += (exit_price - entry_price) * qty_sold
    save_daily_state()


def daily_pnl_pct() -> float:
    if not daily_start_capital or daily_start_capital <= 0:
        return 0.0
    return daily_realized_pnl_usd / daily_start_capital * 100


def buys_allowed():
    """신규매수 허용 여부 + 사유. 보유분 청산(monitor_and_exit)은 이 게이트와 무관하게 항상 동작."""
    if not daily_start_capital or daily_start_capital <= 0:
        # [fix] 기준자본 계산 실패 시 안전하게 신규매수만 차단(fail-closed).
        #       청산(monitor_and_exit)은 이 함수와 무관하게 계속 동작하므로 보유분 관리엔 영향 없음.
        return False, "기준자본 미확정(안전 차단)"
    d = daily_pnl_pct()
    if d >= DAILY_PROFIT_LOCK_PCT:
        return False, f"수익잠금(당일 {d:+.1f}%)"
    if d <= DAILY_LOSS_LIMIT_PCT:
        return False, f"손실한도(당일 {d:+.1f}%)"
    return True, ""


NO_BARS    = set()   # 분봉 없는 종목(워런트/특수) — 세션 내 재조회 스킵

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("surge_scalper_us.log"), logging.StreamHandler()],
)
log = logging.getLogger("surge_us")
trade_logger = TradeLogger(TRADE_ANALYTICS_DB, bot_name="surge_scalper_us",
                           bot_version=VERSION, logger=log)
atexit.register(trade_logger.close)


def analytics_exit_reason(reason: str) -> str:
    """기존 사용자용 청산 문구를 분석용 안정 enum으로만 매핑한다."""
    text = reason or ""
    if "트레일링" in text:
        return "TRAILING_STOP"
    if "1차익절" in text or text.startswith("익절"):
        return "TAKE_PROFIT_1"
    if "손절" in text:
        return "STOP_LOSS"
    if "시간청산" in text:
        return "SIDEWAYS_EXIT"
    if "장마감" in text:
        return "DAILY_RISK_EXIT"
    if "STALE" in text.upper():
        return "EMERGENCY_STALE"
    return "UNKNOWN"


def notify(msg: str):
    log.info("TG: %s", msg)
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5,
        )
    except Exception as e:
        log.warning("텔레그램 전송 실패: %s", e)


# ─────────────────────────────────────────────
# Alpaca: 개장 여부 / 마감까지 남은 시간
# ─────────────────────────────────────────────
def get_clock():
    """Alpaca clock → (is_open, 마감까지_분). DST/휴장 자동 반영."""
    try:
        r = requests.get(f"{ALPACA_TRADE_BASE}/v2/clock",
                         headers=ALPACA_HDR, timeout=8)
        r.raise_for_status()
        d = r.json()
        is_open = bool(d.get("is_open"))
        nc = d.get("next_close")
        mins_to_close = None
        if is_open and nc:
            close_dt = datetime.fromisoformat(nc.replace("Z", "+00:00"))
            mins_to_close = (close_dt - datetime.now(timezone.utc)).total_seconds() / 60
        return is_open, mins_to_close
    except Exception as e:
        log.warning("clock 조회 실패: %s", e)
        return False, None


# ─────────────────────────────────────────────
# Alpaca: 급등 상위 / 종목정보 / 분봉
# ─────────────────────────────────────────────
def get_movers():
    """상승률 상위 → [{symbol, price, change_pct}, ...]"""
    try:
        r = requests.get(f"{ALPACA_DATA_BASE}/v1beta1/screener/stocks/movers",
                         headers=ALPACA_HDR, params={"top": MOVERS_TOP}, timeout=8)
        r.raise_for_status()
        gainers = r.json().get("gainers", [])
        out = []
        for g in gainers:
            out.append({
                "symbol": g.get("symbol"),
                "price": float(g.get("price", 0)),
                "change_pct": float(g.get("percent_change", 0)),
            })
        return out
    except Exception as e:
        log.warning("movers 조회 실패: %s", e)
        return []


_KIS_EXCH = {"NASDAQ": "NASD", "NYSE": "NYSE", "AMEX": "AMEX"}


def get_kis_exchange(symbol: str):
    """Alpaca 자산 거래소 → KIS 거래소코드. 거래불가/미지원 거래소면 None."""
    try:
        r = requests.get(f"{ALPACA_TRADE_BASE}/v2/assets/{symbol}",
                         headers=ALPACA_HDR, timeout=8)
        r.raise_for_status()
        a = r.json()
        if not a.get("tradable", False):
            return None
        return _KIS_EXCH.get(a.get("exchange"))
    except Exception:
        return None


def get_recent_bars(symbol: str, limit: int = 6):
    """최근 1분봉 → [{t,o,h,l,c,v}, ...] 시간순 오름차순"""
    try:
        r = requests.get(
            f"{ALPACA_DATA_BASE}/v2/stocks/{symbol}/bars",
            headers=ALPACA_HDR,
            params={"timeframe": "1Min", "limit": limit, "feed": "iex"},
            timeout=8,
        )
        r.raise_for_status()
        bars = r.json().get("bars") or []
        return [{"c": float(b["c"]), "v": float(b["v"])} for b in bars]
    except Exception as e:
        log.warning("분봉 조회 실패 %s: %s", symbol, e)
        return []


def get_most_actives(top=MOST_ACTIVES_TOP):
    """거래량 급증 종목 심볼 리스트. 급등 초입은 보통 거래량부터 터진다."""
    try:
        r = requests.get(f"{ALPACA_DATA_BASE}/v1beta1/screener/stocks/most-actives",
                         headers=ALPACA_HDR, params={"by": "volume", "top": top}, timeout=8)
        r.raise_for_status()
        return [x.get("symbol") for x in r.json().get("most_actives", []) if x.get("symbol")]
    except Exception as e:
        log.warning("most-actives 조회 실패: %s", e)
        return []


def get_snapshots(symbols):
    """여러 심볼 스냅샷 일괄 조회 → {sym: {price, change_pct}}"""
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        if not chunk:
            continue
        try:
            r = requests.get(f"{ALPACA_DATA_BASE}/v2/stocks/snapshots",
                             headers=ALPACA_HDR,
                             params={"symbols": ",".join(chunk), "feed": "iex"}, timeout=10)
            r.raise_for_status()
            data = r.json()
            snaps = data.get("snapshots", data)
            for sym, snap in snaps.items():
                if not isinstance(snap, dict):
                    continue
                lt = snap.get("latestTrade") or {}
                pdb = snap.get("prevDailyBar") or {}
                price = float(lt.get("p", 0) or 0)
                prev_close = float(pdb.get("c", 0) or 0)
                if price <= 0 or prev_close <= 0:
                    continue
                out[sym] = {"price": price,
                            "change_pct": (price - prev_close) / prev_close * 100}
        except Exception as e:
            log.warning("snapshots 조회 실패(%d개): %s", len(chunk), e)
    return out


def get_surge_candidates():
    """
    급등 초입 후보 = (거래량 급증 most-actives) ∪ (등락률 상위 gainers)
    → 스냅샷으로 등락률 계산 → 전체 반환(분류는 screen_and_enter에서). 등락률 낮은 순.
    """
    actives = get_most_actives()
    gainers = get_movers()
    gain_map = {g["symbol"]: g for g in gainers if g.get("symbol")}

    need = [s for s in actives if s not in gain_map]
    snaps = get_snapshots(need)

    merged = {}
    for sym, g in gain_map.items():
        merged[sym] = {"symbol": sym, "price": g["price"], "change_pct": g["change_pct"]}
    for sym, sd in snaps.items():
        merged.setdefault(sym, {"symbol": sym, "price": sd["price"], "change_pct": sd["change_pct"]})

    cands = [m for m in merged.values() if classify(m)]
    cands.sort(key=lambda x: x["change_pct"])   # 등락률 낮은 순 = 더 초입
    return cands


# 주문용 거래소코드(4글자) → 시세용 거래소코드(3글자)
_EXCD_MAP = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}


def _age_seconds(ts: datetime, now_utc: datetime = None):
    if not isinstance(ts, datetime):
        return None
    now_utc = now_utc or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max((now_utc - ts.astimezone(timezone.utc)).total_seconds(), 0.0)


def _parse_iso_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _parse_kis_timestamp(output: dict):
    """KIS 현재가 응답의 미국 현지 일자/시간을 timezone-aware datetime으로 변환."""
    date_value = next((output.get(k) for k in ("xymd", "stck_bsop_date", "trd_dt")
                       if output.get(k)), None)
    time_value = next((output.get(k) for k in ("xhms", "stck_cntg_hour", "trd_tm")
                       if output.get(k)), None)
    if not date_value or not time_value:
        return None
    digits = "".join(ch for ch in f"{date_value}{time_value}" if ch.isdigit())
    if len(digits) < 14:
        return None
    try:
        return datetime.strptime(digits[:14], "%Y%m%d%H%M%S").replace(
            tzinfo=ZoneInfo("America/New_York"))
    except ValueError:
        return None


def _price_result(source: str, price: float, ts=None, **extra) -> dict:
    age = _age_seconds(ts)
    status = ("unknown" if age is None else
              "emergency_stale" if age >= PRICE_EMERGENCY_STALE_SEC else
              "stale" if age >= PRICE_STALE_SEC else "fresh")
    return {"source": source, "price": float(price or 0), "timestamp": ts,
            "age_sec": age, "status": status, **extra}


def _kis_price(symbol: str, exchange: str) -> dict:
    """한투 해외주식 현재가와 원천 timestamp. 실패/시간누락도 명시적으로 반환."""
    try:
        excd = _EXCD_MAP.get(exchange, "NAS")
        kis._throttle()
        r = requests.get(
            f"{kis.BASE_URL}/uapi/overseas-price/v1/quotations/price",
            headers=kis._headers("HHDFS00000300"),
            params={"AUTH": "", "EXCD": excd, "SYMB": symbol}, timeout=8)
        r.raise_for_status()
        d = r.json()
        if d.get("rt_cd") == "0":
            output = d.get("output") or {}
            return _price_result("kis", float(output.get("last", 0) or 0),
                                 _parse_kis_timestamp(output))
    except Exception as e:
        log.warning("한투 시세 실패 %s: %s", symbol, e)
    return _price_result("kis", 0.0)


def _alpaca_quote(symbol: str, for_entry: bool = False) -> dict:
    """Alpaca IEX latest quote. 매수는 ask, 감시/매도는 bid를 사용."""
    try:
        r = requests.get(f"{ALPACA_DATA_BASE}/v2/stocks/{symbol}/quotes/latest",
                         headers=ALPACA_HDR, params={"feed": "iex"}, timeout=8)
        r.raise_for_status()
        quote = r.json().get("quote") or {}
        bid = float(quote.get("bp", 0) or 0)
        ask = float(quote.get("ap", 0) or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return _price_result("alpaca_quote", 0.0, invalid_quote=True)
        return _price_result("alpaca_quote", ask if for_entry else bid,
                             _parse_iso_timestamp(quote.get("t")), bid=bid, ask=ask)
    except Exception:
        return _price_result("alpaca_quote", 0.0)


def _alpaca_trade(symbol: str) -> dict:
    """Alpaca IEX latest trade와 원천 timestamp."""
    try:
        r = requests.get(f"{ALPACA_DATA_BASE}/v2/stocks/{symbol}/trades/latest",
                         headers=ALPACA_HDR, params={"feed": "iex"}, timeout=8)
        r.raise_for_status()
        tr = r.json().get("trade") or {}
        price = float(tr.get("p", 0) or 0)
        return _price_result("alpaca_trade", price, _parse_iso_timestamp(tr.get("t")))
    except Exception:
        return _price_result("alpaca_trade", 0.0)


def _log_price_choice(symbol: str, result: dict):
    now = time.monotonic()
    signature = (result.get("source"), result.get("status"))
    previous = _PRICE_LOG_STATE.get(symbol)
    if previous and previous[0] == signature and now - previous[1] < PRICE_WARNING_INTERVAL:
        return
    _PRICE_LOG_STATE[symbol] = (signature, now)
    age = result.get("age_sec")
    age_text = "unknown" if age is None else f"{age:.1f}s"
    level = log.info if result.get("status") == "fresh" else log.warning
    level("시세선택 %s source=%s age=%s status=%s price=%.4f",
          symbol, result.get("source"), age_text, result.get("status"),
          result.get("price", 0.0))


def get_resilient_price(symbol: str, exchange: str = "NASD", for_entry: bool = False) -> dict:
    """KIS → Alpaca quote → trade. 모두 지연이면 가장 최신 가격을 감시용으로 반환."""
    candidates = []
    for fetch in (
        lambda: _kis_price(symbol, exchange),
        lambda: _alpaca_quote(symbol, for_entry=for_entry),
        lambda: _alpaca_trade(symbol),
    ):
        result = fetch()
        if result.get("price", 0) <= 0:
            continue
        candidates.append(result)
        if result.get("status") == "fresh":
            _log_price_choice(symbol, result)
            return result

    known = [r for r in candidates if r.get("age_sec") is not None]
    result = min(known, key=lambda r: r["age_sec"]) if known else (
        candidates[0] if candidates else _price_result("none", 0.0))
    _log_price_choice(symbol, result)
    return result


def get_monitor_price(symbol: str, exchange: str = "NASD"):
    """보유 감시용. stale만 있어도 가장 최신 가격을 반환해 감시를 지속한다."""
    result = get_resilient_price(symbol, exchange, for_entry=False)
    return result["price"], result["age_sec"]


# ─────────────────────────────────────────────
# 해외 매도가능수량 (get_kr_sellable_qty 의 해외판 — 유령 포지션 방지)
# ─────────────────────────────────────────────
def get_overseas_sellable_qty(symbol: str, exchange: str = "NASD") -> int:
    holding = kis.get_overseas_holding(symbol, exchange)
    return int(holding.get("sellable_qty", 0) or 0)


# ─────────────────────────────────────────────
# 포지션 상태 (이 봇이 연 것만 별도 저장)
# ─────────────────────────────────────────────
def load_positions() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.warning("포지션 파일 파싱 실패 — 빈 상태로 시작")
    return {}


def save_positions(pos: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(pos, f, ensure_ascii=False, indent=2)


def load_pending_buys() -> dict:
    if os.path.exists(PENDING_BUYS_FILE):
        try:
            with open(PENDING_BUYS_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            log.warning("미체결 매수 파일 파싱 실패 — 빈 상태로 시작")
    return {}


def save_pending_buys():
    with open(PENDING_BUYS_FILE, "w", encoding="utf-8") as f:
        json.dump(PENDING_BUYS, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
# 일일 매매일지 (장마감 후 텔레그램 요약)
# ─────────────────────────────────────────────
def build_daily_report(since: "datetime") -> str:
    """오늘 세션(since 이후) 로그에서 매도(청산) 기록만 뽑아 요약 리포트 생성."""
    import re
    from collections import defaultdict

    try:
        with open("surge_scalper_us.log", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        return f"📊 일지 생성 실패(로그 읽기 오류): {e}"

    trades = []  # (symbol, tier_tag, reason, pnl)
    for line in lines:
        ts_match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),", line)
        if not ts_match:
            continue
        try:
            ts = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < since:
            continue
        if "매도[" not in line:
            continue
        m = re.search(r"매도\[([^\]]+)\]\s+(\w+)", line)
        if not m:
            continue
        tier_tag, sym = m.group(1), m.group(2)
        pcts = re.findall(r"([+-]?\d+\.?\d*)%", line)
        if not pcts:
            continue
        pnl = float(pcts[-1])   # 트레일링청산처럼 %가 여러개면 마지막(=실현손익)이 정답
        reason_m = re.search(r"—\s*([^\n]+)", line)
        reason = reason_m.group(1).strip() if reason_m else ""
        trades.append((sym, tier_tag, reason, pnl))

    if not trades:
        return f"📊 [surge_scalper_us {VERSION}] 오늘의 매매일지 — 청산된 거래 없음"

    by_tier = defaultdict(list)
    for t in trades:
        by_tier[t[1]].append(t)

    total = len(trades)
    wins = sum(1 for t in trades if t[3] > 0)
    avg = sum(t[3] for t in trades) / total
    best = max(trades, key=lambda t: t[3])
    worst = min(trades, key=lambda t: t[3])

    lines_out = [
        f"📊 [surge_scalper_us {VERSION}] 오늘의 매매일지",
        f"총 청산 {total}건 | 승률 {wins}/{total} ({wins/total*100:.0f}%) | 평균 {avg:+.2f}%",
        "",
    ]
    for tier_tag, tl in by_tier.items():
        w = sum(1 for t in tl if t[3] > 0)
        a = sum(t[3] for t in tl) / len(tl)
        lines_out.append(f"[{tier_tag}] {len(tl)}건 승{w}/{len(tl)} 평균{a:+.2f}%")

    lines_out.append("")
    lines_out.append(f"베스트: {best[0]} {best[3]:+.1f}%")
    lines_out.append(f"워스트: {worst[0]} {worst[3]:+.1f}%")

    return "\n".join(lines_out)


def sweep_orphan_positions(positions: dict):
    """계좌 실잔고 vs 장부 대조 — 장부에 없는데 실제로 보유 중인 종목을
    발견하면(예: 부분체결 버그 등으로 장부에서만 지워진 경우) 자동으로
    다시 감시 대상에 편입시켜 방치되지 않게 한다. 봇 시작 시 1회 실행."""
    try:
        bal = kis.get_overseas_balance()
    except Exception as e:
        log.warning("고아포지션 점검 — 잔고조회 실패: %s", e)
        return

    tracked_symbols = {p.get("symbol", symbol_of_key(k)) for k, p in positions.items()}
    adopted = []
    for h in bal.get("output1", []):
        sym = h.get("ovrs_pdno")
        qty = h.get("ord_psbl_qty") or h.get("ovrs_cblc_qty") or 0
        try:
            qty = int(float(qty))
        except (TypeError, ValueError):
            qty = 0
        if not sym or qty <= 0 or sym in tracked_symbols:
            continue

        # 장부에 없는데 실제로 들고 있는 종목 발견 — 티어B(보수적 설정)로 편입해
        # 기존 monitor_and_exit 루프가 알아서 청산 시도하게 만든다.
        exch = h.get("ovrs_excg_cd") or "NASD"
        try:
            avg_price = float(h.get("pchs_avg_pric") or 0)
        except (TypeError, ValueError):
            avg_price = 0.0
        cur, _ = get_monitor_price(sym, exch)
        entry_price = avg_price if avg_price > 0 else (cur if cur > 0 else 1.0)

        key = f"{sym}#ORPHAN"
        positions[key] = {
            "symbol": sym,
            "entry_price": entry_price,
            "peak_price": entry_price,
            "qty": qty,
            "exchange": exch,
            "mode": "B",   # 보수적 청산 기준(+3%/-2%/15분)으로 최대한 빨리 정리
            "entry_time": datetime.now().isoformat(),
        }
        adopted.append(f"{sym}({qty}주)")

    if adopted:
        save_positions(positions)
        notify("⚠️ 고아 포지션 발견 → 감시 편입: " + ", ".join(adopted))
        log.warning("고아 포지션 편입: %s", adopted)


# ─────────────────────────────────────────────
# 후보 검증
# ─────────────────────────────────────────────
def in_price_band(m: dict) -> bool:
    p = m.get("price", 0.0)
    return MIN_PRICE <= p <= MAX_PRICE


def is_warrant(sym: str) -> bool:
    """워런트/유닛 등 파생 티커 판별 (v31 이식). 분봉 데이터가 아예 없거나
    거래소 매핑이 안 되는 경우가 많아 애초에 후보에서 제외."""
    s = sym.upper()
    if any(suffix in s for suffix in (".WS", ".WT", ".U", ".UN", ".RT", ".R")):
        return True
    if len(s) >= 5 and s.endswith("W") and "." not in s:
        return True
    return False


def classify(m: dict):
    """진입 모드 판정 → 'early' | 'chase' | None"""
    sym = m.get("symbol", "")
    if sym and is_warrant(sym):
        return None
    if not in_price_band(m):
        return None
    c = m.get("change_pct", 0.0)
    if ENTRY_MIN_CHANGE <= c <= ENTRY_MAX_CHANGE:
        return "early"
    if CHASE_ENABLED and CHASE_MIN_CHANGE <= c <= CHASE_MAX_CHANGE:
        return "chase"
    return None


def deep_check(symbol: str) -> bool:
    """초입(early) 전용 검증: 최근 급가속 + 거래량 폭발."""
    if symbol in NO_BARS:
        return False
    bars = get_recent_bars(symbol, limit=25)
    if len(bars) < 6:
        NO_BARS.add(symbol)
        return False
    last5 = bars[-5:]
    closes = [b["c"] for b in last5]

    rising = closes[-1] > closes[0]
    not_fading = not (closes[-3] > closes[-2] > closes[-1])
    if not (rising and not_fading):
        return False

    dollar_vol = sum(b["v"] * b["c"] for b in last5)
    if dollar_vol < MIN_5MIN_DOLLAR_VOL:
        return False

    recent_move = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] > 0 else 0
    recent_vol = sum(b["v"] for b in last5) / len(last5)
    prior = bars[:-5]
    vol_mult = None
    if len(prior) >= 5:
        prior_vol = sum(b["v"] for b in prior) / len(prior)
        vol_mult = recent_vol / prior_vol if prior_vol > 0 else None

    if recent_move < RECENT_MOVE_MIN:
        return False
    if vol_mult is not None and vol_mult < VOL_SURGE_MULT:
        return False
    return True


def _basic_bars(symbol: str, limit: int = 6):
    """공용: 상승지속/페이드아웃 판정에 필요한 최소 분봉. bars 부족하면 None."""
    if symbol in NO_BARS:
        return None
    bars = get_recent_bars(symbol, limit=limit)
    if len(bars) < 6:
        NO_BARS.add(symbol)
        return None
    return bars


def _momentum_1m_ok(closes: list) -> bool:
    """v31 이식: 최근 1분봉(마지막 종가 vs 직전 종가) 상승폭이 최소기준 이상인지."""
    if len(closes) < 2 or closes[-2] <= 0:
        return False
    change_1m = (closes[-1] - closes[-2]) / closes[-2] * 100
    return change_1m >= MOMENTUM_1M_MIN_PCT


def passes_tier_a(symbol: str) -> bool:
    """티어A(+티어C 최소필터로 재사용): 하락 중만 아니면 통과 + 최근 1분봉 모멘텀 확인."""
    bars = _basic_bars(symbol, limit=6)
    if not bars:
        return False
    last5 = bars[-5:]
    closes = [b["c"] for b in last5]
    rising = closes[-1] > closes[0]
    not_fading = not (closes[-3] > closes[-2] > closes[-1])
    if not (rising and not_fading):
        return False
    return _momentum_1m_ok(closes)


def passes_tier_b(symbol: str) -> bool:
    """티어B: 티어A(하락중만 아니면 통과)와 동일 + 유동성 안전판만 유지.
    거래량 배수 조건은 제거(이미 튄 종목은 거래량이 식는 게 정상 패턴이라
    이 조건이 진입 자체를 막아버리는 경우가 많았음 - 2026-07-13 데이터로 확인)."""
    bars = _basic_bars(symbol, limit=6)
    if not bars:
        return False
    last5 = bars[-5:]
    closes = [b["c"] for b in last5]
    rising = closes[-1] > closes[0]
    not_fading = not (closes[-3] > closes[-2] > closes[-1])
    if not (rising and not_fading):
        return False
    dollar_vol = sum(b["v"] * b["c"] for b in last5)
    if dollar_vol < MIN_5MIN_DOLLAR_VOL:
        return False
    return _momentum_1m_ok(closes)


def get_rank1_candidate():
    """오늘 등락률 1위 종목(가격 밴드 최소가만 체크, 상한 없음)."""
    gainers = get_movers()
    if not gainers:
        return None
    ranked = sorted(gainers, key=lambda x: x.get("change_pct", 0), reverse=True)
    top = ranked[0]
    if top.get("price", 0) < MIN_PRICE:
        return None
    return top


# ─────────────────────────────────────────────
# 진입 / 청산 (early / 티어A / 티어B / 티어C)
# ─────────────────────────────────────────────
def tier_of_key(key: str) -> str:
    """포지션 키 'SYM#TIER' → TIER. 접미사 없으면 'early'."""
    return key.split("#", 1)[1] if "#" in key else "early"


def symbol_of_key(key: str) -> str:
    return key.split("#", 1)[0]


def count_tier(positions: dict, tier: str) -> int:
    return sum(1 for k in positions if tier_of_key(k) == tier)


def symbol_held_anywhere(sym: str, positions: dict) -> bool:
    """[fix] 같은 티커를 초입/티어A/B/C가 각자 다른 키(sym, sym#A, sym#B, sym#C)로 동시에
    들고 있으면, get_overseas_holding/get_overseas_sellable_qty는 계좌 기준(티어 구분 없이)
    수량을 반환하기 때문에 어느 티어 몫인지 뒤섞여 장부-실잔고 불일치로 이어질 수 있다.
    그래서 한 종목은 항상 한 티어만 보유하도록 진입 전에 전체 포지션 + 미확정 매수주문을
    함께 확인한다."""
    if any(symbol_of_key(k) == sym for k in positions):
        return True
    return any(p.get("symbol") == sym for p in PENDING_BUYS.values())


def place_aggressive(symbol, qty, price, side, exchange, buffer_pct):
    """시장가 대용: 즉시체결 가능성이 높도록 버퍼를 크게 준 지정가.
    (KIS 해외주식 진짜 시장가 지원 여부가 이 환경에서 미검증이라,
     이미 검증된 지정가 주문 경로에 버퍼만 크게 얹는 방식으로 안전하게 구현)"""
    if side == "buy":
        adj = price * (1 + buffer_pct / 100)
    else:
        adj = price * (1 - buffer_pct / 100)
    result = kis.place_order(symbol, qty, adj, side, session="regular", exchange=exchange)
    result["_submitted_price"] = adj
    return result


def _is_kis_rate_limit(result: dict) -> bool:
    msg = str((result or {}).get("msg1", ""))
    return ((result or {}).get("http_status") == 403 or
            "EGW00201" in msg or "초당" in msg or "거래건수" in msg or
            "rate" in msg.lower())


def _retry_wait_seconds(result: dict = None) -> float:
    """공용 KIS throttle보다 짧게 재시도하지 않고 Retry-After가 있으면 우선한다."""
    throttle = float(getattr(kis, "_MIN_REQUEST_INTERVAL", 0.0) or 0.0)
    retry_after = 0.0
    try:
        retry_after = float((result or {}).get("retry_after", 0.0) or 0.0)
    except (TypeError, ValueError):
        pass
    return max(EXIT_RETRY_INTERVAL, throttle, retry_after)


def _us_tick_size(price: float) -> float:
    """현재 전략 가격대($1+)의 KIS 미국주식 지정가 호가단위."""
    return 0.01 if price >= 1.0 else 0.0001


def _floor_to_tick(price: float, tick: float) -> float:
    if price <= 0 or tick <= 0:
        return 0.0
    decimals = 4 if tick < 0.01 else 2
    return round(math.floor((price + 1e-12) / tick) * tick, decimals)


def _marketable_sell_price(symbol: str, exchange: str, previous_price: float = None,
                           initial_reference: float = None) -> tuple[float, dict]:
    """fresh bid 우선 시장성 지정가. 재주문은 이전가보다 최소 1틱 유리하게 조정."""
    quote = _alpaca_quote(symbol, for_entry=False)
    result = quote if quote.get("status") == "fresh" else get_resilient_price(
        symbol, exchange, for_entry=False)
    if result.get("status") != "fresh" or result.get("price", 0) <= 0:
        return 0.0, result

    reference = float(result["price"])
    tick = _us_tick_size(reference)
    price = _floor_to_tick(reference, tick)
    if previous_price and previous_price > 0:
        price = min(price, _floor_to_tick(previous_price - tick, tick))
    anchor = float(initial_reference or reference)
    floor_price = _floor_to_tick(anchor * (1 - EXIT_MAX_REPRICE_PCT / 100), tick)
    price = max(price, floor_price, tick)
    return price, result


def _get_unfilled_orders(symbol: str, exchange: str) -> dict:
    """KIS 공식 해외주식 미체결내역. 조회 실패는 확인 불가로 보수적으로 처리."""
    try:
        tr_id = "VTTS3018R" if kis.USE_MOCK else "TTTS3018R"
        data = kis._request_with_retry(
            "get",
            f"{kis.BASE_URL}/uapi/overseas-stock/v1/trading/inquire-nccs",
            headers=kis._headers(tr_id),
            params={
                "CANO": kis.CANO,
                "ACNT_PRDT_CD": kis.ACNT_PRDT_CD,
                "OVRS_EXCG_CD": exchange,
                "SORT_SQN": "DS",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
            timeout=10,
        )
        if data.get("rt_cd") != "0":
            return {"ok": False, "orders": [], "error": data.get("msg1", "미체결조회 실패")}
        rows = data.get("output") or data.get("output1") or []
        if isinstance(rows, dict):
            rows = [rows]
        orders = [row for row in rows if (row.get("ovrs_pdno") or row.get("pdno")) == symbol]
        return {"ok": True, "orders": orders, "error": ""}
    except Exception as e:
        return {"ok": False, "orders": [], "error": str(e)}


def _order_is_open(order_no: str, symbol: str, exchange: str) -> tuple[bool, bool, str]:
    inquiry = _get_unfilled_orders(symbol, exchange)
    if not inquiry["ok"]:
        return False, False, inquiry["error"]
    for row in inquiry["orders"]:
        row_no = str(row.get("odno") or row.get("ODNO") or row.get("ord_no") or "")
        if row_no == str(order_no):
            return True, True, ""
    return False, True, ""


def execute_exit_with_retries(symbol: str, book_qty: int, exchange: str,
                              observed_price: float, full_exit: bool,
                              trade_id: str = None) -> dict:
    """실잔고 우선·취소 확인 후 재주문하는 bounded marketable-limit 청산."""
    if symbol in _EXIT_IN_PROGRESS:
        return {"success": False, "sold_qty": 0, "remaining_qty": book_qty,
                "attempts": 0, "last_price": 0.0, "error": "duplicate_exit_blocked"}
    _EXIT_IN_PROGRESS.add(symbol)
    try:
        first = kis.get_overseas_holding(symbol, exchange)
        if not first.get("ok", True):
            return {"success": False, "sold_qty": 0, "remaining_qty": book_qty,
                    "attempts": 0, "last_price": 0.0, "error": "balance_lookup_failed"}
        initial_qty = int(first.get("sellable_qty", first.get("qty", 0)) or 0)
        if initial_qty <= 0:
            _EXIT_OPEN_ORDERS.pop(symbol, None)
            return {"success": True, "sold_qty": 0, "remaining_qty": 0,
                    "attempts": 0, "last_price": 0.0, "error": ""}
        target_qty = initial_qty if full_exit else min(max(int(book_qty), 1), initial_qty)
        sold_qty = 0
        previous_price = None
        outstanding = _EXIT_OPEN_ORDERS.get(symbol)
        last_error = ""
        actual_attempts = 0
        rate_limit_retries = 0
        balance_retries = 0

        while actual_attempts < EXIT_MAX_RETRIES:
            holding = kis.get_overseas_holding(symbol, exchange)
            if not holding.get("ok", True):
                last_error = "balance_lookup_failed"
                balance_retries += 1
                if balance_retries >= EXIT_MAX_RETRIES:
                    break
                time.sleep(_retry_wait_seconds())
                continue
            balance_retries = 0
            sellable = int(holding.get("sellable_qty", holding.get("qty", 0)) or 0)
            sold_qty = max(initial_qty - sellable, 0)
            if sellable <= 0 or sold_qty >= target_qty:
                _EXIT_OPEN_ORDERS.pop(symbol, None)
                return {"success": True, "sold_qty": min(sold_qty, target_qty),
                        "remaining_qty": sellable, "attempts": actual_attempts,
                        "rate_limit_retries": rate_limit_retries,
                        "last_price": previous_price or 0.0, "error": ""}

            if outstanding:
                cancel = kis.cancel_overseas_order(
                    symbol, outstanding["order_no"], outstanding["qty"], exchange=exchange)
                if cancel.get("rt_cd") != "0":
                    last_error = f"cancel_failed:{cancel.get('msg1', '')}"
                    break
                time.sleep(_retry_wait_seconds(cancel))
                is_open, confirmed, error = _order_is_open(
                    outstanding["order_no"], symbol, exchange)
                if not confirmed or is_open:
                    last_error = error or "cancel_not_confirmed"
                    break
                outstanding = None
                _EXIT_OPEN_ORDERS.pop(symbol, None)

            price, price_result = _marketable_sell_price(
                symbol, exchange, previous_price, observed_price)
            if price <= 0:
                last_error = f"quote_{price_result.get('status', 'failed')}"
                break
            remaining_target = min(target_qty - sold_qty, sellable)
            order = kis.place_order(symbol, remaining_target, price, "sell",
                                    session="regular", exchange=exchange)
            if order.get("rt_cd") != "0" and _is_kis_rate_limit(order):
                rate_limit_retries += 1
                last_error = str(order.get("msg1", "rate_limit"))
                trade_logger.log_event(
                    trade_id, "EXIT_RATE_LIMITED", symbol=symbol,
                    attempt_no=actual_attempts, rate_limit_count=rate_limit_retries,
                    requested_price=price, requested_qty=remaining_target,
                    remaining_qty=sellable, message=last_error)
                log.warning("KIS rate limit %s: rate_limit=%d/%d actual_attempts=%d/%d",
                            symbol, rate_limit_retries, EXIT_RATE_LIMIT_MAX_RETRIES,
                            actual_attempts, EXIT_MAX_RETRIES)
                if rate_limit_retries > EXIT_RATE_LIMIT_MAX_RETRIES:
                    last_error = "rate_limit_retries_exhausted"
                    break
                time.sleep(_retry_wait_seconds(order))
                continue
            actual_attempts += 1
            if order.get("rt_cd") != "0":
                last_error = str(order.get("msg1", "order_failed"))
                trade_logger.log_event(
                    trade_id, "EXIT_ORDER_REJECTED", symbol=symbol,
                    attempt_no=actual_attempts, rate_limit_count=rate_limit_retries,
                    requested_price=price, requested_qty=remaining_target,
                    remaining_qty=sellable, message=last_error)
                log.warning("청산 주문 실패 %s: actual_attempts=%d/%d rate_limit=%d/%d 오류=%s",
                            symbol, actual_attempts, EXIT_MAX_RETRIES,
                            rate_limit_retries, EXIT_RATE_LIMIT_MAX_RETRIES, last_error)
                time.sleep(_retry_wait_seconds(order))
                continue
            order_no = str((order.get("output") or {}).get("ODNO") or
                           (order.get("output") or {}).get("odno") or "")
            if not order_no:
                last_error = "accepted_without_order_number"
                break
            previous_price = price
            outstanding = {"order_no": order_no, "qty": remaining_target}
            _EXIT_OPEN_ORDERS[symbol] = outstanding
            trade_logger.mark_exit_order(
                trade_id, symbol, price, remaining_target, actual_attempts,
                rate_limit_retries, remaining_qty=sellable)
            time.sleep(_retry_wait_seconds(order))

        final = kis.get_overseas_holding(symbol, exchange)
        if final.get("ok", True):
            remaining = int(final.get("sellable_qty", final.get("qty", 0)) or 0)
            sold_qty = max(initial_qty - remaining, 0)
        else:
            remaining = max(initial_qty - sold_qty, 0)
        return {"success": remaining <= 0 or sold_qty >= target_qty,
                "sold_qty": min(sold_qty, target_qty), "remaining_qty": remaining,
                "attempts": actual_attempts, "rate_limit_retries": rate_limit_retries,
                "last_price": previous_price or 0.0,
                "error": last_error or "max_retries_exhausted"}
    finally:
        _EXIT_IN_PROGRESS.discard(symbol)


def _notify_exit_failure(symbol: str, result: dict):
    signature = (result.get("remaining_qty"), result.get("error"), result.get("last_price"))
    now = time.monotonic()
    previous = _EXIT_ALERT_STATE.get(symbol)
    if previous and previous[0] == signature and now - previous[1] < PRICE_WARNING_INTERVAL:
        return
    _EXIT_ALERT_STATE[symbol] = (signature, now)
    notify(
        f"🚨 긴급: {symbol} 청산 미완료\n"
        f"남은수량={result.get('remaining_qty')}주 | 마지막가격=${result.get('last_price', 0):.4f}\n"
        f"실제시도={result.get('attempts')}/{EXIT_MAX_RETRIES} | "
        f"rate limit={result.get('rate_limit_retries', 0)}/{EXIT_RATE_LIMIT_MAX_RETRIES} | "
        f"오류={result.get('error')}\n"
        "장부를 유지하고 다음 감시 루프에서 재평가합니다."
    )


def _scan_buyable_amount(cands: list) -> float:
    """한 스캔에서 공유할 총 매수가능금액을 한 번만 조회한다."""
    for probe in cands:
        sym, price = probe.get("symbol"), probe.get("price", 0)
        if not sym or price <= 0:
            continue
        exch = get_kis_exchange(sym)
        if not exch:
            continue
        try:
            return max(float(kis.get_buyable_amount(sym, price, exchange=exch) or 0), 0.0)
        except Exception as e:
            # [fix] 첫 후보 조회 실패로 스캔 전체를 포기하지 않고 다음 후보로 재시도
            #       (매수가능금액은 계좌 기준이라 어떤 종목으로 조회해도 값은 동일함).
            log.warning("매수가능금액 조회 실패(%s) — 다음 후보로 재시도: %s", sym, e)
            continue
    return 0.0


def _buy_fill_from_holding(pending: dict, holding: dict) -> tuple[int, float]:
    """주문 전 기준 잔고와 현재 잔고의 차이로 이 주문의 누적 체결분을 계산."""
    if not holding.get("ok", True):
        return 0, 0.0
    requested_qty = int(pending.get("requested_qty", 0) or 0)
    before_qty = int(pending.get("before_qty", 0) or 0)
    before_amount = float(pending.get("before_purchase_amount", 0) or 0)
    current_qty = int(holding.get("qty", 0) or 0)
    filled_qty = min(max(current_qty - before_qty, 0), requested_qty)
    current_amount = float(holding.get("purchase_amount", 0) or 0)
    filled_cost = max(current_amount - before_amount, 0.0)
    if filled_qty > 0 and filled_cost <= 0:
        filled_cost = filled_qty * float(pending.get("order_price", 0) or 0)
    return filled_qty, filled_cost


def _filled_buy(pending: dict) -> tuple[int, float]:
    """주문 후 실제 잔고를 반복 조회해 현재까지의 누적 체결분을 확인."""
    best_qty, best_cost = 0, 0.0
    for attempt in range(BUY_FILL_CHECK_RETRIES):
        if attempt:
            time.sleep(BUY_FILL_CHECK_INTERVAL)
        holding = kis.get_overseas_holding(pending["symbol"], pending["exchange"])
        if not holding.get("ok", True):
            continue
        filled_qty, filled_cost = _buy_fill_from_holding(pending, holding)
        if filled_qty >= best_qty:
            best_qty, best_cost = filled_qty, filled_cost
        if filled_qty >= int(pending["requested_qty"]):
            break
    return best_qty, best_cost


def _apply_pending_fill(positions: dict, pending_id: str, filled_qty: int, filled_cost: float):
    """pending 주문의 누적 체결분을 실제 포지션 장부에 생성/갱신."""
    pending = PENDING_BUYS[pending_id]
    recorded_qty = int(pending.get("recorded_qty", 0) or 0)
    if filled_qty <= recorded_qty:
        return 0

    key = pending["position_key"]
    entry_price = filled_cost / filled_qty if filled_cost > 0 else float(pending["order_price"])
    pos = positions.get(key)
    if pos:
        pos["qty"] = filled_qty
        pos["entry_price"] = entry_price
        pos["peak_price"] = max(pos.get("peak_price", entry_price), entry_price)
        pos.setdefault("trade_id", pending.get("trade_id"))
    else:
        positions[key] = {
            "symbol": pending["symbol"],
            "entry_price": entry_price,
            "peak_price": entry_price,
            "qty": filled_qty,
            "exchange": pending["exchange"],
            "mode": pending["tier"],
            "entry_time": pending["entry_time"],
            "trade_id": pending.get("trade_id"),
        }
    pending["recorded_qty"] = filled_qty
    pending["recorded_cost"] = filled_cost
    save_positions(positions)
    save_pending_buys()
    trade_logger.confirm_entry(pending.get("trade_id"), pending["symbol"], entry_price,
                               filled_qty, pending.get("order_price"))
    return filled_qty - recorded_qty


def _cancel_pending_remainder(pending: dict, filled_qty: int) -> bool:
    """확인 시간 내 체결되지 않은 잔량을 취소. 성공한 경우 True."""
    remaining = max(int(pending["requested_qty"]) - filled_qty, 0)
    order_no = pending.get("order_no")
    if remaining <= 0:
        return True
    if not order_no:
        log.warning("원주문번호 없음 — %s 잔량 %d주 취소 불가", pending["symbol"], remaining)
        return False
    result = kis.cancel_overseas_order(
        pending["symbol"], order_no, remaining, exchange=pending["exchange"])
    return result.get("rt_cd") == "0"


def reconcile_pending_buys(positions: dict):
    """이전 조회 이후 늦게 체결된 매수분을 장부에 반영하고 중복 주문을 차단."""
    today = datetime.now(timezone.utc).date().isoformat()
    for pending_id in list(PENDING_BUYS):
        pending = PENDING_BUYS[pending_id]
        holding = kis.get_overseas_holding(pending["symbol"], pending["exchange"])
        if not holding.get("ok", True):
            log.warning("pending 잔고조회 실패 %s — 주문 상태 유지", pending["symbol"])
            continue
        filled_qty, filled_cost = _buy_fill_from_holding(pending, holding)
        added_qty = _apply_pending_fill(positions, pending_id, filled_qty, filled_cost)
        if added_qty > 0:
            notify(f"🟢 지연체결[{pending['tag']}] {pending['symbol']} +{added_qty}주 "
                   f"(누적 {filled_qty}/{pending['requested_qty']}주)")

        if filled_qty >= int(pending["requested_qty"]):
            del PENDING_BUYS[pending_id]
            save_pending_buys()
            continue

        if pending.get("cancel_confirmed"):
            log.warning("취소 후 최종체결 확정 %s 체결=%d/%s주", pending["symbol"], filled_qty,
                        pending["requested_qty"])
            del PENDING_BUYS[pending_id]
            save_pending_buys()
            continue

        if _cancel_pending_remainder(pending, filled_qty):
            time.sleep(BUY_FILL_CHECK_INTERVAL)
            holding = kis.get_overseas_holding(pending["symbol"], pending["exchange"])
            if not holding.get("ok", True):
                pending["cancel_confirmed"] = True
                save_pending_buys()
                log.warning("잔량 취소 후 잔고조회 실패 %s — 다음 스캔에서 최종 확인",
                            pending["symbol"])
                continue
            filled_qty, filled_cost = _buy_fill_from_holding(pending, holding)
            _apply_pending_fill(positions, pending_id, filled_qty, filled_cost)
            log.warning("매수 잔량 취소 확정 %s 체결=%d/%s주", pending["symbol"], filled_qty,
                        pending["requested_qty"])
            del PENDING_BUYS[pending_id]
            save_pending_buys()
            continue

        if pending.get("order_date") != today:
            if filled_qty > 0:
                log.warning("전일 부분체결 확정 %s %d/%s주", pending["symbol"], filled_qty,
                            pending["requested_qty"])
            del PENDING_BUYS[pending_id]
            save_pending_buys()


def _try_buy(positions, sym, price, exch, tier, tag, budget, order_fn, change_pct=None,
             entry_rank=None):
    """주문 접수 후 실제 체결분만 장부에 반영. 반환값은 실제 체결금액."""
    if PENDING_BUYS:
        log.warning("미확정 매수주문 %d건 존재 — 신규주문 차단", len(PENDING_BUYS))
        return 0.0
    price_result = get_resilient_price(sym, exch, for_entry=True)
    if price_result.get("status") != "fresh" or price_result.get("price", 0) <= 0:
        log.warning("신규매수 차단 %s[%s] — 신뢰 가능한 최신 시세 없음(source=%s status=%s)",
                    sym, tier, price_result.get("source"), price_result.get("status"))
        return 0.0
    price = price_result["price"]
    qty = int(budget // price)
    if qty < 1:
        log.info("예산 부족 스킵: %s[%s] budget=%.1f price=%.2f", sym, tier, budget, price)
        return 0.0

    trade_id = trade_logger.create_trade(
        sym, exch, tag, requested_price=price, requested_qty=qty,
        entry_rank=entry_rank, quote=price_result,
        metrics={"day_change_pct": change_pct})

    before = kis.get_overseas_holding(sym, exch)
    if not before.get("ok", True):
        log.warning("매수 전 기준잔고 조회 실패 %s[%s] — 주문 중단", sym, tier)
        trade_logger.mark_entry_failed(trade_id, sym, "balance_lookup_failed")
        return 0.0
    res = order_fn(sym, qty, price, exch)
    if res.get("rt_cd") != "0":
        log.warning("주문실패 %s[%s] rt_cd=%s msg=%s", sym, tier, res.get("rt_cd"), res.get("msg1"))
        trade_logger.mark_entry_failed(trade_id, sym, "order_rejected")
        return 0.0

    output = res.get("output") or {}
    order_no = str(output.get("ODNO") or output.get("odno") or "")
    pending_id = order_no or f"{sym}-{tier}-{time.time_ns()}"
    key = f"{sym}#{tier}" if tier != "early" else sym
    submitted_price = float(res.get("_submitted_price", price) or price)
    trade_logger.mark_entry_order(trade_id, sym, submitted_price, qty)
    PENDING_BUYS[pending_id] = {
        "order_no": order_no,
        "symbol": sym,
        "exchange": exch,
        "tier": tier,
        "tag": tag,
        "position_key": key,
        "requested_qty": qty,
        "order_price": submitted_price,
        "trade_id": trade_id,
        "before_qty": int(before.get("qty", 0) or 0),
        "before_purchase_amount": float(before.get("purchase_amount", 0) or 0),
        "recorded_qty": 0,
        "recorded_cost": 0.0,
        "entry_time": datetime.now().isoformat(),
        "order_date": datetime.now(timezone.utc).date().isoformat(),
    }
    save_pending_buys()

    filled_qty, filled_cost = _filled_buy(PENDING_BUYS[pending_id])
    cancel_balance_ok = False
    if filled_qty < qty and _cancel_pending_remainder(PENDING_BUYS[pending_id], filled_qty):
        time.sleep(BUY_FILL_CHECK_INTERVAL)
        holding = kis.get_overseas_holding(sym, exch)
        PENDING_BUYS[pending_id]["cancel_confirmed"] = True
        save_pending_buys()
        cancel_balance_ok = holding.get("ok", True)
        if cancel_balance_ok:
            filled_qty, filled_cost = _buy_fill_from_holding(PENDING_BUYS[pending_id], holding)

    if filled_qty <= 0:
        if PENDING_BUYS[pending_id].get("cancel_confirmed") and cancel_balance_ok:
            del PENDING_BUYS[pending_id]
            save_pending_buys()
            log.warning("매수 미체결 취소완료 %s[%s] 요청=%d주 — 장부 미생성", sym, tier, qty)
            trade_logger.mark_entry_failed(trade_id, sym, "unfilled_cancelled")
        else:
            log.warning("매수 체결대기 %s[%s] 요청=%d주 — 장부 미생성, 후속주문 차단", sym, tier, qty)
        return 0.0

    _apply_pending_fill(positions, pending_id, filled_qty, filled_cost)
    entry_price = positions[key]["entry_price"]
    pct_str = f" ({change_pct:+.1f}%)" if change_pct is not None else ""
    fill_note = f"부분체결 요청{qty}주→" if filled_qty < qty else ""
    notify(f"🟢 매수[{tag}] {sym} {fill_note}{filled_qty}주 @${entry_price:.2f} [{exch}]{pct_str}")
    if filled_qty >= qty or (
            PENDING_BUYS[pending_id].get("cancel_confirmed") and cancel_balance_ok):
        del PENDING_BUYS[pending_id]
        save_pending_buys()
    return filled_cost


def screen_and_enter(positions: dict):
    reconcile_pending_buys(positions)
    if PENDING_BUYS:
        log.warning("미확정 매수주문 %d건 감시 중 — 이번 스캔 신규매수 중단", len(PENDING_BUYS))
        return

    ok, lock_reason = buys_allowed()
    if not ok:
        log.info("신규매수 중단 — %s (보유분 관리는 계속 진행)", lock_reason)
        return

    cands = get_surge_candidates()
    if not cands:
        log.info("스캔 — 후보 0건")
        return

    early = [m for m in cands if classify(m) == "early"]
    chase = [m for m in cands if classify(m) == "chase"] if CHASE_ENABLED else []
    candidate_rank = {m.get("symbol"): rank for rank, m in enumerate(cands, 1)}
    scan_total_budget = _scan_buyable_amount(cands)
    if scan_total_budget <= 0:
        log.warning("스캔 총예산 0 — 신규매수 중단")
        return
    remaining_budget = scan_total_budget
    log.info("v34 스캔 예산: 총 $%.2f / 남은 $%.2f", scan_total_budget, remaining_budget)

    n_buy = 0
    rejects = []
    passed = []

    # ── 초입(early): 기존 그대로, 별도 슬롯(symbol 단독 키) ──
    early_open = sum(1 for k in positions if tier_of_key(k) == "early")
    for m in early:
        if early_open >= MAX_POSITIONS:
            break
        sym = m.get("symbol")
        if not sym or symbol_held_anywhere(sym, positions) or is_blacklisted(sym):
            continue
        exch = get_kis_exchange(sym)
        if not exch:
            rejects.append(f"{sym}(거래소)")
            continue
        if not deep_check(sym):
            rejects.append(f"{sym}(early·모멘텀)")
            continue
        passed.append(f"{sym}(early)")
        price = m["price"]
        budget = min(BUDGET_PER_POSITION, remaining_budget)
        filled_cost = _try_buy(
            positions, sym, price, exch, "early", "초입", budget,
            lambda s, q, p, e: kis.place_order(s, q, p, "buy", session="regular", exchange=e),
            change_pct=m.get("change_pct"), entry_rank=candidate_rank.get(sym))
        if filled_cost > 0:
            remaining_budget = max(remaining_budget - filled_cost, 0.0)
            early_open += 1
            n_buy += 1
        if remaining_budget < price:
            break

    # ── 티어A: 하락중만 아니면 매수, 예수금 30% ──
    a_open = count_tier(positions, "A")
    for m in chase:
        if a_open >= TIER_A_MAX_POS:
            break
        sym = m.get("symbol")
        key = f"{sym}#A"
        if not sym or symbol_held_anywhere(sym, positions) or is_blacklisted(key):
            continue
        exch = get_kis_exchange(sym)
        if not exch:
            continue
        if not passes_tier_a(sym):
            continue
        price = m["price"]
        budget = min(scan_total_budget * TIER_A_BUDGET_PCT,
                     TIER_MAX_BUDGET_PER_TRADE, remaining_budget)
        filled_cost = _try_buy(
            positions, sym, price, exch, "A", "추격A", budget,
            lambda s, q, p, e: kis.place_order(s, q, p, "buy", session="regular", exchange=e),
            change_pct=m.get("change_pct"), entry_rank=candidate_rank.get(sym))
        if filled_cost > 0:
            remaining_budget = max(remaining_budget - filled_cost, 0.0)
            a_open += 1
            n_buy += 1
            break  # 한 번에 한 종목만 (슬롯 1)

    # ── 티어B: 기존 추격 로직(거래량 완화), 예수금 60% ──
    b_open = count_tier(positions, "B")
    for m in chase:
        if b_open >= TIER_B_MAX_POS:
            break
        sym = m.get("symbol")
        key = f"{sym}#B"
        if not sym or symbol_held_anywhere(sym, positions) or is_blacklisted(key):
            continue
        exch = get_kis_exchange(sym)
        if not exch:
            continue
        if not passes_tier_b(sym):
            continue
        price = m["price"]
        budget = min(scan_total_budget * TIER_B_BUDGET_PCT,
                     TIER_MAX_BUDGET_PER_TRADE, remaining_budget)
        filled_cost = _try_buy(
            positions, sym, price, exch, "B", "추격B", budget,
            lambda s, q, p, e: kis.place_order(s, q, p, "buy", session="regular", exchange=e),
            change_pct=m.get("change_pct"), entry_rank=candidate_rank.get(sym))
        if filled_cost > 0:
            remaining_budget = max(remaining_budget - filled_cost, 0.0)
            b_open += 1
            n_buy += 1

    # ── 티어C: 오늘 등락률 1위만, 시장가 대용, 예수금 10% ──
    if count_tier(positions, "C") < TIER_C_MAX_POS:
        top = get_rank1_candidate()
        if top:
            sym = top["symbol"]
            key = f"{sym}#C"
            if not symbol_held_anywhere(sym, positions) and not is_blacklisted(key):
                exch = get_kis_exchange(sym)
                if exch and passes_tier_a(sym):   # 최소필터: 하락중만 아니면
                    price = top["price"]
                    budget = min(scan_total_budget * TIER_C_BUDGET_PCT,
                                 TIER_MAX_BUDGET_PER_TRADE, remaining_budget)
                    filled_cost = _try_buy(
                        positions, sym, price, exch, "C", "추격C(1위)", budget,
                        lambda s, q, p, e: place_aggressive(
                            s, q, p, "buy", e, TIER_C_MARKET_BUFFER_PCT),
                        change_pct=top.get("change_pct"), entry_rank=1)
                    if filled_cost > 0:
                        remaining_budget = max(remaining_budget - filled_cost, 0.0)
                        n_buy += 1

    msg = (f"스캔완료 후보={len(cands)} 초입={len(early)} 추격={len(chase)} "
           f"통과={len(passed)} 매수={n_buy} 보유={len(positions)} "
           f"예산=${scan_total_budget:.2f} 남음=${remaining_budget:.2f}")
    if passed:
        msg += " | 통과: " + ", ".join(passed[:8])
    if rejects:
        msg += " | 탈락: " + ", ".join(rejects[:5])
    log.info(msg)


def monitor_and_exit(positions: dict, force_all: bool = False):
    now = datetime.now()
    pending_position_keys = {p.get("position_key") for p in PENDING_BUYS.values()}
    for key in list(positions.keys()):
        if key in pending_position_keys:
            log.info("감시 보류 %s — 매수 잔량 체결 여부 확인 중", key)
            continue
        p = positions[key]
        sym = p.get("symbol", symbol_of_key(key))
        tier = p.get("mode", tier_of_key(key))

        cur, age = get_monitor_price(sym, p.get("exchange", "NASD"))
        if cur <= 0:
            log.warning("감시 %s[%s]: 가격조회 실패(0) — 판단 불가", sym, tier)
            continue

        trade_id = p.get("trade_id")
        trade_logger.track_position(trade_id, p.get("entry_price"), p.get("entry_time"),
                                    p.get("peak_price"), p.get("lowest_price"))
        trade_logger.update_extremes_in_memory(trade_id, cur)

        # stale 가격만 남아도 보유분 감시는 멈추지 않는다. get_resilient_price가 사용 가능한
        # 세 소스 중 timestamp가 가장 최신인 값을 골랐으며, 상태/소스 경고는 중복 제한된다.
        stale = age is not None and age > PRICE_STALE_SEC

        # 초입/티어A/티어C는 고점을 계속 추적 (트레일링 스톱의 기준)
        if tier in ("early", "A", "C"):
            p["peak_price"] = max(p.get("peak_price", p["entry_price"]), cur)
            save_positions(positions)

        pnl = (cur - p["entry_price"]) / p["entry_price"] * 100
        held = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
        log.info("감시 %s[%s] pnl=%+.1f%% price=%.2f age=%ss%s",
                 sym, tier, pnl, cur, int(age) if age is not None else -1,
                 " STALE(강제청산)" if stale else "")

        reason = None
        exit_mode = "full"   # "full"=전량 청산, "half"=절반만 익절(포지션 유지)

        if force_all:
            reason = f"장마감 청산 ({pnl:+.1f}%)"
        elif tier == "C":
            peak = p.get("peak_price", p["entry_price"])
            if peak <= p["entry_price"] and pnl <= TIER_C_INITIAL_SL:
                reason = f"손절(초기) {pnl:.1f}%"
            else:
                dd = (cur - peak) / peak * 100 if peak > 0 else 0
                if dd <= -TIER_C_TRAIL_PCT:
                    reason = f"트레일링청산 (고점대비{dd:.1f}%, 손익{pnl:+.1f}%)"
            # 티어C는 시간청산 없음
        elif tier in ("early", "A"):
            # v31(Railway 봇) 이식: 손절 → 트레일링(고점추적) → 부분익절(절반) 순서로 체크
            if tier == "early":
                sl, trail_activate, trail_gap, partial_pct = (
                    EARLY_STOP_LOSS_V2, EARLY_TRAIL_ACTIVATE, EARLY_TRAIL_GAP, EARLY_PARTIAL_PCT)
            else:  # A
                sl, trail_activate, trail_gap, partial_pct = (
                    TIER_A_SL, TIER_A_TRAIL_ACTIVATE, TIER_A_TRAIL_GAP, TIER_A_PARTIAL_PCT)

            peak_price = p.get("peak_price", p["entry_price"])
            peak_pct = ((peak_price - p["entry_price"]) / p["entry_price"] * 100
                        if p["entry_price"] > 0 else 0.0)

            if pnl <= sl:
                reason = f"손절 {pnl:.1f}%"
            elif peak_pct >= trail_activate and pnl <= (peak_pct - trail_gap):
                reason = f"트레일링청산 (고점{peak_pct:+.1f}%, 손익{pnl:+.1f}%)"
            elif pnl >= partial_pct and not p.get("partial_done"):
                reason = f"{partial_pct:.0f}% 1차익절(절반)"
                exit_mode = "half"
        else:  # 티어B
            if pnl >= TIER_B_TP:
                reason = f"익절 +{pnl:.1f}%"
            elif pnl <= TIER_B_SL:
                reason = f"손절 {pnl:.1f}%"
            elif held >= TIER_B_TIME_EXIT_MIN:
                reason = f"시간청산 {held:.0f}분 ({pnl:+.1f}%)"

        if not reason:
            if stale:
                trade_logger.record_stale_quote(
                    trade_id, sym, age, min_interval_sec=PRICE_WARNING_INTERVAL)
            trade_logger.flush_extremes_if_due(trade_id)
            continue

        intended_qty = max(1, p["qty"] // 2) if exit_mode == "half" else p["qty"]
        emoji = "🔴" if pnl < 0 else "💰"
        tag = {"early": "초입", "A": "추격A", "B": "추격B", "C": "추격C(1위)"}.get(tier, tier)
        exit_reason = analytics_exit_reason(reason)
        exit_signal_time = datetime.now(timezone.utc)
        result = execute_exit_with_retries(
            sym, intended_qty, p.get("exchange", "NASD"), cur, exit_mode == "full",
            trade_id=trade_id)
        trade_logger.mark_exit_signal(
            trade_id, sym, exit_reason, cur, intended_qty,
            remaining_qty=p.get("qty"), payload={"display_reason": reason},
            signal_time=exit_signal_time)
        if stale:
            trade_logger.record_stale_quote(
                trade_id, sym, age, min_interval_sec=PRICE_WARNING_INTERVAL)
        sold_qty = int(result.get("sold_qty", 0) or 0)
        remaining = int(result.get("remaining_qty", p["qty"]) or 0)
        if sold_qty > 0:
            accounting_price = float(result.get("last_price") or cur)
            add_realized_pnl(sold_qty, p["entry_price"], accounting_price)

        if result.get("success") and exit_mode == "half":
            p["qty"] = remaining
            p["partial_done"] = True
            save_positions(positions)
            trade_logger.finalize_trade(
                trade_id, sym, exit_reason, result.get("last_price") or cur,
                sold_qty, remaining, result.get("attempts", 0),
                result.get("rate_limit_retries", 0), p["entry_price"], False,
                "submitted_limit_price", kis_fill_verified=False)
            notify(f"{emoji} 매도[{tag}] {sym} {sold_qty}주 {reason} @${cur:.2f} ({pnl:+.1f}%) "
                   f"(잔여 {p['qty']}주 계속 보유·트레일링 관리)")
            continue

        if result.get("success") and exit_mode == "full" and remaining <= 0:
            trade_logger.finalize_trade(
                trade_id, sym, exit_reason, result.get("last_price") or cur,
                sold_qty, remaining, result.get("attempts", 0),
                result.get("rate_limit_retries", 0), p["entry_price"], True,
                "submitted_limit_price", kis_fill_verified=False)
            if sold_qty > 0:
                notify(f"{emoji} 매도[{tag}] {sym} {sold_qty}주 @${cur:.2f} — {reason}")
            else:
                notify(f"🧹 유령 포지션 정리[{tag}] {sym} — 실잔고 0 확인")
            BLACKLIST[key] = datetime.now()
            del positions[key]
            save_positions(positions)
            continue

        if remaining > 0:
            p["qty"] = remaining
            save_positions(positions)
        trade_logger.flush_extremes_if_due(trade_id, force=True)
        log.warning("청산 미완료 %s[%s]: 체결=%d 잔여=%d 실제시도=%d rate_limit=%d 오류=%s",
                    sym, tier, sold_qty, remaining, result.get("attempts"),
                    result.get("rate_limit_retries", 0), result.get("error"))
        trade_logger.log_event(
            trade_id, "EXIT_FAILED", symbol=sym, reason=exit_reason,
            attempt_no=result.get("attempts"),
            rate_limit_count=result.get("rate_limit_retries", 0),
            requested_price=result.get("last_price") or cur,
            requested_qty=intended_qty, remaining_qty=remaining,
            message=result.get("error"))
        _notify_exit_failure(sym, result)


# ─────────────────────────────────────────────
# 메인 루프
# ─────────────────────────────────────────────
def main():
    global PENDING_BUYS
    mode = "모의투자" if kis.USE_MOCK else "⚠️ 실전투자"
    notify(f"🚀 미장 급등주 스캘퍼 v34 시작 [{mode}]\n"
           f"초입: {EARLY_PARTIAL_PCT:.0f}%절반익절→고점+{EARLY_TRAIL_ACTIVATE:.0f}%트레일링(-{EARLY_TRAIL_GAP:.0f}%p) / 손절{EARLY_STOP_LOSS_V2:.0f}%\n"
           f"티어A: {TIER_A_PARTIAL_PCT:.0f}%절반익절→고점+{TIER_A_TRAIL_ACTIVATE:.0f}%트레일링(-{TIER_A_TRAIL_GAP:.0f}%p) / 손절{TIER_A_SL:.0f}%\n"
           f"티어B: +{TIER_B_TP:.0f}%/{TIER_B_SL:.0f}%/{TIER_B_TIME_EXIT_MIN}분 | 티어C: 고점-{TIER_C_TRAIL_PCT:.0f}%\n"
           f"일일게이트: +{DAILY_PROFIT_LOCK_PCT:.0f}%수익잠금 / {DAILY_LOSS_LIMIT_PCT:.0f}%손실한도")

    positions = load_positions()
    PENDING_BUYS = load_pending_buys()
    open_analytics = trade_logger.open_trades()
    open_by_symbol = {row.get("symbol"): row for row in open_analytics}
    linked = False
    for key, position in positions.items():
        sym = position.get("symbol", symbol_of_key(key))
        row = open_by_symbol.get(sym)
        if not position.get("trade_id"):
            position["trade_id"] = (row.get("trade_id") if row else
                trade_logger.recover_position(
                    sym, position.get("exchange", "NASD"), position.get("entry_price"),
                    position.get("qty", 0), position.get("entry_time")))
            linked = True
        trade_logger.track_position(
            position.get("trade_id"), position.get("entry_price"), position.get("entry_time"),
            (row or {}).get("highest_price") or position.get("peak_price"),
            (row or {}).get("lowest_price") or position.get("entry_price"))
    if linked:
        save_positions(positions)
    log.info("거래 분석 DB 열린 거래=%d 상태=%s", len(trade_logger.open_trades()),
             trade_logger.health_summary())
    if positions:
        notify(f"♻️ 기존 포지션 {len(positions)}건 복원: " + ", ".join(positions.keys()))
    if PENDING_BUYS:
        notify(f"⏳ 미확정 매수주문 {len(PENDING_BUYS)}건 복원 — 체결 확인 전 신규매수 차단")
        reconcile_pending_buys(positions)
    sweep_orphan_positions(positions)   # 장부에 없는데 실제 보유 중인 종목 정리(부분체결 등 대비)

    last_screen = 0.0
    was_open = False
    force_close_announced = False   # 마감임박 청산완료 알림 — 세션당 1회만
    session_start = datetime.now()  # 오늘 매매일지 집계 시작 시각(개장 시 갱신)
    while True:
        try:
            is_open, mins_to_close = get_clock()

            # 개장 상태 변화 알림 (새 거래일 시작 시 마감알림 플래그/집계시작/일일게이트도 리셋)
            if is_open and not was_open:
                global daily_realized_pnl_usd, daily_start_capital
                global daily_state_date, daily_session_start_iso
                today = datetime.now(timezone.utc).date().isoformat()
                stored = load_daily_state()
                if stored.get("date") == today:
                    # [fix] 오늘 이미 시작된 거래일 안에서의 재진입(장중 프로세스 재시작 등) —
                    #       당일 손익/기준자본/일지 집계시각을 0으로 되돌리지 않고 그대로 복원한다.
                    daily_realized_pnl_usd = float(stored.get("daily_realized_pnl_usd", 0.0) or 0.0)
                    daily_start_capital = stored.get("daily_start_capital")
                    try:
                        session_start = datetime.fromisoformat(stored["session_start"])
                    except (KeyError, ValueError, TypeError):
                        session_start = datetime.now()
                    force_close_announced = False
                    daily_state_date = today
                    daily_session_start_iso = session_start.isoformat()
                    log.info("당일 상태 복원(재시작) — 실현손익 $%.2f, 기준자본 %s",
                             daily_realized_pnl_usd,
                             f"${daily_start_capital:.2f}" if daily_start_capital else "미확정")
                    notify(f"♻️ 재시작 감지 — 당일 실현손익 ${daily_realized_pnl_usd:+.2f} 상태 복원 (리스크 게이트 유지)")
                else:
                    notify("🔔 미국장 개장 — 스캔 시작")
                    force_close_announced = False
                    session_start = datetime.now()
                    daily_realized_pnl_usd = 0.0
                    try:
                        cap = kis.get_buyable_amount("AAPL", 200.0)
                    except Exception as e:
                        cap = 0.0
                        log.warning("일일 기준자본 조회 실패(신규매수 차단): %s", e)
                    daily_start_capital = cap if cap and cap > 0 else None
                    if daily_start_capital:
                        log.info("일일 리스크 게이트 기준자본: $%.2f", daily_start_capital)
                    daily_state_date = today
                    daily_session_start_iso = session_start.isoformat()
                    save_daily_state()
            was_open = is_open

            if not is_open:
                # 휴장/장외: 감시할 포지션만 남았으면 그대로 두고 대기
                time.sleep(60)
                continue

            near_close = mins_to_close is not None and mins_to_close <= CLOSE_BUFFER_MIN

            if near_close and positions:
                monitor_and_exit(positions, force_all=True)
                if not positions and not force_close_announced:
                    # 실제로 전량 청산이 끝난 순간에만, 세션당 딱 한 번 알림 + 일지 전송
                    notify("🏁 마감 임박 — 전량청산 완료")
                    notify(build_daily_report(session_start))
                    force_close_announced = True
                elif positions:
                    # 매도가 안 먹혀서 아직 남아있으면 계속 재시도(알림은 스팸 안 되게 로그로만)
                    log.warning("마감임박 청산 재시도 중 — 남은 포지션: %s", list(positions.keys()))

            if positions:
                monitor_and_exit(positions)

            if not near_close:
                if time.time() - last_screen >= SCREEN_INTERVAL:
                    screen_and_enter(positions)
                    last_screen = time.time()
                    if positions:          # 긴 스캔 직후 즉시 재감시 → 손절 지연 최소화
                        monitor_and_exit(positions)

            time.sleep(MONITOR_INTERVAL)

        except KeyboardInterrupt:
            notify("⛔ 수동 종료")
            break
        except Exception as e:
            log.exception("메인 루프 예외: %s", e)
            time.sleep(30)


if __name__ == "__main__":
    main()
