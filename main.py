import os
import requests

token = os.getenv("TELEGRAM_TOKEN")
chat_id = os.getenv("CHAT_ID")

print("START TEST")

r = requests.get(
    f"https://api.telegram.org/bot{token}/sendMessage",
    params={
        "chat_id": chat_id,
        "text": "🚀 Railway 최종 테스트 성공!"
    }
)

print("STATUS:", r.status_code)
print("RESPONSE:", r.text)
