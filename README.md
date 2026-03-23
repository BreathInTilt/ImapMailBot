# mail2tg — пересылка университетской почты в Telegram

Небольшой Docker-сервис, который:

- подключается к почте по IMAP;
- ищет новые письма в `INBOX`;
- отправляет в Telegram краткую карточку письма;
- при необходимости пересылает вложения отдельными сообщениями;
- запоминает последний обработанный UID, чтобы не было дублей после перезапуска.

## Возможности

- пересылка темы, отправителя, даты и фрагмента текста письма;
- пересылка вложений в Telegram;
- ограничение максимального размера вложения;
- опциональная обработка только непрочитанных писем;
- безопасный запуск в Docker на Ubuntu/VPS.

## Структура

```text
mail2tg-attachments/
├── app.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── README.md
└── data/
```

## Подготовка

### 1. Создай Telegram-бота

Через `@BotFather` создай бота и получи `TELEGRAM_BOT_TOKEN`.

### 2. Узнай свой `chat_id`

1. Напиши боту любое сообщение.
2. Открой в браузере:

```text
https://api.telegram.org/bot<ТВОЙ_ТОКЕН>/getUpdates
```

3. Найди `chat.id`.

### 3. Подготовь `.env`

Скопируй пример и заполни своими данными:

```bash
cp .env.example .env
nano .env
```

Пример:

```env
IMAP_HOST=imap.example.edu
IMAP_PORT=993
EMAIL_LOGIN=your_email@example.edu
EMAIL_PASSWORD=your_password_or_app_password
MAILBOX=INBOX
CHECK_INTERVAL=30
TELEGRAM_BOT_TOKEN=123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
TELEGRAM_CHAT_ID=123456789
MAX_ATTACHMENT_MB=20
FORWARD_ATTACHMENTS=true
SEND_TEXT_BODY=true
ONLY_UNSEEN=false
SKIP_OLD_ON_FIRST_RUN=true
```

## Значение переменных

- `IMAP_HOST` — адрес IMAP-сервера университета.
- `IMAP_PORT` — обычно `993`.
- `EMAIL_LOGIN` — логин почты.
- `EMAIL_PASSWORD` — пароль или app password.
- `MAILBOX` — обычно `INBOX`.
- `CHECK_INTERVAL` — интервал проверки в секундах.
- `TELEGRAM_BOT_TOKEN` — токен бота.
- `TELEGRAM_CHAT_ID` — твой chat id.
- `MAX_ATTACHMENT_MB` — максимальный размер одного вложения.
- `FORWARD_ATTACHMENTS` — отправлять ли вложения в Telegram.
- `SEND_TEXT_BODY` — отправлять ли фрагмент текста письма.
- `ONLY_UNSEEN` — проверять только непрочитанные письма.
- `SKIP_OLD_ON_FIRST_RUN` — при первом запуске пропустить старые письма и начать только с новых.

## Запуск

```bash
docker compose up -d --build
```

## Логи

```bash
docker compose logs -f
```

## Остановка

```bash
docker compose down
```

## Как это работает

Сервис подключается к `MAILBOX`, ищет письма по UID и отправляет:

1. уведомление с полями:
   - от кого;
   - тема;
   - дата;
   - фрагмент текста;
2. затем, если включено `FORWARD_ATTACHMENTS=true`, пересылает все найденные вложения отдельными сообщениями в Telegram.

Информация о последнем обработанном UID сохраняется в `data/state.json`.

## Что важно знать

- Если у университета Microsoft 365 / Exchange, обычный пароль может не сработать. Тогда нужен app password или другой способ авторизации.
- Telegram может не принять слишком большие файлы. В этом случае бот пришлёт текстовое уведомление, что вложение слишком большое.
- Если в письме только HTML, сервис попробует извлечь читаемый текст и отправить укороченный фрагмент.

## Полезные доработки

При желании можно легко добавить:

- фильтр только по определённым отправителям;
- отдельный whitelist доменов преподавателей;
- команды `/status`, `/last`, `/pause`;
- пересылку только писем с темой вроде "exam", "kolokvijum", "studentska služba";
- загрузку вложений в папку на сервере.
