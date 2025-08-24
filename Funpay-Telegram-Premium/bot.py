import os
import logging
import time
import json
import re
import requests
from typing import Optional, Tuple, List, Any
from dotenv import load_dotenv

try:
    from FunPayAPI import Account
except Exception:
    from FunPayAPI.account import Account

from FunPayAPI.updater.runner import Runner
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent

load_dotenv()

# ===================== КОНСТАНТЫ/НАСТРОЙКИ =====================
COOLDOWN_SECONDS = float(os.getenv("REPLY_COOLDOWN_SECONDS", "1"))
TOKEN_FILE = os.getenv("FRAGMENT_TOKEN_FILE", "token.json")
FRAGMENT_API_URL = os.getenv("FRAGMENT_API_URL", "https://api.fragment-api.com/v1")
PREMIUM_SUBCATEGORY_ID_RAW = os.getenv("PREMIUM_SUBCATEGORY_ID")
PREMIUM_SUBCATEGORY_ID = int(PREMIUM_SUBCATEGORY_ID_RAW) if PREMIUM_SUBCATEGORY_ID_RAW else None

MIN_TON_BALANCE = float(os.getenv("MIN_TON_BALANCE", "3.0"))
DEACTIVATE_ON_LOW_TON = os.getenv("DEACTIVATE_ON_LOW_TON", "1") == "1"

DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("premium_bot")

FRAGMENT_TOKEN: Optional[str] = None
FRAGMENT_API_KEY = os.getenv("FRAGMENT_API_KEY")
FRAGMENT_PHONE = os.getenv("FRAGMENT_PHONE")
FRAGMENT_MNEMONICS = os.getenv("FRAGMENT_MNEMONICS")

waiting_for_nick: dict[int, dict] = {}

# ===================== УТИЛИТЫ ЛОГОВ =====================
def _safe_attr(o: Any, *names: str, default: Any = None):
    for n in names:
        try:
            v = getattr(o, n, None)
            if v is not None:
                return v
        except Exception:
            pass
    return default

def _short(s: Optional[str], n: int = 120) -> str:
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"

# ===================== FRAGMENT AUTH =====================
def load_fragment_token() -> Optional[str]:
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("token")
        except Exception:
            return None
    return None

def save_fragment_token(token: str) -> None:
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"token": token}, f)

def authenticate_fragment() -> Optional[str]:
    if not FRAGMENT_API_KEY or not FRAGMENT_PHONE or not FRAGMENT_MNEMONICS:
        logger.error("❌ Не заданы FRAGMENT_API_KEY/PHONE/MNEMONICS в .env")
        return None
    try:
        mnemonics_list = FRAGMENT_MNEMONICS.strip().split()
        payload = {
            "api_key": FRAGMENT_API_KEY,
            "phone_number": FRAGMENT_PHONE,
            "mnemonics": mnemonics_list,
        }
        url = f"{FRAGMENT_API_URL}/auth/authenticate/"
        res = requests.post(url, json=payload, timeout=30)
        if res.status_code == 200:
            token = res.json().get("token")
            if token:
                save_fragment_token(token)
                logger.info("✅ Успешная авторизация Fragment.")
                return token
        logger.error(f"❌ Ошибка авторизации Fragment: {res.text}")
        return None
    except Exception as e:
        logger.exception(f"❌ Исключение при авторизации Fragment: {e}")
        return None

def fragment_headers(token: Optional[str] = None) -> dict:
    t = token or FRAGMENT_TOKEN
    return {
        "Accept": "application/json",
        "Authorization": f"JWT {t}",
        "Content-Type": "application/json",
    }

# ===================== BALANCE CHECK (через /misc/wallet/) =====================
def _extract_ton_from_wallet_json(data) -> float:
    if data is None:
        return 0.0

    for key in ("ton_balance", "ton", "TON"):
        if isinstance(data, dict) and key in data:
            try:
                return float(data[key])
            except Exception:
                pass

    if isinstance(data, dict) and "balance" in data:
        bal = data["balance"]
        if isinstance(bal, (int, float, str)):
            try:
                return float(bal)
            except Exception:
                pass
        if isinstance(bal, dict):
            for key in ("ton", "TON"):
                if key in bal:
                    try:
                        return float(bal[key])
                    except Exception:
                        pass

    for container_key in ("available", "balances", "wallet", "totals"):
        cont = data.get(container_key) if isinstance(data, dict) else None
        if isinstance(cont, dict):
            for key in ("ton", "TON", "Ton", "balanceTon"):
                if key in cont:
                    try:
                        return float(cont[key])
                    except Exception:
                        pass
    return 0.0

