# -*- coding: utf-8 -*-
"""
surge_scalper_kr.py
국장 급등주 초입 포착 → +5% 익절 스캘핑 (KIS 모의투자)

⚠️ 기존 stock_pump_bot_kr.py 와 "같은 파일"이 아니라 "옆에 나란히 도는 별도 프로세스"입니다.
   - 인증/랭킹/분봉/주문/rate limit 은 전부 기존 kis_client.py 를 import 해서 그대로 씀 (중복 구현 없음)
   - 자기가 연 포지션만 별도 state 파일로 관리 → 메인 봇 원장과 안 섞이게

⚠️⚠️ 중요: 같은 모의계좌에서 메인 kr 봇과 "동시에" 돌리지 마세요.
   현금·잔고를 공유해서 예산/포지션이 꼬입니다(지난번 누리플랜 사고와 같은 계열).
   → 오늘은 메인 kr 봇 stop 하고 이것만 돌리거나, 별도 모의계좌를 쓰세요.

전략
  09:05~14:50 스캔 → 등락률 +5~15% & 최근5분 거래대금 충분 & 상승지속 → 지정가(+버퍼) 매수
  익절 +5% / 손절 -2.5% / 30분 시간청산 / 15:10 전량청산 후 종료
  VI 위험구간(+8.5~10.5%) 진입금지, 상한가 근접(+25%↑) 진입금지, MAX_POSITIONS=3
"""

import os
import json
import time
import logging
from datetime import datetime, time as dtime
from pathlib import Path

import requests

# 기존 공용 모듈 재활용 — 여기 있는 함수/스로틀/재시도 로직을 그대로 씀
import kis_client as kis

# ─────────────────────────────────────────────
# 전략 파라미터 (내일 돌려보고 여기부터 튜닝)
# ─────────────────────────────────────────────
MAX_POSITIONS        = 3
BUDGET_PER_POSITION  = 1_000_000     # 종목당 상한 예산(원) — 매수가능현금과 min 처리됨
ENTRY_MIN_CHANGE     = 5.0           # 진입 최소 등락률(%)
ENTRY_MAX_CHANGE     = 15.0          # 진입 최대 등락률(%)
VI_BAND              = (8.5, 10.5)   # VI 발동 위험구간 — 진입 금지
LIMIT_UP_GUARD       = 25.0          # 이 이상이면 절대 진입 금지(상한가 직행 위험)
MIN_PRICE            = 1_000         # 동전주 제외
MIN_5MIN_TURNOVER    = 200_000_000   # 최근 5분 거래대금 하한(원) — 유동성/체결 확보용
TAKE_PROFIT          = 5.0           # 익절(%)
STOP_LOSS            = -2.5          # 손절(%)
TIME_EXIT_MIN        = 30            # 진입 후 N분 내 미도달 시 청산
BUY_BUFFER_PCT       = 1.0           # 매수 지정가 상향 버퍼(체결률)
SELL_BUFFER_PCT      = 1.0           # 매도 지정가 하향 버퍼(체결률)

SCAN_START       = dtime(9, 5)       # 장초반 페이크 회피
SCAN_END         = dtime(14, 50)     # 이후 신규 진입 금지
FORCE_CLOSE      = dtime(15, 10)     # 전량 청산 시각(동시호가 전)
SCREEN_INTERVAL  = 60                # 스크리닝 주기(초)
MONITOR_INTERVAL = 10                # 보유 종목 감시 주기(초)
RANKING_TOP      = 30                # 등락률 상위 몇 개까지 볼지
DEEP_CHECK_MAX   = 8                 # 한 스캔에서 분봉까지 확인할 후보 최대 수(호출 절약)

STATE_FILE = Path(os.path.expanduser("~/surge_scalper_positions.json"))
BLACKLIST  = set()                   # 당일 청산 종목 재진입 금지

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("surge_scalper.log"), logging.StreamHandler()],
)
log = logging.getLogger("surge")


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
# 포지션 상태 (이 봇이 연 것만 별도 저장 — 메인 봇 원장과 분리)
# ─────────────────────────────────────────────
def load_positions() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("포지션 파일 파싱 실패 — 빈 상태로 시작")
    return {}


