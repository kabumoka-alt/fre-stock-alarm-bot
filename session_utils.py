"""
프리마켓/애프터마켓 세션 판단 + KIS 확장세션 주문 지원
──────────────────────────────────────────────────
⚠️⚠️⚠️ 반드시 읽을 것 ⚠️⚠️⚠️
아래 EXTENDED_HOURS_ORD_DVSN 값은 확정된 값이 아닙니다.
KIS 개발자센터(apiportal.koreainvestment.com) 문서에서
"해외주식 프리마켓/애프터마켓 주문" 또는 "미국 주간거래" 관련
최신 가이드를 반드시 확인하고 정확한 ORD_DVSN 값으로 교체한 뒤 사용하세요.
확인 전에는 모의투자에서도 프리/애프터 주문을 실행하지 마세요.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

ET_TZ = ZoneInfo("America/New_York")


def get_et_now() -> datetime:
    return datetime.now(ET_TZ)


def _et_minutes(now_et: datetime) -> int:
    return now_et.hour * 60 + now_et.minute


def is_premarket_session() -> bool:
    """프리마켓: 04:00 ~ 09:30 ET"""
    now_et = get_et_now()
    if now_et.weekday() >= 5:
        return False
    et_min = _et_minutes(now_et)
    return (4 * 60) <= et_min < (9 * 60 + 30)


def is_afterhours_session() -> bool:
    """애프터마켓: 16:00 ~ 20:00 ET"""
    now_et = get_et_now()
    if now_et.weekday() >= 5:
        return False
    et_min = _et_minutes(now_et)
    return (16 * 60) <= et_min < (20 * 60)


def current_session() -> str:
    """
    현재 세션 이름 반환: "premarket" | "regular" | "afterhours" | "closed"
    ⚠️ 정규장 판단은 stock_pump_bot.py의 is_regular_session()과
       is_market_holiday_or_closed()를 함께 써서 공휴일까지 반영할 것.
       여기서는 시간대 구분 목적의 간단 버전만 제공.
    """
    now_et = get_et_now()
    if now_et.weekday() >= 5:
        return "closed"
    et_min = _et_minutes(now_et)
    if (9 * 60 + 30) <= et_min < (16 * 60):
        return "regular"
    if is_premarket_session():
        return "premarket"
    if is_afterhours_session():
        return "afterhours"
    return "closed"


# ── KIS 주문구분코드 (KIS 공식 문서 확인 완료) ──
# 프리마켓/정규장/애프터마켓 모두 일반 해외주식 주문 API(TTTT1002U/TTTT1006U) 사용,
# ORD_DVSN은 전부 "00"(지정가). 프리/애프터는 지정가만 가능.
# ※ 미국 주간거래는 전용 API(TTTS6036U/6037U) + 전용 거래소코드(BAY/BAQ/BAA)라
#    여기가 아니라 별도 함수로 처리해야 함 (미구현).
EXTENDED_HOURS_ORD_DVSN = {
    "regular":    "00",    # 지정가
    "premarket":  "00",    # 지정가 (프리마켓, 일반 주문 API 사용)
    "afterhours": "00",    # 지정가 (애프터마켓, 일반 주문 API 사용, 지정가만 가능)
}


def get_ord_dvsn_for_session(session: str) -> str:
    code = EXTENDED_HOURS_ORD_DVSN.get(session)
    if code is None:
        raise ValueError(
            f"알 수 없는 세션 '{session}'. "
            f"'regular' / 'premarket' / 'afterhours' 중 하나여야 합니다. "
            f"(미국 주간거래는 전용 API를 써야 하므로 여기서 처리하지 않습니다.)"
        )
    return code


if __name__ == "__main__":
    session = current_session()
    print(f"[현재 세션] {session}")
    print(f"[ET 시각] {get_et_now().strftime('%Y-%m-%d %H:%M %Z')}")
    if session == "closed":
        print("[안내] 현재는 장 마감 시간대(closed)라 주문 가능한 세션이 아닙니다.")
    else:
        print(f"[ORD_DVSN] {get_ord_dvsn_for_session(session)}")