def get_fragment_ton_balance(token: str) -> float:
    try:
        url = f"{FRAGMENT_API_URL}/misc/wallet/"
        res = requests.get(url, headers=fragment_headers(token), timeout=20)
        if res.status_code == 200:
            ton = _extract_ton_from_wallet_json(res.json())
            return float(ton or 0.0)
        logger.error(f"❌ Ошибка получения баланса (HTTP {res.status_code}): {res.text}")
    except Exception as e:
        logger.exception(f"❌ Не удалось получить баланс Fragment: {e}")
    return 0.0

# ===================== ДЕАКТИВАЦИЯ ЛОТОВ (детальные логи) =====================
def _list_my_subcat_lots(account: Account, subcat_id: int):
    try:
        lots = account.get_my_subcategory_lots(subcat_id)
        logger.info(f"🔎 get_my_subcategory_lots({subcat_id}) → {len(lots)} лотов.")
        return lots
    except Exception as e:
        logger.error(f"⚠️ get_my_subcategory_lots({subcat_id}) упал: {e}. Пробую через get_categories().")

    try:
        categories = account.get_categories()
        result = []
        for cat in categories:
            for subcat in getattr(cat, "subcategories", []) or []:
                if getattr(subcat, "id", None) == subcat_id:
                    result.extend(getattr(subcat, "lots", []) or [])
        logger.info(f"🔎 get_categories() → найдено {len(result)} лотов в subcat_id={subcat_id}.")
        return result
    except Exception as e:
        logger.error(f"❌ Запасной путь (get_categories) тоже упал: {e}")
        return []

def update_lot_state(account: Account, lot, active: bool) -> bool:
    attempts = 3
    while attempts:
        try:
            lot_fields = account.get_lot_fields(lot.id)
            if getattr(lot_fields, "active", None) == active:
                logger.info(f"ℹ️ Лот уже в нужном состоянии: {getattr(lot, 'title', lot.id)} (id={lot.id}), active={active}")
                return True

            if DRY_RUN:
                logger.warning(f"[DRY_RUN] Пропущено изменение лота {lot.id}: active={active}")
                return True

            lot_fields.active = active
            account.save_lot(lot_fields)

            action = "Включил" if active else "Деактивировал"
            logger.warning(f"⛔ {action} лот {getattr(lot, 'title', lot.id)} (id={lot.id}).")
            return True
        except Exception as e:
            status = getattr(e, "status_code", None)
            if status == 404:
                logger.error(f"❌ Лот {getattr(lot, 'id', '?')} не найден (404).")
                return False
            logger.error(f"❌ Ошибка при изменении лота {getattr(lot, 'id', '?')}: {e}")
            attempts -= 1
            time.sleep(1.0)
    logger.error(f"❌ Не удалось изменить состояние лота {getattr(lot, 'id', '?')}: исчерпаны попытки.")
    return False

def deactivate_premium_lots(account: Account):
    if not DEACTIVATE_ON_LOW_TON:
        logger.info("🔕 Авто-деактивация лотов отключена (DEACTIVATE_ON_LOW_TON=0).")
        return

    if not PREMIUM_SUBCATEGORY_ID:
        logger.error("⚠️ PREMIUM_SUBCATEGORY_ID не задан — деактивация невозможна.")
        return

    logger.warning(f"🚫 Запускаю деактивацию лотов в подкатегории {PREMIUM_SUBCATEGORY_ID}…")
    lots = _list_my_subcat_lots(account, PREMIUM_SUBCATEGORY_ID)

    if not lots:
        logger.warning(f"⚠️ Лоты в подкатегории {PREMIUM_SUBCATEGORY_ID} не найдены.")
        return

    affected: List[str] = []
    for lot in lots:
        try:
            fields = account.get_lot_fields(lot.id)
            is_active = bool(getattr(fields, "active", False))
            title = _safe_attr(lot, "description", "title", default=str(lot.id))

            if not is_active:
                logger.info(f"ℹ️ Лот уже выключен: {title} (id={lot.id}).")
                continue

            ok = update_lot_state(account, lot, active=False)
            if ok:
                affected.append(f"{title} (id={lot.id})")
            time.sleep(0.4)
        except Exception as e:
            logger.error(f"❌ Ошибка при обработке лота id={_safe_attr(lot, 'id', default='?')}: {e}")

    if affected:
        logger.warning("⛔ Деактивированы лоты:\n- " + "\n- ".join(affected))
    else:
        logger.info("ℹ️ Активных лотов к деактивации не найдено или все уже были выключены.")

