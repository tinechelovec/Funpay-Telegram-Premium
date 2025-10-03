import os
import logging
import time
import json
import re
from typing import Optional, Tuple, List, Any, Dict
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv

try:
    from rich.logging import RichHandler
    _HAS_RICH = True
except Exception:
    _HAS_RICH = False

try:
    from FunPayAPI import Account
except Exception:
    from FunPayAPI.account import Account
from FunPayAPI.updater.runner import Runner
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent

load_dotenv()

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on", "t")

NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")

def _coerce_float(x) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip().replace(",", ".")
        m = NUM_RE.search(s)
        if m:
            try:
                return float(m.group(0))
            except Exception:
                return None
    return None

def _order_wallet_version(v_raw: str) -> str:
    v = (v_raw or "").strip().lower()
    if "/" in v:
        v = v.split("/", 1)[0].strip()
    if v in {"v4", "v4r2"}:
        return "v4r2"
    if v in {"w5", "w5r1", "v5", "v5r1", "w5rlib", "v5rdlib"}:
        return "w5"
    return "v4r2"

def _auth_version_for_api(order_wallet_version: str) -> str:
    return "V4R2" if order_wallet_version == "v4r2" else "W5"

COOLDOWN_SECONDS = float(os.getenv("REPLY_COOLDOWN_SECONDS", "1"))
TOKEN_FILE = os.getenv("FRAGMENT_TOKEN_FILE", "token.json")

FRAGMENT_API_URL = "https://api.fragment-api.com/v1"

PREMIUM_SUBCATEGORY_ID = 1391

MIN_BALANCE = float(os.getenv("FRAGMENT_MIN_BALANCE", "1"))
AUTO_REFUND = _env_bool("AUTO_REFUND", True)
AUTO_DEACTIVATE = _env_bool("AUTO_DEACTIVATE", True)

FRAGMENT_WALLET_VERSION_ORDER = _order_wallet_version(os.getenv("FRAGMENT_WALLET_VERSION", "v4r2"))
FRAGMENT_AUTH_VERSION = _auth_version_for_api(FRAGMENT_WALLET_VERSION_ORDER)

DRY_RUN = _env_bool("DRY_RUN", False)

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
_executor = ThreadPoolExecutor(max_workers=max(1, MAX_WORKERS))

LOG_FILE = "log.txt"
_handlers: List[logging.Handler] = []
if _HAS_RICH:
    _handlers.append(RichHandler(rich_tracebacks=True, show_time=True, markup=True))
_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
_handlers.append(_file_handler)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s" if not _HAS_RICH else "%(message)s",
    handlers=_handlers,
)
logger = logging.getLogger("premium_bot")

FRAGMENT_TOKEN: Optional[str] = None
FRAGMENT_API_KEY = os.getenv("FRAGMENT_API_KEY")
FRAGMENT_PHONE = os.getenv("FRAGMENT_PHONE")
FRAGMENT_MNEMONICS = os.getenv("FRAGMENT_MNEMONICS")

CREATOR_NAME = os.getenv("CREATOR_NAME", "@tinechelovec")
CREATOR_URL = os.getenv("CREATOR_URL", "https://t.me/tinechelovec")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/by_thc")
GITHUB_URL = os.getenv("GITHUB_URL", "https://github.com/tinechelovec/Funpay-Telegram-Premium")
BANNER_NOTE = os.getenv(
    "BANNER_NOTE",
    "Бот бесплатный и с открытым исходным кодом на GitHub. "
    "Создатель бота его НЕ продаёт. Если вы где-то видите платную версию — "
    "это решение перепродавца, к автору отношения не имеет."
)

