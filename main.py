import os
import time
import json
import requests
from typing import List, Dict, Any, Optional, Tuple


# ================== ENV CONFIG ==================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BANK_USD = float(os.environ.get("BANK_USD", "980"))
MIN_SPREAD = float(os.environ.get("MIN_SPREAD", "0.03"))          # 0.01 = 1 цент
MIN_PROFIT_USD = float(os.environ.get("MIN_PROFIT_USD", "10"))    # мин. профит в $
MIN_VOLUME_USD = float(os.environ.get("MIN_VOLUME_USD", "10000")) # мин. объём рынка

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "20"))        # опрос раз в 20 сек
MAX_PAGES = int(os.environ.get("MAX_PAGES", "4"))                 # 4 × 150 = 600 маркетов

ONLY_OPEN_MARKETS = os.environ.get("ONLY_OPEN_MARKETS", "true").lower() == "true"
DEBUG_TO_TELEGRAM = os.environ.get("DEBUG_TO_TELEGRAM", "false").lower() == "true"

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
ORDERBOOK_URL = "https://clob.polymarket.com/book"


# ================== HELPERS ==================

def send_telegram_raw(text: str, parse_mode: str = "Markdown"):
    """Отправка сообщения в Telegram."""
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
    except Exception:
        pass


def log(msg: str):
    """Лог в stdout + опционально в Telegram."""
    print(msg)
    if DEBUG_TO_TELEGRAM:
        short = msg if len(msg) <= 3500 else msg[:3500] + "...(truncated)"
        send_telegram_raw(f"[DEBUG] {short}", parse_mode="Markdown")


# ================== MARKET FETCHING ==================

def fetch_all_markets(max_pages: int = 4) -> List[Dict[str, Any]]:
    """Грузим до max_pages страниц маркетов (по 150 штук)."""
    all_markets: List[Dict[str, Any]] = []

    for page in range(max_pages):
        offset = page * 150
        params: Dict[str, Any] = {"limit": 150, "offset": offset}
        if ONLY_OPEN_MARKETS:
            params["closed"] = "false"

        log(f"[fetch_all_markets] Страница {page+1}/{max_pages}, offset={offset}, params={params}")

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

        # небольшой пауз между страницами, чтобы не словить throttle
        time.sleep(0.3)

    log(f"[fetch_all_markets] Итог: получено {len(all_markets)} маркетов")
    return all_markets


# ================== ORDERBOOK ==================

def fetch_orderbook(token_id: str) -> Optional[Dict[str, Any]]:
    """Запрос ордербука по конкретному token_id."""
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
    """Находим лучший bid/ask и их размер."""
    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])

    if not bids or not asks:
        return None, None, 0.0, 0.0

    def parse_level(level: Dict[str, Any]) -> Tuple[float, float]:
        try:
            return float(level.get("price", "0")), float(level.get("size", "0"))
        except Exception:
            return 0.0, 0.0

    best_bid_price, best_bid_size = 0.0, 0.0
    for b in bids:
        p, s = parse_level(b)
        if p > best_bid_price and s > 0:
            best_bid_price, best_bid_size = p, s

    best_ask_price, best_ask_size = None, 0.0
    for a in asks:
        p, s = parse_level(a)
        if s <= 0:
            continue
        if best_ask_price is None or p < best_ask_price:
            best_ask_price, best_ask_size = p, s

    return best_bid_price, best_ask_price, best_bid_size, best_ask_size


def calc_max_size_for_bank(price: float, bank: float) -> float:
    """Макс. кол-во контрактов при данном банке и цене."""
    if price <= 0:
        return 0.0
    return bank / price


# ================== MAIN BOT LOOP ==================