# ===================== PREMIUM CHECK =====================
def check_username_and_premium(username: str) -> Tuple[bool, bool, Optional[str]]:
    global FRAGMENT_TOKEN
    if not username:
        return False, False, None
    clean = username.lstrip("@").strip()
    url = f"{FRAGMENT_API_URL}/misc/user/{clean}/"
    try:
        res = requests.get(url, headers=fragment_headers(), timeout=20)
        if res.status_code != 200:
            logger.warning(f"Fragment /misc/user/ returned {res.status_code} for {clean}: {res.text}")
            return False, False, None

        data = res.json()
        has_premium = False
        premium_info = None

        if isinstance(data, dict):
            for key in ("is_premium", "has_premium", "premium"):
                if key in data:
                    val = data.get(key)
                    has_premium = bool(val)
                    premium_info = str(val)
                    break

            for key in ("premium_until", "premium_expiry", "premium_expires"):
                if key in data and data.get(key):
                    premium_info = str(data.get(key))
                    has_premium = True
                    break

        exists = isinstance(data, (dict, list)) and bool(data)
        return exists, has_premium, premium_info

    except Exception as e:
        logger.exception(f"Ошибка при запросе Fragment /misc/user/ для {clean}: {e}")
        return False, False, None

# ===================== PREMIUM ORDER =====================
def direct_send_premium(token: str, username: str, months: int) -> Tuple[bool, str]:
    payload = {"username": username, "months": months}
    try:
        res = requests.post(f"{FRAGMENT_API_URL}/order/premium/", json=payload, headers=fragment_headers(token), timeout=40)
        if res.status_code == 200:
            return True, res.text
        else:
            return False, res.text
    except Exception as e:
        logger.exception(f"Ошибка при попытке POST /order/premium/ с payload {payload}: {e}")
        return False, str(e)

def parse_fragment_error(text: str) -> str:
    try:
        data = json.loads(text)
        logger.error(f"Fragment API error details: {json.dumps(data, ensure_ascii=False)}")
    except Exception:
        logger.error(f"Fragment API error (raw): {text}")
    return "❌ Произошла ошибка при оформлении."

def refund_order(account: Account, order_id: int, chat_id: int) -> bool:
    try:
        account.refund(order_id)
        logger.info(f"✔️ Возврат оформлен для заказа {order_id}")
        try:
            account.send_message(chat_id, "✅ Средства успешно возвращены.")
        except Exception:
            pass
        return True
    except Exception as e:
        logger.exception(f"❌ Не удалось вернуть средства за заказ {order_id}: {e}")
        try:
            account.send_message(chat_id, "❌ Ошибка возврата. Свяжитесь с админом.")
        except Exception:
            pass
        return False

# ===================== HELPERS =====================
MONTHS_ALLOWED = {3, 6, 12}
MONTHS_PATTERNS = [
    r"\b(3|6|12)\s*(?:m|mo|mon|mons|months|мес|месяц(?:ев|а)?|м)\b",
    r"\bна\s*(3|6|12)\s*(?:мес|месяц(?:ев|а)?|м)\b",
    r"\b(3|6|12)\b",
]

def extract_months(title: str) -> int:
    if not title:
        return 3
    t = title.lower()
    for p in MONTHS_PATTERNS:
        m = re.search(p, t)
        if m:
            val = int(m.group(1))
            if val in MONTHS_ALLOWED:
                return val
    return 3

def get_subcategory_id_safe(order, account: Account):
    subcat = getattr(order, "subcategory", None) or getattr(order, "sub_category", None)
    if subcat and hasattr(subcat, "id"):
        return subcat.id, subcat
    try:
        full_order = account.get_order(order.id)
        subcat = getattr(full_order, "subcategory", None) or getattr(full_order, "sub_category", None)
        if subcat and hasattr(subcat, "id"):
            return subcat.id, subcat
    except Exception as e:
        logger.warning(f"⚠️ Не удалось загрузить полный заказ: {e}")
    return None, None