def _print_banner():
    title = "FunPay ↔ Fragment Premium Bot"
    if _HAS_RICH:
        try:
            from rich.console import Console
            from rich.panel import Panel
            from rich.text import Text
            from rich.rule import Rule

            console = Console()

            console.print(Rule(style="cyan"))
            console.print(f"[bold bright_cyan]{title}[/bold bright_cyan]")
            console.print(Rule(style="cyan"))

            body = Text()
            body.append("Создатель: ", style="bold")
            body.append(f"{CREATOR_NAME}\n", style="bold magenta")
            if CREATOR_URL:
                body.append(f"  → {CREATOR_URL}\n", style="magenta")

            if CHANNEL_URL:
                body.append("Канал с ботами/плагинами:\n", style="bold")
                body.append(f"  → {CHANNEL_URL}\n", style="yellow")

            if GITHUB_URL:
                body.append("GitHub проекта:\n", style="bold")
                body.append(f"  → {GITHUB_URL}\n", style="green")

            body.append("\nВерсия кошелька для авторизации (auth): ", style="bold")
            body.append(f"{FRAGMENT_AUTH_VERSION}\n", style="bright_cyan")
            body.append("Версия кошелька для заказов (order): ", style="bold")
            body.append(f"{FRAGMENT_WALLET_VERSION_ORDER}\n", style="bright_cyan")

            body.append(
                "\n[ ВНИМАНИЕ ] БОТ ПОЛНОСТЬЮ БЕСПЛАТНЫЙ И ОТКРЫТЫЙ (OPENSOURCE).\n", style="bold bright_red"
            )
            body.append(
                "Автор НИКОГДА НЕ ПРОДАЁТ этот бот. Любые платные перепродажи совершаются третьими лицами по их инициативе.\n",
                style="bold bright_red",
            )
            if GITHUB_URL:
                body.append("Исходники доступны на GitHub (см. ссылку выше).\n", style="bright_red")

            console.print(Panel(body, title="[bold cyan]Информация[/bold cyan]", expand=False))
            return
        except Exception:
            pass

    border = "=" * 70
    print(border)
    print(title)
    print(border)
    print(f"Создатель: {CREATOR_NAME}")
    if CREATOR_URL:
        print(f"  → {CREATOR_URL}")
    if CHANNEL_URL:
        print(f"Канал с ботами/плагинами:\n  → {CHANNEL_URL}")
    if GITHUB_URL:
        print(f"GitHub проекта:\n  → {GITHUB_URL}")
    print("")
    print(f"Версия кошелька (auth):  {FRAGMENT_AUTH_VERSION}")
    print(f"Версия кошелька (order): {FRAGMENT_WALLET_VERSION_ORDER}")
    print("")
    print("!!! БОТ БЕСПЛАТНЫЙ И ОТКРЫТЫЙ. Автор не продаёт этот бот. !!!")
    print(BANNER_NOTE)
    print(border)

waiting_by_chat: Dict[int, Dict[str, Any]] = {}
waiting_by_user: Dict[int, Dict[str, Any]] = {}
_last_reply_by_chat: Dict[int, float] = defaultdict(float)

def _cooldown(chat_id: int):
    now = time.time()
    delta = now - _last_reply_by_chat.get(chat_id, 0.0)
    if delta < COOLDOWN_SECONDS:
        time.sleep(max(0.01, COOLDOWN_SECONDS - delta))
    _last_reply_by_chat[chat_id] = time.time()

def _bind_state(state: Dict[str, Any]):
    waiting_by_chat[state["chat_id"]] = state
    waiting_by_user[state["buyer_id"]] = state

def _pop_state_by_chat(chat_id: int):
    st = waiting_by_chat.pop(chat_id, None)
    if st:
        waiting_by_user.pop(st.get("buyer_id"), None)
    return st

def _pop_state_by_user(user_id: int):
    st = waiting_by_user.pop(user_id, None)
    if st:
        waiting_by_chat.pop(st.get("chat_id"), None)
    return st

def _get_state(chat_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    st = waiting_by_chat.get(chat_id)
    if st:
        return st
    return waiting_by_user.get(user_id)

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

def AfterRefound(account: Account, order_id: int, chat_id: int):
    try:
        _cooldown(chat_id)
        account.send_message(chat_id, "ℹ️ AfterRefound: возврат оформлен. Если что — напишите нам.")
    except Exception:
        pass
    logger.info(f"[HOOK] AfterRefound вызван для заказа #{order_id}")

def AfterDictivate(account: Account, affected_lots: List[str]):
    msg = ", ".join(affected_lots) if affected_lots else "[пусто]"
    logger.info(f"[HOOK] AfterDictivate: деактивировано: {msg}")

def after_refund(account: Account, order_id: int, chat_id: int):
    return AfterRefound(account, order_id, chat_id)

def after_deactivate(account: Account, affected_lots: List[str]):
    return AfterDictivate(account, affected_lots)

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
            "version": FRAGMENT_AUTH_VERSION,
            "mnemonics": mnemonics_list,
        }
        url = f"{FRAGMENT_API_URL}/auth/authenticate/"
        res = requests.post(url, json=payload, timeout=30)
        if res.status_code == 200:
            token = res.json().get("token")
            if token:
                save_fragment_token(token)
                logger.info(f"✅ Успешная авторизация Fragment. [auth_version={FRAGMENT_AUTH_VERSION}]")
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

