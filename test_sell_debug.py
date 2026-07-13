"""매도 주문 디버그: 요청/응답 전문 출력"""
import json
import kis_client as kis

# 현재 보유 종목 확인
bal = kis.get_overseas_balance() if hasattr(kis, 'get_overseas_balance') else None
print("=== 잔고 ===")
print(json.dumps(bal, ensure_ascii=False, indent=2) if bal else "잔고조회 함수명 확인 필요")

# 보유 종목 중 하나로 1주 매도 테스트 (심볼/가격 직접 수정)
SYMBOL = "AMPG"   # 보유 중인 종목으로 변경
PRICE  = 6.50     # 현재가 근처로 변경
result = kis.place_order(SYMBOL, 1, PRICE, "sell", session="regular")
print("=== 매도 응답 전문 ===")
print(json.dumps(result, ensure_ascii=False, indent=2))