# ===================== ОСНОВНОЙ ЦИКЛ =====================
def main():
    global FRAGMENT_TOKEN

    golden_key = os.getenv("FUNPAY_AUTH_TOKEN")
    if not golden_key:
        logger.error("❌ FUNPAY_AUTH_TOKEN не найден в .env")
        return

    account = Account(golden_key)
    account.get()
    if not getattr(account, "username", None):
        logger.error("❌ Не удалось получить имя пользователя FunPay. Проверьте токен.")
        return

    logger.info(f"✅ Авторизован на FunPay как {account.username}")
    runner = Runner(account)

    FRAGMENT_TOKEN = load_fragment_token() or authenticate_fragment()
    if not FRAGMENT_TOKEN:
        logger.error("❌ Не удалось авторизоваться в Fragment.")
        return

    logger.info("🤖 Бот запущен. Ожидаем события FunPay…")

    last_reply_time = 0.0

    while True:
        for event in runner.listen(requests_delay=3.0):
            try:
                now = time.time()

                if isinstance(event, NewOrderEvent):
                    buyer_username = _safe_attr(event.order, "buyer_username", "buyer_name", default="unknown_buyer")
                    order_title = _safe_attr(event.order, "title", "short_description", "full_description", default="")
                    subcat_id, subcat = get_subcategory_id_safe(event.order, account)

                    logger.info(
                        f"🛒 Новый заказ #{event.order.id} от {buyer_username}: "
                        f"\"{_short(order_title)}\" (subcat_id={subcat_id})"
                    )

                    if PREMIUM_SUBCATEGORY_ID and subcat_id != PREMIUM_SUBCATEGORY_ID:
                        logger.info(
                            f"⏭ Пропуск заказа — не Premium (получено subcat_id={subcat_id}, ожидаю {PREMIUM_SUBCATEGORY_ID})"
                        )
                        continue

                    order = account.get_order(event.order.id)
                    full_title = (
                        _safe_attr(order, "title")
                        or _safe_attr(order, "short_description")
                        or _safe_attr(order, "full_description")
                        or ""
                    )
                    months = extract_months(full_title)
                    if months not in MONTHS_ALLOWED:
                        months = 3

                    logger.info(f"📦 Заказ #{order.id}: Premium на {months} мес, лот=\"{_short(full_title)}\".")

                    buyer_id = order.buyer_id
                    chat_id = order.chat_id
                    waiting_for_nick[buyer_id] = {
                        "chat_id": chat_id,
                        "months": months,
                        "order_id": order.id,
                        "state": "awaiting_nick",
                        "temp_nick": None,
                    }

                    account.send_message(
                        chat_id,
                        f"Спасибо за покупку Premium!\nПришлите ваш Telegram-тег (@username), чтобы получить {months} мес.",
                    )
                    last_reply_time = now

                elif isinstance(event, NewMessageEvent):
                    msg = event.message
                    chat_id = msg.chat_id
                    user_id = msg.author_id
                    text = (msg.text or "").strip()

                    if user_id == getattr(account, "id", None) or user_id not in waiting_for_nick:
                        continue

                    user_state = waiting_for_nick[user_id]
                    months = user_state["months"]
                    order_id = user_state["order_id"]

                    if user_state["state"] == "awaiting_nick":
                        exists, has_premium, info = check_username_and_premium(text)
                        if not exists:
                            account.send_message(chat_id, f'❌ Ник "{text}" не найден. Введите правильный тег (пример: @username).')
                            last_reply_time = now
                            continue
                        if has_premium:
                            account.send_message(
                                chat_id,
                                f'⚠️ У {text} уже активен Premium ({info if info else "после авторизации"}). Укажите другой ник.',
                            )
                            last_reply_time = now
                            continue

                        user_state["temp_nick"] = text
                        user_state["state"] = "awaiting_confirmation"
                        account.send_message(
                            chat_id,
                            f'Вы указали: "{text}". Если это верно — напишите "+", иначе отправьте другой тег.',
                        )
                        last_reply_time = now

                    elif user_state["state"] == "awaiting_confirmation":
                        if text == "+":
                            username = user_state["temp_nick"].lstrip("@")
                            account.send_message(chat_id, f"🚀 Оформляю Premium на {months} мес для @{username}…")
                            success, response = direct_send_premium(FRAGMENT_TOKEN, username, months)

                            if success:
                                account.send_message(
                                    chat_id, f"✅ Успешно оформлен Premium на {months} мес для @{username}!"
                                )
                                logger.info(f"✅ @{username} получил премиум на {months} мес")
                            else:
                                short_error = parse_fragment_error(response)
                                account.send_message(chat_id, short_error + "\n🔁 Пытаюсь оформить возврат…")

                                if "not enough funds" in response.lower() or "Недостаточно средств" in response:
                                    ton_balance = get_fragment_ton_balance(FRAGMENT_TOKEN)
                                    logger.warning(f"⚠️ Баланс (TON) на момент ошибки: {ton_balance:.6f}")
                                    if ton_balance < MIN_TON_BALANCE:
                                        logger.warning("⛔ Баланс ниже порога — запускаю деактивацию лотов (немедленно).")
                                        deactivate_premium_lots(account)

                                refund_order(account, order_id, chat_id)

                            waiting_for_nick.pop(user_id, None)
                            last_reply_time = now
                        else:
                            exists, has_premium, info = check_username_and_premium(text)
                            if not exists:
                                account.send_message(chat_id, f'❌ Ник "{text}" не найден. Введите правильный тег.')
                            elif has_premium:
                                account.send_message(
                                    chat_id,
                                    f'⚠️ У {text} уже активен Premium ({info if info else "после авторизации"}). Укажите другой ник.',
                                )
                            else:
                                user_state["temp_nick"] = text
                                account.send_message(
                                    chat_id,
                                    f'Вы указали: "{text}". Если верно — напишите "+", иначе пришлите новый тег.',
                                )
                            last_reply_time = now

            except Exception as e:
                logger.exception(f"❌ Ошибка обработки события: {e}")


if __name__ == "__main__":
    main()
