# Vaadin HTTP Client (No Browser)

Асинхронный клиент на `aiohttp`, повторяющий Vaadin-логин **без Selenium/браузера**.

## Установка
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Запуск
CLI:
```bash
python vaadin_client.py --base https://wsp.kbtu.kz -u name -p 'password'
```

Или пример:
```bash
python run_login.py
```

## Примечания
- Клиент автоматически извлекает `v-uiId` и `Vaadin-Security-Key` (CSRF) из bootstrap/UIDL,
  ведёт `syncId/clientId`, находит server-id полей (`ComboBox` «Пользователь»), `PasswordField`, кнопку «Вход».
- После клика "Вход" сервер может сделать реальную навигацию (GET /News). Клиент валидирует сессию запросом.
