"""Fail-open SQLite analytics for the US surge scalper.

No broker credentials, account identifiers, tokens, or raw API responses belong here.
Every public method contains its own failure boundary so analytics can never stop trading.
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
ET = ZoneInfo("America/New_York")
EXIT_REASON_VALUES = frozenset({
    "STOP_LOSS", "TAKE_PROFIT_1", "TRAILING_STOP", "BREAKEVEN_STOP",
    "SIDEWAYS_EXIT", "DAILY_RISK_EXIT", "EMERGENCY_STALE", "MANUAL",
    "SERVICE_RECOVERY", "UNKNOWN",
})
SENSITIVE_PARTS = ("token", "secret", "api_key", "apikey", "account", "cano", "authorization")


def _utc_now():
    return datetime.now(timezone.utc)


def _iso_utc(value=None):
    return (value or _utc_now()).astimezone(timezone.utc).isoformat()


def _iso_et(value=None):
    return (value or _utc_now()).astimezone(ET).isoformat()


def _clean_payload(value):
    if isinstance(value, dict):
        return {str(k): _clean_payload(v) for k, v in value.items()
                if not any(part in str(k).lower() for part in SENSITIVE_PARTS)}
    if isinstance(value, (list, tuple)):
        return [_clean_payload(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class TradeLogger:
    """Short-lived SQLite connections plus in-memory hold telemetry."""

    def __init__(self, db_path, bot_name="surge_scalper_us", bot_version="unknown",
                 busy_timeout_ms=250, checkpoint_every=100, extreme_flush_sec=10.0,
                 extreme_flush_pct=1.0, enabled=True, logger=None):
        self.db_path = os.path.abspath(os.path.expanduser(db_path))
        self.bot_name = bot_name
        self.bot_version = bot_version
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.checkpoint_every = max(int(checkpoint_every), 1)
        self.extreme_flush_sec = float(extreme_flush_sec)
        self.extreme_flush_pct = float(extreme_flush_pct)
        self.enabled = bool(enabled)
        self.log = logger or logging.getLogger(__name__)
        self._extremes = {}
        self._event_last = {}
        self._lock = threading.RLock()
        self._writes = 0
        self._last_checkpoint = 0.0
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000.0)
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        return conn

    def _init_db(self):
        if not self.enabled:
            return
        try:
            os.makedirs(os.path.dirname(self.db_path), mode=0o750, exist_ok=True)
            with self._connect() as conn:
                conn.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY, bot_name TEXT NOT NULL, bot_version TEXT,
                    symbol TEXT NOT NULL, exchange TEXT, trading_date_et TEXT,
                    entry_attempt_no INTEGER, same_symbol_entry_count_today INTEGER,
                    entry_signal_time_et TEXT, entry_order_time_et TEXT, entry_fill_time_et TEXT,
                    entry_price_requested REAL, entry_price_filled REAL, entry_qty INTEGER,
                    entry_notional REAL, entry_reason TEXT, entry_rank INTEGER,
                    entry_quote_source TEXT, entry_bid REAL, entry_ask REAL,
                    entry_spread_pct REAL, entry_quote_age_sec REAL,
                    entry_1m_change_pct REAL, entry_5m_change_pct REAL,
                    entry_day_change_pct REAL, entry_volume REAL, entry_dollar_volume REAL,
                    entry_atr REAL, entry_rsi REAL, entry_obv_direction TEXT,
                    highest_price REAL, lowest_price REAL, mfe_pct REAL, mae_pct REAL,
                    time_to_mfe_sec REAL, time_to_mae_sec REAL,
                    max_spread_pct_during_hold REAL, stale_quote_count INTEGER DEFAULT 0,
                    hold_seconds REAL, exit_signal_time_et TEXT, exit_order_time_et TEXT,
                    exit_fill_time_et TEXT, exit_reason TEXT, exit_price_requested REAL,
                    exit_price_filled REAL, exit_qty INTEGER, exit_retry_count INTEGER,
                    exit_rate_limit_count INTEGER, exit_slippage_pct REAL,
                    exit_quote_source TEXT, exit_bid REAL, exit_ask REAL,
                    exit_quote_age_sec REAL, gross_pnl REAL, fees REAL, net_pnl REAL,
                    return_pct REAL, remaining_qty INTEGER, fully_closed INTEGER DEFAULT 0,
                    accounting_price_source TEXT, kis_fill_verified INTEGER DEFAULT 0,
                    created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
                    schema_version INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(fully_closed, symbol);
                CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(trading_date_et);
                CREATE TABLE IF NOT EXISTS trade_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT, event_time_utc TEXT NOT NULL, event_time_et TEXT NOT NULL,
                    event_type TEXT NOT NULL, symbol TEXT, reason TEXT, attempt_no INTEGER,
                    rate_limit_count INTEGER, requested_price REAL, requested_qty INTEGER,
                    remaining_qty INTEGER, quote_source TEXT, bid REAL, ask REAL,
                    quote_age_sec REAL, message TEXT, payload_json TEXT,
                    FOREIGN KEY(trade_id) REFERENCES trades(trade_id)
                );
                CREATE INDEX IF NOT EXISTS idx_events_trade ON trade_events(trade_id, event_id);
                CREATE TABLE IF NOT EXISTS logger_meta (
                    meta_key TEXT PRIMARY KEY, meta_value TEXT, updated_at_utc TEXT NOT NULL
                );
                """)
                now = _iso_utc()
                conn.execute("INSERT OR IGNORE INTO logger_meta VALUES (?, ?, ?)",
                             ("schema_version", str(SCHEMA_VERSION), now))
                conn.execute("INSERT OR IGNORE INTO logger_meta VALUES (?, ?, ?)",
                             ("created_at_utc", now, now))
                conn.execute("INSERT OR IGNORE INTO logger_meta VALUES (?, ?, ?)",
                             ("retention_policy", "manual_by_trading_date_et", now))
            os.chmod(self.db_path, 0o600)
        except Exception as exc:
            self.log.warning("거래 분석 DB 초기화 실패(매매 계속): %s", exc)

    def _after_write(self):
        with self._lock:
            self._writes += 1
            due = self._writes % self.checkpoint_every == 0
        if due and time.monotonic() - self._last_checkpoint >= 60.0:
            self.checkpoint()

    def create_trade(self, symbol, exchange, entry_reason, requested_price=None,
                     requested_qty=None, entry_rank=None, quote=None, metrics=None,
                     trade_id=None):
        trade_id = trade_id or str(uuid.uuid4())
        if not self.enabled:
            return trade_id
        try:
            now = _utc_now(); et = now.astimezone(ET); quote = quote or {}; metrics = metrics or {}
            bid, ask = quote.get("bid"), quote.get("ask")
            spread = ((ask - bid) / bid * 100.0 if bid and ask and bid > 0 else None)
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE symbol=? AND trading_date_et=?",
                    (symbol, et.date().isoformat())).fetchone()
                count = int(row[0]) + 1
                conn.execute("""INSERT OR IGNORE INTO trades (
                    trade_id,bot_name,bot_version,symbol,exchange,trading_date_et,
                    entry_attempt_no,same_symbol_entry_count_today,entry_signal_time_et,
                    entry_price_requested,entry_qty,entry_reason,entry_rank,entry_quote_source,
                    entry_bid,entry_ask,entry_spread_pct,entry_quote_age_sec,
                    entry_1m_change_pct,entry_5m_change_pct,entry_day_change_pct,entry_volume,
                    entry_dollar_volume,entry_atr,entry_rsi,entry_obv_direction,
                    highest_price,lowest_price,remaining_qty,created_at_utc,updated_at_utc,schema_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (trade_id,self.bot_name,self.bot_version,symbol,exchange,et.date().isoformat(),
                     count,count,et.isoformat(),requested_price,requested_qty,entry_reason,entry_rank,
                     quote.get("source"),bid,ask,spread,quote.get("age_sec"),
                     metrics.get("change_1m_pct"),metrics.get("change_5m_pct"),
                     metrics.get("day_change_pct"),metrics.get("volume"),
                     metrics.get("dollar_volume"),metrics.get("atr"),metrics.get("rsi"),
                     metrics.get("obv_direction"),requested_price,requested_price,requested_qty,
                     now.isoformat(),now.isoformat(),SCHEMA_VERSION))
            self.log_event(trade_id, "ENTRY_SIGNAL", symbol=symbol, reason=entry_reason,
                           requested_price=requested_price, requested_qty=requested_qty,
                           quote_source=quote.get("source"), bid=bid, ask=ask,
                           quote_age_sec=quote.get("age_sec"))
            self._after_write()
        except Exception as exc:
            self.log.warning("거래 분석 생성 실패(매매 계속) %s: %s", symbol, exc)
        return trade_id

    def confirm_entry(self, trade_id, symbol, filled_price, filled_qty, requested_price=None):
        try:
            now = _utc_now()
            with self._connect() as conn:
                conn.execute("""UPDATE trades SET entry_order_time_et=COALESCE(entry_order_time_et,?),
                    entry_fill_time_et=?,entry_price_filled=?,entry_qty=?,entry_notional=?,
                    highest_price=?,lowest_price=?,remaining_qty=?,accounting_price_source=?,
                    kis_fill_verified=1,updated_at_utc=? WHERE trade_id=?""",
                    (_iso_et(now),_iso_et(now),filled_price,filled_qty,filled_price*filled_qty,
                     filled_price,filled_price,filled_qty,"kis_holding_delta",_iso_utc(now),trade_id))
            self.track_position(trade_id, filled_price, now)
            self.log_event(trade_id,"ENTRY_CONFIRMED",symbol=symbol,requested_price=requested_price,
                           requested_qty=filled_qty)
            self._after_write()
        except Exception as exc:
            self.log.warning("진입 분석 확정 실패(매매 계속) %s: %s", symbol, exc)

    def mark_entry_order(self, trade_id, symbol, requested_price, requested_qty):
        try:
            now=_utc_now()
            with self._connect() as conn:
                conn.execute("UPDATE trades SET entry_order_time_et=?,entry_price_requested=?,updated_at_utc=? WHERE trade_id=?",
                             (_iso_et(now),requested_price,_iso_utc(now),trade_id))
            self.log_event(trade_id,"ENTRY_ORDER_SUBMITTED",symbol=symbol,
                           requested_price=requested_price,requested_qty=requested_qty)
            self._after_write()
        except Exception as exc:
            self.log.warning("진입 주문 분석 기록 실패(매매 계속): %s",exc)

    def mark_exit_signal(self, trade_id, symbol, reason, reference_price, qty,
                         remaining_qty=None, payload=None, signal_time=None):
        try:
            now=signal_time or _utc_now()
            with self._connect() as conn:
                conn.execute("UPDATE trades SET exit_signal_time_et=?,exit_reason=?,updated_at_utc=? WHERE trade_id=?",
                             (_iso_et(now),reason,_iso_utc(),trade_id))
            self.log_event(trade_id,"EXIT_SIGNAL",symbol=symbol,reason=reason,
                           requested_price=reference_price,requested_qty=qty,
                           remaining_qty=remaining_qty,payload=payload,event_time=now)
            self._after_write()
        except Exception as exc:
            self.log.warning("청산 신호 분석 기록 실패(매매 계속): %s",exc)

    def mark_exit_order(self, trade_id, symbol, requested_price, requested_qty,
                        attempt_no, rate_limit_count, remaining_qty=None):
        try:
            now=_utc_now()
            with self._connect() as conn:
                conn.execute("""UPDATE trades SET exit_order_time_et=COALESCE(exit_order_time_et,?),
                    exit_price_requested=?,exit_retry_count=?,exit_rate_limit_count=?,updated_at_utc=?
                    WHERE trade_id=?""",(_iso_et(now),requested_price,attempt_no,
                    rate_limit_count,_iso_utc(now),trade_id))
            self.log_event(trade_id,"EXIT_ORDER_SUBMITTED",symbol=symbol,
                           attempt_no=attempt_no,rate_limit_count=rate_limit_count,
                           requested_price=requested_price,requested_qty=requested_qty,
                           remaining_qty=remaining_qty)
            self._after_write()
        except Exception as exc:
            self.log.warning("청산 주문 분석 기록 실패(매매 계속): %s",exc)

    def record_stale_quote(self, trade_id, symbol, quote_age_sec, min_interval_sec=300):
        if not self.log_event(trade_id,"STALE_QUOTE",symbol=symbol,
                              quote_age_sec=quote_age_sec,min_interval_sec=min_interval_sec):
            return False
        try:
            with self._connect() as conn:
                conn.execute("UPDATE trades SET stale_quote_count=stale_quote_count+1,updated_at_utc=? WHERE trade_id=?",
                             (_iso_utc(),trade_id))
            self._after_write(); return True
        except Exception as exc:
            self.log.warning("stale 시세 분석 집계 실패(매매 계속): %s",exc); return False

    def mark_entry_failed(self, trade_id, symbol, reason):
        try:
            with self._connect() as conn:
                conn.execute("UPDATE trades SET fully_closed=1,remaining_qty=0,updated_at_utc=? WHERE trade_id=?",
                             (_iso_utc(),trade_id))
            self.log_event(trade_id,"ENTRY_FAILED",symbol=symbol,reason=reason)
            self._after_write()
        except Exception as exc:
            self.log.warning("진입 실패 분석 기록 실패(매매 계속): %s", exc)

    def track_position(self, trade_id, entry_price, entry_time=None, highest=None, lowest=None):
        try:
            if not trade_id or not entry_price or entry_price <= 0:
                return
            if isinstance(entry_time, str):
                entry_time = datetime.fromisoformat(entry_time)
            if entry_time is None:
                entry_time = _utc_now()
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=timezone.utc)
            with self._lock:
                self._extremes.setdefault(trade_id, {
                    "entry": float(entry_price), "entry_time": entry_time,
                    "high": float(highest or entry_price), "low": float(lowest or entry_price),
                    "mfe_time": entry_time, "mae_time": entry_time,
                    "last_flush": 0.0, "flushed_high": float(highest or entry_price),
                    "flushed_low": float(lowest or entry_price), "dirty": False})
        except Exception as exc:
            self.log.warning("극값 추적 초기화 실패(매매 계속): %s", exc)

    def update_extremes_in_memory(self, trade_id, price, now=None):
        try:
            now = now or _utc_now()
            if now.tzinfo is None: now = now.replace(tzinfo=timezone.utc)
            with self._lock:
                state = self._extremes.get(trade_id)
                if not state: return False
                changed = False
                if price and price > 0:
                    if price > state["high"]: state["high"] = float(price); state["mfe_time"] = now; changed = True
                    if price < state["low"]: state["low"] = float(price); state["mae_time"] = now; changed = True
                state["dirty"] = state["dirty"] or changed
                return changed
        except Exception as exc:
            self.log.warning("MFE/MAE 메모리 갱신 실패(매매 계속): %s", exc); return False

    def flush_extremes_if_due(self, trade_id, force=False):
        try:
            with self._lock:
                state = self._extremes.get(trade_id)
                if not state: return False
                if not state["dirty"] and not force: return False
                elapsed = time.monotonic() - state["last_flush"]
                high_delta = abs(state["high"]-state["flushed_high"])/state["entry"]*100
                low_delta = abs(state["low"]-state["flushed_low"])/state["entry"]*100
                if not force and elapsed < self.extreme_flush_sec and max(high_delta,low_delta) < self.extreme_flush_pct:
                    return False
                snapshot = dict(state)
            entry = snapshot["entry"]
            values = (snapshot["high"],snapshot["low"],(snapshot["high"]-entry)/entry*100,
                      (snapshot["low"]-entry)/entry*100,
                      (snapshot["mfe_time"]-snapshot["entry_time"]).total_seconds(),
                      (snapshot["mae_time"]-snapshot["entry_time"]).total_seconds(),_iso_utc(),trade_id)
            with self._connect() as conn:
                conn.execute("""UPDATE trades SET highest_price=?,lowest_price=?,mfe_pct=?,mae_pct=?,
                    time_to_mfe_sec=?,time_to_mae_sec=?,updated_at_utc=? WHERE trade_id=?""",values)
            with self._lock:
                state=self._extremes.get(trade_id)
                if state: state.update(last_flush=time.monotonic(),flushed_high=snapshot["high"],flushed_low=snapshot["low"],dirty=False)
            self._after_write(); return True
        except Exception as exc:
            self.log.warning("MFE/MAE 기록 실패(매매 계속): %s", exc); return False

    def update_extremes(self, trade_id, price, now=None, force=False):
        """Compatibility wrapper for non-latency-sensitive callers."""
        self.update_extremes_in_memory(trade_id, price, now=now)
        return self.flush_extremes_if_due(trade_id, force=force)

    def log_event(self, trade_id, event_type, symbol=None, reason=None, attempt_no=None,
                  rate_limit_count=None, requested_price=None, requested_qty=None,
                  remaining_qty=None, quote_source=None, bid=None, ask=None,
                  quote_age_sec=None, message=None, payload=None, min_interval_sec=0,
                  event_time=None):
        try:
            key=(trade_id,event_type)
            if min_interval_sec:
                with self._lock:
                    last=self._event_last.get(key); now_mono=time.monotonic()
                    if last is not None and now_mono-last < min_interval_sec: return False
                    self._event_last[key]=now_mono
            now=event_time or _utc_now()
            payload_json=json.dumps(_clean_payload(payload or {}),ensure_ascii=False)[:4000]
            with self._connect() as conn:
                conn.execute("""INSERT INTO trade_events
                    (trade_id,event_time_utc,event_time_et,event_type,symbol,reason,attempt_no,
                     rate_limit_count,requested_price,requested_qty,remaining_qty,quote_source,
                     bid,ask,quote_age_sec,message,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (trade_id,_iso_utc(now),_iso_et(now),event_type,symbol,reason,attempt_no,
                     rate_limit_count,requested_price,requested_qty,remaining_qty,quote_source,
                     bid,ask,quote_age_sec,message,payload_json))
            self._after_write(); return True
        except Exception as exc:
            self.log.warning("거래 이벤트 기록 실패(매매 계속) %s: %s",event_type,exc); return False

    def finalize_trade(self, trade_id, symbol, exit_reason, exit_price_requested,
                       exit_qty, remaining_qty, attempts, rate_limit_count,
                       entry_price, fully_closed, accounting_price_source,
                       kis_fill_verified=False, exit_price_filled=None, fees=None):
        try:
            exit_reason = exit_reason if exit_reason in EXIT_REASON_VALUES else "UNKNOWN"
            self.update_extremes(trade_id, exit_price_filled or exit_price_requested, force=True)
            now=_utc_now(); verified_price=exit_price_filled if kis_fill_verified else None
            gross=((verified_price-entry_price)*exit_qty if verified_price is not None and entry_price else None)
            ret=((verified_price-entry_price)/entry_price*100 if verified_price is not None and entry_price else None)
            slip=((verified_price-exit_price_requested)/exit_price_requested*100
                  if verified_price is not None and exit_price_requested else None)
            net=(gross-fees if gross is not None and fees is not None else None)
            with self._connect() as conn:
                conn.execute("""UPDATE trades SET exit_signal_time_et=COALESCE(exit_signal_time_et,?),
                    exit_order_time_et=?,exit_fill_time_et=?,exit_reason=?,exit_price_requested=?,
                    exit_price_filled=?,exit_qty=?,exit_retry_count=?,exit_rate_limit_count=?,
                    exit_slippage_pct=?,gross_pnl=?,fees=?,net_pnl=?,return_pct=?,remaining_qty=?,
                    fully_closed=?,accounting_price_source=?,kis_fill_verified=?,hold_seconds=
                    CASE WHEN entry_fill_time_et IS NOT NULL THEN MAX(0,julianday(?) - julianday(entry_fill_time_et))*86400 END,
                    updated_at_utc=? WHERE trade_id=?""",
                    (_iso_et(now),_iso_et(now),_iso_et(now) if kis_fill_verified else None,exit_reason,
                     exit_price_requested,verified_price,exit_qty,attempts,rate_limit_count,slip,gross,
                     fees,net,ret,remaining_qty,int(bool(fully_closed)),accounting_price_source,
                     int(bool(kis_fill_verified)),_iso_et(now),_iso_utc(now),trade_id))
            self.log_event(trade_id,"EXIT_CONFIRMED" if fully_closed else "EXIT_PARTIAL",
                           symbol=symbol,reason=exit_reason,attempt_no=attempts,
                           rate_limit_count=rate_limit_count,requested_price=exit_price_requested,
                           requested_qty=exit_qty,remaining_qty=remaining_qty)
            self._after_write()
            if fully_closed:
                with self._lock: self._extremes.pop(trade_id,None)
            self.checkpoint()
        except Exception as exc:
            self.log.warning("청산 분석 확정 실패(매매 계속) %s: %s",symbol,exc)

    def open_trades(self):
        try:
            with self._connect() as conn:
                cols=("trade_id","symbol","exchange","entry_price_filled","entry_qty",
                      "remaining_qty","highest_price","lowest_price","entry_fill_time_et")
                return [dict(zip(cols,row)) for row in conn.execute(
                    "SELECT trade_id,symbol,exchange,entry_price_filled,entry_qty,remaining_qty,"
                    "highest_price,lowest_price,entry_fill_time_et FROM trades WHERE fully_closed=0")]
        except Exception as exc:
            self.log.warning("열린 거래 조회 실패(매매 계속): %s",exc); return []

    def recover_position(self, symbol, exchange, entry_price, qty, entry_time=None):
        trade_id=self.create_trade(symbol,exchange,"SERVICE_RECOVERY",entry_price,qty)
        try:
            now=_utc_now()
            with self._connect() as conn:
                conn.execute("""UPDATE trades SET entry_fill_time_et=?,entry_price_filled=?,entry_qty=?,
                    entry_notional=?,highest_price=?,lowest_price=?,remaining_qty=?,
                    accounting_price_source='positions_state_recovery',kis_fill_verified=0,
                    updated_at_utc=? WHERE trade_id=?""",
                    (entry_time or _iso_et(now),entry_price,qty,entry_price*qty,entry_price,
                     entry_price,qty,_iso_utc(now),trade_id))
            self.track_position(trade_id,entry_price,entry_time)
            self.log_event(trade_id,"SERVICE_RECOVERY",symbol=symbol,
                           remaining_qty=qty,message="trade_id linked from positions state")
            self._after_write()
        except Exception as exc:
            self.log.warning("거래 복구 분석 기록 실패(매매 계속) %s: %s",symbol,exc)
        return trade_id

    def checkpoint(self):
        try:
            with self._connect() as conn:
                result=conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                now=_iso_utc(); conn.execute("INSERT OR REPLACE INTO logger_meta VALUES (?,?,?)",
                    ("last_checkpoint_utc",now,now))
            self._last_checkpoint=time.monotonic(); return result
        except Exception as exc:
            self.log.warning("WAL checkpoint 실패(매매 계속): %s",exc); return None

    def health_summary(self):
        try:
            with self._connect() as conn:
                journal=conn.execute("PRAGMA journal_mode").fetchone()[0]
                sync=conn.execute("PRAGMA synchronous").fetchone()[0]
                opened=conn.execute("SELECT COUNT(*) FROM trades WHERE fully_closed=0").fetchone()[0]
            size=lambda p: os.path.getsize(p) if os.path.exists(p) else 0
            return {"ok":True,"db_path":self.db_path,"db_size":size(self.db_path),
                    "wal_size":size(self.db_path+"-wal"),"shm_size":size(self.db_path+"-shm"),
                    "journal_mode":journal,"synchronous":sync,"open_trades":opened,
                    "writes":self._writes,"schema_version":SCHEMA_VERSION}
        except Exception as exc:
            self.log.warning("거래 분석 상태 조회 실패(매매 계속): %s",exc); return {"ok":False,"error":str(exc)}

    def close(self):
        try:
            with self._lock: trade_ids=list(self._extremes)
            for trade_id in trade_ids: self.update_extremes(trade_id, 0, force=True)
            self.checkpoint()
        except Exception as exc:
            self.log.warning("거래 분석 종료 flush 실패(매매 계속): %s",exc)
