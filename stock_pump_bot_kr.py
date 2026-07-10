import time
import os
import sys
from datetime import datetime
import pytz

# 한국 시간 기준 설정
def get_kst_now():
    return datetime.now(pytz.timezone('Asia/Seoul'))

# [수정된 부분] 매도 타이밍 로직 (타임존 문제 해결)
def check_sell_timing(code: str, current_price: float):
    if code not in entry_prices:
        return
    entry = entry_prices[code]
    entry_price = entry["entry"]
    now = get_kst_now() # KST 기준으로 통일
    gain_pct = net_gain_pct(entry_price, current_price)
    
    # ... (기존 매도 로직 유지) ...

# [수정된 부분] 포지션 복원 로직 (초기값 문제 해결)
def restore_positions_from_account():
    # ... (기존 복원 로직) ...
    # entry_prices[code] = {..., "peak_gain": net_gain_pct(avg, avg)}
    # ... 

# [수정된 부분] 메인 실행부 (무조건 실행되도록 강제)
if __name__ == "__main__":
    print("🚀 강제 감시 모드 시작")
    try:
        # 실계좌 포지션 복원
        restore_positions_from_account()
        
        # 루프를 통해 1분마다 스캔
        while True:
            run_scan()
            time.sleep(60)
    except Exception as e:
        print(f"실행 중 오류 발생: {e}")
