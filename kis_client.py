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

# 미국 주간거래 전용 tr_id (⚠️ 모의투자 지원 여부 및 접두어는 KIS 문서 재확인 필요)
TR_ID_DAY_BUY  = "TTTS6036U"   # 미국 주간거래 매수
TR_ID_DAY_SELL = "TTTS6037U"   # 미국 주간거래 매도

_token_cache = {"access_token": None, "expires_at": 0}


def get_access_token() -> str:
    """접근토큰 발급/캐싱. 만료 5분 전에 자동 갱신."""
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["access_token"]

    _throttle()
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


# ── 초당 호출수 제한(Rate Limit) 대응 ──
# KIS는 계정 등급에 따라 초당 허용 호출 수가 정해져 있어, 짧은 시간에
# 여러 함수(잔고조회→매수가능조회→주문 등)가 연달아 호출되면 거절될 수 있다.
_last_request_ts = 0.0
_MIN_REQUEST_INTERVAL = 0.5   # 최소 호출 간격(초). 계속 걸리면 값을 늘릴 것.


def _throttle():
    """직전 KIS 호출 이후 최소 간격을 보장. 매 요청 직전에 호출."""
    global _last_request_ts
    elapsed = time.time() - _last_request_ts
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_ts = time.time()


def _is_rate_limited(result: dict) -> bool:
    msg = str(result.get("msg1", ""))
    return "초당" in msg or result.get("rt_cd") == "1" and "거래건수" in msg


def _request_with_retry(method: str, url: str, retries: int = 3, backoff: float = 1.0, **kwargs) -> dict:
    """
    KIS API 호출을 스로틀 + 초당 한도 초과 시 자동 재시도로 감싼 공통 래퍼.
    method: "get" 또는 "post"
    """
    for attempt in range(retries + 1):
        _throttle()
        resp = requests.request(method, url, timeout=kwargs.pop("timeout", 10), **kwargs)
        try:
            data = resp.json()
        except ValueError:
            return {"rt_cd": "-1", "msg1": f"응답 파싱 실패: {resp.text[:200]}"}

        if _is_rate_limited(data) and attempt < retries:
            wait = backoff * (attempt + 1)
            print(f"[KIS 호출제한] {wait:.1f}초 대기 후 재시도 ({attempt + 1}/{retries})")
            time.sleep(wait)
            continue
        return data
    return data


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

    result = _request_with_retry(
        "post",
        f"{BASE_URL}/uapi/overseas-stock/v1/trading/order",
        headers=_headers(tr_id),
        json=body,
        timeout=10,
    )

    if result.get("rt_cd") != "0":
        print(f"[KIS 주문 오류] {symbol} {side} → {result}")
    else:
        print(f"[KIS 주문 성공] {symbol} {side} {qty}주 @ {price} → {result.get('msg1')}")

    return result


