# -*- coding: utf-8 -*-
"""
surge_scalper_us.py
미장 급등주 초입 포착 → +5% 익절 스캘핑 (KIS 모의투자, 해외주식)

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

⚠️ get_exchange_code 가 "NASD" 고정이라, 이 봇은 NASDAQ 상장 종목만 매매 (거래소코드 안전).
   NYSE/AMEX 급등주까지 잡으려면 place_order 에 거래소 인자 추가가 선행돼야 함.
"""

import os
import json
import time
import logging
from datetime import datetime, timezone

import requests

# KIS 해외주식 체결은 기존 공용 모듈 그대로 재활용
import kis_client as kis

# ─────────────────────────────────────────────
# 전략 파라미터 (매매내역 분석 기반 — 내일 돌려보고 여기부터 튜닝)
# ─────────────────────────────────────────────
MAX_POSITIONS       = 3
BUDGET_PER_POSITION = 200.0        # 종목당 상한 예산(USD) — 매수가능액과 min 처리
ENTRY_MIN_CHANGE    = 5.0          # 진입 최소 등락률(%, 전일종가 대비)
ENTRY_MAX_CHANGE    = 12.0         # 진입 최대 등락률(%)
MIN_PRICE           = 1.0          # $1 미만 제외(호가/LULD 정밀도 이슈 + 승률 애매)
MAX_PRICE           = 20.0         # $20 초과 제외 (과거 $5~20 승률은 낮으니 로그 보고 조정)
MIN_5MIN_DOLLAR_VOL = 100_000.0    # 최근 5분 거래대금 하한(USD) — 유동성/체결
TAKE_PROFIT         = 5.0          # 익절(%)
STOP_LOSS           = -3.0         # 손절(%)  ← 데이터가 지목한 유일한 교정 포인트
TIME_EXIT_MIN       = 30           # 진입 후 N분 내 미도달 시 청산

SCREEN_INTERVAL     = 60           # 스크리닝 주기(초)
MONITOR_INTERVAL    = 10           # 보유 종목 감시 주기(초)
PRICE_STALE_SEC     = 90           # 감시 가격이 이보다 오래되면 STALE 경고
MOVERS_TOP          = 50           # Alpaca 급등 상위 몇 개까지 볼지
MOST_ACTIVES_TOP    = 100          # 거래량 상위 몇 개를 초입 후보 풀로 볼지
RECENT_MOVE_MIN     = 3.0          # 최근 5분 최소 상승폭(%) — "지금 움직이는 중"만 통과
VOL_SURGE_MULT      = 3.0          # 초입: 최근 분당거래량 / 직전평균 배수
# ── 추격 = 3개 티어로 자본 분할 (같은 급등 후보풀, 서로 다른 필터/청산/예산) ──
CHASE_ENABLED         = True
CHASE_MIN_CHANGE      = 20.0       # 추격 후보 최소 등락률(%)
CHASE_MAX_CHANGE      = 120.0      # 추격 후보 최대 등락률(%) — 이 이상은 상투라 제외

# 티어A: 급상승 상위, "하락 중만 아니면" 매수 (거래량 조건 없음) — 예수금 30%
TIER_A_BUDGET_PCT     = 0.30
TIER_A_MAX_POS        = 1
TIER_A_TP             = 20.0
TIER_A_SL             = -3.0
TIER_A_TIME_EXIT_MIN  = 15

# 티어B: 기존 추격 로직 유지, 거래량 조건만 대폭 완화 — 예수금 60%
TIER_B_BUDGET_PCT     = 0.60
TIER_B_MAX_POS        = 2
TIER_B_VOL_SURGE_MULT = 0.3        # 기존 2.0 → 0.3: 거래량이 크게 줄어도 통과
TIER_B_TP             = 3.0
TIER_B_SL             = -2.0
TIER_B_TIME_EXIT_MIN  = 15

