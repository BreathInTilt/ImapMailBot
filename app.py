import imaplib
import email
import os
import time
import json
import re
import html
import mimetypes
from pathlib import Path
from email.header import decode_header
from email.utils import parseaddr
from dotenv import load_dotenv
import requests
import logging
from datetime import datetime
from html import escape
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
import tempfile


load_dotenv()
IMAP_HOST = os.getenv("IMAP_HOST", "")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
EMAIL_LOGIN = os.getenv("EMAIL_LOGIN", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
MAILBOX = os.getenv("MAILBOX", "INBOX")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MAX_ATTACHMENT_MB = int(os.getenv("MAX_ATTACHMENT_MB", "20"))
FORWARD_ATTACHMENTS = os.getenv("FORWARD_ATTACHMENTS", "true").lower() == "true"
SEND_TEXT_BODY = os.getenv("SEND_TEXT_BODY", "true").lower() == "true"
ONLY_UNSEEN = os.getenv("ONLY_UNSEEN", "false").lower() == "true"
SKIP_OLD_ON_FIRST_RUN = os.getenv("SKIP_OLD_ON_FIRST_RUN", "true").lower() == "true"
SEND_ATTACHMENTS = os.getenv("SEND_ATTACHMENTS", "true").lower() == "true"
STATE_FILE = Path("/app/data/state.json")
TMP_DIR = Path("/app/data/tmp")
TMP_DIR.mkdir(parents=True, exist_ok=True)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


class PrettyFormatter(logging.Formatter):
    LEVEL_ICONS = {
        "DEBUG": "🔎",
        "INFO": "ℹ️ ",
        "WARNING": "⚠️ ",
        "ERROR": "❌",
        "CRITICAL": "🔥",
    }

    def format(self, record):
        icon = self.LEVEL_ICONS.get(record.levelname, "•")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = record.getMessage()
        return f"{timestamp} | {icon} {record.levelname:<8} | {message}"


logger = logging.getLogger("mail2tg")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger.handlers.clear()

handler = logging.StreamHandler()
handler.setFormatter(PrettyFormatter())
logger.addHandler(handler)
logger.propagate = False


def mask_email(value: str) -> str:
    if not value or "@" not in value:
        return value or "unknown"
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "*"
    else:
        masked_local = local[:2] + "*" * max(1, len(local) - 2)
    return f"{masked_local}@{domain}"


def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            logger.info(
                f"Загружено состояние: last_uid={state.get('last_uid', 0)}"
            )
            return state
        except Exception as e:
            logger.warning(f"Не удалось прочитать state.json, будет создан новый: {e}")
    else:
        logger.info("Файл состояния не найден, будет создан новый.")
    return {"last_uid": 0, "initialized": False}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    logger.debug(
        f"Состояние сохранено: last_uid={state.get('last_uid', 0)}, "
        f"initialized={state.get('initialized', False)}"
    )


def decode_mime_header(value):
    if not value:
        return ""
    fragments = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            fragments.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            fragments.append(part)
    return "".join(fragments).strip()


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def strip_pmfu_service_footer(text: str) -> str:
    patterns = [
        r'Ово је копија поруке објављене на форуму.*$',
        r'You are receiving this because you are subscribed to the forum.*$',
        r'Ова порука вам је послата зато што сте претплаћени.*$',
        r'Промените своје преференције везане за резиме порука са форума:.*$',
    ]

    result = text
    for pattern in patterns:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE | re.DOTALL).strip()

    return result


def telegram_html_escape_preserve_links(text: str) -> str:
    if not text:
        return ""

    url_pattern = re.compile(r'https?://[^\s<>"\]]+')
    parts = []
    last = 0

    for match in url_pattern.finditer(text):
        start, end = match.span()
        url = match.group(0)

        if start > last:
            parts.append(escape(text[last:start]))

        safe_url = escape(url, quote=True)
        parts.append(f'<a href="{safe_url}">ссылка</a>')
        last = end

    if last < len(text):
        parts.append(escape(text[last:]))

    return "".join(parts)