def place_day_order(symbol: str, qty: int, price: float, side: str, exchange: str = "BAQ") -> dict:
    """
    미국 주간거래 전용 주문.
    ⚠️ 전용 API(TTTS6036U/6037U) + 전용 거래소코드 사용:
       BAY(뉴욕), BAQ(나스닥), BAA(아멕스) - 종목 상장 거래소에 맞게 지정.
    ⚠️ 주간거래는 서비스 중단 이력이 있으니 KIS 최신 공지 확인 필요.
    ⚠️ 모의투자 지원 여부도 KIS 문서로 확인할 것 (미지원일 수 있음).
    side: "buy" 또는 "sell", ORD_DVSN은 "00"(지정가)만 가능.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side는 'buy' 또는 'sell'이어야 합니다")

    tr_id = TR_ID_DAY_BUY if side == "buy" else TR_ID_DAY_SELL

    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": exchange,   # BAY / BAQ / BAA
        "PDNO": symbol,
        "ORD_QTY": str(qty),
        "OVRS_ORD_UNPR": str(price),
        "ORD_SVR_DVSN_CD": "0",
        "ORD_DVSN": "00",   # 지정가만 가능
    }

    _throttle()
    resp = requests.post(
        f"{BASE_URL}/uapi/overseas-stock/v1/trading/daytime-order",
        headers=_headers(tr_id),
        json=body,
        timeout=10,
    )
    result = resp.json()

    if result.get("rt_cd") != "0":
        print(f"[KIS 주간거래 오류] {symbol} {side} → {result}")
    else:
        print(f"[KIS 주간거래 성공] {symbol} {side} {qty}주 @ {price} → {result.get('msg1')}")

    return result


# 매수가능금액조회 tr_id (⚠️ 파라미터/필드명 KIS 최신 문서로 재확인 권장)
TR_ID_BUYABLE = "VTTS3007R" if USE_MOCK else "TTTS3007R"


def get_buyable_amount(symbol: str, price: float) -> float:
    """
    해외주식 매수가능금액 조회.
    ⚠️ tr_id/응답 필드명은 확인이 완전하지 않으니, 실사용 전 KIS 개발자센터
       문서나 챗봇으로 "해외주식 매수가능금액조회" API를 재확인할 것.
    실패 시 0.0을 반환하여 상위 로직이 안전하게(매수 안 함) 처리하도록 함.
    """
    exchange = get_exchange_code(symbol)
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": exchange,
        "ITEM_CD": symbol,
        "OVRS_ORD_UNPR": str(price),
    }
    try:
        data = _request_with_retry(
            "get",
            f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-psamount",
            headers=_headers(TR_ID_BUYABLE),
            params=params,
            timeout=10,
        )
        if data.get("rt_cd") != "0":
            print(f"[KIS 매수가능금액 조회 오류] {data}")
            return 0.0
        output = data.get("output", {})
        # ⚠️ 필드명 추정치: 실제 응답 구조 확인 후 정확한 키로 교체 필요
        amt = output.get("ord_psbl_frcr_amt") or output.get("frcr_ord_psbl_amt1") or 0
        return float(amt)
    except Exception as e:
        print(f"[KIS 매수가능금액 예외] {e}")
        return 0.0


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
    _throttle()
    resp = requests.get(
        f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance",
        headers=_headers(TR_ID_BALANCE),
        params=params,
        timeout=10,
    )
    return resp.json()


# ══════════════════════════════════════════
# 국내주식 (KRX) — 해외주식과 tr_id/파라미터가 다름
# ⚠️ 아래 tr_id는 국내주식 API에서 널리 알려진 값이지만,
#    KIS 개발자센터 최신 문서로 한 번은 재확인 권장.
# ══════════════════════════════════════════

TR_ID_KR_ORDER_BUY  = "VTTC0802U" if USE_MOCK else "TTTC0802U"  # 국내주식 매수 주문
TR_ID_KR_ORDER_SELL = "VTTC0801U" if USE_MOCK else "TTTC0801U"  # 국내주식 매도 주문
TR_ID_KR_BALANCE    = "VTTC8434R" if USE_MOCK else "TTTC8434R"  # 국내주식 잔고조회
TR_ID_KR_BUYABLE    = "VTTC8908R" if USE_MOCK else "TTTC8908R"  # 국내주식 매수가능조회


def get_krx_tick_size(price: float) -> int:
    """
    KRX 호가단위(틱사이즈). 가격대별로 다르며, 이 단위에 안 맞으면 주문 자체가 거부될 수 있다.
    ⚠️ 2023년 이후 개편된 기준. 최신 규정과 다를 수 있으니 필요 시 재확인.
    """
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


def round_to_krx_tick(price: float, side: str) -> int:
    """
    가격을 KRX 호가단위에 맞춰 반올림.
    매수는 체결 확률을 위해 올림, 매도는 내림 방향으로 맞춘다.
    """
    tick = get_krx_tick_size(price)
    if side == "buy":
        return int((price // tick + 1) * tick) if price % tick != 0 else int(price)
    else:
        return int((price // tick) * tick)


def place_domestic_order(code: str, qty: int, price: int, side: str, buffer_pct: float = 1.0) -> dict:
    """
    국내주식 주문 (현금매수/매도).
    code: 6자리 종목코드 (예: "005930" 삼성전자)
    price: 원 단위 기준가 (스냅샷 관측가). 아래에서 buffer_pct만큼 유리하지 않은
           방향으로(매수는 올려서, 매도는 내려서) 조정 후 KRX 호가단위로 반올림해
           지정가를 넣는다 — 짧은 시간에 가격이 움직여도 체결 확률을 높이기 위함.
    side: "buy" 또는 "sell"
    buffer_pct: 가격 조정 폭(%). 기본 1.0% — 필요 시 조정.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side는 'buy' 또는 'sell'이어야 합니다")

    # ── 체결률 개선을 위한 가격 버퍼 ──
    if side == "buy":
        adj_price = price * (1 + buffer_pct / 100)
    else:
        adj_price = price * (1 - buffer_pct / 100)
    order_price = round_to_krx_tick(adj_price, side)

    tr_id = TR_ID_KR_ORDER_BUY if side == "buy" else TR_ID_KR_ORDER_SELL
    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": code,
        "ORD_DVSN": "00",   # 00: 지정가
        "ORD_QTY": str(qty),
        "ORD_UNPR": str(order_price),
    }
    _throttle()
    resp = requests.post(
        f"{BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
        headers=_headers(tr_id),
        json=body,
        timeout=10,
    )
    result = resp.json()
    if result.get("rt_cd") != "0":
        print(f"[KIS 국내주문 오류] {code} {side} @ {order_price}원(관측가 {price}) → {result}")
    else:
        print(f"[KIS 국내주문 성공] {code} {side} {qty}주 @ {order_price}원(관측가 {price}) → {result.get('msg1')}")
    return result


