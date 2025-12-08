import os
import time
import json
import requests
from typing import List, Dict, Any, Optional, Tuple


# ================== ENV CONFIG ==================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BANK_USD = float(os.environ.get("BANK_USD", "980"))
MIN_SPREAD = float(os.environ.get("MIN_SPREAD", "0.03"))
MIN_PROFIT_USD = float(os.environ.get("MIN_PROFIT_USD", "10"))
MIN_VOLUME_USD = float(os.environ.get("MIN_VOLUME_USD", "1000000"))

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))  # 5 минут
MAX_PAGES = int(os.environ.get("MAX_PAGES", "4"))  # 4 страницы × 150 = 600 маркетов

ONLY_OPEN_MARKETS = os.environ.get("ONLY_OPEN_MARKETS", "true").lower() == "true"
DEBUG_TO_TELEGRAM = os.environ.get("DEBUG_TO_TELEGRAM", "false").lower() == "true"

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
ORDERBOOK_URL = "https://clob.polymarket.com/book"


# ================== HELPERS ==================

def send_telegram_raw(text: str, parse_mode="MarkdownV2"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
        "parse_mode": parse_mode,
    }
    try:
        requests.post(url, data=data, timeout=10)
    except:
        pass


def log(msg: str):
    print(msg)
    if DEBUG_TO_TELEGRAM:
        short = msg if len(msg) <= 3500 else msg[:3500] + "...(truncated)"
        send_telegram_raw(f"[DEBUG] {short}")


def escape_md(text: str) -> str:
    """Экранирование для Telegram MarkdownV2."""
    for ch in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


# ================== MARKET FETCHING ==================

def fetch_all_markets(max_pages=4) -> List[Dict[str, Any]]:
    all_markets = []

    for page in range(max_pages):
        offset = page * 150
        params = {"limit": 150, "offset": offset}
        if ONLY_OPEN_MARKETS:
            params["closed"] = "false"

        log(f"[fetch_all_markets] Загружаю страницу {page+1}/{max_pages}, offset={offset}")

        try:
            resp = requests.get(GAMMA_MARKETS_URL, params=params, timeout=15)
            log(f"[fetch_all_markets] HTTP статус: {resp.status_code}")

            if resp.status_code != 200:
                continue

            data = resp.json()
            if isinstance(data, list):
                markets = data
            else:
                markets = data.get("data", [])

            if not markets:
                break

            all_markets.extend(markets)

        except Exception as e:
            log(f"[fetch_all_markets] Ошибка: {e}")

        time.sleep(0.3)  # anti-throttle pause

    log(f"[fetch_all_markets] Итог: получено {len(all_markets)} маркетов")
    return all_markets


# ================== ORDERBOOK ==================

def fetch_orderbook(token_id: str) -> Optional[Dict[str, Any]]:
    params = {"token_id": token_id}
    log(f"[fetch_orderbook] token_id={token_id}")

    try:
        resp = requests.get(ORDERBOOK_URL, params=params, timeout=15)
        log(f"[fetch_orderbook] HTTP статус: {resp.status_code}")

        if resp.status_code != 200:
            return None

        return resp.json()
    except Exception as e:
        log(f"[fetch_orderbook] Ошибка token_id={token_id}: {e}")
        return None


def best_bid_ask(orderbook: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], float, float]:
    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])

    if not bids or not asks:
        return None, None, 0, 0

    def parse(level):
        try:
            return float(level.get("price", "0")), float(level.get("size", "0"))
        except:
            return 0, 0

    best_bid_price, best_bid_size = 0, 0
    for b in bids:
        p, s = parse(b)
        if p > best_bid_price and s > 0:
            best_bid_price, best_bid_size = p, s

    best_ask_price, best_ask_size = None, 0
    for a in asks:
        p, s = parse(a)
        if s <= 0:
            continue
        if best_ask_price is None or p < best_ask_price:
            best_ask_price, best_ask_size = p, s

    return best_bid_price, best_ask_price, best_bid_size, best_ask_size


def calc_max_size_for_bank(price, bank):
    return bank / price if price > 0 else 0


# ================== MAIN BOT LOOP ==================

def main():
    log(">>> Bot started")
    log(f"Config: BANK={BANK_USD}, MIN_SPREAD={MIN_SPREAD}, PROFIT>={MIN_PROFIT_USD}, VOLUME>={MIN_VOLUME_USD}")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        send_telegram_raw("🚀 Polymarket спред-бот запущен на Render.")

    last_alert = {}

    while True:
        try:
            log("\n[main] Новый цикл...")

            markets = fetch_all_markets(MAX_PAGES)
            log(f"[main] Маркетов загружено: {len(markets)}")

            for m in markets:
                volume = float(
                    m.get("volumeNum")
                    or m.get("volumeClob")
                    or m.get("volume")
                    or 0
                )
                if volume < MIN_VOLUME_USD:
                    continue

                slug = m.get("slug") or ""
                events = m.get("events") or []
                event_slug = events[0].get("slug") if events else ""

                if slug and event_slug:
                    market_url = f"https://polymarket.com/event/{event_slug}/{slug}"
                else:
                    market_url = "https://polymarket.com"

                token_ids_raw = m.get("clobTokenIds") or []
                if isinstance(token_ids_raw, str):
                    try:
                        token_ids_raw = json.loads(token_ids_raw)
                    except:
                        token_ids_raw = [token_ids_raw]

                token_ids = [str(t) for t in token_ids_raw]

                question = m.get("question") or slug or "Untitled"
                market_id = m.get("id", "unknown")

                for token_id in token_ids:
                    if len(token_id) < 10:
                        continue

                    now = time.time()
                    if token_id in last_alert and now - last_alert[token_id] < 300:
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

                    tradable = min(
                        bid_size,
                        ask_size,
                        calc_max_size_for_bank(bid, BANK_USD),
                        calc_max_size_for_bank(ask, BANK_USD),
                    )

                    potential_profit = tradable * spread
                    if potential_profit < MIN_PROFIT_USD:
                        continue

                    last_alert[token_id] = now

                    # ===================
                    # TELEGRAM MESSAGE
                    # ===================
                    text = (
                        "📈 Найден спред на Polymarket\n"
                        f"**[{escape_md(question)}]({market_url})**\n\n"
                        f"Объём: ${volume:,.0f}\n"
                        f"**Спред: {(spread * 100):.2f}¢**\n\n"
                        f"Оценочный профит за 1 цикл: **${potential_profit:.2f}**\n\n"
                        "**************************************************"
                    )

                    send_telegram_raw(text)

            log(f"[main] Цикл завершён, пауза {POLL_INTERVAL} сек...")
            time.sleep(POLL_INTERVAL)

        except Exception as e:
            log(f"[main] Ошибка цикла: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
