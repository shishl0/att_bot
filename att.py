import signal
import sys
import time
import requests
import argparse
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

driver = None
shutdown_called = False

def create_optimized_chrome():
    options = ChromeOptions()
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
    return webdriver.Chrome(options=options)


def wait_for_internet(url="https://wsp.kbtu.kz", initial_delay=3):
    delay = initial_delay
    attempt = 1
    while True:
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                if attempt != 1:
                    print(f"[INFO] Подключение восстановлено (попытка {attempt}).", flush=True)
                return
        except requests.RequestException:
            print(f"[WARN] Нет подключения. Повтор через {delay} сек...", flush=True)
            time.sleep(delay)
            delay += 3
            attempt += 1


def shutdown(signum=None, frame=None):
    global shutdown_called, driver
    if shutdown_called:
        return
    shutdown_called = True
    print("\n[EXIT] Завершение работы...", flush=True)
    try:
        if driver:
            driver.quit()
    except Exception:
        pass
    sys.exit(0)


def verificate(driver, username, password):
    try:
        wait_for_internet()
        driver.get("https://wsp.kbtu.kz")

        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.v-overlay-container"))
        )

        login_button = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.v-slot.v-align-right div[role='button']"))
        )
        driver.execute_script("arguments[0].click();", login_button)

        username_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input.v-filterselect-input"))
        )
        driver.execute_script("""
            arguments[0].value = '';
            arguments[0].dispatchEvent(new Event('input'));
            arguments[0].dispatchEvent(new Event('change'));
        """, username_input)
        username_input.send_keys(username)
        time.sleep(0.4)

        password_input = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password'].v-textfield"))
        )
        password_input.clear()
        password_input.send_keys(password)

        enter_button = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.v-button.v-widget.primary.v-button-primary"))
        )
        enter_button.click()

        WebDriverWait(driver, 5).until(
            lambda d: d.current_url == "https://wsp.kbtu.kz/News"
        )
        print(f"[INFO] Верификация прошла успешно.", flush=True)
        return True

    except TimeoutException:
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.v-window.global-error"))
            )
            print(f"[INFO] Верификация неуспешна: неверный логин или пароль.", flush=True)
        except TimeoutException:
            print(f"[INFO] Верификация неуспешна: неизвестная ошибка.", flush=True)
        return False

    except WebDriverException as e:
        print(f"[ERROR] WebDriver ошибка: {e}", flush=True)
        return False

    except Exception as e:
        print(f"[ERROR] Ошибка при попытке логина: {e}", flush=True)
        return False


def attend(username, password):
    global driver
    driver = create_optimized_chrome()

    try:
        wait_for_internet()
        if not verificate(driver, username, password):
            return

        driver.get("https://wsp.kbtu.kz/RegistrationOnline")

        while True:
            current_time_str = datetime.now().strftime("%H:%M:%S")
            try:
                wait_for_internet()
                attend_button = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//div[contains(@class, 'v-button') and contains(@class, 'primary') and contains(@class, 'v-button-primary') and .//span[text()='Отметиться']]"))
                )
            except TimeoutException:
                print(f"[{current_time_str}] Нету аттенданса. Пробуем заново через ~30 сек.", flush=True)
                driver.refresh()
                time.sleep(20)
                continue

            try:
                parent_card = attend_button.find_element(By.XPATH, "./ancestor::div[contains(@class, 'card')]")
                info_element = parent_card.find_element(By.XPATH, ".//div[contains(@class, 'v-label') and contains(@class, 'bold')]")
                info_text = info_element.text.strip()
                info_lines = info_text.split("\n")
                lesson_title = info_lines[0].strip() if len(info_lines) > 0 else "Unknown"
                teacher = info_lines[1].strip() if len(info_lines) > 1 else "Unknown"
                lesson_time = info_lines[2].strip() if len(info_lines) > 2 else "Unknown"

                attend_button.click()
                print(f"[{current_time_str}] ✅ Отметился на \"{lesson_title}\" с \"{teacher}\" ({lesson_time})", flush=True)
                time.sleep(0.2)
            except Exception as e:
                print(f"[ERROR] Ошибка при отметке: {e}", flush=True)

    except Exception as e:
        print(f"[FATAL] Критическая ошибка: {e}", flush=True)
    finally:
        shutdown()


signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Автоматическая система отметки посещаемости KBTU")
    parser.add_argument("-u", "--username", required=True, help="Имя пользователя (логин)")
    parser.add_argument("-p", "--password", required=True, help="Пароль")
    args = parser.parse_args()

    attend(args.username, args.password)