def get_domestic_balance() -> dict:
    """국내주식 잔고조회."""
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    _throttle()
    resp = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
        headers=_headers(TR_ID_KR_BALANCE),
        params=params,
        timeout=10,
    )
    return resp.json()


def get_domestic_buyable_amount(code: str, price: int) -> float:
    """
    국내주식 매수가능금액 조회.
    ⚠️ 응답 필드명은 실행 후 실제 응답 구조로 재확인 필요 (추정치 사용 중).
    실패 시 0.0 반환 → 상위 로직이 안전하게 매수 보류하도록.
    """
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": code,
        "ORD_UNPR": str(price),
        "ORD_DVSN": "00",
        "CMA_EVLU_AMT_ICLD_YN": "N",
        "OVRS_ICLD_YN": "N",
    }
    try:
        _throttle()
        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            headers=_headers(TR_ID_KR_BUYABLE),
            params=params,
            timeout=10,
        )
        data = resp.json()
        if data.get("rt_cd") != "0":
            print(f"[KIS 국내 매수가능금액 오류] {data}")
            return 0.0
        output = data.get("output", {})
        amt = output.get("ord_psbl_cash") or 0
        return float(amt)
    except Exception as e:
        print(f"[KIS 국내 매수가능금액 예외] {e}")
        return 0.0


# ══════════════════════════════════════════
# 국내주식 시세/스크리닝 (시세 API는 tr_id가 모의/실전 구분 없이 동일)
# ⚠️ 응답 필드명은 실제 실행 결과로 재확인 필요 (아래는 통상 알려진 명칭 기준)
# ══════════════════════════════════════════

TR_ID_KR_RANKING     = "FHPST01700000"   # 국내주식 등락률 순위
TR_ID_KR_MINUTE_BAR  = "FHKST03010200"   # 국내주식 당일 분봉 조회
TR_ID_KR_CURRENT_PX  = "FHKST01010100"   # 국내주식 현재가 시세


