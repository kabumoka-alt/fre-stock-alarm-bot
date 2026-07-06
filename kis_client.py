"""
한국투자증권(KIS) Open API 연동 모듈
────────────────────────────────────
⚠️ 반드시 모의투자(MOCK)로 먼저 충분히 검증 후 실전 전환할 것.
⚠️ tr_id 값은 KIS 개발자센터 최신 문서로 재확인 필요 (API 개편 시 변경될 수 있음).

필요 환경변수:
  KIS_APP_KEY       - 발급받은 앱키
  KIS_APP_SECRET    - 발급받은 앱시크릿
  KIS_ACCOUNT_NO    - 계좌번호 전체 (예: 12345678-01)
  KIS_USE_MOCK      - "true"면 모의투자 서버 사용, "false"면 실전 서버 (기본: true)
"""

import os
import time
import json
import requests

KIS_APP_KEY    = os.environ["KIS_APP_KEY"]
KIS_APP_SECRET = os.environ["KIS_APP_SECRET"]
KIS_ACCOUNT_NO = os.environ["KIS_ACCOUNT_NO"]   # "12345678-01" 형태
USE_MOCK       = os.environ.get("KIS_USE_MOCK", "true").lower() == "true"

CANO         = KIS_ACCOUNT_NO.split("-")[0]          # 계좌번호 앞 8자리
ACNT_PRDT_CD = KIS_ACCOUNT_NO.split("-")[1]           # 상품코드 뒤 2자리

BASE_URL = "https://openapivts.koreainvestment.com:29443" if USE_MOCK \
    else "https://openapi.koreainvestment.com:9443"

# ── tr_id (⚠️ KIS 최신 문서로 재확인 필수) ──
TR_ID_ORDER_BUY  = "VTTT1002U" if USE_MOCK else "TTTT1002U"   # 해외주식 매수 주문
TR_ID_ORDER_SELL = "VTTT1006U" if USE_MOCK else "TTTT1006U"   # 해외주식 매도 주문
TR_ID_BALANCE    = "VTTS3012R" if USE_MOCK else "TTTS3012R"   # 해외주식 잔고조회

_token_cache = {"access_token": None, "expires_at": 0}


def get_access_token() -> str:
    """접근토큰 발급/캐싱. 만료 5분 전에 자동 갱신."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["access_token"]

    resp = requests.post(
        f"{BASE_URL}/oauth2/tokenP",
        json={
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 86400))
    print(f"[KIS] 토큰 발급 완료 (만료: {data.get('expires_in')}초 후)")
    return _token_cache["access_token"]


def _headers(tr_id: str) -> dict:
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {get_access_token()}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }


def get_exchange_code(symbol: str) -> str:
    """
    종목의 해외거래소코드 반환.
    ⚠️ 간단화를 위해 기본값만 처리. 정확한 매핑은 Alpaca 자산 정보(exchange)를
    조회해서 NASDAQ→NASD, NYSE→NYSE, AMEX→AMEX 로 변환하는 로직을 추가할 것.
    """
    return "NASD"   # TODO: 실제 거래소별 매핑 필요


def place_order(symbol: str, qty: int, price: float, side: str, session: str = "regular") -> dict:
    """
    해외주식 주문.
    side: "buy" 또는 "sell"
    price: 지정가 (시장가 주문 시 KIS 정책에 맞는 별도 처리 필요)
    session: "regular" | "premarket" | "afterhours"
             ⚠️ premarket/afterhours는 session_utils.py의
             EXTENDED_HOURS_ORD_DVSN 값을 먼저 채워넣어야 사용 가능합니다.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side는 'buy' 또는 'sell'이어야 합니다")

    from session_utils import get_ord_dvsn_for_session
    ord_dvsn = get_ord_dvsn_for_session(session)

    tr_id = TR_ID_ORDER_BUY if side == "buy" else TR_ID_ORDER_SELL
    exchange = get_exchange_code(symbol)

    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": exchange,
        "PDNO": symbol,
        "ORD_QTY": str(qty),
        "OVRS_ORD_UNPR": str(price),
        "ORD_SVR_DVSN_CD": "0",
        "ORD_DVSN": ord_dvsn,
    }

    resp = requests.post(
        f"{BASE_URL}/uapi/overseas-stock/v1/trading/order",
        headers=_headers(tr_id),
        json=body,
        timeout=10,
    )
    result = resp.json()

    if result.get("rt_cd") != "0":
        print(f"[KIS 주문 오류] {symbol} {side} → {result}")
    else:
        print(f"[KIS 주문 성공] {symbol} {side} {qty}주 @ {price} → {result.get('msg1')}")

    return result


def get_overseas_balance() -> dict:
    """해외주식 잔고 조회."""
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": "NASD",
        "TR_CRCY_CD": "USD",
        "CTX_AREA_FK200": "",
        "CTX_AREA_NK200": "",
    }
    resp = requests.get(
        f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance",
        headers=_headers(TR_ID_BALANCE),
        params=params,
        timeout=10,
    )
    return resp.json()


if __name__ == "__main__":
    # 단독 실행 시 토큰 발급 + 잔고 조회 테스트
    print(f"[모드] {'모의투자' if USE_MOCK else '⚠️ 실전투자'}")
    token = get_access_token()
    print(f"[토큰] {token[:20]}...")
    balance = get_overseas_balance()
    print(json.dumps(balance, indent=2, ensure_ascii=False))