def shorten_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    urls = re.findall(r'https?://[^\s<>"\]]+', text)
    result = []
    seen = set()
    for url in urls:
        cleaned = url.rstrip(".,);]")
        if cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def remove_urls(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'https?://[^\s<>"\]]+', '', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()


def first_real_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not re.fullmatch(r"-{5,}", line):
            return line
    return ""


def parse_pmfu_sender(sender_value: str) -> str:
    name, email_addr = parseaddr(sender_value or "")
    if name:
        name = name.replace("(преко еПМФ)", "").replace("(via ePMF)", "").strip()
        return name
    return email_addr or "ePMF"


def parse_pmfu_message_structure(raw_text: str) -> dict:
    text = normalize_whitespace(raw_text)
    text = strip_pmfu_service_footer(text)

    parts = re.split(r"\s*-{10,}\s*", text)
    header_part = parts[0].strip() if parts else text
    body_part = parts[1].strip() if len(parts) > 1 else ""

    header_urls = extract_urls(header_part)
    body_urls = extract_urls(body_part)

    header_clean = remove_urls(header_part)

    forum_title = ""
    author_line = ""
    pre_re = header_clean

    m_re = re.search(r'Re:\s+"?([^"\n]+?)"?\s*-\s*([^\n]+)$', header_clean)
    if m_re:
        author_line = m_re.group(1).strip()
        date_line = m_re.group(2).strip()
        pre_re = header_clean[:m_re.start()].strip()
    else:
        date_line = ""

    path_segments = [seg.strip() for seg in pre_re.split("->") if seg.strip()]
    discussion_title = ""
    course_path = ""

    if path_segments:
        discussion_title = path_segments[-1]
        if len(path_segments) > 1:
            course_path = " → ".join(path_segments[:-1])
    else:
        discussion_title = ""

    discussion_title = discussion_title.strip('" ').strip()
    discussion_link = header_urls[0] if header_urls else ""

    body = strip_pmfu_service_footer(body_part)
    body = normalize_whitespace(body)

    if not body:
        body = ""

    return {
        "course_path": course_path,
        "discussion_title": discussion_title,
        "discussion_link": discussion_link,
        "author_line": author_line,
        "date_line": date_line,
        "body": body,
        "body_urls": body_urls,
    }


def format_pmfu_forum_message(msg_dict):
    raw_date = (msg_dict.get("date") or "").strip()
    raw_snippet = (msg_dict.get("snippet") or "").strip()
    sender_name = parse_pmfu_sender(msg_dict.get("from") or "")

    parsed = parse_pmfu_message_structure(raw_snippet)

    discussion_title = parsed["discussion_title"] or "Обавештење"
    course_path = parsed["course_path"]
    discussion_link = parsed["discussion_link"]
    author_line = parsed["author_line"]
    body = parsed["body"]
    date_line = parsed["date_line"] or raw_date

    lines = [
        "📚 <b>ePMF</b>",
        f"<b>{escape(discussion_title)}</b>",
    ]

    meta = []
    if course_path:
        meta.append(f"🎓 {escape(course_path)}")
    if sender_name:
        meta.append(f"👤 {escape(sender_name)}")
    if date_line:
        meta.append(f"🕒 {escape(date_line)}")

    if meta:
        lines.append("\n".join(meta))

    if discussion_link:
        safe_link = escape(discussion_link, quote=True)
        lines.append(f'🔗 <a href="{safe_link}">Открыть тему</a>')

    if body:
        clean_body = shorten_text(body, 1200)
        lines.append("")
        lines.append(telegram_html_escape_preserve_links(clean_body))

    return "\n".join(lines)


def html_message_from_mail(msg_dict):
    sender_raw = msg_dict.get("from", "") or ""
    sender = sender_raw.lower()

    if "noreply@pmf.uns.ac.rs" in sender or "епмф" in sender or "epmf" in sender:
        return format_pmfu_forum_message(msg_dict)

    subject = escape(msg_dict.get("subject", "Без темы"))
    sender_pretty = escape(msg_dict.get("from", "Неизвестный отправитель"))
    date_str = escape(msg_dict.get("date", ""))
    snippet = telegram_html_escape_preserve_links(msg_dict.get("snippet", ""))

    lines = [
        "📬 <b>Новое письмо</b>",
        f"<b>От:</b> {sender_pretty}",
        f"<b>Тема:</b> {subject}",
    ]
    if date_str:
        lines.append(f"<b>Дата:</b> {date_str}")
    if snippet:
        lines.append("")
        lines.append(snippet)

    return "\n".join(lines)


def send_telegram_message(text):
    logger.debug("Отправка текстового уведомления в Telegram...")
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=60,
    )
    response.raise_for_status()
    logger.info("Текстовое уведомление успешно отправлено в Telegram.")


