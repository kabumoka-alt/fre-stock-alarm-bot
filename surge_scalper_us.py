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
MAX_PRICE           = 5.0          # $5 초과 제외($5~20 구간 승률 28%로 최악)
MIN_5MIN_DOLLAR_VOL = 100_000.0    # 최근 5분 거래대금 하한(USD) — 유동성/체결
TAKE_PROFIT         = 5.0          # 익절(%)
STOP_LOSS           = -3.0         # 손절(%)  ← 데이터가 지목한 유일한 교정 포인트
TIME_EXIT_MIN       = 30           # 진입 후 N분 내 미도달 시 청산

SCREEN_INTERVAL     = 60           # 스크리닝 주기(초)
MONITOR_INTERVAL    = 10           # 보유 종목 감시 주기(초)
MOVERS_TOP          = 50           # Alpaca 급등 상위 몇 개까지 볼지
MOST_ACTIVES_TOP    = 100          # 거래량 상위 몇 개를 초입 후보 풀로 볼지
RECENT_MOVE_MIN     = 3.0          # 최근 5분 최소 상승폭(%) — "지금 움직이는 중"만 통과
VOL_SURGE_MULT      = 3.0          # 초입: 최근 분당거래량 / 직전평균 배수
# ── 추격 모드 (이미 크게 오른 급등주에 올라타 아주 짧게) ──
CHASE_ENABLED         = True
CHASE_MIN_CHANGE      = 20.0       # 추격 최소 등락률(%)
CHASE_MAX_CHANGE      = 120.0      # 추격 최대 등락률(%) — 이 이상은 상투라 제외
CHASE_RECENT_MOVE_MIN = 0.5        # 추격: 최근 5분 여전히 상승 중이면 통과
CHASE_VOL_SURGE_MULT  = 2.0        # 추격: 거래량 아직 살아있으면 통과
CHASE_TAKE_PROFIT     = 3.0        # 추격 익절(%) — 짧게
CHASE_STOP_LOSS       = -2.0       # 추격 손절(%) — 타이트
CHASE_TIME_EXIT_MIN   = 15         # 추격 시간청산(분) — 빠르게
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
        bars = r.json().get("bars", [])
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


def get_last_price(symbol: str) -> float:
    """최신 체결가 (감시용)"""
    try:
        r = requests.get(f"{ALPACA_DATA_BASE}/v2/stocks/{symbol}/trades/latest",
                         headers=ALPACA_HDR, params={"feed": "iex"}, timeout=8)
        r.raise_for_status()
        return float(r.json().get("trade", {}).get("p", 0))
    except Exception:
        return 0.0


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


def deep_check(symbol: str, mode: str = "early") -> bool:
    bars = get_recent_bars(symbol, limit=25)
    if len(bars) < 6:
        return False
    last5 = bars[-5:]
    closes = [b["c"] for b in last5]

    # 상승 지속 & 페이드아웃 아님 (두 모드 공통)
    rising = closes[-1] > closes[0]
    not_fading = not (closes[-3] > closes[-2] > closes[-1])
    if not (rising and not_fading):
        return False

    # 유동성 바닥 (두 모드 공통)
    dollar_vol = sum(b["v"] * b["c"] for b in last5)
    if dollar_vol < MIN_5MIN_DOLLAR_VOL:
        return False

    recent_move = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] > 0 else 0

    # 거래량 배수 (직전 평균 대비)
    recent_vol = sum(b["v"] for b in last5) / len(last5)
    prior = bars[:-5]
    vol_mult = None
    if len(prior) >= 5:
        prior_vol = sum(b["v"] for b in prior) / len(prior)
        vol_mult = recent_vol / prior_vol if prior_vol > 0 else None

    if mode == "chase":
        # 이미 오른 걸 추격: 상승 유지 + 거래량 아직 살아있으면 OK
        if recent_move < CHASE_RECENT_MOVE_MIN:
            return False
        if vol_mult is not None and vol_mult < CHASE_VOL_SURGE_MULT:
            return False
    else:
        # 초입: 최근 급가속 + 거래량 폭발
        if recent_move < RECENT_MOVE_MIN:
            return False
        if vol_mult is not None and vol_mult < VOL_SURGE_MULT:
            return False
    return True


