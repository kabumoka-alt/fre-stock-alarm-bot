"""
미국 주식 급등 감지 내부 시뮬레이션 봇 v35.1 (정규장 전용 + 매매일지)
- 정규장(09:30~16:00 ET)만 스캔
- 1분봉 3%+ 조건 충족 시 진입 (거래량은 참고용 표시만)
- OBV 방향 참고 표시 (필터 아님)
- 매도 타이밍: +7% 1차(절반) → 나머지는 트레일링 스톱으로 관리, -8% 손절
- [v16] 텔레그램 알림 최소화: 매시 정각 중간 일지 / 장마감 최종 일지만 수신
- [v17] ATR 기반 변동성 정렬: 상위 30종목 중 ATR 높은 순으로 재정렬 후 진입
- [v18] 횡보 청산: 매수 후 10분 경과 & +3~+6% 구간 시 전량 청산
- [v19] 매매일지 보유 종목에 현재가/수익률 표시 (API 조회)
- [v21] 스크리너 변경: most-actives(거래횟수) → movers(상승률 기준)
- [v25] 저가주 필터: $1 미만 종목 진입 제외
- [v30] 예수금 배분 방식 변경: 30% 고정 → 남은 슬롯 균등 분배 (예수금 ÷ 남은 슬롯)
- [v31] 손절 폭 조정 -10% → -8%, 폴링 60→30초 (슬리피지 축소)
- [v31] 동시 보유 7 → 4 종목 (자본 집중)
- [v31] 트레일링 스톱 도입: 고점 +TRAIL_ACTIVATE_PCT 이상에서 활성화,
        고점 대비 -TRAIL_GAP_PCT%p 하락 시 청산 (기존 +15% 전량 대체 → 승자 태우기)
- [v31] 횡보청산 완화: 트레일링 활성(고점≥활성임계) 종목은 횡보청산 면제
- [v31] 일일 리스크 게이트: 당일 실현손익 +5% 도달 시 신규매수 중단(수익잠금),
        -4% 도달 시 신규매수 중단(손실한도). 보유분 청산 로직은 계속 동작.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[v32] 손절 슬리피지 근본 원인 제거 — 4개 수정
  1) 보유 종목 감시를 별도 스레드로 분리 (3초 주기).
     기존: 손절 체크가 run_scan() 맨 앞 1회 → 스캔(최대 63 API콜, 15~30초)
           + sleep 30초 = 실효 감시 간격 45~90초.
           그래서 -8% 손절이 -18%, -21%에 체결됨.
     변경: 감시 스레드는 보유 종목만 API 1콜로 3초마다 체크. 스캔과 무관.
  2) sim_close 시 entry_prices 정리 (기존엔 영구 잔류 → 스캔 누적 지연 유발)
  3) 유령 진입 제거: 매수 성공한 종목만 entry_prices 등록
     (기존엔 매수 실패/스킵해도 등록되어 감시 대상에 포함)
  4) 중복 get_bars 제거: ATR 재정렬에서 받은 bars를 analyze_regular에 재사용
     (스캔당 최대 30콜 절감 → 스캔 소요시간 약 절반)

  ※ 이번 버전에서 의도적으로 "안 바꾼" 것들 (효과 측정을 위해 분리):
     - 1주 절반익절 버그 (max(1, qty//2) → 1주면 전량청산)
     - 스프레드/유동성 필터
     - ATR 기반 사이징
     → 위 4개 효과를 하루 확인한 뒤 v33에서 적용할 것.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[v33] v32 실측 결과 반영

  v32 실측(7/16): 손절 슬리피지 평균 초과 6.6%p → 3.5%p (NVVE 제외) 개선.
  스레드 분리 효과 확인됨. 남은 3.5%p와 NVVE 참사(-52.95%)를 잡는다.

  1) [핵심] 청산 판단 가격을 latestTrade(마지막 체결) → latestQuote의 bid로 변경.
     Alpaca 무료 IEX 피드는 전체 거래의 2~3%만 포착. 얇은 종목은 몇 분간
     체결이 안 잡혀 latestTrade가 얼어붙고, 감시 스레드가 3초마다 같은
     옛 가격을 반복해서 읽는다. 폴링이 아니라 데이터가 정지한 것.
     호가(bid)는 체결이 없어도 갱신되고, 매도 시 실제 받는 가격이라 더 정확.
     + 호가 타임스탬프로 데이터 정체(staleness) 감지 및 경고.

  2) ATR 기반 사이징. NVVE $21.83 1주 = 계좌의 22% → -53%에 계좌 -11.6% 직격.
     1건 최악 손실이 자본의 MAX_RISK_PER_TRADE_PCT를 넘지 않게 수량 제한.
     최악 손실은 max(손절폭, ATR% × ATR_LOSS_MULT)로 보수적 가정.
     + MAX_POSITION_PCT: 단일 종목 명목가 상한 (사이징 실패 대비 백스톱)

  3) 개장 직후 OPEN_BLACKOUT_MIN분 신규 진입 금지.
     7/16 5건 전부 22:30~22:35(개장 5분) 진입 → 4건 손절. 개장 직후는
     호가 스프레드가 가장 넓고 스냅샷이 가장 부정확한 구간.

  4) 스프레드 필터: 호가 스프레드 MAX_SPREAD_PCT 초과 종목 진입 제외.

  5) 1주 포지션 금지(MIN_QTY=2) + 절반익절 방어 코드.
     기존 half = max(1, qty//2) 는 1주 보유 시 전량을 팔아버려 트레일링
     기회를 없앰 (QTTB +8.4%, JLHL +11.5% 사례).

  6) trade_log 일단위 초기화. 기존엔 한 번도 안 지워서 매매일지에
     며칠치 거래가 누적 출력되고 있었음.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[v34] 내부 시뮬레이션 회계·복원 안정화
  1) 최초수량/누적매도/남은수량/총원가/누적실현손익을 포지션별로 분리 관리.
  2) 초과·0주 매도 차단, 최종 승패는 포지션 전체 누적손익으로 판정.
  3) 매수·매도 직후 가상계좌 상태를 홈 디렉터리 JSON 파일에 원자적으로 저장.
  4) 재시작 시 현금·포지션·통계·거래일지·감시기준을 복원.
  5) stale/비정상 가격 청산 및 한 번의 가격 확인 내 중복 청산 방지.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[v35.1] 하드 필터 통과 후보 점수 랭킹 + 스캔당 2종목 + 60초 배치 쿨다운.
"""

import os
import csv
import json
import time
import threading
import uuid
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ALPACA_API_KEY    = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
ET_ZONE = ZoneInfo("America/New_York")

# 정규장 조건
REGULAR_TOP_N        = 30
REGULAR_RSI          = 50
PRICE_CHANGE_1M      = 3.0
VOLUME_SURGE_RATIO   = 1.5   # 최근 봉 평균 대비 현재 거래량 배율 기준
MIN_PRICE            = 1.0   # [v25] 저가주 필터: $1 미만 종목 진입 제외

# [v33] 진입 필터 / 사이징 / 데이터 품질
MAX_SPREAD_PCT         = 1.0   # 호가 스프레드 상한(%) — 초과 시 진입 제외
MIN_QTY                = 2     # 1주 포지션 금지 (절반익절이 전량청산이 되는 문제)
MAX_RISK_PER_TRADE_PCT = 2.0   # 1건 최악 손실 = 자본의 2% 이내
MAX_POSITION_PCT       = 25.0  # 단일 종목 명목가 상한 = 자본의 25% (백스톱)
ATR_LOSS_MULT          = 2.5   # 최악 손실 가정 = ATR% × 이 배수 (갭 하락 대비)
OPEN_BLACKOUT_MIN      = 5     # 개장 후 이 시간(분)까지 신규 진입 금지
DATA_STALE_WARN_SEC    = 60    # 호가가 이 초 이상 갱신 안 되면 경고 로그

# [v32] 스캔 주기와 보유 종목 감시 주기를 분리
SCAN_INTERVAL           = 30  # 신규 종목 스캔 주기(초) — 기존 CHECK_INTERVAL
POSITION_CHECK_INTERVAL = 3   # 보유 종목 감시 주기(초) — 손절/트레일링 반응 속도