def main():
    log(">>> Polymarket spread-bot started")
    log(
        f"Config: BANK={BANK_USD}, "
        f"MIN_SPREAD={MIN_SPREAD}, "
        f"MIN_PROFIT_USD={MIN_PROFIT_USD}, "
        f"MIN_VOLUME_USD={MIN_VOLUME_USD}, "
        f"POLL_INTERVAL={POLL_INTERVAL}, "
        f"MAX_PAGES={MAX_PAGES}"
    )

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        send_telegram_raw("🚀 Polymarket спред-бот запущен на Render.", parse_mode="Markdown")

    # защита от спама по одному и тому же token_id
    last_alert: Dict[str, float] = {}

    while True:
        try:
            log("\n[main] Новый цикл...")

            markets = fetch_all_markets(MAX_PAGES)
            log(f"[main] Маркетов загружено: {len(markets)}")

            for m in markets:
                # ---- фильтр по объёму ----
                volume_raw = (
                    m.get("volumeNum")
                    or m.get("volumeClob")
                    or m.get("volume")
                    or 0
                )
                try:
                    volume = float(volume_raw)
                except (TypeError, ValueError):
                    volume = 0.0

                if volume < MIN_VOLUME_USD:
                    continue

                # ---- URL маркета ----
                slug = m.get("slug") or ""
                events = m.get("events") or []
                event_slug = events[0].get("slug") if events else ""

                if slug and event_slug:
                    market_url = f"https://polymarket.com/event/{event_slug}/{slug}"
                elif slug:
                    market_url = f"https://polymarket.com/event/{slug}"
                else:
                    market_url = "https://polymarket.com"

                # ---- clobTokenIds ----
                token_ids_raw = m.get("clobTokenIds") or []
                if isinstance(token_ids_raw, str):
                    try:
                        token_ids_raw = json.loads(token_ids_raw)
                    except Exception:
                        token_ids_raw = [token_ids_raw]

                if not isinstance(token_ids_raw, list):
                    token_ids_raw = [token_ids_raw]

                token_ids = [str(t) for t in token_ids_raw]

                question = m.get("question") or slug or "Untitled"
                market_id = m.get("id", "unknown")

                for token_id in token_ids:
                    token_id = str(token_id).strip()
                    if len(token_id) < 10:
                        continue

                    now = time.time()
                    if token_id in last_alert and now - last_alert[token_id] < 300:
                        # не чаще, чем раз в 5 минут по одному token_id
                        continue

                    ob = fetch_orderbook(token_id)
                    if not ob:
                        continue

                    bid, ask, bid_size, ask_size = best_bid_ask(ob)
                    if bid is None or ask is None or bid <= 0 or ask <= 0:
                        continue

                    spread = ask - bid

                    tradable = min(
                        bid_size,
                        ask_size,
                        calc_max_size_for_bank(bid, BANK_USD),
                        calc_max_size_for_bank(ask, BANK_USD),
                    )

                    potential_profit = tradable * spread

                    # ---- DEBUG по спреду и профиту ----
                    log(
                        f"[spread_debug] '{question[:60]}' | "
                        f"token_id={token_id[:12]}... | "
                        f"volume≈{volume:.0f} | "
                        f"bid={bid:.3f} ({bid_size:.2f}) | "
                        f"ask={ask:.3f} ({ask_size:.2f}) | "
                        f"spread={spread:.4f} | tradable={tradable:.2f} | "
                        f"profit={potential_profit:.4f}"
                    )

                    # ---- ФИЛЬТРЫ ----
                    if spread < MIN_SPREAD:
                        continue

                    if potential_profit < MIN_PROFIT_USD:
                        continue

                    # ---- СИГНАЛ ----
                    last_alert[token_id] = now

                    text = (
                        "📈 Найден спред на Polymarket\n"
                        f"*[{question}]({market_url})*\n\n"
                        f"Объём: ${volume:,.0f}\n"
                        f"*Спред: {(spread * 100):.2f}¢*\n\n"
                        f"Оценочный профит за 1 цикл: *${potential_profit:.2f}*\n\n"
                        "**************************************************"
                    )

                    send_telegram_raw(text, parse_mode="Markdown")

            log(f"[main] Цикл завершён, пауза {POLL_INTERVAL} сек...")
            time.sleep(POLL_INTERVAL)

        except Exception as e:
            log(f"[main] Ошибка цикла: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
