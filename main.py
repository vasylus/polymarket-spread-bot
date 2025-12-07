import os
import time
import requests
from typing import List, Dict, Any, Optional, Tuple

# ================== НАСТРОЙКИ ЧЕРЕЗ ENV ==================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# твой банк
BANK_USD = float(os.environ.get("BANK_USD", "980"))

# минимальный спред (0.03 = 3ц, 0.05 = 5ц)
MIN_SPREAD = float(os.environ.get("MIN_SPREAD", "0.03"))

# минимальный ожидаемый профит, чтобы не спамить (в $)
MIN_PROFIT_USD = float(os.environ.get("MIN_PROFIT_USD", "10"))

# как часто опрашивать API (в секундах)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "20"))

# максимум маркета за проход (чтобы не грузить API)
MAX_MARKETS = int(os.environ.get("MAX_MARKETS", "150"))

# слежение только за открытыми рынками
ONLY_OPEN_MARKETS = os.environ.get("ONLY_OPEN_MARKETS", "true").lower() == "true"

# ================== ЭНДПОИНТЫ ==================

# Gamma API — список маркетов (в том числе clob_token_ids)
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# CLOB — ордербук по конкретному token_id
ORDERBOOK_URL = "https://clob.polymarket.com/book"


# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================


def send_telegram_message(text: str) -> None:
    """Отправка сообщения в Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Не задан TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, data=data, timeout=10)
        if resp.status_code != 200:
            print("Ошибка Telegram:", resp.status_code, resp.text[:200])
    except Exception as e:
        print("Ошибка отправки в Telegram:", e)


def fetch_markets() -> List[Dict[str, Any]]:
    """
    Забираем список маркетов из Gamma API.
    Берём только часть (MAX_MARKETS), чтобы не долбить API.
    """
    params = {
        "limit": MAX_MARKETS,
        "offset": 0,
    }

    if ONLY_OPEN_MARKETS:
        params["closed"] = "false"

    try:
        resp = requests.get(GAMMA_MARKETS_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # Gamma /markets возвращает список маркетов
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            # На всякий случай, если обёрнуто
            return data.get("data", [])
        else:
            print("Неожиданный формат ответа markets:", type(data))
            return []
    except Exception as e:
        print("Ошибка загрузки рынков:", e)
        return []


def fetch_orderbook(token_id: str) -> Optional[Dict[str, Any]]:
    """
    Получаем ордербук по token_id через CLOB /book.
    """
    try:
        params = {"token_id": token_id}
        resp = requests.get(ORDERBOOK_URL, params=params, timeout=15)
        if resp.status_code != 200:
            # Часто 404, если по токену нет книги — не критично
            # print(f"Orderbook {token_id} status {resp.status_code}")
            return None
        return resp.json()
    except Exception as e:
        print(f"Ошибка загрузки ордербука token_id={token_id}:", e)
        return None


def best_bid_ask(orderbook: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], float, float]:
    """
    Достаём лучший bid/ask и их size из структуры ордербука.
    Цена и размер приходят строками — конвертим в float.
    """
    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])

    if not bids or not asks:
        return None, None, 0.0, 0.0

    def parse_price_size(level: Dict[str, str]) -> Tuple[float, float]:
        # в API price/size — строки
        try:
            return float(level.get("price", "0")), float(level.get("size", "0"))
        except Exception:
            return 0.0, 0.0

    # лучший bid — максимальная цена
    best_bid_price, best_bid_size = 0.0, 0.0
    for b in bids:
        p, s = parse_price_size(b)
        if p > best_bid_price and s > 0:
            best_bid_price, best_bid_size = p, s

    # лучший ask — минимальная цена
    best_ask_price, best_ask_size = None, 0.0
    for a in asks:
        p, s = parse_price_size(a)
        if s <= 0:
            continue
        if best_ask_price is None or p < best_ask_price:
            best_ask_price, best_ask_size = p, s

    if best_bid_price <= 0 or best_ask_price is None or best_ask_price <= 0:
        return None, None, 0.0, 0.0

    return best_bid_price, best_ask_price, best_bid_size, best_ask_size


def calc_max_size_for_bank(price: float, bank: float) -> float:
    """Сколько контрактов можно купить на банк по данной цене."""
    if price <= 0:
        return 0.0
    return bank / price


# ================== ОСНОВНОЙ ЦИКЛ БОТА ==================


def main() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Внимание: TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не установлены.")
        print("Бот запустится, но не сможет слать сообщения.")
    else:
        send_telegram_message("🚀 Polymarket спред-бот запущен на Render.")

    print("Бот запущен. Начинаю опрос рынков...")

    # простой антиспам: запоминаем последнюю отправку по token_id
    last_alert_ts: Dict[str, float] = {}

    while True:
        try:
            markets = fetch_markets()
            print(f"Загружено маркетов: {len(markets)}")

            if not markets:
                time.sleep(POLL_INTERVAL)
                continue

            for m in markets:
                # Gamma markets формата:
                # { id, question, clob_token_ids: [ "...", "..." ], ... }
                token_ids = m.get("clob_token_ids") or []
                if not token_ids:
                    continue

                question = m.get("question") or m.get("slug") or "No title"
                market_id = m.get("id", "unknown")

                for token_id in token_ids:
                    # антиспам: не слать по одному и тому же токену чаще, чем раз в 5 минут
                    now = time.time()
                    if token_id in last_alert_ts and now - last_alert_ts[token_id] < 300:
                        continue

                    ob = fetch_orderbook(token_id)
                    if not ob:
                        continue

                    bid, ask, bid_size, ask_size = best_bid_ask(ob)
                    if bid is None or ask is None:
                        continue

                    spread = ask - bid
                    if spread < MIN_SPREAD:
                        continue

                    # считаем объём, который можно провернуть под твой банк
                    max_size_bid = calc_max_size_for_bank(bid, BANK_USD)
                    max_size_ask = calc_max_size_for_bank(ask, BANK_USD)

                    # реальный лимит по ликвидности
                    tradable_size = min(bid_size, ask_size, max_size_bid, max_size_ask)
                    if tradable_size <= 0:
                        continue

                    potential_profit = tradable_size * spread

                    if potential_profit < MIN_PROFIT_USD:
                        continue

                    # если дошли до сюда — это интересный спред
                    last_alert_ts[token_id] = now

                    text = (
                        "📈 Найден спред на Polymarket\n\n"
                        f"Маркет: {question}\n"
                        f"Gamma market id: {market_id}\n"
                        f"Token ID: `{token_id}`\n\n"
                        f"Bid: {bid:.3f} (liq ≈ {bid_size:.2f})\n"
                        f"Ask: {ask:.3f} (liq ≈ {ask_size:.2f})\n"
                        f"Спред: {(spread * 100):.2f}¢\n\n"
                        f"Твой банк: ${BANK_USD:.2f}\n"
                        f"Доступный объём под банк: {tradable_size:.2f} контрактов\n"
                        f"Оценочный профит за 1 цикл: ~${potential_profit:.2f}\n\n"
                        "⚠️ Это только сигнал по спреду. Торговля руками и на свой риск."
                    )

                    print(text.replace("\n", " ")[:300] + "...")
                    send_telegram_message(text)

            time.sleep(POLL_INTERVAL)

        except Exception as e:
            print("Ошибка в основном цикле:", e)
            # чтобы бот не умер от единственной ошибки
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