def get_domestic_ranking(top: int = 30) -> list:
    """
    국내주식 등락률 순위 조회 (상승률 상위 top개).
    반환: [{"code": 종목코드, "name": 종목명, "price": 현재가, "change_pct": 등락률}, ...]
    ⚠️ 응답 필드명(stck_prpr, prdy_ctrt 등)은 KIS 표준 관례 기준 — 실행 후 확인 필요.
    """
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",     # J: 코스피+코스닥 통합
        "FID_COND_SCR_DIV_CODE": "20170",
        "FID_INPUT_ISCD": "0000",
        "FID_DIV_CLS_CODE": "0",           # 0: 상승률 순
        "FID_RANK_SORT_CLS_CODE": "0",
        "FID_INPUT_CNT_1": "0",
        "FID_PRC_CLS_CODE": "0",
        "FID_INPUT_PRICE_1": "",
        "FID_INPUT_PRICE_2": "",
        "FID_VOL_CNT": "",
        "FID_TRGT_CLS_CODE": "0",
        "FID_TRGT_EXLS_CLS_CODE": "0",
        "FID_INPUT_DATE_1": "",
        "FID_RSFL_RATE1": "",   # [버그수정] 등락비율 하한 - 누락 시 OPSQ20001 오류로 조회 전체 실패
        "FID_RSFL_RATE2": "",   # 등락비율 상한
    }
    try:
        _throttle()
        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/ranking/fluctuation",
            headers=_headers(TR_ID_KR_RANKING),
            params=params,
            timeout=10,
        )
        data = resp.json()
        if data.get("rt_cd") != "0":
            print(f"[KIS 국내 순위조회 오류] {data}")
            return []
        rows = data.get("output", [])[:top]
        result = []
        for r in rows:
            try:
                result.append({
                    "code": r.get("stck_shrn_iscd") or r.get("mksc_shrn_iscd"),
                    "name": r.get("hts_kor_isnm"),
                    "price": float(r.get("stck_prpr", 0)),
                    "change_pct": float(r.get("prdy_ctrt", 0)),
                })
            except (TypeError, ValueError):
                continue
        return result
    except Exception as e:
        print(f"[KIS 국내 순위조회 예외] {e}")
        return []


def get_domestic_minute_bars(code: str, count: int = 30) -> list:
    """
    국내주식 당일 분봉 조회.
    반환: Alpaca 봉 형식과 호환되도록 [{"t","o","h","l","c","v"}, ...] (시간순 오름차순)
    ⚠️ 응답 필드명(stck_cntg_hour, stck_prpr 등) 재확인 필요.
    """
    params = {
        "FID_ETC_CLS_CODE": "",
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_HOUR_1": "",
        "FID_PW_DATA_INCU_YN": "Y",
    }
    try:
        _throttle()
        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            headers=_headers(TR_ID_KR_MINUTE_BAR),
            params=params,
            timeout=10,
        )
        data = resp.json()
        if data.get("rt_cd") != "0":
            print(f"[KIS 국내 분봉조회 오류] {code} {data}")
            return []
        rows = data.get("output2", [])[:count]
        bars = []
        for r in reversed(rows):   # KIS는 최신순 반환 → 오름차순으로 뒤집기
            try:
                bars.append({
                    "t": r.get("stck_cntg_hour"),
                    "o": float(r.get("stck_oprc", 0)),
                    "h": float(r.get("stck_hgpr", 0)),
                    "l": float(r.get("stck_lwpr", 0)),
                    "c": float(r.get("stck_prpr", 0)),
                    "v": float(r.get("cntg_vol", 0)),
                })
            except (TypeError, ValueError):
                continue
        return bars
    except Exception as e:
        print(f"[KIS 국내 분봉조회 예외] {code} {e}")
        return []


def get_domestic_current_price(code: str) -> float:
    """국내주식 현재가 단건 조회. 실패 시 0.0."""
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    try:
        _throttle()
        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=_headers(TR_ID_KR_CURRENT_PX),
            params=params,
            timeout=10,
        )
        data = resp.json()
        if data.get("rt_cd") != "0":
            return 0.0
        return float(data.get("output", {}).get("stck_prpr", 0))
    except Exception:
        return 0.0


if __name__ == "__main__":
    # 단독 실행 시 토큰 발급 + 해외/국내 잔고 조회 테스트
    print(f"[모드] {'모의투자' if USE_MOCK else '⚠️ 실전투자'}")
    token = get_access_token()
    print(f"[토큰] {token[:20]}...")

    print("\n── 해외주식 잔고 ──")
    print(json.dumps(get_overseas_balance(), indent=2, ensure_ascii=False))

    print("\n── 국내주식 잔고 ──")
    print(json.dumps(get_domestic_balance(), indent=2, ensure_ascii=False))

    print("\n── 국내주식 등락률 순위 (상위 5) ──")
    print(json.dumps(get_domestic_ranking(top=5), indent=2, ensure_ascii=False))


