import time
import os
import requests

# ==== НАСТРОЙКИ ====
TELEGRAM_BOT_TOKEN = os.environ.get("8081830319:AAGnsV0M5TKGcobd-W2GZGjupKCNW6G41Yc")
TELEGRAM_CHAT_ID = os.environ.get("@polymarketspreadbot")
BANK_USD = float(os.environ.get("BANK_USD", "980"))
MIN_SPREAD = float(os.environ.get("MIN_SPREAD", "0.03"))  # 0.03 = 3 цента
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "15"))  # секунд

MARKETS_URL = "https://clob.polymarket.com/markets"
ORDERBOOK_URL = "https://clob.polymarket.com/markets/{market_id}/orderbook"


def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Не задан TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print("Ошибка отправки в Telegram:", e)


def fetch_markets():
    try:
        resp = requests.get(MARKETS_URL, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("Ошибка загрузки рынков:", e)
        return []


def fetch_orderbook(market_id: str):
    try:
        url = ORDERBOOK_URL.format(market_id=market_id)
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Ошибка загрузки ордербука {market_id}:", e)
        return None


def best_bid_ask(orderbook):
    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])
    if not bids or not asks:
        return None, None, 0, 0

    best_bid = max(bids, key=lambda x: x["price"])
    best_ask = min(asks, key=lambda x: x["price"])

    return (
        best_bid["price"],
        best_ask["price"],
        best_bid["size"],
        best_ask["size"],
    )


def calc_max_size_for_bank(price: float, bank: float) -> float:
    if price <= 0:
        return 0
    return bank / price


def main():
    send_telegram_message("🚀 Polymarket спред-бот запущен на Render.")

    while True:
        markets = fetch_markets()
        if not markets:
            time.sleep(POLL_INTERVAL)
            continue

        for m in markets:
            market_id = m.get("id")
            question = m.get("question", "No title")

            ob = fetch_orderbook(market_id)
            if not ob:
                continue

            bid, ask, bid_size, ask_size = best_bid_ask(ob)
            if bid is None or ask is None:
                continue

            spread = ask - bid
            if spread < MIN_SPREAD:
                continue

            max_size_for_bank_bid = calc_max_size_for_bank(bid, BANK_USD)
            max_size_for_bank_ask = calc_max_size_for_bank(ask, BANK_USD)

            size_on_bid = min(bid_size, max_size_for_bank_bid)
            size_on_ask = min(ask_size, max_size_for_bank_ask)

            tradable_size = min(size_on_bid, size_on_ask)

            if tradable_size <= 0:
                continue

            profit_per_contract = spread
            potential_profit = tradable_size * profit_per_contract

            if potential_profit < 10:
                continue

            text = (
                "📈 Найден спред на Polymarket\n\n"
                f"Маркет: {question}\n"
                f"ID: {market_id}\n"
                f"Bid: {bid:.3f} (liq: {bid_size:.0f})\n"
                f"Ask: {ask:.3f} (liq: {ask_size:.0f})\n"
                f"Спред: {spread*100:.1f}¢\n"
                f"Твой банк: ${BANK_USD}\n"
                f"Доступный объём под банк: {tradable_size:.0f} контрактов\n"
                f"Потенц. профит за 1 прогон: ~${potential_profit:.2f}\n\n"
                "⚠️ Это не рекомендация. Торгуй руками и на свой риск."
            )

            print(text)
            send_telegram_message(text)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
