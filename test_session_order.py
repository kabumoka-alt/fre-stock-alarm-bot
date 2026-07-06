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
import os
import requests
import kis_client as kis
import session_utils as su

SYMBOL = "DE"
QTY = 1
PRICE = 470.00   # ⚠️ 실행 전 DE 대략 현재가로 수정

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def notify(msg: str):
    """텔레그램 알림 전송 (실패해도 스크립트는 계속)."""
    print(msg)
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=5,
        )
    except Exception as e:
        print(f"[텔레그램 전송 실패] {e}")


def main():
    if not kis.USE_MOCK:
        print("⚠️ KIS_USE_MOCK=true 상태에서만 실행 가능. 중단합니다.")
        return

    session = su.current_session()
    et_str = su.get_et_now().strftime('%Y-%m-%d %H:%M %Z')

    if session == "closed":
        print(f"[{et_str}] 장 마감 시간대(closed). 주문 없이 종료.")
        return

    result = kis.place_order(SYMBOL, QTY, PRICE, "buy", session=session)
    rt_cd = result.get("rt_cd")
    msg1 = result.get("msg1", "")

    if rt_cd == "0":
        notify(f"✅ [{session}] {SYMBOL} {QTY}주 @ ${PRICE} 매수 성공\n{et_str}\n{msg1}")
    else:
        notify(f"⚠️ [{session}] {SYMBOL} 매수 실패 (rt_cd={rt_cd})\n{et_str}\n{msg1}")

    time.sleep(5)
    balance = kis.get_overseas_balance()
    print("── 잔고 재조회 ──")
    print(balance)


if __name__ == "__main__":
    main()