def save_positions(pos: dict):
    STATE_FILE.write_text(json.dumps(pos, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ─────────────────────────────────────────────
# 후보 심층 검증 (분봉 1콜로 상승지속 + 유동성 동시 확인 → 호출 절약)
# ─────────────────────────────────────────────
def deep_check(code: str) -> bool:
    bars = kis.get_domestic_minute_bars(code, count=6)   # 오름차순(오래된→최신)
    if len(bars) < 5:
        return False
    last5 = bars[-5:]
    closes = [b["c"] for b in last5]

    # 1) 상승 지속: 5분 전 대비 상승 & 최근 3봉이 연속 하락은 아님
    rising = closes[-1] > closes[0]
    not_fading = not (closes[-3] > closes[-2] > closes[-1])
    if not (rising and not_fading):
        return False

    # 2) 유동성: 최근 5분 거래대금(원) ≈ Σ거래량 × 현재가
    turnover_5min = sum(b["v"] for b in last5) * closes[-1]
    if turnover_5min < MIN_5MIN_TURNOVER:
        return False

    return True


def passes_prelim(item: dict) -> bool:
    """랭킹 결과 1차 필터 (분봉 조회 전, 콜 아끼려고 먼저 거름)"""
    change = item.get("change_pct", 0.0)
    price  = item.get("price", 0.0)
    if not (ENTRY_MIN_CHANGE <= change <= ENTRY_MAX_CHANGE):
        return False
    if VI_BAND[0] <= change <= VI_BAND[1]:
        return False
    if change >= LIMIT_UP_GUARD:
        return False
    if price < MIN_PRICE:
        return False
    return True


# ─────────────────────────────────────────────
# 진입 / 청산
# ─────────────────────────────────────────────
def screen_and_enter(positions: dict):
    if len(positions) >= MAX_POSITIONS:
        return

    ranking = kis.get_domestic_ranking(top=RANKING_TOP)
    if not ranking:
        return

    deep_checked = 0
    for item in ranking:
        if len(positions) >= MAX_POSITIONS:
            break
        code = item.get("code")
        name = item.get("name") or code
        if not code or code in positions or code in BLACKLIST:
            continue
        if not passes_prelim(item):
            continue
        if deep_checked >= DEEP_CHECK_MAX:
            break
        deep_checked += 1
        if not deep_check(code):
            continue

        price = int(item["price"])

        # 예산: 종목당 상한과 실제 매수가능현금 중 작은 값 → 과대 포지션 방지
        buyable = kis.get_domestic_buyable_amount(code, price)
        budget = min(BUDGET_PER_POSITION, buyable) if buyable > 0 else BUDGET_PER_POSITION
        est_price = price * (1 + BUY_BUFFER_PCT / 100)   # 버퍼 감안한 예상 체결가
        qty = int(budget // est_price)
        if qty < 1:
            log.info("예산 부족으로 스킵: %s(%s) budget=%s", name, code, budget)
            continue

        res = kis.place_domestic_order(code, qty, price, "buy", buffer_pct=BUY_BUFFER_PCT)
        if res.get("rt_cd") == "0":
            positions[code] = {
                "name": name,
                "entry_price": price,
                "qty": qty,
                "entry_time": datetime.now().isoformat(),
            }
            save_positions(positions)
            notify(f"🟢 매수 {name}({code}) {qty}주 @{price:,} "
                   f"(등락률 {item['change_pct']:.1f}%)")


def monitor_and_exit(positions: dict, force_all: bool = False):
    now = datetime.now()
    for code in list(positions.keys()):
        p = positions[code]
        cur = kis.get_domestic_current_price(code)
        if cur <= 0:
            continue

        pnl = (cur - p["entry_price"]) / p["entry_price"] * 100
        held_min = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60

        reason = None
        if force_all:
            reason = "장마감 청산"
        elif pnl >= TAKE_PROFIT:
            reason = f"익절 +{pnl:.1f}%"
        elif pnl <= STOP_LOSS:
            reason = f"손절 {pnl:.1f}%"
        elif held_min >= TIME_EXIT_MIN:
            reason = f"시간청산 {held_min:.0f}분 ({pnl:+.1f}%)"

        if not reason:
            continue

        # 유령 포지션 방지: 실제 매도가능수량으로 클램프
        sellable = kis.get_kr_sellable_qty(code)
        sell_qty = min(p["qty"], sellable) if sellable > 0 else 0
        if sell_qty < 1:
            log.warning("매도가능수량 0 → 원장에서만 제거: %s(%s)", p["name"], code)
            BLACKLIST.add(code)
            del positions[code]
            save_positions(positions)
            continue

        res = kis.place_domestic_order(code, sell_qty, int(cur), "sell",
                                       buffer_pct=SELL_BUFFER_PCT)
        if res.get("rt_cd") == "0":
            emoji = "🔴" if pnl < 0 else "💰"
            notify(f"{emoji} 매도 {p['name']}({code}) {sell_qty}주 @{int(cur):,} — {reason}")
            BLACKLIST.add(code)
            del positions[code]
            save_positions(positions)


# ─────────────────────────────────────────────
# 메인 루프
# ─────────────────────────────────────────────
def main():
    mode = "모의투자" if kis.USE_MOCK else "⚠️ 실전투자"
    notify(f"🚀 급등주 스캘퍼 시작 [{mode}] (+{TAKE_PROFIT}% 익절 / {STOP_LOSS}% 손절)")

    positions = load_positions()
    if positions:
        notify(f"♻️ 기존 포지션 {len(positions)}건 복원: "
               + ", ".join(v["name"] for v in positions.values()))

    last_screen = 0.0
    while True:
        try:
            t = datetime.now().time()

            if t >= FORCE_CLOSE:
                if positions:
                    monitor_and_exit(positions, force_all=True)
                notify("🏁 장마감 — 봇 종료")
                break

            if positions:
                monitor_and_exit(positions)

            if SCAN_START <= t <= SCAN_END:
                if time.time() - last_screen >= SCREEN_INTERVAL:
                    screen_and_enter(positions)
                    last_screen = time.time()

            time.sleep(MONITOR_INTERVAL)

        except KeyboardInterrupt:
            notify("⛔ 수동 종료")
            break
        except Exception as e:
            # silent crash 방지 — 무슨 일이 있어도 루프 유지
            log.exception("메인 루프 예외: %s", e)
            time.sleep(30)


if __name__ == "__main__":
    main()