def _extract_ton_from_wallet_json(data) -> float:
    if data is None:
        return 0.0

    if isinstance(data, dict):
        for k in ("ton_balance", "ton", "TON", "balanceTon", "balance_ton", "tonAmount", "amountTon"):
            if k in data:
                v = _coerce_float(data[k])
                if v is not None:
                    return v

        if "balance" in data:
            bal = data["balance"]
            v = _coerce_float(bal)
            if v is not None:
                return v
            if isinstance(bal, dict):
                for k in ("ton", "TON", "Ton", "amount", "value"):
                    if k in bal:
                        v2 = _coerce_float(bal[k])
                        if v2 is not None:
                            return v2
            if isinstance(bal, list):
                for item in bal:
                    v2 = _extract_ton_from_wallet_json(item)
                    if v2 is not None:
                        return v2

        for key in ("available", "balances", "wallet", "totals", "data", "result", "items"):
            if key in data:
                cont = data[key]
                if isinstance(cont, dict):
                    if "TON" in cont:
                        v = _coerce_float(cont["TON"])
                        if v is not None:
                            return v
                    for _, subv in cont.items():
                        if isinstance(subv, dict):
                            cur = str(subv.get("currency") or subv.get("asset") or "").upper()
                            if cur == "TON":
                                for name in ("balance", "amount", "value", "ton", "TON"):
                                    if name in subv:
                                        v = _coerce_float(subv[name])
                                        if v is not None:
                                            return v
                        else:
                            v = _extract_ton_from_wallet_json(subv)
                            if v is not None:
                                return v
                elif isinstance(cont, list):
                    for it in cont:
                        if isinstance(it, dict):
                            cur = str(it.get("currency") or it.get("asset") or "").upper()
                            if cur == "TON":
                                for name in ("balance", "amount", "value", "ton", "TON"):
                                    if name in it:
                                        v = _coerce_float(it[name])
                                        if v is not None:
                                            return v
                        v = _extract_ton_from_wallet_json(it)
                        if v is not None:
                            return v

        for v in data.values():
            val = _extract_ton_from_wallet_json(v)
            if val is not None:
                return val

    if isinstance(data, list):
        for it in data:
            v = _extract_ton_from_wallet_json(it)
            if v is not None:
                return v

    v = _coerce_float(data)
    return v if v is not None else 0.0

def get_fragment_ton_balance(token: str) -> float:
    try:
        url = f"{FRAGMENT_API_URL}/misc/wallet/"
        res = requests.get(url, headers=fragment_headers(token), timeout=20)
        if res.status_code == 200:
            ton = _extract_ton_from_wallet_json(res.json())
            return float(ton if ton is not None else 0.0)
        logger.error(f"❌ Ошибка получения баланса (HTTP {res.status_code}): {res.text}")
    except Exception as e:
        logger.exception(f"❌ Не удалось получить баланс Fragment: {e}")
    return 0.0

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
    if not AUTO_DEACTIVATE:
        logger.info("🔕 Авто-деактивация лотов отключена.")
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
        try:
            AfterDictivate(account, affected)
        except Exception as e:
            logger.warning(f"AfterDictivate ошибка: {e}")
    else:
        logger.info("ℹ️ Активных лотов к деактивации не найдено или все уже были выключены.")

def check_username_and_premium(username: str) -> Tuple[bool, bool, Optional[str]]:
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

def direct_send_premium(token: str, username: str, months: int) -> Tuple[bool, str]:
    payload = {
        "username": username,
        "months": months,
        "wallet_version": FRAGMENT_WALLET_VERSION_ORDER,
    }
    try:
        res = requests.post(f"{FRAGMENT_API_URL}/order/premium/", json=payload, headers=fragment_headers(token), timeout=40)
        if res.status_code == 200:
            return True, res.text
        else:
            return False, res.text
    except Exception as e:
        logger.exception(f"Ошибка при POST /order/premium/ с payload {payload}: {e}")
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
            _cooldown(chat_id)
            account.send_message(chat_id, "✅ Средства успешно возвращены.")
        except Exception:
            pass
        try:
            AfterRefound(account, order_id, chat_id)
        except Exception as e:
            logger.warning(f"AfterRefound ошибка: {e}")
        return True
    except Exception as e:
        logger.exception(f"❌ Не удалось вернуть средства за заказ {order_id}: {e}")
        try:
            _cooldown(chat_id)
            account.send_message(chat_id, "❌ Ошибка возврата. Свяжитесь с админом.")
        except Exception:
            pass
        return False

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