def send_telegram_document(file_path: Path, caption: str | None = None):
    file_size_mb = file_path.stat().st_size / (1024 * 1024)
    logger.debug(
        f"Отправка вложения в Telegram: {file_path.name} ({file_size_mb:.2f} MB)"
    )

    with file_path.open("rb") as f:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption or "",
                "parse_mode": "HTML",
            },
            files={"document": (file_path.name, f)},
            timeout=300,
        )
    response.raise_for_status()
    logger.info(f"Вложение отправлено в Telegram: {file_path.name}")


def connect_imap():
    logger.info(
        f"Подключение к IMAP: host={IMAP_HOST}, port={IMAP_PORT}, "
        f"user={mask_email(EMAIL_LOGIN)}"
    )
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(EMAIL_LOGIN, EMAIL_PASSWORD)
    status, _ = mail.select(MAILBOX)

    if status != "OK":
        raise RuntimeError(f"Не удалось открыть mailbox '{MAILBOX}'")

    logger.info(f"Успешное подключение к mailbox: {MAILBOX}")
    return mail


def get_latest_uid(mail):
    status, data = mail.uid("search", None, "ALL")
    if status != "OK":
        raise RuntimeError("Не удалось получить UID писем")

    raw = data[0].split()
    latest_uid = int(raw[-1].decode()) if raw else 0
    logger.info(f"Последний UID в ящике: {latest_uid}")
    return latest_uid


def extract_text_snippet(msg):
    snippet = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if content_type == "text/plain" and "attachment" not in disposition.lower():
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    snippet = payload.decode(charset, errors="replace").strip()
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            snippet = payload.decode(charset, errors="replace").strip()

    snippet = " ".join(snippet.split())
    if len(snippet) > 4000:
        snippet = snippet[:4000] + "..."
    return snippet


def parse_email_date(raw_date: str) -> str:
    if not raw_date:
        return ""
    try:
        dt = parsedate_to_datetime(raw_date)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return raw_date


