path = "stock_pump_bot_kr.py"
with open(path, encoding="utf-8") as f:
    lines = f.readlines()

# 1) 복원 함수 정의를 def main() 바로 앞에 삽입
func = '''def restore_positions_from_account():
    """\ubd07 \uc2dc\uc791 \uc2dc \uc2e4\uacc4\uc88c \uc794\uace0\ub97c \uc77d\uc5b4 \uac10\uc2dc \ub300\uc0c1(entry_prices/sim_positions) \ubcf5\uc6d0."""
    try:
        bal = kis.get_domestic_balance()
    except Exception as e:
        print(f"[\ud3ec\uc9c0\uc158 \ubcf5\uc6d0 \uc2e4\ud328] {e}")
        return
    restored = 0
    for h in bal.get("output1", []):
        code = h.get("pdno")
        qty = int(h.get("hldg_qty", 0) or 0)
        avg = float(h.get("pchs_avg_pric", 0) or 0)
        name = h.get("prdt_name", "")
        if not code or qty <= 0 or avg <= 0:
            continue
        if code in entry_prices:
            continue
        entry_prices[code] = {"entry": avg, "time": get_kst_now(), "alert1": None, "alert2": None, "stop": None}
        sim_positions[code] = {"entry": avg, "qty": qty, "partial_done": False, "name": name}
        restored += 1
    if restored:
        print(f"[\ud3ec\uc9c0\uc158 \ubcf5\uc6d0] \uc2e4\uacc4\uc88c {restored}\uc885\ubaa9 \uac10\uc2dc \ub4f1\ub85d \uc644\ub8cc")
        send_telegram(f"\U0001F504 [\uad6d\uc7a5] \uc2e4\uacc4\uc88c {restored}\uc885\ubaa9 \uac10\uc2dc \ubcf5\uc6d0 \uc644\ub8cc (\uc190\uc808/\uc775\uc808 \uac10\uc2dc \uc2dc\uc791)")


'''

for i, ln in enumerate(lines):
    if ln.startswith("def main("):
        main_idx = i
        break
else:
    print("def main 못 찾음"); raise SystemExit

if "def restore_positions_from_account" in "".join(lines):
    print("이미 적용됨"); raise SystemExit

lines = lines[:main_idx] + [func] + lines[main_idx:]

# 2) while True: 앞에 복원 호출 삽입
for i in range(main_idx, len(lines)):
    if lines[i].strip() == "while True:":
        indent = len(lines[i]) - len(lines[i].lstrip())
        call = " " * indent + "restore_positions_from_account()\n"
        lines = lines[:i] + [call, "\n"] + lines[i:]
        break

with open(path, "w", encoding="utf-8") as f:
    f.writelines(lines)
print("복원 로직 추가 완료")