COOLDOWN_MINUTES      = 30
SELL_COOLDOWN_MINUTES = 60
MAX_BUYS_PER_SCAN     = 2    # [v35.1] 스캔 1회당 실제 신규매수 성공 상한
MAX_POSITIONS         = 4    # [v31] 7 → 4 (자본 집중)
BUY_COOLDOWN_SECONDS  = 60

BOT_NAME = "stock_pump_bot_v30.py"
BOT_VERSION = "v35.1"
TELEGRAM_HEADER = f"🤖 {BOT_NAME}\n🚀 Version {BOT_VERSION}"

# 매도 타이밍 임계값
SELL_PARTIAL_PCT = 7.0
STOP_LOSS_PCT    = -8.0      # [v31] -10 → -8

# [v31] 트레일링 스톱
TRAIL_ACTIVATE_PCT = 6.0     # 고점이 +6% 이상 찍히면 트레일링 활성화
TRAIL_GAP_PCT      = 4.0     # 고점 대비 -4%p 하락 시 청산 (마이크로캡 변동성 감안 넓게)

# [v18] 횡보 청산 조건
SIDEWAYS_MINUTES = 10
SIDEWAYS_MIN_PCT = 3.0     # [v20] 횡보 구간 하한
SIDEWAYS_MAX_PCT = 6.0     # [v31] 상한을 트레일링 활성임계(6%)와 일치시켜 구간 겹침 제거

# [v31] 일일 리스크 게이트
DAILY_PROFIT_LOCK_PCT = 5.0   # 당일 실현손익 +5% 도달 시 신규매수 중단 (수익잠금)
DAILY_LOSS_LIMIT_PCT  = -4.0  # 당일 실현손익 -4% 도달 시 신규매수 중단 (손실한도)

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

entry_prices = {}
last_alert   = {}

# [v32] 감시 스레드와 메인 스캔 스레드가 공유 상태를 동시에 건드리지 않도록 보호.
# RLock인 이유: sim_close 내부에서 다시 락을 잡는 경로가 생겨도 데드락이 나지 않게.
state_lock = threading.RLock()

# ──────────────────────────────────────────
# 시뮬레이션 상태
# ──────────────────────────────────────────
SIM_INITIAL_CASH = 100.0
SIM_STATE_FILE = os.path.expanduser("~/stock_pump_v34_sim_state.json")
V35_LOG_DIR = os.path.expanduser("~/stock_pump_v35_logs")
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

# [v31] 일일 리스크 게이트 기준값 (장 시작 시 갱신)
daily_start_pnl:  float = 0.0
daily_start_cash: float = SIM_INITIAL_CASH

# [v23] 종목당 손절 횟수 제한으로 전환
# stop_loss_count[sym] = 당일 손절 횟수, MAX_STOP_LOSS_COUNT 도달 시 당일 블랙리스트
stop_loss_count: dict = {}
MAX_STOP_LOSS_COUNT = 2   # 2회 도달 즉시 당일 차단

# 손절 횟수가 MAX_STOP_LOSS_COUNT 이상 도달한 종목 (실질 블랙리스트)
blacklisted_today: set = set()

# 오늘 거래 일지: [{"sym", "action", "qty", "price", "pnl", "pnl_pct", "time_kst"}]
trade_log: list = []

# 매시 정각 / 장마감 전송 추적
last_hourly_report_et: int = -1
market_close_sent: bool    = False

# [v32] 슬리피지 진단용 — 손절 트리거 시 실제 체결 괴리 기록
slippage_log: list = []

# [v35.1] 마지막 실제 신규매수 성공 시각. 재시작 후에도 쿨다운을 유지한다.
last_buy_time = None

V35_CSV_FIELDS = [
    "timestamp_et", "event_type", "scan_id", "symbol", "score", "scan_rank",
    "candidate_count", "momentum_1m_pct", "momentum_5m_pct", "dollar_volume_5m",
    "volume_multiplier", "fading", "existing_filter_passed", "selected_for_buy",
    "skip_reason", "entry_price", "exit_price", "exit_reason", "pnl_pct",
    "score_error",
]


def _position_remaining_qty(pos: dict) -> int:
    """v33 상태와 v34 상태를 모두 읽을 수 있는 남은 수량 접근자."""
    return max(int(pos.get("remaining_qty", pos.get("qty", 0)) or 0), 0)


def _normalize_position(pos: dict) -> dict:
    """v33 포지션을 포함해 v34 회계 필드가 빠진 상태를 안전하게 보정."""
    remaining = _position_remaining_qty(pos)
    initial = max(int(pos.get("initial_qty", remaining) or remaining), remaining)
    sold = max(int(pos.get("sold_qty", initial - remaining) or 0), 0)
    entry = float(pos.get("entry", 0) or 0)
    total_cost = float(pos.get("total_cost", entry * initial) or 0)
    pos.update({
        "qty": remaining,
        "initial_qty": initial,
        "remaining_qty": remaining,
        "total_cost": total_cost,
        "realized_pnl": float(pos.get("realized_pnl", 0.0) or 0.0),
        "sold_qty": sold,
        "partial_done": bool(pos.get("partial_done", sold > 0)),
        "entry_score": pos.get("entry_score"),
        "entry_scan_id": pos.get("entry_scan_id", pos.get("scan_id", "")),
        "entry_scan_rank": pos.get("entry_scan_rank"),
        "entry_candidate_count": pos.get("entry_candidate_count"),
        "entry_scan_time": pos.get("entry_scan_time", ""),
    })
    return pos


def _serialize_entry_prices() -> dict:
    data = {}
    for sym, entry in entry_prices.items():
        row = dict(entry)
        for key in ("time", "alert1", "stop"):
            value = row.get(key)
            if isinstance(value, datetime):
                row[key] = value.isoformat()
        data[sym] = row
    return data


