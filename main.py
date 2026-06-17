import os
import requests
import time
from datetime import datetime, timezone

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")

SCAN_INTERVAL = 60
ALERT_COOLDOWN = 600


def is_market_open():
   """미국 정규장: 09:30 ~ 16:00 ET = 13:30 ~ 20:00 UTC"""
   now = datetime.now(timezone.utc)
   h, m = now.hour, now.minute
   total_min = h * 60 + m
   return 13 * 60 + 30 <= total_min <= 20 * 60


def send(msg):
   try:
       requests.get(
           f"https://api.telegram.org/bot{TOKEN}/sendMessage",
           params={"chat_id": CHAT_ID, "text": msg},
           timeout=5
       )
   except Exception as e:
       print(f"텔레그램 오류: {e}")


def get_gainers():
   """Yahoo Finance — 정규장 상승 종목 전체 수집"""
   all_quotes = []
   offset = 0
   batch = 250
   headers = {"User-Agent": "Mozilla/5.0"}

   while True:
       try:
           r = requests.get(
               "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
               params={
                   "scrIds": "day_gainers",
                   "count":  batch,
                   "offset": offset,
                   "lang":   "en-US",
                   "region": "US"
               },
               headers=headers,
               timeout=10
           ).json()

           quotes = (
               r.get("finance", {})
                .get("result", [{}])[0]
                .get("quotes", [])
           )

           if not quotes:
               break

           for q in quotes:
               sym    = q.get("symbol", "")
               price  = q.get("regularMarketPrice")
               change = q.get("regularMarketChangePercent")

               # ✅ 정규장 거래 중인 종목만
               state  = q.get("marketState", "")
               if state != "REGULAR":
                   continue

               if sym and price and change is not None:
                   all_quotes.append((sym, price, change))

           print(f"  Yahoo: offset={offset} → {len(quotes)}개 수집 (누적 {len(all_quotes)}개)")

           if len(quotes) < batch:
               break

           offset += batch
           time.sleep(0.5)

       except Exception as e:
           print(f"  Yahoo 오류: {e}")
           break

   all_quotes.sort(key=lambda x: x[2], reverse=True)
   return all_quotes


def get_rsi_cross(symbol):
   """5분봉 RSI(14) 직전값, 현재값 반환"""
   try:
       to_ts   = int(time.time())
       from_ts = to_ts - (60 * 5 * 60)

       r = requests.get(
           "https://finnhub.io/api/v1/indicator",
           params={
               "symbol":     symbol,
               "resolution": "5",
               "from":       from_ts,
               "to":         to_ts,
               "indicator":  "rsi",
               "timeperiod": 14,
               "token":      FINNHUB_KEY
           },
           timeout=5
       ).json()

       values = r.get("rsi", [])
       if len(values) < 2:
           return None, None

       return values[-2], values[-1]

   except:
       return None, None


alert_times: dict[str, float] = {}

print("🚀 RSI 50 돌파 스캐너 시작")
print("   조건: 정규장 상승률 상위 50% + 5분봉 RSI 50 상향 돌파\n")

while True:
   try:
       if not is_market_open():
           now = datetime.now(timezone.utc)
           print(f"⛔ 정규장 외 시간 ({now.strftime('%H:%M')} UTC) — 5분 대기")
           time.sleep(300)
           continue

       print(f"[{datetime.now().strftime('%H:%M:%S')}] ── 스캔 시작 ──")

       # 1단계: 정규장 상승 종목 수집
       all_gainers = get_gainers()
       if not all_gainers:
           print("⚠️ 종목 로드 실패")
           time.sleep(60)
           continue

       # 2단계: 상위 50% 추출
       top_half = all_gainers[:max(1, len(all_gainers) // 2)]
       print(f"\n  정규장 상승 종목: {len(all_gainers)}개 → 상위 50%: {len(top_half)}개")
       print(f"  상위 5개:")
       for sym, p, c in top_half[:5]:
           print(f"    {sym}: +{c:.2f}%  ${p:.2f}")
       print()

       now = time.time()

       # 3단계: RSI 50 돌파 체크
       for sym, price, change in top_half:
           time.sleep(0.7)

           rsi_prev, rsi_curr = get_rsi_cross(sym)

           if rsi_prev is None or rsi_curr is None:
               continue

           crossed = rsi_prev < 50 and rsi_curr >= 50

           print(f"  {sym:6s} | +{change:.2f}% | RSI {rsi_prev:.1f} → {rsi_curr:.1f}"
                 f"{' 🔥 돌파!' if crossed else ''}")

           if not crossed:
               continue

           if now - alert_times.get(sym, 0) < ALERT_COOLDOWN:
               print(f"    ⏭ 쿨다운 중")
               continue

           msg = (
               f"🚨 RSI 50 돌파 (정규장)\n"
               f"종목: {sym}\n"
               f"현재가: ${price:.2f}\n"
               f"당일 등락: +{change:.2f}%\n"
               f"RSI: {rsi_prev:.1f} → {rsi_curr:.1f} ✅\n"
               f"⏰ {datetime.now().strftime('%H:%M:%S')} UTC"
           )
           print(msg)
           send(msg)
           alert_times[sym] = now

       print(f"\n✅ 스캔 완료 — {SCAN_INTERVAL}초 후 재스캔\n")
       time.sleep(SCAN_INTERVAL)

   except Exception as e:
       print(f"루프 오류: {e}")
       time.sleep(15)
