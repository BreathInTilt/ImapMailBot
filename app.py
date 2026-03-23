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

STATE_FILE = Path("/app/data/state.json")
TMP_DIR = Path("/app/data/tmp")
TMP_DIR.mkdir(parents=True, exist_ok=True)


def require_env():
    required = {
        "IMAP_HOST": IMAP_HOST,
        "EMAIL_LOGIN": EMAIL_LOGIN,
        "EMAIL_PASSWORD": EMAIL_PASSWORD,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_uid": 0, "initialized": False}



def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")



def decode_mime_header(value: str) -> str:
    if not value:
        return ""
    result = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)



def safe_filename(name: str) -> str:
    name = decode_mime_header(name or "attachment")
    name = name.replace("/", "_").replace("\\", "_").strip()
    return name or "attachment"



def strip_html(html_text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?(</\\1>)", " ", html_text)
    text = re.sub(r"(?s)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)</p>", "\n", text)
    text = re.sub(r"(?s)<.*?>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()



def collapse_text(text: str, limit: int = 900) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        return text[:limit] + "..."
    return text



def telegram_api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"



def send_telegram_message(text: str):
    response = requests.post(
        telegram_api_url("sendMessage"),
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=60,
    )
    response.raise_for_status()



def send_telegram_document(file_path: Path, caption: str = ""):
    mime_type, _ = mimetypes.guess_type(str(file_path))
    with file_path.open("rb") as f:
        response = requests.post(
            telegram_api_url("sendDocument"),
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption[:1024],
                "parse_mode": "HTML",
            },
            files={
                "document": (file_path.name, f, mime_type or "application/octet-stream")
            },
            timeout=300,
        )
    response.raise_for_status()



def connect_imap():
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(EMAIL_LOGIN, EMAIL_PASSWORD)
    status, _ = mail.select(MAILBOX)
    if status != "OK":
        raise RuntimeError(f"Cannot select mailbox: {MAILBOX}")
    return mail



def search_uids(mail, since_uid: int = 0):
    criteria = []
    if ONLY_UNSEEN:
        criteria.append("UNSEEN")

    if since_uid > 0:
        uid_filter = f"UID {since_uid + 1}:*"
    else:
        uid_filter = "UID 1:*"

    joined = " ".join(criteria).strip()
    query = f"({joined})" if joined else "ALL"

    status, data = mail.uid("search", None, query, uid_filter)
    if status != "OK":
        raise RuntimeError("Failed to search emails by UID")

    raw = data[0].split() if data and data[0] else []
    return [int(x.decode()) for x in raw]



def fetch_email_by_uid(mail, uid: int):
    status, msg_data = mail.uid("fetch", str(uid), "(RFC822)")
    if status != "OK" or not msg_data or not msg_data[0]:
        return None
    raw_email = msg_data[0][1]
    return email.message_from_bytes(raw_email)



def extract_text_and_attachments(msg, uid: int):
    text_plain = []
    text_html = []
    attachments = []

    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition", ""))
        content_type = part.get_content_type()
        filename = part.get_filename()

        if filename:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            attachments.append({
                "filename": safe_filename(filename),
                "bytes": payload,
                "content_type": content_type,
            })
            continue

        if "attachment" in content_disposition.lower():
            continue

        payload = part.get_payload(decode=True)
        if payload is None:
            continue

        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except Exception:
            decoded = payload.decode("utf-8", errors="replace")

        if content_type == "text/plain":
            text_plain.append(decoded)
        elif content_type == "text/html":
            text_html.append(decoded)

    if text_plain:
        body = "\n\n".join(text_plain)
    elif text_html:
        body = strip_html("\n\n".join(text_html))
    else:
        body = ""

    return collapse_text(body), attachments



def save_attachment(uid: int, filename: str, content: bytes) -> Path:
    uid_dir = TMP_DIR / str(uid)
    uid_dir.mkdir(parents=True, exist_ok=True)
    file_path = uid_dir / filename
    counter = 1
    while file_path.exists():
        stem = file_path.stem
        suffix = file_path.suffix
        file_path = uid_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    file_path.write_bytes(content)
    return file_path



def cleanup_attachments(uid: int):
    uid_dir = TMP_DIR / str(uid)
    if not uid_dir.exists():
        return
    for child in uid_dir.iterdir():
        try:
            child.unlink()
        except Exception:
            pass
    try:
        uid_dir.rmdir()
    except Exception:
        pass



def format_email_message(sender: str, subject: str, date_text: str, snippet: str, attachment_count: int) -> str:
    parts = [
        "📬 <b>Новое письмо</b>",
        f"<b>От:</b> {html.escape(sender)}",
        f"<b>Тема:</b> {html.escape(subject or 'Без темы')}",
    ]
    if date_text:
        parts.append(f"<b>Дата:</b> {html.escape(date_text)}")
    if attachment_count:
        parts.append(f"<b>Вложений:</b> {attachment_count}")
    if SEND_TEXT_BODY and snippet:
        parts.append("")
        parts.append(html.escape(snippet))
    return "\n".join(parts)



def process_uid(mail, uid: int):
    msg = fetch_email_by_uid(mail, uid)
    if msg is None:
        return

    subject = decode_mime_header(msg.get("Subject", "Без темы"))
    from_header = decode_mime_header(msg.get("From", "Неизвестный отправитель"))
    sender_name, sender_email = parseaddr(from_header)
    sender = f"{sender_name} <{sender_email}>" if sender_email else from_header
    date_text = decode_mime_header(msg.get("Date", ""))

    snippet, attachments = extract_text_and_attachments(msg, uid)

    send_telegram_message(
        format_email_message(sender, subject, date_text, snippet, len(attachments))
    )

    if FORWARD_ATTACHMENTS and attachments:
        max_bytes = MAX_ATTACHMENT_MB * 1024 * 1024
        try:
            for item in attachments:
                if len(item["bytes"]) > max_bytes:
                    send_telegram_message(
                        "📎 <b>Вложение слишком большое</b>\n"
                        f"<b>Файл:</b> {html.escape(item['filename'])}\n"
                        f"<b>Лимит:</b> {MAX_ATTACHMENT_MB} MB"
                    )
                    continue

                path = save_attachment(uid, item["filename"], item["bytes"])
                caption = (
                    f"📎 <b>Вложение из письма</b>\n"
                    f"<b>Тема:</b> {html.escape(subject or 'Без темы')}\n"
                    f"<b>Файл:</b> {html.escape(path.name)}"
                )
                send_telegram_document(path, caption=caption)
        finally:
            cleanup_attachments(uid)



def bootstrap_last_uid(mail, state):
    uids = search_uids(mail, 0)
    if not uids:
        state["initialized"] = True
        state["last_uid"] = 0
        save_state(state)
        return 0

    last_uid = max(uids) if SKIP_OLD_ON_FIRST_RUN else 0
    state["initialized"] = True
    state["last_uid"] = last_uid
    save_state(state)
    return last_uid



def run_loop():
    require_env()
    state = load_state()

    while True:
        mail = None
        try:
            mail = connect_imap()

            if not state.get("initialized", False):
                last_uid = bootstrap_last_uid(mail, state)
                if SKIP_OLD_ON_FIRST_RUN:
                    send_telegram_message(
                        f"✅ mail2tg запущен. Стартовая точка установлена на UID {last_uid}. Старые письма пропущены."
                    )
                else:
                    send_telegram_message("✅ mail2tg запущен. Начинаю обработку писем с начала ящика.")
            else:
                last_uid = int(state.get("last_uid", 0))

            new_uids = search_uids(mail, last_uid)
            for uid in sorted(new_uids):
                process_uid(mail, uid)
                state["last_uid"] = uid
                save_state(state)

        except Exception as exc:
            try:
                send_telegram_message(f"⚠️ Ошибка mail2tg: {html.escape(str(exc))}")
            except Exception:
                pass
        finally:
            try:
                if mail is not None:
                    mail.logout()
            except Exception:
                pass

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_loop()
