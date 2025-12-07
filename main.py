import os
import time
import json
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

# включать ли отправку логов в Telegram
DEBUG_TO_TELEGRAM = os.environ.get("DEBUG_TO_TELEGRAM", "false").lower() == "true"

# ================== ЭНДПОИНТЫ ==================

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
ORDERBOOK_URL = "https://clob.polymarket.com/book"


# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================


def send_telegram_raw(text: str) -> None:
    """Базовая отправка сообщения в Telegram, без логирования."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, data=data, timeout=10)
    except Exception:
        # тут уже ничего не логируем, чтобы не уйти в рекурсию
        pass


def log(msg: str) -> None:
    """Лог: в stdout и опционально в Telegram."""
    try:
        print(msg)
    except Exception:
        pass

    if DEBUG_TO_TELEGRAM and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        short = msg
        if len(short) > 3500:
            short = short[:3500] + "...(truncated)"
        try:
            send_telegram_raw(f"[DEBUG] {short}")
        except Exception:
            pass


def send_telegram_message(text: str) -> None:
    """Отправка рабочих (не debug) сообщений."""
    send_telegram_raw(text)


def fetch_markets() -> List[Dict[str, Any]]:
    """Забираем список маркетов из Gamma API."""
    params = {
        "limit": MAX_MARKETS,
        "offset": 0,
    }
    if ONLY_OPEN_MARKETS:
        params["closed"] = "false"

    log(f"[fetch_markets] Запрос к {GAMMA_MARKETS_URL} params={params}")

    try:
        resp = requests.get(GAMMA_MARKETS_URL, params=params, timeout=15)
        log(f"[fetch_markets] HTTP статус: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        log(f"[fetch_markets] Тип ответа: {type(data)}")

        if isinstance(data, list):
            log(f"[fetch_markets] Получен список из {len(data)} маркетов (list)")
            return data
        elif isinstance(data, dict):
            markets = data.get("data", [])
            log(f"[fetch_markets] Получен dict, в data {len(markets)} маркетов")
            return markets
        else:
            log(f"[fetch_markets] Неожиданный формат ответа: {type(data)}")
            return []
    except Exception as e:
        log(f"[fetch_markets] Ошибка загрузки рынков: {e}")
        return []


def fetch_orderbook(token_id: str) -> Optional[Dict[str, Any]]:
    """Получаем ордербук по token_id через CLOB /book."""
    try:
        params = {"token_id": token_id}
        log(f"[fetch_orderbook] Запрос ордербука для token_id={token_id}")
        resp = requests.get(ORDERBOOK_URL, params=params, timeout=15)
        log(f"[fetch_orderbook] HTTP статус: {resp.status_code} для token_id={token_id}")
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data
    except Exception as e:
        log(f"[fetch_orderbook] Ошибка загрузки ордербука token_id={token_id}: {e}")
        return None


def best_bid_ask(orderbook: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], float, float]:
    """Достаём лучший bid/ask и их size из ордербука."""
    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])

    if not bids or not asks:
        log("[best_bid_ask] Пустые bids или asks")
        return None, None, 0.0, 0.0

    def parse_price_size(level: Dict[str, str]) -> Tuple[float, float]:
        try:
            return float(level.get("price", "0")), float(level.get("size", "0"))
        except Exception:
            return 0.0, 0.0

    best_bid_price, best_bid_size = 0.0, 0.0
    for b in bids:
        p, s = parse_price_size(b)
        if p > best_bid_price and s > 0:
            best_bid_price, best_bid_size = p, s

    best_ask_price, best_ask_size = None, 0.0
    for a in asks:
        p, s = parse_price_size(a)
        if s <= 0:
            continue
        if best_ask_price is None or p < best_ask_price:
            best_ask_price, best_ask_size = p, s

    if best_bid_price <= 0 or best_ask_price is None or best_ask_price <= 0:
        log("[best_bid_ask] Не удалось найти валидные bid/ask")
        return None, None, 0.0, 0.0

    return best_bid_price, best_ask_price, best_bid_size, best_ask_size


def calc_max_size_for_bank(price: float, bank: float) -> float:
    """Сколько контрактов можно купить на банк по данной цене."""
    if price <= 0:
        return 0.0
    return bank / price


# ================== ОСНОВНОЙ ЦИКЛ БОТА ==================


def main() -> None:
    log(">>> main() стартанул")
    log(
        "Текущие настройки:\n"
        f"  BANK_USD = {BANK_USD}\n"
        f"  MIN_SPREAD = {MIN_SPREAD}\n"
        f"  MIN_PROFIT_USD = {MIN_PROFIT_USD}\n"
        f"  POLL_INTERVAL = {POLL_INTERVAL}\n"
        f"  MAX_MARKETS = {MAX_MARKETS}\n"
        f"  ONLY_OPEN_MARKETS = {ONLY_OPEN_MARKETS}\n"
        f"  DEBUG_TO_TELEGRAM = {DEBUG_TO_TELEGRAM}\n"
    )

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("⚠️ TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не установлены. Бот не сможет слать сообщения.")
    else:
        log("Пробуем отправить стартовое сообщение в Telegram...")
        send_telegram_message("🚀 Polymarket спред-бот запущен на Render.")

    log("Бот запущен. Начинаю опрос рынков...")

    last_alert_ts: Dict[str, float] = {}

    while True:
        try:
            log("\n[main] Новый цикл опроса...")
            markets = fetch_markets()
            log(f"[main] Загружено маркетов: {len(markets)}")

            if not markets:
                log("[main] Маркеты не получены, спим...")
                time.sleep(POLL_INTERVAL)
                continue

            for m in markets:
                # clobTokenIds приходит как строка с JSON, типа '["id1","id2"]'
                token_ids_raw = m.get("clobTokenIds") or m.get("clob_token_ids") or []
                log(f"[main] Raw clobTokenIds: {token_ids_raw}")

                if isinstance(token_ids_raw, str):
                    try:
                        token_ids = json.loads(token_ids_raw)
                    except Exception as e:
                        log(f"[main] Не удалось распарсить clobTokenIds: {e}")
                        token_ids = []
                else:
                    token_ids = token_ids_raw

                if not token_ids:
                    continue

                question = m.get("question") or m.get("slug") or "No title"
                market_id = m.get("id", "unknown")
                log(f"[main] Маркет {market_id}, question='{question[:60]}', token_ids={token_ids}")

                for token_id in token_ids:
                    now = time.time()
                    if token_id in last_alert_ts and now - last_alert_ts[token_id] < 300:
                        # не спамим по одному и тому же токену чаще, чем раз в 5 минут
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

                    max_size_bid = calc_max_size_for_bank(bid, BANK_USD)
                    max_size_ask = calc_max_size_for_bank(ask, BANK_USD)
                    tradable_size = min(bid_size, ask_size, max_size_bid, max_size_ask)

                    if tradable_size <= 0:
                        continue

                    potential_profit = tradable_size * spread
                    if potential_profit < MIN_PROFIT_USD:
                        continue

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

                    log("[ALERT] " + text.replace("\n", " ")[:300] + "...")
                    send_telegram_message(text)

            log(f"[main] Цикл окончен, спим {POLL_INTERVAL} секунд...")
            time.sleep(POLL_INTERVAL)

        except Exception as e:
            log(f"[main] Ошибка в основном цикле: {e}")
            log(f"[main] Ждём {POLL_INTERVAL} секунд и пробуем снова...")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
