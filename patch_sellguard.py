path = "stock_pump_bot_kr.py"
with open(path, encoding="utf-8") as f:
    c = f.read()

anchor = '''    try:
        result = kis.place_domestic_order(code, qty, price, side)'''

guard = '''    try:
        # \u2500\u2500 \ub9e4\ub3c4 \uac00\ub4dc: \uc2e4\uc81c \uc794\uace0 \ud655\uc778 (\uc720\ub839 \ud3ec\uc9c0\uc158 \ubc29\uc9c0) \u2500\u2500
        if side == "sell":
            _sellable = kis.get_kr_sellable_qty(code)
            if _sellable <= 0:
                print(f"  [\ub9e4\ub3c4 \uc2a4\ud0b5] {code} \uc2e4\uc794\uace0 0\uc8fc (\uc7a5\ubd80 \ubd88\uc77c\uce58)")
                sim_positions.pop(code, None)
                return {"rt_cd": "-1", "msg1": "\uc2e4\uc794\uace0 \uc5c6\uc74c - \ub9e4\ub3c4 \uc2a4\ud0b5"}
            if qty > _sellable:
                print(f"  [\ub9e4\ub3c4 \uc218\ub7c9 \ucd95\uc18c] {code} {qty}\uc8fc \u2192 {_sellable}\uc8fc (\uc2e4\uc794\uace0)")
                qty = _sellable
        result = kis.place_domestic_order(code, qty, price, side)'''

if "\ub9e4\ub3c4 \uac00\ub4dc" in c:
    print("이미 적용됨")
elif anchor in c:
    c = c.replace(anchor, guard, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(c)
    print("매도 가드 추가 완료")
else:
    print("앵커 못 찾음")
