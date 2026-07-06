"""
KIS 모의투자 매수 테스트
────────────────────────
AAPL 1주를 지정가로 매수 주문 넣고, 잔고를 다시 조회해서 반영되는지 확인.

⚠️ 반드시 KIS_USE_MOCK=true 상태에서만 실행할 것.
⚠️ PRICE 값은 실제 현재가와 너무 동떨어지면 체결이 안 될 수 있으니
   실행 전 대략적인 현재가로 맞춰서 조정할 것.
"""

import os
import time
import kis_client as kis

SYMBOL = "DE"
QTY = 1
PRICE = 470.00   # ⚠️ 실행 전 대략적인 현재가로 수정하세요

def main():
    if not kis.USE_MOCK:
        print("⚠️ 안전을 위해 KIS_USE_MOCK=true 상태에서만 실행 가능합니다. 중단합니다.")
        return

    print(f"[테스트] {SYMBOL} {QTY}주 @ ${PRICE} 매수 주문 시도 (모의투자)")
    result = kis.place_order(SYMBOL, QTY, PRICE, "buy")
    print("── 주문 응답 ──")
    print(result)

    print("\n주문 반영 대기 중 (5초)...")
    time.sleep(5)

    print("\n── 잔고 재조회 ──")
    balance = kis.get_overseas_balance()
    print(balance)


if __name__ == "__main__":
    main()
