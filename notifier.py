import time
import requests
import logging
from queue import Queue
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

message_queue = Queue()

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    except Exception as e:
        logging.warning(f"TG发送失败: {e}")

def queue_message(msg):
    message_queue.put(msg)

def message_worker():
    while True:
        msg = message_queue.get()
        if msg:
            send_telegram_message(msg)
            time.sleep(2)
        message_queue.task_done()

