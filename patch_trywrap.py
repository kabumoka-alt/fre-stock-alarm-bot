path = "stock_pump_bot_kr.py"
with open(path, encoding="utf-8") as f:
    lines = f.readlines()

# while True: 줄 찾기 (main 함수 안)
w = None
for i, ln in enumerate(lines):
    if ln.strip() == "while True:":
        w = i
        break
if w is None:
    print("while True 못 찾음"); raise SystemExit

# while 다음 줄부터 함수 끝(들여쓰기 빠지는 곳)까지가 루프 본체
base_indent = len(lines[w]) - len(lines[w].lstrip())      # while의 들여쓰기
body_indent = base_indent + 4                              # 본체 최소 들여쓰기

# 이미 감쌌는지 체크
if "except Exception as _loop_e" in "".join(lines):
    print("이미 적용됨"); raise SystemExit

# 본체 범위 찾기
start = w + 1
end = start
for j in range(start, len(lines)):
    s = lines[j]
    if s.strip() == "":
        end = j + 1; continue
    ind = len(s) - len(s.lstrip())
    if ind < body_indent:
        break
    end = j + 1

body = lines[start:end]
# 본체를 한 단계 더 들여쓰기 + try/except 삽입
try_line = " " * body_indent + "try:\n"
indented_body = [(" " * 4 + b) if b.strip() else b for b in body]
except_block = [
    " " * body_indent + "except Exception as _loop_e:\n",
    " " * (body_indent + 4) + "print(f\"[\\ub8e8\\ud504 \\uc624\\ub958] {_loop_e}\")\n",
    " " * (body_indent + 4) + "import traceback; traceback.print_exc()\n",
    " " * (body_indent + 4) + "time.sleep(POSITION_CHECK_INTERVAL)\n",
]
new_lines = lines[:start] + [try_line] + indented_body + except_block + lines[end:]

with open(path, "w", encoding="utf-8") as f:
    f.writelines(new_lines)
print(f"try/except 감싸기 완료 (본체 {len(body)}줄)")