def _parse_datetime(value):
    if not value or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def save_sim_state():
    """가상계좌 상태를 같은 디렉터리 임시 파일에 쓴 뒤 원자적으로 교체."""
    state_dir = os.path.dirname(SIM_STATE_FILE) or "."
    tmp_path = f"{SIM_STATE_FILE}.tmp"
    payload = {
        "version": 35.1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "sim_positions": sim_positions,
        "sim_stats": sim_stats,
        "trade_log": trade_log,
        "entry_prices": _serialize_entry_prices(),
        "stop_loss_count": stop_loss_count,
        "blacklisted_today": sorted(blacklisted_today),
        "slippage_log": slippage_log,
        "daily_start_pnl": daily_start_pnl,
        "daily_start_cash": daily_start_cash,
        "last_buy_time": (
            last_buy_time.isoformat() if isinstance(last_buy_time, datetime) else None
        ),
    }
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, SIM_STATE_FILE)
    except Exception as e:
        print(f"[상태 저장 실패] {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def reset_sim_state_in_memory():
    """상태 파일이 없거나 손상됐을 때 사용하는 안전한 초기 상태."""
    global daily_start_pnl, daily_start_cash, last_buy_time
    sim_positions.clear()
    entry_prices.clear()
    trade_log.clear()
    stop_loss_count.clear()
    blacklisted_today.clear()
    slippage_log.clear()
    sim_stats.clear()
    sim_stats.update({
        "initial_cash": SIM_INITIAL_CASH,
        "cash": SIM_INITIAL_CASH,
        "total_pnl": 0.0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
    })
    daily_start_pnl = 0.0
    daily_start_cash = SIM_INITIAL_CASH
    last_buy_time = None


def load_sim_state() -> bool:
    """저장 상태를 복원. 파일 없음/손상 시 초기 상태로 계속 실행."""
    global daily_start_pnl, daily_start_cash, last_buy_time
    if not os.path.exists(SIM_STATE_FILE):
        reset_sim_state_in_memory()
        print(f"[상태 복원] 저장 파일 없음 — ${SIM_INITIAL_CASH:.2f} 초기 상태")
        return False
    try:
        with open(SIM_STATE_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        positions = payload.get("sim_positions", {})
        stats = payload.get("sim_stats", {})
        logs = payload.get("trade_log", [])
        entries = payload.get("entry_prices", {})
        if not isinstance(positions, dict) or not isinstance(stats, dict) or not isinstance(logs, list):
            raise ValueError("상태 파일 구조가 올바르지 않음")

        sim_positions.clear()
        for sym, pos in positions.items():
            if isinstance(pos, dict):
                sim_positions[sym] = _normalize_position(pos)

        sim_stats.clear()
        sim_stats.update({
            "initial_cash": float(stats.get("initial_cash", SIM_INITIAL_CASH)),
            "cash": max(float(stats.get("cash", SIM_INITIAL_CASH)), 0.0),
            "total_pnl": float(stats.get("total_pnl", 0.0)),
            "trades": int(stats.get("trades", 0)),
            "wins": int(stats.get("wins", 0)),
            "losses": int(stats.get("losses", 0)),
        })
        trade_log.clear()
        trade_log.extend(logs)

        entry_prices.clear()
        for sym, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            row = dict(entry)
            for key in ("time", "alert1", "stop"):
                row[key] = _parse_datetime(row.get(key))
            entry_prices[sym] = row

        # 포지션은 있는데 감시기준이 누락된 구버전 상태도 복원 가능하게 보정.
        for sym, pos in sim_positions.items():
            entry_prices.setdefault(sym, {
                "entry": pos["entry"],
                "time": datetime.now(timezone.utc),
                "alert1": None,
                "stop": None,
                "sideways_done": False,
                "peak": 0.0,
            })

        stop_loss_count.clear()
        stop_loss_count.update({
            str(k): int(v) for k, v in payload.get("stop_loss_count", {}).items()
        })
        blacklisted_today.clear()
        blacklisted_today.update(payload.get("blacklisted_today", []))
        slippage_log.clear()
        slippage_log.extend(payload.get("slippage_log", []))
        daily_start_pnl = float(payload.get("daily_start_pnl", sim_stats["total_pnl"]))
        daily_start_cash = max(float(payload.get("daily_start_cash", sim_stats["cash"])), 0.0)
        # v35.1 정식 키와 선행 개발 버전 키를 모두 허용한다.
        last_buy_time = _parse_datetime(
            payload.get("last_buy_time", payload.get("last_new_buy_time"))
        )
        print(f"[상태 복원] 현금 ${sim_stats['cash']:.2f} | 포지션 {len(sim_positions)}개 "
              f"| 누적손익 ${sim_stats['total_pnl']:+.2f}")
        return True
    except Exception as e:
        print(f"[상태 복원 실패] {e} — ${SIM_INITIAL_CASH:.2f} 초기 상태로 시작")
        reset_sim_state_in_memory()
        return False


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
    """공통 봇 헤더를 붙이고, 긴 본문은 헤더가 있는 여러 메시지로 전송."""
    TELEGRAM_MAX = 4000   # 안전 여유 (실제 한도 4096)
    header_prefix = f"{TELEGRAM_HEADER}\n\n"
    if message.startswith(header_prefix):
        body = message[len(header_prefix):]
    elif message.startswith(TELEGRAM_HEADER):
        body = message[len(TELEGRAM_HEADER):].lstrip("\n")
    else:
        body = message
    body_max = TELEGRAM_MAX - len(header_prefix)

    def send_body(text: str):
        _send_telegram_chunk(header_prefix + text)

    if len(body) <= body_max:
        send_body(body)
        return

    # 줄 단위로 잘라서 청크 구성
    lines = body.split("\n")
    chunk = ""
    for line in lines:
        # 한 줄 자체가 너무 길면 강제로 잘라 전송
        if len(line) > body_max:
            if chunk:
                send_body(chunk)
                chunk = ""
            for i in range(0, len(line), body_max):
                send_body(line[i:i + body_max])
            continue
        # 현재 청크에 이 줄을 더하면 한도 초과 → 지금까지 청크 전송 후 새로 시작
        if len(chunk) + len(line) + 1 > body_max:
            send_body(chunk)
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        send_body(chunk)


def get_et_now(now_utc: datetime = None):
    """미국 동부시간. America/New_York 규칙에 따라 EST/EDT를 자동 적용."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return now_utc.astimezone(ET_ZONE)


def is_regular_session(now_utc: datetime = None) -> bool:
    now_et = get_et_now(now_utc)
    et_min = now_et.hour * 60 + now_et.minute
    if now_et.weekday() >= 5:
        return False
    return (9 * 60 + 30) <= et_min <= (16 * 60)


def in_open_blackout(now_utc: datetime = None) -> bool:
    """
    [v33] 개장 직후 OPEN_BLACKOUT_MIN분은 신규 진입 금지.
    7/16 진입 5건이 전부 개장 5분 안에 몰렸고 그중 4건이 손절.
    개장 직후는 스프레드가 가장 넓고 스냅샷 데이터가 가장 부정확하다.
    (보유분 청산은 이 게이트와 무관하게 계속 동작)
    """
    now_et = get_et_now(now_utc)
    et_min = now_et.hour * 60 + now_et.minute
    open_min = 9 * 60 + 30
    return open_min <= et_min < (open_min + OPEN_BLACKOUT_MIN)


# ──────────────────────────────────────────
# [v31] 일일 리스크 게이트
# ──────────────────────────────────────────

def daily_pnl_pct() -> float:
    """당일 실현손익률(%) — 장 시작 자본 대비."""
    if daily_start_cash <= 0:
        return 0.0
    return (sim_stats["total_pnl"] - daily_start_pnl) / daily_start_cash * 100


def buys_allowed() -> tuple[bool, str]:
    """신규매수 허용 여부 + 사유. 보유분 청산은 이 게이트와 무관하게 항상 동작."""
    d = daily_pnl_pct()
    if d >= DAILY_PROFIT_LOCK_PCT:
        return False, f"수익잠금(당일 {d:+.1f}%)"
    if d <= DAILY_LOSS_LIMIT_PCT:
        return False, f"손실한도(당일 {d:+.1f}%)"
    return True, ""


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
        _normalize_position(pos)
        status = "1차완료" if pos["partial_done"] else "전량보유"
        initial_qty = pos["initial_qty"]
        sold_qty = pos["sold_qty"]
        remaining_qty = pos["remaining_qty"]
        realized = pos["realized_pnl"]
        if current_prices and sym in current_prices:
            cur   = current_prices[sym]
            pnl_pct = ((cur - pos["entry"]) / pos["entry"]) * 100
            pnl_amt = (cur - pos["entry"]) * remaining_qty
            icon  = "📈" if pnl_pct >= 0 else "📉"
            lines.append(
                f"  • {naver_link(sym)} 최초 {initial_qty}주 / 매도 {sold_qty}주 / "
                f"잔여 {remaining_qty}주 @ ${pos['entry']:.2f} [{status}]\n"
                f"    💵 실현 {realized:+.2f}$ | {icon} 미실현 {pnl_amt:+.2f}$ "
                f"({pnl_pct:+.2f}%, 현재 ${cur:.2f})"
            )
        else:
            lines.append(
                f"  • {naver_link(sym)} 최초 {initial_qty}주 / 매도 {sold_qty}주 / "
                f"잔여 {remaining_qty}주 @ ${pos['entry']:.2f} [{status}] "
                f"| 실현 {realized:+.2f}$"
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
# [v32] 슬리피지 진단 블록
# ──────────────────────────────────────────

def slippage_block() -> str:
    """
    손절 실제 체결이 STOP_LOSS_PCT에서 얼마나 벗어났는지 요약.
    감시 스레드 분리 효과를 하루 단위로 검증하기 위한 지표.
    """
    if not slippage_log:
        return ""
    excesses = [s["excess"] for s in slippage_log]
    worst    = max(slippage_log, key=lambda s: s["excess"])
    avg      = sum(excesses) / len(excesses)
    return (
        f"🩺 <b>손절 슬리피지:</b> {len(slippage_log)}건 | "
        f"평균 초과 {avg:.2f}%p | 최악 {worst['sym']} {worst['excess']:.2f}%p"
    )


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
                position_note = ""
                if "position_realized_pnl" in t:
                    position_note = (
                        f" | 누적 {t['position_realized_pnl']:+.2f}$"
                        f" / 잔여 {t.get('remaining_qty', 0)}주"
                    )
                lines.append(
                    f"  {icon} {t['time_kst']} {t['sym']} {t['qty']}주 {t['reason']} "
                    f"@ ${t['price']:.2f} ({t['pnl']:+.2f}$, {t['pnl_pct']:+.2f}%"
                    f"{position_note})"
                )
    else:
        lines.append("📝 거래 내역: 없음")

    lines.append("━━━━━━━━━━━━━━")
    lines.append(holdings_block(current_prices))

    bl = blacklist_block()
    if bl:
        lines.append(bl)

    # [v32] 슬리피지 진단
    sl = slippage_block()
    if sl:
        lines.append(sl)

    # [v31] 일일 게이트 상태 표시
    ok, reason = buys_allowed()
    if not ok:
        lines.append(f"🔒 <b>신규매수 중단:</b> {reason}")

    lines.append("━━━━━━━━━━━━━━")

    pnl_sign = "+" if sim_stats["total_pnl"] >= 0 else ""
    lines += [
        f"💵 예수금: <b>${sim_stats['cash']:.2f}</b>",
        f"💰 누적 손익: <b>{pnl_sign}{sim_stats['total_pnl']:.2f}$</b> "
        f"(<b>{total_return_pct:+.2f}%</b>)",
        f"📅 당일 실현손익: <b>{daily_pnl_pct():+.2f}%</b>",
        f"🏆 {sim_stats['wins']}승 {sim_stats['losses']}패 "
        f"(승률 {win_rate:.0f}%) | 총 {sim_stats['trades']}거래",
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────
# 시뮬레이션 헬퍼
# ──────────────────────────────────────────

def sim_open(sym: str, price: float, atr: float = 0.0) -> bool:
    """매수 신호 → 남은 슬롯 균등 분배 + ATR 기반 리스크 사이징."""
    if not price or price <= 0:
        print(f"  [시뮬 매수 불가] {sym} 유효하지 않은 가격: {price}")
        return False
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

    # ── [v33] ATR 기반 리스크 사이징 ──
    # 자본 = 예수금 + 보유 포지션 평가액(진입가 기준)
    capital = sim_stats["cash"] + sum(
        p["entry"] * _position_remaining_qty(p) for p in sim_positions.values()
    )

    # 최악 손실 가정: 손절폭(-8%)을 그대로 믿지 않는다.
    # 변동성이 큰 종목은 손절선을 훌쩍 뛰어넘어 체결되므로 ATR% × 배수로 보수적 가정.
    # (NVVE 사례: -8% 손절이 -52.95%에 체결)
    atr_pct      = (atr / price * 100) if (atr and price > 0) else 0.0
    assumed_loss = max(abs(STOP_LOSS_PCT), atr_pct * ATR_LOSS_MULT)

    # 이 종목에 넣을 수 있는 최대 명목가 = 자본 × 허용리스크 ÷ 최악손실가정
    risk_notional = capital * (MAX_RISK_PER_TRADE_PCT / 100) / (assumed_loss / 100)
    # 백스톱: 사이징이 어떻게 나오든 단일 종목이 자본의 MAX_POSITION_PCT를 넘지 않게
    cap_notional  = capital * (MAX_POSITION_PCT / 100)

    allowed = min(budget, risk_notional, cap_notional)
    qty     = int(allowed // price)

    if qty <= 1 or qty < MIN_QTY:
        print(f"  [시뮬 매수 불가] {sym} @ ${price:.2f} | qty={qty} < {MIN_QTY}"
              f" (슬롯예산 ${budget:.1f} / 리스크상한 ${risk_notional:.1f}"
              f" / 종목상한 ${cap_notional:.1f} | ATR {atr_pct:.1f}%,"
              f" 최악손실가정 {assumed_loss:.1f}%)")
        return False

    cost = price * qty
    if cost > sim_stats["cash"] + 1e-9:
        print(f"  [시뮬 매수 불가] {sym} | 매수금액 ${cost:.2f} > 가상현금 ${sim_stats['cash']:.2f}")
        return False

    next_cash = sim_stats["cash"] - cost
    if next_cash < -1e-9:
        print(f"  [시뮬 매수 불가] {sym} | 현금 음수 방지 ({next_cash:.4f})")
        return False

    sim_stats["cash"] = max(next_cash, 0.0)
    sim_positions[sym] = {
        "entry": price,
        "qty": qty,  # v33 호환 필드. remaining_qty와 항상 동기화.
        "initial_qty": qty,
        "remaining_qty": qty,
        "total_cost": cost,
        "realized_pnl": 0.0,
        "sold_qty": 0,
        "partial_done": False,
    }
    now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
    trade_log.append({
        "action": "BUY", "sym": sym, "qty": qty, "price": price,
        "pnl": 0.0, "pnl_pct": 0.0, "reason": "매수",
        "time_kst": now_kst.strftime("%H:%M"),
    })
    print(f"  [시뮬 매수] {sym} {qty}주 @ ${price:.2f} = ${cost:.2f} "
          f"(자본 대비 {cost / capital * 100:.0f}% | ATR {atr_pct:.1f}%, "
          f"최악손실가정 {assumed_loss:.1f}%, 남은슬롯 {remaining_slots}) "
          f"| 잔여: ${sim_stats['cash']:.2f}")
    save_sim_state()
    return True


def sim_close(sym: str, exit_price: float, reason: str, qty: int = None) -> str:
    """
    포지션 청산.
    qty=None 이면 전량 청산.
    손절 시 블랙리스트 카운트 등록.
    반환값: 텔레그램 시뮬 요약 문자열.
    """
    pos = sim_positions.get(sym)
    if not pos:
        return ""

    _normalize_position(pos)
    if not exit_price or exit_price <= 0:
        print(f"  [시뮬 매도 거부] {sym} 유효하지 않은 가격: {exit_price}")
        return ""

    entry_price = pos["entry"]   # 청산 전에 미리 저장
    position_scan_id = pos.get("entry_scan_id", pos.get("scan_id", ""))
    position_entry_score = pos.get("entry_score", "")
    position_scan_rank = pos.get("entry_scan_rank", "")
    position_candidate_count = pos.get("entry_candidate_count", "")
    position_total_cost = float(pos.get("total_cost", 0.0) or 0.0)
    remaining_qty = pos["remaining_qty"]
    close_qty = remaining_qty if qty is None else int(qty)
    if close_qty <= 0:
        print(f"  [시뮬 매도 거부] {sym} close_qty={close_qty} (0 이하)")
        return ""
    if close_qty > remaining_qty:
        print(f"  [시뮬 매도 거부] {sym} 요청 {close_qty}주 > 보유 {remaining_qty}주")
        return ""

    proceeds    = exit_price * close_qty
    pnl         = (exit_price - entry_price) * close_qty
    pnl_pct     = ((exit_price - entry_price) / entry_price) * 100

    sim_stats["cash"] += proceeds
    pos["sold_qty"] += close_qty
    pos["remaining_qty"] = max(remaining_qty - close_qty, 0)
    pos["qty"] = pos["remaining_qty"]
    pos["realized_pnl"] += pnl
    sim_stats["total_pnl"] += pnl

    if pos["remaining_qty"] == 0:
        position_total_pnl = pos["realized_pnl"]
        del sim_positions[sym]
        # [v32] 포지션이 완전히 닫혔으면 entry_prices도 같이 제거.
        # 기존엔 남아 있어서 (a) 청산된 종목이 매 스캔마다 API 조회 대상이 되고
        # (b) check_sell_timing이 옛 진입가로 계속 돌아 스캔이 날이 갈수록 느려짐 → 슬리피지 악화.
        entry_prices.pop(sym, None)
        sim_stats["trades"]    += 1
        if position_total_pnl >= 0:
            sim_stats["wins"]   += 1
        else:
            sim_stats["losses"] += 1
    else:
        pos["partial_done"]     = True
        position_total_pnl = pos["realized_pnl"]

    # [v23] 손절 시 카운트 증가, 허용 횟수(MAX_STOP_LOSS_COUNT) 도달 시에만 블랙리스트 등록
    if "손절" in reason:
        stop_loss_count[sym] = stop_loss_count.get(sym, 0) + 1
        cnt = stop_loss_count[sym]
        if cnt >= MAX_STOP_LOSS_COUNT:
            blacklisted_today.add(sym)
            print(f"  [블랙리스트 등록] {sym} — 손절 {cnt}회 누적, 당일 재진입 금지")
        else:
            remaining = MAX_STOP_LOSS_COUNT - cnt
            print(f"  [손절 카운트] {sym} — {cnt}회째 (재진입 {remaining}회 더 허용)")

        # [v32] 슬리피지 기록: 의도한 -8%와 실제 체결률의 괴리
        excess = abs(pnl_pct) - abs(STOP_LOSS_PCT)
        if excess > 0:
            slippage_log.append({"sym": sym, "actual": pnl_pct, "excess": excess})
            mark = "🚨" if excess >= 3.0 else "⚠️"
            print(f"  {mark} [슬리피지] {sym} 의도 {STOP_LOSS_PCT:.1f}% → 실제 {pnl_pct:+.2f}% "
                  f"(초과 {excess:.2f}%p)")

    now_kst = datetime.now(timezone.utc) + timedelta(hours=9)
    trade_log.append({
        "action": "SELL", "sym": sym, "qty": close_qty, "price": exit_price,
        "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason,
        "position_realized_pnl": position_total_pnl,
        "remaining_qty": pos["remaining_qty"] if sym in sim_positions else 0,
        "time_kst": now_kst.strftime("%H:%M"),
    })
    if sym not in sim_positions and position_scan_id:
        final_pnl_pct = (
            position_total_pnl / position_total_cost * 100 if position_total_cost > 0 else 0.0
        )
        append_candidate_csv([_candidate_csv_row(
            position_scan_id,
            sym,
            {"score": position_entry_score},
            event_type="exit",
            scan_rank=position_scan_rank,
            candidate_count=position_candidate_count,
            existing_filter_passed=True,
            selected_for_buy=True,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_pct=round(final_pnl_pct, 4),
            exit_reason=reason,
        )])
    save_sim_state()

    win_rate         = (
        sim_stats["wins"] / sim_stats["trades"] * 100
        if sim_stats["trades"] > 0 else 0.0
    )
    total_return_pct = (sim_stats["total_pnl"] / sim_stats["initial_cash"]) * 100

    bl_note = f"\n🚫 {sym} 당일 블랙리스트 등록" if sym in blacklisted_today else ""

    summary = (
        f"\n\n💹 <b>[시뮬레이션]</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"📤 청산: {reason} | {close_qty}주 @ ${exit_price:.2f}\n"
        f"📥 진입가: ${entry_price:.2f}\n"
        f"{'📈' if pnl >= 0 else '📉'} 건별 손익: "
        f"<b>{'+' if pnl >= 0 else ''}{pnl:.2f}$ ({pnl_pct:+.2f}%)</b>\n"
        f"🧾 포지션 누적 실현손익: <b>{position_total_pnl:+.2f}$</b>\n"
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


def score_candidate(symbol: str, bars: list, current_price: float) -> dict:
    """기존 하드 필터 통과 후보의 v35.1 점수와 원시 지표를 계산한다."""
    if not bars or len(bars) < 6:
        return {"score": None, "score_error": "insufficient_bars"}
    try:
        recent = bars[-5:]
        closes = [float(bar["c"]) for bar in recent]
        volumes = [float(bar["v"]) for bar in recent]
        prior = bars[:-5][-20:]
        prior_volumes = [float(bar["v"]) for bar in prior]
        if current_price <= 0 or closes[0] <= 0 or closes[-2] <= 0 or not prior_volumes:
            return {"score": None, "score_error": "invalid_score_data"}

        momentum_1m = (current_price - closes[-2]) / closes[-2] * 100
        momentum_5m = (current_price - closes[0]) / closes[0] * 100
        dollar_volume_5m = sum(
            float(bar["c"]) * float(bar["v"]) for bar in recent
        )
        prior_avg = sum(prior_volumes) / len(prior_volumes)
        recent_avg = sum(volumes) / len(volumes)
        volume_multiplier = recent_avg / prior_avg if prior_avg > 0 else None

        trend_closes = closes[:-1] + [current_price]
        rising_steps = sum(
            1 for before, after in zip(trend_closes, trend_closes[1:]) if after > before
        )
        fading = trend_closes[-3] > trend_closes[-2] > trend_closes[-1]

        momentum_1m_score = min(max(momentum_1m, 0.0) / 8.0, 1.0) * 25.0
        momentum_5m_score = min(max(momentum_5m, 0.0) / 15.0, 1.0) * 20.0
        dollar_volume_score = min(max(dollar_volume_5m, 0.0) / 250_000.0, 1.0) * 20.0
        volume_score = (min(max(volume_multiplier, 0.0) / 5.0, 1.0) * 20.0
                        if volume_multiplier is not None else 0.0)
        trend_score = rising_steps / 4 * 10.0 + (0.0 if fading else 5.0)
        score = min(100.0, momentum_1m_score + momentum_5m_score
                    + dollar_volume_score + volume_score + trend_score)
        return {
            "score": round(score, 2),
            "score_error": "",
            "symbol": symbol,
            "momentum_1m_pct": round(momentum_1m, 4),
            "momentum_5m_pct": round(momentum_5m, 4),
            "dollar_volume_5m": round(dollar_volume_5m, 2),
            "volume_multiplier": (round(volume_multiplier, 4)
                                  if volume_multiplier is not None else None),
            "fading": fading,
        }
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
        return {"score": None, "score_error": type(e).__name__}


def assign_scan_ranks(candidates: list) -> None:
    """분석용 순위만 부여한다. 전달받은 실제 매수 후보 순서는 변경하지 않는다."""
    def rank_key(item):
        data = item["score_data"]
        score = data.get("score")
        if score is None:
            return (1, 0.0, 0.0, 0.0, item["symbol"])
        return (
            0,
            -float(score),
            -float(data.get("dollar_volume_5m") or 0.0),
            -float(data.get("momentum_1m_pct") or 0.0),
            item["symbol"],
        )

    for scan_rank, item in enumerate(sorted(candidates, key=rank_key), start=1):
        item["scan_rank"] = scan_rank


def _candidate_csv_row(scan_id: str, symbol: str, score_data: dict = None, **updates) -> dict:
    score_data = score_data or {}
    row = {
        "timestamp_et": datetime.now(ET_ZONE).isoformat(),
        "event_type": "candidate",
        "scan_id": scan_id,
        "symbol": symbol,
        "score": score_data.get("score", ""),
        "scan_rank": "",
        "candidate_count": "",
        "momentum_1m_pct": score_data.get("momentum_1m_pct", ""),
        "momentum_5m_pct": score_data.get("momentum_5m_pct", ""),
        "dollar_volume_5m": score_data.get("dollar_volume_5m", ""),
        "volume_multiplier": score_data.get("volume_multiplier", ""),
        "fading": score_data.get("fading", ""),
        "existing_filter_passed": False,
        "selected_for_buy": False,
        "skip_reason": "",
        "entry_price": "",
        "exit_price": "",
        "exit_reason": "",
        "pnl_pct": "",
        "score_error": score_data.get("score_error", ""),
    }
    row.update(updates)
    return row


def append_candidate_csv(rows: list):
    """미국 동부 날짜별 후보 CSV append. 실패해도 시뮬레이션은 계속한다."""
    if not rows:
        return
    try:
        os.makedirs(V35_LOG_DIR, exist_ok=True)
        date_et = datetime.now(ET_ZONE).strftime("%Y-%m-%d")
        path = os.path.join(V35_LOG_DIR, f"candidates_{date_et}.csv")
        needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=V35_CSV_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerows(rows)
    except Exception as e:
        print(f"[v35.1 후보 CSV 저장 실패] {e}")


def _sim_open_failure_reason(price: float, atr: float) -> str:
    """sim_open 실패를 기록용으로만 분류한다. 사이징 결정에는 사용하지 않는다."""
    if len(sim_positions) >= MAX_POSITIONS:
        return "position_limit"
    remaining_slots = MAX_POSITIONS - len(sim_positions)
    budget = sim_stats["cash"] / remaining_slots
    capital = sim_stats["cash"] + sum(
        pos["entry"] * _position_remaining_qty(pos) for pos in sim_positions.values()
    )
    atr_pct = (atr / price * 100) if (atr and price > 0) else 0.0
    assumed_loss = max(abs(STOP_LOSS_PCT), atr_pct * ATR_LOSS_MULT)
    risk_notional = capital * (MAX_RISK_PER_TRADE_PCT / 100) / (assumed_loss / 100)
    cap_notional = capital * (MAX_POSITION_PCT / 100)
    qty = int(min(budget, risk_notional, cap_notional) // price) if price > 0 else 0
    if qty < MIN_QTY:
        return "qty_too_small"
    if price * qty > sim_stats["cash"] + 1e-9:
        return "insufficient_cash"
    return "open_failed"


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


def parse_ts(ts: str):
    """
    [v33] Alpaca RFC3339 타임스탬프 파서.
    나노초(9자리)가 붙어 오는데 fromisoformat은 마이크로초(6자리)까지만 받으므로 잘라낸다.
    """
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        if "." in s:
            head, rest = s.split(".", 1)
            if "+" in rest:
                frac, off = rest.split("+", 1)
                off = "+" + off
            else:
                frac, off = rest, "+00:00"
            s = f"{head}.{frac[:6]}{off}"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def spread_pct(snap: dict) -> float:
    """
    [v33] 호가 스프레드(%). 호가가 없거나 비정상이면 -1.0 반환(판단 불가).
    """
    q = snap.get("latestQuote") or {}
    bid, ask = q.get("bp"), q.get("ap")
    if not bid or not ask or bid <= 0 or ask <= bid:
        return -1.0
    return (ask - bid) / ((ask + bid) / 2) * 100


def get_exit_price(snap: dict):
    """
    [v33] 청산 판단용 가격 — 호가(bid) 우선.

    왜 bid인가:
      Alpaca 무료 IEX 피드는 전체 거래의 2~3%만 포착한다. 얇은 종목은 몇 분간
      IEX에 체결이 안 잡혀 latestTrade가 그대로 얼어붙고, 감시 스레드가 3초마다
      같은 옛 가격을 반복해 읽는다. 그래서 -8% 손절이 -53%에 체결됐다.
      호가는 체결이 없어도 계속 갱신되고, 매도 시 실제로 받는 가격이 bid이므로
      손절 판단에 더 정확하고 보수적이다.

    반환: (price, source, stale_sec)
      stale_sec: 이 가격이 몇 초 전 것인지 (판단 불가 시 None)
    """
    q = snap.get("latestQuote") or {}
    bid, ask = q.get("bp"), q.get("ap")

    stale_sec = None
    ts = parse_ts(q.get("t"))
    if ts:
        stale_sec = (datetime.now(timezone.utc) - ts).total_seconds()

    # 정상 호가(교차/역전 아님)면 bid 사용
    if bid and ask and bid > 0 and ask > bid:
        return float(bid), "bid", stale_sec

    # 호가가 없으면 기존 방식으로 폴백
    price, src = get_live_price(snap)
    lt_ts = parse_ts((snap.get("latestTrade") or {}).get("t"))
    if lt_ts:
        stale_sec = (datetime.now(timezone.utc) - lt_ts).total_seconds()
    return (float(price) if price else None), src, stale_sec


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

def check_sell_timing(sym: str, current_price: float, price_source: str, stale_sec: float = None):
    if sym not in entry_prices:
        return
    if not current_price or current_price <= 0:
        print(f"  [청산 판단 스킵] {sym} 유효하지 않은 {price_source} 가격: {current_price}")
        return
    if price_source == "bid" and stale_sec is None:
        print(f"  [청산 판단 스킵] {sym} bid 타임스탬프 없음")
        return
    if stale_sec is not None and stale_sec > DATA_STALE_WARN_SEC:
        print(f"  [청산 판단 스킵] {sym} stale {price_source} {stale_sec:.0f}초")
        return

    entry       = entry_prices[sym]
    entry_price = entry["entry"]
    now_utc     = datetime.now(timezone.utc)
    gain_pct    = ((current_price - entry_price) / entry_price) * 100
    ticker_link = naver_link(sym)

    # [v31] 고점(peak) 갱신 — 트레일링 스톱 기준
    if gain_pct > entry.get("peak", 0.0):
        entry["peak"] = gain_pct
        save_sim_state()
    peak = entry.get("peak", 0.0)

    def cooldown_ok(key):
        last = entry.get(key)
        if last is None:
            return True
        return (now_utc - last).total_seconds() / 60 >= SELL_COOLDOWN_MINUTES

    # ── 손절 (-8%) ──
    if gain_pct <= STOP_LOSS_PCT:
        if cooldown_ok("stop"):
            if sym in sim_positions:
                closed = sim_close(sym, current_price, "손절(-8%)", qty=None)
                if closed:
                    entry["stop"] = now_utc
                    print(f"[🔴 손절] {sym} | ${entry_price:.2f} → "
                          f"${current_price:.2f} ({gain_pct:+.2f}%)")
        return

    # ── [v31] 트레일링 스톱 (고점 +6% 이상 활성화 & 고점 대비 -4%p 하락 시 전량 청산) ──
    if peak >= TRAIL_ACTIVATE_PCT and gain_pct <= (peak - TRAIL_GAP_PCT):
        if sym in sim_positions:
            closed = sim_close(sym, current_price, f"트레일링청산(고점{peak:+.1f}%)", qty=None)
            if closed:
                print(f"[🟢 트레일링청산] {sym} | 고점 {peak:+.2f}% → 현재 {gain_pct:+.2f}% "
                      f"| ${entry_price:.2f} → ${current_price:.2f}")
        return

    # ── +7% 1차 매도 (절반 확보). 나머지는 트레일링이 관리 ──
    if gain_pct >= SELL_PARTIAL_PCT:
        if cooldown_ok("alert1"):
            pos = sim_positions.get(sym)
            if pos and not pos.get("partial_done"):
                _normalize_position(pos)
                # [v33] 1주 보유면 절반익절 스킵.
                # 기존 max(1, qty//2)는 1주일 때 전량을 팔아버려 트레일링 기회를 없앰.
                remaining_qty = pos["remaining_qty"]
                if remaining_qty >= 2:
                    partial_qty = remaining_qty // 2
                    closed = sim_close(
                        sym, current_price, "+7% 1차(절반)", qty=partial_qty)
                    if closed:
                        entry["alert1"] = now_utc
                        print(f"[🟡 1차매도] {sym} | ${entry_price:.2f} → "
                              f"${current_price:.2f} ({gain_pct:+.2f}%)")
                else:
                    print(f"  [1차매도 스킵] {sym} 1주 보유 — 트레일링에 위임 ({gain_pct:+.2f}%)")
        return

    # ── [v31] 횡보 청산 완화: 트레일링 미활성(고점<활성임계) & 10분 경과 & +3~+6% 구간만 ──
    elapsed_min = (now_utc - entry["time"]).total_seconds() / 60
    if (peak < TRAIL_ACTIVATE_PCT
            and elapsed_min >= SIDEWAYS_MINUTES
            and not entry.get("sideways_done")):
        if SIDEWAYS_MIN_PCT <= gain_pct < SIDEWAYS_MAX_PCT:
            if sym in sim_positions:
                closed = sim_close(sym, current_price, "횡보청산", qty=None)
                if closed:
                    entry["sideways_done"] = True
                    print(f"[➡️ 횡보청산] {sym} | ${entry_price:.2f} → ${current_price:.2f} "
                          f"({gain_pct:+.2f}%) | {elapsed_min:.0f}분 경과")
            return


# ──────────────────────────────────────────
# [v32] 보유 종목 감시 스레드
# ──────────────────────────────────────────

def position_monitor_loop():
    """
    [v32] 보유 종목만 POSITION_CHECK_INTERVAL(3초)마다 감시하는 독립 스레드.

    왜 필요한가:
      기존 v31은 손절 체크가 run_scan() 맨 앞에서 1회만 돌았고, 그 뒤로
      스크리너 1콜 + 스냅샷 2콜 + get_bars 최대 60콜(ATR용 30 + analyze용 30 중복)
      + time.sleep(0.5)×N 이 순차 실행됐음. 여기에 sleep(30)이 더해져
      실효 감시 간격이 45~90초. -8% 손절이 -18%, -21%에 체결된 원인.

    이 스레드는 보유 종목만 스냅샷 1콜로 조회하므로 3초 주기가 가능.
    신규 스캔이 아무리 느려도 손절 반응 속도에 영향을 주지 않음.
    """
    while True:
        try:
            if is_regular_session():
                with state_lock:
                    syms = list(sim_positions.keys())

                if syms:
                    # 네트워크 요청은 락 밖에서 (스캔 스레드를 불필요하게 막지 않도록)
                    snaps = get_snapshots(syms)

                    with state_lock:
                        for sym in syms:
                            # 락 대기 중 다른 스레드가 청산했을 수 있으므로 재확인
                            if sym not in sim_positions:
                                continue
                            snap = snaps.get(sym)
                            if not snap:
                                continue
                            # [v33] 체결가 대신 호가(bid) 기준으로 청산 판단
                            price, src, stale = get_exit_price(snap)
                            if not price:
                                continue
                            if stale is not None and stale > DATA_STALE_WARN_SEC:
                                print(f"  ⏱ [데이터 정체] {sym} — {src} 가격이 {stale:.0f}초 전 것")
                            check_sell_timing(sym, float(price), src, stale)
        except Exception as e:
            # 감시 스레드는 절대 죽으면 안 됨 — 어떤 예외든 삼키고 계속 돈다
            print(f"[감시 스레드 예외] {e}")

        time.sleep(POSITION_CHECK_INTERVAL)


# ──────────────────────────────────────────
# 종목 분석
# ──────────────────────────────────────────

def analyze_regular(sym: str, snap: dict, bars: list = None):
    # [v32] bars를 인자로 받아 재사용. ATR 재정렬 단계에서 이미 조회한 걸
    # 여기서 또 부르던 중복 API 콜(스캔당 최대 30콜) 제거.
    if bars is None:
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

    # [v33] 스프레드 필터 — 호가가 벌어진 종목은 손절이 미끄러진다.
    # 호가가 아예 없으면(IEX 미포착) 차단하지 않고 경고만 (fail-open).
    # 하루 로그에서 "호가 없음"이 드물면 fail-close(return None)로 바꿀 것.
    sp = spread_pct(snap)
    if sp > MAX_SPREAD_PCT:
        print(f"  └ 스프레드 제외: {sp:.2f}% > {MAX_SPREAD_PCT}%")
        return None
    if sp < 0:
        print(f"  └ ⚠️ 호가 없음 — 스프레드 필터 미적용")

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
        with state_lock:
            if sim_positions:
                held = list(sim_positions.keys())
                snaps = get_snapshots(held)
                for sym in held:
                    snap = snaps.get(sym, {})
                    price, source, stale = get_exit_price(snap)   # [v33] bid 기준
                    if source == "bid" and stale is None:
                        print(f"  [장마감 청산 보류] {sym} bid 타임스탬프 없음")
                        continue
                    if stale is not None and stale > DATA_STALE_WARN_SEC:
                        print(f"  [장마감 청산 보류] {sym} stale {source} {stale:.0f}초")
                        continue
                    if price and price > 0:
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
    global last_buy_time
    # [v32] 보유 종목 손절/트레일링 체크 로직은 position_monitor_loop()로 이전.
    # 이 함수는 이제 신규 진입 탐색만 담당한다.
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
    scan_id = str(uuid.uuid4())
    csv_rows = []

    top = ranked[:REGULAR_TOP_N]
    print(f"[정규장] 상위 {REGULAR_TOP_N}종목 | 1위: {top[0]['symbol']} {top[0]['change_pct']:+.2f}%")
    with state_lock:
        print(f"  {holdings_block().replace(chr(10), ' | ')}")
    if blacklisted_today:
        print(f"  🚫 블랙리스트: {', '.join(sorted(blacklisted_today))}")

    # [v33] 개장 직후 블랙아웃 — 신규 진입만 차단
    if in_open_blackout():
        print(f"  [⏳ 개장 블랙아웃] 개장 {OPEN_BLACKOUT_MIN}분 경과 전 — 신규매수 스킵")
        return

    # [v31] 일일 리스크 게이트 확인 — 막혀 있으면 신규매수 스킵
    # (보유분 관리는 감시 스레드가 계속 수행하므로 여기서 return해도 안전)
    can_buy, lock_reason = buys_allowed()
    if not can_buy:
        print(f"  [🔒 신규매수 중단] {lock_reason} (당일 {daily_pnl_pct():+.2f}%) — 보유분 관리만 진행")
        return

    # ATR 계산 후 높은 순으로 재정렬
    top_with_atr = []
    for stock in top:
        sym = stock["symbol"]
        if sym in blacklisted_today:
            continue
        bars = get_bars(sym)
        atr  = calc_atr(bars) if bars else 0.0
        # [v32] bars를 함께 실어 보내 analyze_regular에서 재조회하지 않게 함
        top_with_atr.append({**stock, "_atr": atr, "_bars": bars})

    top_with_atr.sort(key=lambda x: x["_atr"], reverse=True)
    print(f"  [ATR 재정렬] " + " | ".join(
        f"{s['symbol']}({s['_atr']:.3f})" for s in top_with_atr[:5]
    ))

    # [v35.1] 기존 하드 필터를 모두 통과시킨 뒤에만 점수를 계산한다.
    candidates = []
    for stock in top_with_atr:
        sym = stock["symbol"]

        if sym in last_alert:
            elapsed = (now_utc - last_alert[sym]).total_seconds() / 60
            if elapsed < COOLDOWN_MINUTES:
                continue

        print(f"  [{sym}] 분석 중...")
        result = analyze_regular(sym, stock["snap"], stock.get("_bars"))
        if result is None:
            continue

        try:
            score_data = score_candidate(sym, stock.get("_bars"), stock["price"])
        except Exception as e:
            # Shadow Score는 분석 전용이므로 어떤 계산 오류도 기존 매매를 막지 않는다.
            score_data = {"score": None, "score_error": type(e).__name__}
        if score_data.get("score") is None:
            print(f"  [점수 실패] {sym} — {score_data.get('score_error', 'unknown')}")
        candidates.append({
            **stock,
            "analysis": result,
            "score_data": score_data,
        })

    candidate_count = len(candidates)
    assign_scan_ranks(candidates)
    if candidates:
        ranked_for_display = sorted(candidates, key=lambda item: item["scan_rank"])
        print("  [v35.1 후보 랭킹] " + " | ".join(
            f"{item['symbol']}({item['score_data'].get('score')})"
            for item in ranked_for_display[:8]
        ))

    # 쿨다운은 스캔 시작 시점 기준이다. 같은 스캔의 최대 2건 배치는 허용하며,
    # 성공 시각은 즉시 갱신해 다음 스캔의 신규매수를 차단한다.
    cooldown_active = False
    if last_buy_time is not None:
        cooldown_elapsed = (now_utc - last_buy_time).total_seconds()
        cooldown_active = cooldown_elapsed < BUY_COOLDOWN_SECONDS
        if cooldown_active:
            remaining = BUY_COOLDOWN_SECONDS - cooldown_elapsed
            print(f"  [⏳ 신규매수 쿨다운] {remaining:.0f}초 남음 — 보유분 감시는 계속")

    bought_this_scan = 0
    # 중요: 실제 매수 검토는 기존 ATR 후보 순서(candidates)를 그대로 사용한다.
    for stock in candidates:
        sym = stock["symbol"]
        result = stock["analysis"]
        score_data = stock["score_data"]
        score = score_data.get("score")
        row_common = {
            "existing_filter_passed": True,
            "scan_rank": stock["scan_rank"],
            "candidate_count": candidate_count,
        }

        if cooldown_active:
            csv_rows.append(_candidate_csv_row(
                scan_id, sym, score_data, **row_common, skip_reason="buy_cooldown",
            ))
            continue
        if sym in sim_positions:
            csv_rows.append(_candidate_csv_row(
                scan_id, sym, score_data, **row_common, skip_reason="already_held",
            ))
            continue
        if bought_this_scan >= MAX_BUYS_PER_SCAN:
            print(f"  [매수 제한] {sym} — 이번 스캔 {MAX_BUYS_PER_SCAN}종목 성공, 스킵")
            csv_rows.append(_candidate_csv_row(
                scan_id, sym, score_data, **row_common, skip_reason="scan_buy_limit",
            ))
            continue
        if len(sim_positions) >= MAX_POSITIONS:
            csv_rows.append(_candidate_csv_row(
                scan_id, sym, score_data, **row_common, skip_reason="position_limit",
            ))
            continue

        # [v32] 유령 진입 제거:
        # 기존엔 sim_open 성공 여부와 무관하게 entry_prices에 먼저 등록해서,
        # 실제로 사지 않은 종목(슬롯 풀/예수금 부족/블랙리스트)이 감시 대상에 남았음.
        # 이제 매수에 성공한 종목만 등록한다.
        with state_lock:
            # [v33] ATR을 넘겨 리스크 사이징에 사용
            bought = sim_open(sym, stock["price"], stock.get("_atr", 0.0))
            if not bought:
                csv_rows.append(_candidate_csv_row(
                    scan_id, sym, score_data, **row_common,
                    skip_reason=_sim_open_failure_reason(
                        stock["price"], stock.get("_atr", 0.0)
                    ),
                ))
                continue

            bought_this_scan  += 1
            last_alert[sym]    = now_utc
            last_buy_time      = datetime.now(timezone.utc)
            sim_positions[sym]["entry_score"] = score
            sim_positions[sym]["entry_scan_id"] = scan_id
            sim_positions[sym]["entry_scan_rank"] = stock["scan_rank"]
            sim_positions[sym]["entry_candidate_count"] = candidate_count
            sim_positions[sym]["entry_scan_time"] = now_utc.isoformat()
            entry_prices[sym]  = {
                "entry": stock["price"], "time": datetime.now(timezone.utc),
                "alert1": None, "stop": None,
                "sideways_done": False, "peak": 0.0,   # [v31] peak 추적
                "scan_id": scan_id, "entry_score": score,
            }
            save_sim_state()

        csv_rows.append(_candidate_csv_row(
            scan_id, sym, score_data, **row_common,
            selected_for_buy=True, skip_reason="selected",
            entry_price=stock["price"],
        ))

        print(
            f"[🚀 감지] {sym} | 점수 {score if score is not None else 'N/A'} | {stock['change_pct']:+.2f}% | RSI {result['rsi']:.1f} "
            f"| 거래량 {result['vol_ratio']:.1f}x | ATR {result['atr']:.3f} | 진입가 ${stock['price']:.2f}"
        )
        time.sleep(0.5)

    append_candidate_csv(csv_rows)


# ──────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────

def main():
    global market_close_sent, daily_start_pnl, daily_start_cash

    load_sim_state()
    print("=" * 60)
    print("🚀 급등 감지 내부 시뮬레이션 봇 v35.1 (정규장 전용 + 매매일지) 시작!")
    print(f"📈 정규장: 상위 {REGULAR_TOP_N}종목 | 1분 {PRICE_CHANGE_1M}%+ | ${MIN_PRICE}+ 종목만")
    print(f"🩸 [v35.1] 청산 판단: 호가(bid) 기준 | 스프레드 ≤{MAX_SPREAD_PCT}% | 최소 {MIN_QTY}주")
    print(f"⚖️  [v35.1] 사이징: 1건 리스크 ≤{MAX_RISK_PER_TRADE_PCT}% | 종목당 ≤{MAX_POSITION_PCT}% | ATR×{ATR_LOSS_MULT} 손실가정")
    print(f"💾 [v35.1] 상태 저장: {SIM_STATE_FILE}")
    print(f"⏳ [v35.1] 개장 {OPEN_BLACKOUT_MIN}분 신규진입 금지")
    print(f"🎯 매도: +{SELL_PARTIAL_PCT}% 1차(절반) | 트레일링(고점 +{TRAIL_ACTIVATE_PCT}% 활성, -{TRAIL_GAP_PCT}%p 갭) | {STOP_LOSS_PCT}% 손절")
    print(f"➡️  횡보청산: {SIDEWAYS_MINUTES}분 경과 & +{SIDEWAYS_MIN_PCT}~+{SIDEWAYS_MAX_PCT}% (트레일링 미활성 종목만)")
    print(f"📦 동시 보유 최대 {MAX_POSITIONS}종목 | 스캔당 최대 {MAX_BUYS_PER_SCAN}종목")
    print(f"🧮 [v35.1] Shadow Score 분석 순위 | 신규매수 쿨다운 {BUY_COOLDOWN_SECONDS}초")
    print(f"👁 [v32] 보유 감시 {POSITION_CHECK_INTERVAL}초 (독립 스레드) | 신규 스캔 {SCAN_INTERVAL}초")
    print(f"🔒 일일 게이트: +{DAILY_PROFIT_LOCK_PCT}% 수익잠금 / {DAILY_LOSS_LIMIT_PCT}% 손실한도 (신규매수 중단)")
    print(f"🔔 장마감 보유 종목 전량 강제 청산")
    print(f"🚫 손절 {MAX_STOP_LOSS_COUNT}회 도달 시 당일 블랙리스트 등록")
    print("=" * 60)

    send_telegram(
        f"🤖 <b>급등 감지 내부 시뮬레이션 봇 v34 시작!</b>\n"
        f"📈 1분 {PRICE_CHANGE_1M}%+ | ${MIN_PRICE}+ | 스프레드 ≤{MAX_SPREAD_PCT}%\n"
        f"🎯 +{SELL_PARTIAL_PCT}% 1차(절반) → 트레일링(고점 +{TRAIL_ACTIVATE_PCT}%, -{TRAIL_GAP_PCT}%p) | {STOP_LOSS_PCT}% 손절\n"
        f"🩸 <b>[v34] 청산 판단 = 유효한 호가(bid) 기준</b> (stale 가격 청산 차단)\n"
        f"⚖️ <b>[v34] ATR 사이징·포지션 회계 강화</b> — 1건 리스크 ≤{MAX_RISK_PER_TRADE_PCT}% | 종목당 ≤{MAX_POSITION_PCT}%\n"
        f"💾 재시작 시 가상 현금·포지션·손익 자동 복원\n"
        f"⏳ [v34] 개장 {OPEN_BLACKOUT_MIN}분 신규진입 금지 | 최소 {MIN_QTY}주\n"
        f"📦 동시 보유 최대 {MAX_POSITIONS}종목\n"
        f"👁 보유 감시 {POSITION_CHECK_INTERVAL}초 (스레드 분리) | 스캔 {SCAN_INTERVAL}초\n"
        f"🩺 손절 슬리피지 추적\n"
        f"🔒 당일 +{DAILY_PROFIT_LOCK_PCT}% 수익잠금 / {DAILY_LOSS_LIMIT_PCT}% 손실한도\n"
        f"🔔 장마감 보유 종목 전량 강제 청산\n"
        f"💹 텔레그램: 매시 정각 일지 / 장마감 최종 일지만 수신"
    )

    # [v32] 보유 종목 감시 스레드 기동. daemon=True라 메인이 죽으면 같이 종료됨.
    monitor = threading.Thread(target=position_monitor_loop, daemon=True, name="pos-monitor")
    monitor.start()
    print(f"👁 [v32] 보유 종목 감시 스레드 시작 ({POSITION_CHECK_INTERVAL}초 주기)")

    while True:
        now_str = datetime.now().strftime('%H:%M:%S')
        now_et  = get_et_now()

        # 날짜 바뀌면 당일 플래그 리셋
        if now_et.hour == 9 and now_et.minute < 30:
            if market_close_sent:
                market_close_sent = False
                # [v31] 일일 리스크 게이트 기준 갱신 (장 시작 = 전량 청산 후라 예수금 = 자본)
                daily_start_pnl  = sim_stats["total_pnl"]
                daily_start_cash = sim_stats["cash"]
                print(f"[리셋] 장마감 플래그 초기화 | 일일게이트 기준 갱신 "
                      f"(기준자본 ${daily_start_cash:.2f})")
                slippage_log.clear()   # [v32] 슬리피지 로그도 일단위 리셋
                # [v33] 기존엔 trade_log를 한 번도 안 지워서 매매일지에
                # 며칠치 거래가 통째로 누적 출력되고 있었음.
                trade_log.clear()
                print("[리셋] 매매일지 초기화 (전일 거래 내역 정리)")
                save_sim_state()
            if blacklisted_today or stop_loss_count:
                blacklisted_today.clear()
                stop_loss_count.clear()
                print("[리셋] 블랙리스트 및 손절 카운트 초기화 (새 장 시작)")
                save_sim_state()

        check_scheduled_reports()

        if not is_regular_session():
            print(f"[{now_str}] 정규장 외 시간 — 대기 중...")
        else:
            scan_start = time.time()
            print(f"\n[{now_str}] 정규장 스캔 시작")
            run_scan()
            # [v32] 스캔 소요시간 로깅 — 중복 get_bars 제거 효과 확인용.
            # 감시가 스레드로 분리됐으므로 이 값이 커져도 손절 반응 속도와는 무관.
            print(f"  [스캔 소요] {time.time() - scan_start:.1f}초")

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
