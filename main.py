import os
import requests

token = os.environ.get("TELEGRAM_TOKEN")
chat_id = os.environ.get("CHAT_ID")

print("TOKEN OK:", bool(token))
print("CHAT ID OK:", chat_id)

r = requests.get(
    f"https://api.telegram.org/bot{token}/sendMessage",
    params={"chat_id": chat_id, "text": "테스트 메시지"}
)

print("STATUS:", r.status_code)
print("RESPONSE:", r.text)