# 티어C: 급상승 "1위"만, 시장가(대용 공격적 지정가), 트레일링 스톱 — 예수금 10%
TIER_C_BUDGET_PCT        = 0.10
TIER_C_MAX_POS           = 1
TIER_C_INITIAL_SL        = -10.0   # 고점 형성 전(진입가 대비) 안전판 손절
TIER_C_TRAIL_PCT         = 15.0    # 고점 대비 이만큼 빠지면 트레일링 청산
TIER_C_MARKET_BUFFER_PCT = 5.0     # 시장가 대용: 매수 +5%/매도 -5% 공격적 지정가
# TIER_C는 시간청산 없음 — 트레일링 하나로만 청산

# 티어별 종목당 상한 예산(안전판, 실제는 이 값과 tier예산비율×매수가능액 중 작은 쪽)
TIER_MAX_BUDGET_PER_TRADE = 500.0
DEEP_CHECK_MAX      = 10           # 한 스캔에서 분봉까지 볼 후보 최대 수
CLOSE_BUFFER_MIN    = 15           # 장 마감 N분 전엔 신규진입 중단 + 전량청산

# ── Alpaca ──
ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_TRADE_BASE = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_DATA_BASE  = "https://data.alpaca.markets"
ALPACA_HDR = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = os.path.expanduser("~/surge_scalper_us_positions.json")
BLACKLIST  = set()
NO_BARS    = set()   # 분봉 없는 종목(워런트/특수) — 세션 내 재조회 스킵

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("surge_scalper_us.log"), logging.StreamHandler()],
)
log = logging.getLogger("surge_us")


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


def _kis_price(symbol: str, exchange: str) -> float:
    """한투 해외주식 실시간 현재가. 실패 시 0.0. (전체시장 데이터 + 실시간)"""
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
            return float(d.get("output", {}).get("last", 0) or 0)
    except Exception as e:
        log.warning("한투 시세 실패 %s: %s", symbol, e)
    return 0.0


def _iex_price(symbol: str):
    """IEX 최신체결가 폴백. (price, age_sec)."""
    try:
        r = requests.get(f"{ALPACA_DATA_BASE}/v2/stocks/{symbol}/trades/latest",
                         headers=ALPACA_HDR, params={"feed": "iex"}, timeout=8)
        r.raise_for_status()
        tr = r.json().get("trade") or {}
        price = float(tr.get("p", 0) or 0)
        age = None
        ts = tr.get("t")
        if ts:
            from datetime import timezone
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - t).total_seconds()
        return price, age
    except Exception:
        return 0.0, None


def get_monitor_price(symbol: str, exchange: str = "NASD"):
    """감시용 현재가 + 데이터 나이(초).
    1순위 한투 실시간(age=0), 실패 시 IEX 폴백. (price, age_sec) 반환."""
    p = _kis_price(symbol, exchange)
    if p > 0:
        return p, 0          # 한투는 실시간 → age 0
    return _iex_price(symbol)  # 폴백


# ─────────────────────────────────────────────
# 해외 매도가능수량 (get_kr_sellable_qty 의 해외판 — 유령 포지션 방지)
# ─────────────────────────────────────────────
def get_overseas_sellable_qty(symbol: str) -> int:
    try:
        bal = kis.get_overseas_balance()
    except Exception as e:
        log.warning("해외잔고 조회 예외 %s: %s", symbol, e)
        return 0
    for h in bal.get("output1", []):
        if h.get("ovrs_pdno") == symbol:
            qty = h.get("ord_psbl_qty") or h.get("ovrs_cblc_qty") or 0
            try:
                return int(float(qty))
            except (TypeError, ValueError):
                return 0
    return 0


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


# ─────────────────────────────────────────────
# 후보 검증
# ─────────────────────────────────────────────
def in_price_band(m: dict) -> bool:
    p = m.get("price", 0.0)
    return MIN_PRICE <= p <= MAX_PRICE


def classify(m: dict):
    """진입 모드 판정 → 'early' | 'chase' | None"""
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