# ─────────────────────────────────────────────
# 진입 / 청산
# ─────────────────────────────────────────────
def screen_and_enter(positions: dict):
    if len(positions) >= MAX_POSITIONS:
        log.info("스캔 건너뜀 — 포지션 만석(%d/%d): %s",
                 len(positions), MAX_POSITIONS, ", ".join(positions.keys()))
        return
    cands = get_surge_candidates()
    if not cands:
        log.info("스캔 — 후보 0건")
        return

    early = [m for m in cands if classify(m) == "early"]
    chase = [m for m in cands if classify(m) == "chase"]
    # 초입 먼저 채우고, 빈 칸 있을 때만 추격
    ordered = [("early", m) for m in early]
    if CHASE_ENABLED:
        ordered += [("chase", m) for m in chase]

    n_deep = 0
    n_buy = 0
    rejects = []
    for mode, m in ordered:
        if len(positions) >= MAX_POSITIONS:
            break
        sym = m.get("symbol")
        if not sym or sym in positions or sym in BLACKLIST:
            continue
        if n_deep >= DEEP_CHECK_MAX:
            break
        n_deep += 1
        exch = get_kis_exchange(sym)
        if not exch:
            rejects.append(f"{sym}(거래소)")
            continue
        if not deep_check(sym, mode):
            rejects.append(f"{sym}({mode}·모멘텀)")
            continue

        price = m["price"]
        try:
            buyable = kis.get_buyable_amount(sym, price)
        except Exception:
            buyable = 0.0
        budget = min(BUDGET_PER_POSITION, buyable) if buyable > 0 else BUDGET_PER_POSITION
        qty = int(budget // price)
        if qty < 1:
            log.info("예산 부족 스킵: %s budget=%.1f price=%.2f", sym, budget, price)
            continue

        res = kis.place_order(sym, qty, price, "buy", session="regular", exchange=exch)
        if res.get("rt_cd") == "0":
            positions[sym] = {
                "entry_price": price,
                "qty": qty,
                "exchange": exch,
                "mode": mode,
                "entry_time": datetime.now().isoformat(),
            }
            save_positions(positions)
            tag = "초입" if mode == "early" else "추격"
            notify(f"🟢 매수[{tag}] {sym} {qty}주 @${price:.2f} [{exch}] ({m['change_pct']:+.1f}%)")
            n_buy += 1

    msg = (f"스캔완료 후보={len(cands)} 초입={len(early)} 추격={len(chase)} "
           f"매수={n_buy} 보유={len(positions)}/{MAX_POSITIONS}")
    if rejects:
        msg += " | 탈락: " + ", ".join(rejects[:5])
    log.info(msg)


def monitor_and_exit(positions: dict, force_all: bool = False):
    now = datetime.now()
    for sym in list(positions.keys()):
        p = positions[sym]
        cur = get_last_price(sym)
        if cur <= 0:
            continue
        pnl = (cur - p["entry_price"]) / p["entry_price"] * 100
        held = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60

        mode = p.get("mode", "early")
        if mode == "chase":
            tp, sl, tmax = CHASE_TAKE_PROFIT, CHASE_STOP_LOSS, CHASE_TIME_EXIT_MIN
        else:
            tp, sl, tmax = TAKE_PROFIT, STOP_LOSS, TIME_EXIT_MIN

        reason = None
        if force_all:
            reason = "장마감 청산"
        elif pnl >= tp:
            reason = f"익절 +{pnl:.1f}%"
        elif pnl <= sl:
            reason = f"손절 {pnl:.1f}%"
        elif held >= tmax:
            reason = f"시간청산 {held:.0f}분 ({pnl:+.1f}%)"
        if not reason:
            continue

        sellable = get_overseas_sellable_qty(sym)
        # 잔고조회가 거래소차이 등으로 종목을 못 찾으면(0) 원장 수량으로 매도 시도(모의 검증 우선)
        sell_qty = min(p["qty"], sellable) if sellable > 0 else p["qty"]
        if sell_qty < 1:
            log.warning("매도수량 0 → 스킵: %s", sym)
            continue

        res = kis.place_order(sym, sell_qty, cur, "sell", session="regular", exchange=p.get("exchange"))
        if res.get("rt_cd") == "0":
            emoji = "🔴" if pnl < 0 else "💰"
            tag = "추격" if p.get("mode") == "chase" else "초입"
            notify(f"{emoji} 매도[{tag}] {sym} {sell_qty}주 @${cur:.2f} — {reason}")
            BLACKLIST.add(sym)
            del positions[sym]
            save_positions(positions)


# ─────────────────────────────────────────────
# 메인 루프
# ─────────────────────────────────────────────
def main():
    mode = "모의투자" if kis.USE_MOCK else "⚠️ 실전투자"
    notify(f"🚀 미장 급등주 스캘퍼 시작 [{mode}] 초입(+{TAKE_PROFIT}%/{STOP_LOSS}%) + 추격(+{CHASE_TAKE_PROFIT}%/{CHASE_STOP_LOSS}%)")

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

            time.sleep(MONITOR_INTERVAL)

        except KeyboardInterrupt:
            notify("⛔ 수동 종료")
            break
        except Exception as e:
            log.exception("메인 루프 예외: %s", e)
            time.sleep(30)


if __name__ == "__main__":
    main()