def _process_issue_flow(account: Account, chat_id: int, order_id: int, username: str, months: int):
    try:
        _cooldown(chat_id)
        account.send_message(chat_id, f"🚀 Оформляю Premium на {months} мес для @{username}…")

        success, response = direct_send_premium(FRAGMENT_TOKEN or "", username, months)
        if success:
            _cooldown(chat_id)
            account.send_message(chat_id, f"✅ Успешно оформлен Premium на {months} мес для @{username}!")
            logger.info(f"✅ @{username} получил премиум на {months} мес (order #{order_id})")
            return

        short_error = parse_fragment_error(response)
        _cooldown(chat_id)
        account.send_message(chat_id, short_error)

        low_funds = ("not enough funds" in str(response).lower()) or ("Недостаточно средств" in str(response))
        if low_funds:
            ton_balance = get_fragment_ton_balance(FRAGMENT_TOKEN or "")
            logger.warning(f"⚠️ Баланс (TON) на момент ошибки: {ton_balance:.6f}")
            if ton_balance < MIN_BALANCE and AUTO_DEACTIVATE:
                logger.warning("⛔ Баланс ниже порога — запускаю деактивацию лотов (немедленно).")
                deactivate_premium_lots(account)

        if AUTO_REFUND:
            _cooldown(chat_id)
            account.send_message(chat_id, "🔁 Пытаюсь оформить возврат…")
            refund_order(account, order_id, chat_id)
        else:
            _cooldown(chat_id)
            account.send_message(chat_id, "⚠️ Авто-рефанд отключён. Свяжитесь с админом для возврата.")

    except Exception as e:
        logger.exception(f"❌ Ошибка воркера по заказу #{order_id} (@{username}): {e}")

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

    _print_banner()
    logger.info("🤖 Бот запущен. Ожидаем события FunPay…")

    while True:
        for event in runner.listen(requests_delay=3.0):
            try:
                if isinstance(event, NewOrderEvent):
                    buyer_username = _safe_attr(event.order, "buyer_username", "buyer_name", default="unknown_buyer")
                    order_title = _safe_attr(event.order, "title", "short_description", "full_description", default="")
                    subcat_id, _ = get_subcategory_id_safe(event.order, account)

                    logger.info(
                        f"🛒 Новый заказ #{event.order.id} от {buyer_username}: \"{_short(order_title)}\" (subcat_id={subcat_id})"
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

                    _cooldown(chat_id)
                    account.send_message(
                        chat_id,
                        f"Спасибо за покупку Premium!\nПришлите ваш Telegram-тег (@username), чтобы получить {months} мес.",
                    )

                    state = {
                        "buyer_id": buyer_id,
                        "chat_id": chat_id,
                        "months": months,
                        "order_id": order.id,
                        "state": "awaiting_nick",
                        "temp_nick": None,
                    }
                    _bind_state(state)

                elif isinstance(event, NewMessageEvent):
                    msg = event.message
                    chat_id = msg.chat_id
                    user_id = msg.author_id
                    text = (msg.text or "").strip()

                    logger.info(f"✉️ NewMessage: chat_id={chat_id}, author_id={user_id}, text={_short(text, 80)!r}")

                    if user_id == getattr(account, "id", None):
                        continue

                    state = _get_state(chat_id, user_id)
                    if not state:
                        continue

                    months = state["months"]
                    order_id = state["order_id"]

                    if state["state"] == "awaiting_nick":
                        exists, has_premium, info = check_username_and_premium(text)
                        if not exists:
                            _cooldown(state["chat_id"])
                            account.send_message(state["chat_id"], f'❌ Ник "{text}" не найден. Введите правильный тег (пример: @username).')
                            continue
                        if has_premium:
                            _cooldown(state["chat_id"])
                            account.send_message(
                                state["chat_id"],
                                f'⚠️ У {text} уже активен Premium ({info if info else "после авторизации"}). Укажите другой ник.',
                            )
                            continue

                        state["temp_nick"] = text
                        state["state"] = "awaiting_confirmation"
                        _cooldown(state["chat_id"])
                        account.send_message(
                            state["chat_id"],
                            f'Вы указали: "{text}". Если это верно — напишите "+", иначе отправьте другой тег.',
                        )

                    elif state["state"] == "awaiting_confirmation":
                        if text == "+":
                            username = state["temp_nick"].lstrip("@")
                            _pop_state_by_chat(state["chat_id"])
                            _executor.submit(_process_issue_flow, account, state["chat_id"], order_id, username, months)
                        else:
                            exists, has_premium, info = check_username_and_premium(text)
                            if not exists:
                                _cooldown(state["chat_id"])
                                account.send_message(state["chat_id"], f'❌ Ник "{text}" не найден. Введите правильный тег.')
                            elif has_premium:
                                _cooldown(state["chat_id"])
                                account.send_message(
                                    state["chat_id"],
                                    f'⚠️ У {text} уже активен Premium ({info if info else "после авторизации"}). Укажите другой ник.',
                                )
                            else:
                                state["temp_nick"] = text
                                _cooldown(state["chat_id"])
                                account.send_message(
                                    state["chat_id"],
                                    f'Вы указали: "{text}". Если верно — напишите "+", иначе пришлите новый тег.',
                                )

            except Exception as e:
                logger.exception(f"❌ Ошибка обработки события: {e}")

if __name__ == "__main__":
    main()