def passes_tier_a(symbol: str) -> bool:
    """티어A: 하락 중(연속 페이드아웃)만 아니면 통과 — 거래량/유동성 조건 없음."""
    bars = _basic_bars(symbol, limit=6)
    if not bars:
        return False
    last5 = bars[-5:]
    closes = [b["c"] for b in last5]
    rising = closes[-1] > closes[0]
    not_fading = not (closes[-3] > closes[-2] > closes[-1])
    return rising and not_fading


def passes_tier_b(symbol: str) -> bool:
    """티어B: 기존 추격 로직 유지, 거래량 배수만 대폭 완화(TIER_B_VOL_SURGE_MULT)."""
    bars = _basic_bars(symbol, limit=25)
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

    recent_vol = sum(b["v"] for b in last5) / len(last5)
    prior = bars[:-5]
    if len(prior) >= 5:
        prior_vol = sum(b["v"] for b in prior) / len(prior)
        if prior_vol > 0 and recent_vol < prior_vol * TIER_B_VOL_SURGE_MULT:
            return False
    return True


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


def place_aggressive(symbol, qty, price, side, exchange, buffer_pct):
    """시장가 대용: 즉시체결 가능성이 높도록 버퍼를 크게 준 지정가.
    (KIS 해외주식 진짜 시장가 지원 여부가 이 환경에서 미검증이라,
     이미 검증된 지정가 주문 경로에 버퍼만 크게 얹는 방식으로 안전하게 구현)"""
    if side == "buy":
        adj = price * (1 + buffer_pct / 100)
    else:
        adj = price * (1 - buffer_pct / 100)
    return kis.place_order(symbol, qty, adj, side, session="regular", exchange=exchange)


def _tier_budget(symbol: str, price: float, tier_pct: float) -> float:
    """티어 예산 = min(해당 종목 기준 매수가능액×티어비율, 안전상한)."""
    try:
        buyable = kis.get_buyable_amount(symbol, price)
    except Exception:
        buyable = 0.0
    if buyable <= 0:
        buyable = BUDGET_PER_POSITION  # 조회 실패 시 기존 안전값으로 폴백
    return min(buyable * tier_pct, TIER_MAX_BUDGET_PER_TRADE)


