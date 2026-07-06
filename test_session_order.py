"""
세션 자동감지 매수 테스트 - Deere(DE) 1주
──────────────────────────────────────
현재 세션(프리/정규/애프터)을 자동 감지해서 그에 맞는 주문을 넣는다.
cron으로 각 세션 시간대에 실행되도록 걸어두면 세션별 테스트가 가능하다.

⚠️ 모의투자(KIS_USE_MOCK=true)에서만 실행.
⚠️ PRICE는 실행 전 DE 대략 현재가로 조정할 것 (지정가라 동떨어지면 미체결).
⚠️ 주간거래는 전용 API/모의지원 여부 불확실 → 실행 시 별도 확인 필요.
"""

import time
import kis_client as kis
import session_utils as su

SYMBOL = "DE"
QTY = 1
PRICE = 470.00   # ⚠️ 실행 전 DE 대략 현재가로 수정


def main():
    if not kis.USE_MOCK:
        print("⚠️ KIS_USE_MOCK=true 상태에서만 실행 가능. 중단합니다.")
        return

    session = su.current_session()
    print(f"[현재 세션] {session}")
    print(f"[ET 시각] {su.get_et_now().strftime('%Y-%m-%d %H:%M %Z')}")

    if session == "closed":
        print("[안내] 지금은 장 마감 시간대(closed)입니다. 주문을 넣지 않습니다.")
        return

    print(f"[테스트] {SYMBOL} {QTY}주 @ ${PRICE} 매수 시도 (세션: {session})")
    result = kis.place_order(SYMBOL, QTY, PRICE, "buy", session=session)
    print("── 주문 응답 ──")
    print(result)

    print("\n주문 반영 대기 (5초)...")
    time.sleep(5)

    print("\n── 잔고 재조회 ──")
    print(kis.get_overseas_balance())


if __name__ == "__main__":
    main()