def fetch_messages_since(mail, last_uid):
    logger.info(f"Поиск новых писем после UID={last_uid}")

    if ONLY_UNSEEN:
        status, data = mail.uid("search", None, "UNSEEN")
    else:
        status, data = mail.uid("search", None, "ALL")

    if status != "OK":
        raise RuntimeError("Не удалось выполнить поиск писем")

    raw_uids = data[0].split()
    logger.debug(f"UID от сервера: {[x.decode(errors='ignore') for x in raw_uids]}")

    filtered_uid_bytes = []
    for uid_bytes in raw_uids:
        try:
            uid = int(uid_bytes.decode())
            if uid > last_uid:
                filtered_uid_bytes.append(uid_bytes)
        except Exception:
            logger.warning(f"Пропускаю некорректный UID: {uid_bytes!r}")

    logger.info(f"Новых UID после фильтрации: {len(filtered_uid_bytes)}")

    messages = []

    for uid_bytes in filtered_uid_bytes:
        uid = int(uid_bytes.decode())
        logger.debug(f"Чтение письма UID={uid}")

        status, msg_data = mail.uid("fetch", uid_bytes, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            logger.warning(f"Не удалось загрузить письмо UID={uid}")
            continue

        raw_email = msg_data[0][1]
        msg = email.message_from_bytes(raw_email)

        subject = decode_mime_header(msg.get("Subject", "Без темы"))
        sender = decode_mime_header(msg.get("From", "Неизвестный отправитель"))
        date_raw = decode_mime_header(msg.get("Date", ""))
        date_pretty = parse_email_date(date_raw)
        snippet = extract_text_snippet(msg)

        attachments = []
        if SEND_ATTACHMENTS:
            for part in msg.walk():
                filename = part.get_filename()
                if not filename:
                    continue

                decoded_name = decode_mime_header(filename)
                payload = part.get_payload(decode=True)
                if not payload:
                    continue

                size_mb = len(payload) / (1024 * 1024)
                attachments.append({
                    "filename": decoded_name,
                    "content": payload,
                    "size_mb": size_mb,
                })

        logger.info(
            f"Письмо UID={uid}: тема='{subject[:80]}', "
            f"от='{sender[:80]}', вложений={len(attachments)}"
        )

        messages.append({
            "uid": uid,
            "subject": subject,
            "from": sender,
            "date": date_pretty,
            "snippet": snippet,
            "attachments": attachments,
        })

    return messages


def process_attachments(attachments, subject):
    if not SEND_ATTACHMENTS:
        logger.debug("Отправка вложений отключена.")
        return

    if not attachments:
        logger.debug("Во вложениях ничего нет.")
        return

    logger.info(f"Начинаю обработку вложений: {len(attachments)} шт.")

    for attachment in attachments:
        filename = attachment["filename"]
        size_mb = attachment["size_mb"]
        final_path = None

        if size_mb > MAX_ATTACHMENT_MB:
            logger.warning(
                f"Вложение пропущено: {filename} ({size_mb:.2f} MB) "
                f"превышает лимит {MAX_ATTACHMENT_MB:.2f} MB"
            )
            send_telegram_message(
                f"📎 <b>Вложение пропущено</b>\n"
                f"<b>Файл:</b> {escape(filename)}\n"
                f"<b>Причина:</b> размер {size_mb:.2f} MB превышает лимит {MAX_ATTACHMENT_MB:.2f} MB\n"
                f"<b>Письмо:</b> {escape(subject)}"
            )
            continue

        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(attachment["content"])
                tmp_path = Path(tmp.name)

            final_path = tmp_path.with_name(filename)
            tmp_path.rename(final_path)

            send_telegram_document(
                final_path,
                caption=f"📎 Вложение из письма: <b>{escape(subject)}</b>"
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке вложения '{filename}': {e}")
            send_telegram_message(
                f"⚠️ <b>Ошибка отправки вложения</b>\n"
                f"<b>Файл:</b> {escape(filename)}\n"
                f"<b>Письмо:</b> {escape(subject)}\n"
                f"<b>Ошибка:</b> {escape(str(e))}"
            )
        finally:
            try:
                if final_path is not None and final_path.exists():
                    final_path.unlink()
            except Exception:
                pass

    logger.info("Обработка вложений завершена.")


def validate_env():
    required = {
        "IMAP_HOST": IMAP_HOST,
        "EMAIL_LOGIN": EMAIL_LOGIN,
        "EMAIL_PASSWORD": EMAIL_PASSWORD,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }

    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Не заданы обязательные переменные окружения: {', '.join(missing)}")


def main():
    logger.info("Запуск сервиса mail2tg...")
    validate_env()

    state = load_state()
    last_uid = state.get("last_uid", 0)
    initialized = state.get("initialized", False)

    logger.info(
        f"Конфигурация: mailbox={MAILBOX}, interval={CHECK_INTERVAL}s, "
        f"attachments={'on' if SEND_ATTACHMENTS else 'off'}, "
        f"max_attachment={MAX_ATTACHMENT_MB}MB, "
        f"skip_old_on_first_start={SKIP_OLD_ON_FIRST_RUN}"
    )

    while True:
        mail = None
        try:
            mail = connect_imap()

            if not initialized and SKIP_OLD_ON_FIRST_RUN:
                latest_uid = get_latest_uid(mail)
                state["last_uid"] = latest_uid
                state["initialized"] = True
                save_state(state)

                last_uid = latest_uid
                initialized = True

                logger.info(
                    "Первый запуск: старая почта пропущена, сервис будет слушать только новые письма."
                )
            elif not initialized:
                state["initialized"] = True
                save_state(state)
                initialized = True
                logger.info("Первый запуск: старая почта НЕ пропускается.")

            messages = fetch_messages_since(mail, last_uid)

            if not messages:
                logger.info("Новых писем нет.")
            else:
                logger.info(f"Начинаю обработку новых писем: {len(messages)} шт.")

            for msg in messages:
                logger.info(
                    f"Отправка уведомления по письму UID={msg['uid']} "
                    f"с темой '{msg['subject'][:80]}'"
                )
                if msg["uid"] <= last_uid:
                    logger.warning(
                        f"Пропуск письма UID={msg['uid']}, так как оно уже обработано (last_uid={last_uid})"
                    )
                    continue
                send_telegram_message(html_message_from_mail(msg))
                process_attachments(msg["attachments"], msg["subject"])

                last_uid = max(last_uid, msg["uid"])
                state["last_uid"] = last_uid
                save_state(state)

                logger.info(f"Письмо UID={msg['uid']} успешно обработано.")

        except Exception as e:
            logger.error(f"Сбой в основном цикле: {e}")
            try:
                send_telegram_message(
                    f"⚠️ <b>Ошибка mail2tg</b>\n<code>{escape(str(e))}</code>"
                )
            except Exception as tg_error:
                logger.error(f"Не удалось отправить сообщение об ошибке в Telegram: {tg_error}")
        finally:
            try:
                if mail is not None:
                    mail.logout()
                    logger.debug("IMAP-сессия закрыта.")
            except Exception as logout_error:
                logger.warning(f"Ошибка при закрытии IMAP-сессии: {logout_error}")

        logger.info(f"Ожидание {CHECK_INTERVAL} сек. до следующей проверки...")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()