def _try_buy(positions, sym, price, exch, tier, tag, budget, order_fn, change_pct=None):
    qty = int(budget // price)
    if qty < 1:
        log.info("예산 부족 스킵: %s[%s] budget=%.1f price=%.2f", sym, tier, budget, price)
        return False
    res = order_fn(sym, qty, price, exch)
    if res.get("rt_cd") == "0":
        key = f"{sym}#{tier}" if tier != "early" else sym
        positions[key] = {
            "symbol": sym,
            "entry_price": price,
            "peak_price": price,
            "qty": qty,
            "exchange": exch,
            "mode": tier,
            "entry_time": datetime.now().isoformat(),
        }
        save_positions(positions)
        pct_str = f" ({change_pct:+.1f}%)" if change_pct is not None else ""
        notify(f"🟢 매수[{tag}] {sym} {qty}주 @${price:.2f} [{exch}]{pct_str}")
        return True
    log.warning("주문실패 %s[%s] rt_cd=%s msg=%s", sym, tier, res.get("rt_cd"), res.get("msg1"))
    return False


def screen_and_enter(positions: dict):
    cands = get_surge_candidates()
    if not cands:
        log.info("스캔 — 후보 0건")
        return

    early = [m for m in cands if classify(m) == "early"]
    chase = [m for m in cands if classify(m) == "chase"] if CHASE_ENABLED else []

    n_buy = 0
    rejects = []
    passed = []

    # ── 초입(early): 기존 그대로, 별도 슬롯(symbol 단독 키) ──
    early_open = sum(1 for k in positions if tier_of_key(k) == "early")
    for m in early:
        if early_open >= MAX_POSITIONS:
            break
        sym = m.get("symbol")
        if not sym or sym in positions or sym in BLACKLIST:
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
        try:
            buyable = kis.get_buyable_amount(sym, price)
        except Exception:
            buyable = 0.0
        budget = min(BUDGET_PER_POSITION, buyable) if buyable > 0 else BUDGET_PER_POSITION
        if _try_buy(positions, sym, price, exch, "early", "초입", budget,
                    lambda s, q, p, e: kis.place_order(s, q, p, "buy", session="regular", exchange=e),
                    change_pct=m.get("change_pct")):
            early_open += 1
            n_buy += 1

    # ── 티어A: 하락중만 아니면 매수, 예수금 30% ──
    a_open = count_tier(positions, "A")
    for m in chase:
        if a_open >= TIER_A_MAX_POS:
            break
        sym = m.get("symbol")
        key = f"{sym}#A"
        if not sym or key in positions or key in BLACKLIST:
            continue
        exch = get_kis_exchange(sym)
        if not exch:
            continue
        if not passes_tier_a(sym):
            continue
        price = m["price"]
        budget = _tier_budget(sym, price, TIER_A_BUDGET_PCT)
        if _try_buy(positions, sym, price, exch, "A", "추격A", budget,
                    lambda s, q, p, e: kis.place_order(s, q, p, "buy", session="regular", exchange=e),
                    change_pct=m.get("change_pct")):
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
        if not sym or key in positions or key in BLACKLIST:
            continue
        exch = get_kis_exchange(sym)
        if not exch:
            continue
        if not passes_tier_b(sym):
            continue
        price = m["price"]
        budget = _tier_budget(sym, price, TIER_B_BUDGET_PCT)
        if _try_buy(positions, sym, price, exch, "B", "추격B", budget,
                    lambda s, q, p, e: kis.place_order(s, q, p, "buy", session="regular", exchange=e),
                    change_pct=m.get("change_pct")):
            b_open += 1
            n_buy += 1

    # ── 티어C: 오늘 등락률 1위만, 시장가 대용, 예수금 10% ──
    if count_tier(positions, "C") < TIER_C_MAX_POS:
        top = get_rank1_candidate()
        if top:
            sym = top["symbol"]
            key = f"{sym}#C"
            if key not in positions and key not in BLACKLIST:
                exch = get_kis_exchange(sym)
                if exch and passes_tier_a(sym):   # 최소필터: 하락중만 아니면
                    price = top["price"]
                    budget = _tier_budget(sym, price, TIER_C_BUDGET_PCT)
                    if _try_buy(positions, sym, price, exch, "C", "추격C(1위)", budget,
                                lambda s, q, p, e: place_aggressive(
                                    s, q, p, "buy", e, TIER_C_MARKET_BUFFER_PCT),
                                change_pct=top.get("change_pct")):
                        n_buy += 1

    msg = (f"스캔완료 후보={len(cands)} 초입={len(early)} 추격={len(chase)} "
           f"통과={len(passed)} 매수={n_buy} 보유={len(positions)}")
    if passed:
        msg += " | 통과: " + ", ".join(passed[:8])
    if rejects:
        msg += " | 탈락: " + ", ".join(rejects[:5])
    log.info(msg)


def monitor_and_exit(positions: dict, force_all: bool = False):
    now = datetime.now()
    for key in list(positions.keys()):
        p = positions[key]
        sym = p.get("symbol", symbol_of_key(key))
        tier = p.get("mode", tier_of_key(key))

        cur, age = get_monitor_price(sym, p.get("exchange", "NASD"))
        if cur <= 0:
            log.warning("감시 %s[%s]: 가격조회 실패(0) — 판단 불가", sym, tier)
            continue

        # 티어C는 고점을 계속 추적 (트레일링 스톱의 기준)
        if tier == "C":
            p["peak_price"] = max(p.get("peak_price", p["entry_price"]), cur)
            save_positions(positions)

        pnl = (cur - p["entry_price"]) / p["entry_price"] * 100
        held = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60
        stale = age is not None and age > PRICE_STALE_SEC
        log.info("감시 %s[%s] pnl=%+.1f%% price=%.2f age=%ss%s",
                 sym, tier, pnl, cur, int(age) if age is not None else -1,
                 " STALE" if stale else "")

        reason = None
        if force_all:
            reason = "장마감 청산"
        elif tier == "C":
            peak = p.get("peak_price", p["entry_price"])
            if peak <= p["entry_price"] and pnl <= TIER_C_INITIAL_SL:
                reason = f"손절(초기) {pnl:.1f}%"
            else:
                dd = (cur - peak) / peak * 100 if peak > 0 else 0
                if dd <= -TIER_C_TRAIL_PCT:
                    reason = f"트레일링청산 (고점대비{dd:.1f}%, 손익{pnl:+.1f}%)"
            # 티어C는 시간청산 없음
        else:
            if tier == "A":
                tp, sl, tmax = TIER_A_TP, TIER_A_SL, TIER_A_TIME_EXIT_MIN
            elif tier == "B":
                tp, sl, tmax = TIER_B_TP, TIER_B_SL, TIER_B_TIME_EXIT_MIN
            else:  # early
                tp, sl, tmax = TAKE_PROFIT, STOP_LOSS, TIME_EXIT_MIN
            if pnl >= tp:
                reason = f"익절 +{pnl:.1f}%"
            elif pnl <= sl:
                reason = f"손절 {pnl:.1f}%"
            elif held >= tmax:
                reason = f"시간청산 {held:.0f}분 ({pnl:+.1f}%)"

        if not reason:
            continue

        sellable = get_overseas_sellable_qty(sym)
        sell_qty = min(p["qty"], sellable) if sellable > 0 else p["qty"]
        if sell_qty < 1:
            log.warning("매도수량 0 → 스킵: %s[%s]", sym, tier)
            continue

        if tier == "C":
            res = place_aggressive(sym, sell_qty, cur, "sell", p.get("exchange"),
                                    TIER_C_MARKET_BUFFER_PCT)
        else:
            res = kis.place_order(sym, sell_qty, cur, "sell", session="regular",
                                   exchange=p.get("exchange"))
        if res.get("rt_cd") == "0":
            emoji = "🔴" if pnl < 0 else "💰"
            tag = {"early": "초입", "A": "추격A", "B": "추격B", "C": "추격C(1위)"}.get(tier, tier)
            notify(f"{emoji} 매도[{tag}] {sym} {sell_qty}주 @${cur:.2f} — {reason}")
            BLACKLIST.add(key)
            del positions[key]
            save_positions(positions)


# ─────────────────────────────────────────────
# 메인 루프
# ─────────────────────────────────────────────
def main():
    mode = "모의투자" if kis.USE_MOCK else "⚠️ 실전투자"
    notify(f"🚀 미장 급등주 스캘퍼 시작 [{mode}] 초입(+{TAKE_PROFIT}%/{STOP_LOSS}%) + 티어A(+{TIER_A_TP}%/{TIER_A_SL}%) + 티어B(+{TIER_B_TP}%/{TIER_B_SL}%) + 티어C(고점-{TIER_C_TRAIL_PCT}%)")

    positions = load_positions()
    if positions:
        notify(f"♻️ 기존 포지션 {len(positions)}건 복원: " + ", ".join(positions.keys()))

    last_screen = 0.0
    was_open = False
    while True:
        try:
            is_open, mins_to_close = get_clock()

            # 개장 상태 변화 알림
            if is_open and not was_open:
                notify("🔔 미국장 개장 — 스캔 시작")
            was_open = is_open

            if not is_open:
                # 휴장/장외: 감시할 포지션만 남았으면 그대로 두고 대기
                time.sleep(60)
                continue

            near_close = mins_to_close is not None and mins_to_close <= CLOSE_BUFFER_MIN

            if near_close and positions:
                monitor_and_exit(positions, force_all=True)
                notify("🏁 마감 임박 — 전량청산 완료")

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
