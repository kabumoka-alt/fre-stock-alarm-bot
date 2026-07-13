[1mdiff --git a/surge_scalper_us.py b/surge_scalper_us.py[m
[1mindex c3438bf..0900979 100644[m
[1m--- a/surge_scalper_us.py[m
[1m+++ b/surge_scalper_us.py[m
[36m@@ -55,6 +55,7 @@[m [mTIME_EXIT_MIN       = 30           # 진입 후 N분 내 미도달 시 청산[m
 [m
 SCREEN_INTERVAL     = 60           # 스크리닝 주기(초)[m
 MONITOR_INTERVAL    = 10           # 보유 종목 감시 주기(초)[m
[32m+[m[32mPRICE_STALE_SEC     = 90           # 감시 가격이 이보다 오래되면 STALE 경고[m
 MOVERS_TOP          = 50           # Alpaca 급등 상위 몇 개까지 볼지[m
 MOST_ACTIVES_TOP    = 100          # 거래량 상위 몇 개를 초입 후보 풀로 볼지[m
 RECENT_MOVE_MIN     = 2.0          # 최근 5분 최소 상승폭(%) — "지금 움직이는 중"만 통과[m
[36m@@ -83,6 +84,7 @@[m [mTELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")[m
 [m
 STATE_FILE = os.path.expanduser("~/surge_scalper_us_positions.json")[m
 BLACKLIST  = set()[m
[32m+[m[32mNO_BARS    = set()   # 분봉 없는 종목(워런트/특수) — 세션 내 재조회 스킵[m
 [m
 logging.basicConfig([m
     level=logging.INFO,[m
[36m@@ -178,7 +180,7 @@[m [mdef get_recent_bars(symbol: str, limit: int = 6):[m
             timeout=8,[m
         )[m
         r.raise_for_status()[m
[31m-        bars = r.json().get("bars", [])[m
[32m+[m[32m        bars = r.json().get("bars") or [][m
         return [{"c": float(b["c"]), "v": float(b["v"])} for b in bars][m
     except Exception as e:[m
         log.warning("분봉 조회 실패 %s: %s", symbol, e)[m
[36m@@ -250,15 +252,52 @@[m [mdef get_surge_candidates():[m
     return cands[m
 [m
 [m
[31m-def get_last_price(symbol: str) -> float:[m
[31m-    """최신 체결가 (감시용)"""[m
[32m+[m[32m_EXCD_MAP = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}[m
[32m+[m
[32m+[m
[32m+[m[32mdef _kis_price(symbol: str, exchange: str) -> float:[m
[32m+[m[32m    """한투 해외주식 실시간 현재가. 실패 시 0.0. (전체시장 + 실시간)"""[m
[32m+[m[32m    try:[m
[32m+[m[32m        excd = _EXCD_MAP.get(exchange, "NAS")[m
[32m+[m[32m        kis._throttle()[m
[32m+[m[32m        r = requests.get([m
[32m+[m[32m            f"{kis.BASE_URL}/uapi/overseas-price/v1/quotations/price",[m
[32m+[m[32m            headers=kis._headers("HHDFS00000300"),[m
[32m+[m[32m            params={"AUTH": "", "EXCD": excd, "SYMB": symbol}, timeout=8)[m
[32m+[m[32m        r.raise_for_status()[m
[32m+[m[32m        d = r.json()[m
[32m+[m[32m        if d.get("rt_cd") == "0":[m
[32m+[m[32m            return float(d.get("output", {}).get("last", 0) or 0)[m
[32m+[m[32m    except Exception as e:[m
[32m+[m[32m        log.warning("한투 시세 실패 %s: %s", symbol, e)[m
[32m+[m[32m    return 0.0[m
[32m+[m
[32m+[m
[32m+[m[32mdef _iex_price(symbol: str):[m
[32m+[m[32m    """IEX 최신체결가 폴백. (price, age_sec)."""[m
     try:[m
         r = requests.get(f"{ALPACA_DATA_BASE}/v2/stocks/{symbol}/trades/latest",[m
                          headers=ALPACA_HDR, params={"feed": "iex"}, timeout=8)[m
         r.raise_for_status()[m
[31m-        return float(r.json().get("trade", {}).get("p", 0))[m
[32m+[m[32m        tr = r.json().get("trade") or {}[m
[32m+[m[32m        price = float(tr.get("p", 0) or 0)[m
[32m+[m[32m        age = None[m
[32m+[m[32m        ts = tr.get("t")[m
[32m+[m[32m        if ts:[m
[32m+[m[32m            from datetime import timezone[m
[32m+[m[32m            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))[m
[32m+[m[32m            age = (datetime.now(timezone.utc) - t).total_seconds()[m
[32m+[m[32m        return price, age[m
     except Exception:[m
[31m-        return 0.0[m
[32m+[m[32m        return 0.0, None[m
[32m+[m
[32m+[m
[32m+[m[32mdef get_monitor_price(symbol: str, exchange: str = "NASD"):[m
[32m+[m[32m    """감시용 현재가 + 데이터나이. 1순위 한투 실시간(age=0), 실패 시 IEX 폴백."""[m
[32m+[m[32m    p = _kis_price(symbol, exchange)[m
[32m+[m[32m    if p > 0:[m
[32m+[m[32m        return p, 0[m
[32m+[m[32m    return _iex_price(symbol)[m
 [m
 [m
 # ─────────────────────────────────────────────[m
[36m@@ -319,8 +358,11 @@[m [mdef classify(m: dict):[m
 [m
 [m
 def deep_check(symbol: str, mode: str = "early") -> bool:[m
[32m+[m[32m    if symbol in NO_BARS:[m
[32m+[m[32m        return False[m
     bars = get_recent_bars(symbol, limit=25)[m
     if len(bars) < 6:[m
[32m+[m[32m        NO_BARS.add(symbol)[m
         return False[m
     last5 = bars[-5:][m
     closes = [b["c"] for b in last5][m
[36m@@ -451,12 +493,18 @@[m [mdef monitor_and_exit(positions: dict, force_all: bool = False):[m
     now = datetime.now()[m
     for sym in list(positions.keys()):[m
         p = positions[sym][m
[31m-        cur = get_last_price(sym)[m
[32m+[m[32m        cur, age = get_monitor_price(sym, p.get("exchange", "NASD"))[m
         if cur <= 0:[m
[32m+[m[32m            log.warning("감시 %s: 가격조회 실패(0) — 손절 판단 불가", sym)[m
             continue[m
         pnl = (cur - p["entry_price"]) / p["entry_price"] * 100[m
         held = (now - datetime.fromisoformat(p["entry_time"])).total_seconds() / 60[m
 [m
[32m+[m[32m        stale = age is not None and age > PRICE_STALE_SEC[m
[32m+[m[32m        log.info("감시 %s pnl=%+.1f%% price=%.2f age=%ss%s",[m
[32m+[m[32m                 sym, pnl, cur, int(age) if age is not None else -1,[m
[32m+[m[32m                 " STALE" if stale else "")[m
[32m+[m
         mode = p.get("mode", "early")[m
         if mode == "chase":[m
             tp, sl, tmax = CHASE_TAKE_PROFIT, CHASE_STOP_LOSS, CHASE_TIME_EXIT_MIN[m
[36m@@ -532,6 +580,8 @@[m [mdef main():[m
                 if time.time() - last_screen >= SCREEN_INTERVAL:[m
                     screen_and_enter(positions)[m
                     last_screen = time.time()[m
[32m+[m[32m                    if positions:[m
[32m+[m[32m                        monitor_and_exit(positions)[m
 [m
             time.sleep(MONITOR_INTERVAL)[m
 [m
