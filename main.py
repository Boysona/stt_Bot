# main.py
import os
import uuid
import logging
import requests
import telebot
import json
from flask import Flask, request, abort, render_template_string, jsonify
from datetime import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
import threading
import time
import io
import re
from pymongo import MongoClient
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
import traceback
import tempfile  # added
import shutil    # optional fast copy if needed

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

GEMINI_API_KEY = "AIzaSyDpb3UvnrRgk6Fu61za_VrRN8byZRSyq_I"
ASSEMBLYAI_API_KEY = "401f03e8f03c4519b603c896973b41e5"
BOT_TOKEN = "7790991731:AAF4NHGm0BJCf08JTdBaUWKzwfs82_Y9Ecw"
WEBHOOK_BASE = "https://stt-bot-ckt1.onrender.com"
ADMIN_ID = 6964068910
REQUIRED_CHANNEL = "@boyso20Channel"
SECRET_KEY = "super-secret-please-change"
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024
MONGO_URI = "mongodb+srv://hoskasii:GHyCdwpI0PvNuLTg@cluster0.dy7oe7t.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "telegram_bot_db"

MAX_WEB_UPLOAD_MB = 250

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
users_collection = db["users"]
tokens_collection = db["tokens"]

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, parse_mode='HTML')
serializer = URLSafeTimedSerializer(SECRET_KEY)

admin_state = {}
user_transcriptions = {}  # structure: { "<chat_id_str>": { "<message_id_or_token_str>": "transcribed text" } }
admin_button_state = {"translate": True, "summarize": True}

LANG_OPTIONS = [
    ("🇬🇧 English", "en"),
    ("🇩🇪 Deutsch", "de"),
    ("🇮🇳 हिन्दी", "hi"),
    ("🇷🇺 Русский", "ru"),
    ("🇮🇷 فارسی", "fa"),
    ("🇮🇩 Indonesia", "id"),
    ("🇸🇴 Somali", "so"),
    ("🇦🇿 Azərbaycan", "az"),
    ("🇮🇹 Italiano", "it"),
    ("🇹🇷 Türkçe", "tr"),
    ("🇧🇬 Български", "bg"),
    ("🇷🇸 Srpski", "sr"),
    ("🇫🇷 Français", "fr"),
    ("🇸🇦 العربية", "ar"),
    ("🇪🇸 Español", "es"),
    ("🇵🇰 اردو", "ur"),
    ("🇹🇭 ไทย", "th"),
    ("🇻🇳 Tiếng Việt", "vi"),
    ("🇯🇵 日本語", "ja"),
    ("🇰🇷 한국어", "ko"),
    ("🇨🇳 中文", "zh"),
    ("🇳🇱 Nederlands", "nl"),
    ("🇸🇪 Svenska", "sv"),
    ("🇳🇴 Norsk", "no"),
    ("🇩🇰 Dansk", "da"),
    ("🇫🇮 Suomi", "fi"),
    ("🇵🇱 Polski", "pl"),
    ("🇬🇷 Ελληνικά", "el"),
    ("🇨🇿 Čeština", "cs"),
    ("🇭🇺 Magyar", "hu"),
    ("🇷🇴 Română", "ro"),
    ("🇲🇾 Melayu", "ms"),
    ("🇺🇿 O'zbekcha", "uz"),
    ("🇵🇭 Tagalog", "tl"),
    ("🇵🇹 Português", "pt")
]

CODE_TO_LABEL = {code: label for (label, code) in LANG_OPTIONS}
LABEL_TO_CODE = {label: code for (label, code) in LANG_OPTIONS}

STT_LANGUAGES = {}
for label, code in LANG_OPTIONS:
    STT_LANGUAGES[label.split(" ", 1)[-1]] = {
        "code": code,
        "emoji": label.split(" ", 1)[0],
        "native": label.split(" ", 1)[-1]
    }

memory_lock = threading.Lock()
in_memory_data = {"pending_media": {}}

def log_callback(call):
    try:
        user = getattr(call.from_user, 'id', None)
        username = getattr(call.from_user, 'username', None)
        data = getattr(call, 'data', None)
        msg = getattr(call, 'message', None)
        chat_id = getattr(msg.chat, 'id', None) if msg else None
        message_id = getattr(msg, 'message_id', None) if msg else None
        logging.info(f"Callback received - from_user: {user} ({username}), chat_id: {chat_id}, message_id: {message_id}, data: {data}")
    except Exception:
        logging.exception("Failed to log callback")

def ask_gemini(text: str, instruction: str, timeout=60) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": instruction},
                    {"text": text}
                ]
            }
        ]
    }
    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    result = resp.json()
    if "candidates" in result and isinstance(result["candidates"], list) and len(result["candidates"]) > 0:
        cand = result['candidates'][0]
        try:
            return cand['content']['parts'][0]['text']
        except Exception:
            return json.dumps(cand)
    return json.dumps(result)

def chunk_text(text: str, max_chars: int = 24000):
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start+max_chars])
        start += max_chars
    return chunks

def translate_large_text_with_gemini(text: str, target_lang_name: str):
    chunks = chunk_text(text, max_chars=24000)
    translated_chunks = []
    for i, chunk in enumerate(chunks):
        instr = f"Translate the following text to {target_lang_name}. Only provide the translation for this chunk. Chunk {i+1}/{len(chunks)}:"
        res = ask_gemini(chunk, instr)
        if res is None:
            raise RuntimeError("No response from Gemini for translation chunk")
        translated_chunks.append(res)
    combined = "\n\n".join(translated_chunks)
    final_instr = f"Combine and polish the following translated chunks into one coherent translation in {target_lang_name}. Only provide the translation:"
    final = ask_gemini(combined, final_instr)
    return final

def summarize_large_text_with_gemini(text: str, target_lang_name: str):
    chunks = chunk_text(text, max_chars=24000)
    partial_summaries = []
    for i, chunk in enumerate(chunks):
        instr = f"Summarize the following text in {target_lang_name}. Provide a concise summary (short). Chunk {i+1}/{len(chunks)}:"
        res = ask_gemini(chunk, instr)
        if res is None:
            raise RuntimeError("No response from Gemini for summary chunk")
        partial_summaries.append(res)
    combined = "\n\n".join(partial_summaries)
    final_instr = f"Combine and polish these partial summaries into a single concise summary in {target_lang_name}. Only provide the summary:"
    final = ask_gemini(combined, final_instr)
    return final

def update_user_activity(user_id: int):
    user_id_str = str(user_id)
    now = datetime.now()
    users_collection.update_one(
        {"_id": user_id_str},
        {"$set": {"last_active": now}, "$setOnInsert": {"first_seen": now, "stt_conversion_count": 0}},
        upsert=True
    )

def increment_processing_count(user_id: str, service_type: str):
    field_to_inc = f"{service_type}_conversion_count"
    users_collection.update_one(
        {"_id": str(user_id)},
        {"$inc": {field_to_inc: 1}}
    )

def get_stt_user_lang(user_id: str) -> str:
    user_data = users_collection.find_one({"_id": user_id})
    if user_data and "stt_language" in user_data:
        return user_data["stt_language"]
    return "en"

def set_stt_user_lang(user_id: str, lang_code: str):
    users_collection.update_one(
        {"_id": str(user_id)},
        {"$set": {"stt_language": lang_code}},
        upsert=True
    )

def user_has_stt_setting(user_id: str) -> bool:
    user_data = users_collection.find_one({"_id": user_id})
    return user_data is not None and "stt_language" in user_data

def save_pending_media(user_id: str, media_type: str, data: dict):
    with memory_lock:
        in_memory_data["pending_media"][user_id] = {
            "media_type": media_type,
            "data": data,
            "saved_at": datetime.now()
        }
    logging.info(f"Saved pending media for user {user_id}: {media_type}")

def pop_pending_media(user_id: str):
    with memory_lock:
        return in_memory_data["pending_media"].pop(user_id, None)

def delete_transcription_later(user_id: str, message_id):
    """
    message_id can be an int-like string or a token string; we store everything as strings.
    After 10 minutes remove it from memory.
    """
    try:
        mid = str(message_id)
        time.sleep(600)
        with memory_lock:
            if user_id in user_transcriptions and mid in user_transcriptions[user_id]:
                del user_transcriptions[user_id][mid]
    except Exception:
        logging.exception("Error in delete_transcription_later")

def assemblyai_upload_from_stream(stream_iterable):
    upload_url = "https://api.assemblyai.com/v2/upload"
    headers = {"authorization": ASSEMBLYAI_API_KEY}
    resp = requests.post(upload_url, headers=headers, data=stream_iterable, timeout=3600)
    resp.raise_for_status()
    return resp.json().get("upload_url")

def select_speech_model_for_lang(language_code: str):
    """
    Return speech_model string to send to AssemblyAI:
      - if language is English (starting with 'en') -> 'slam-1'
      - otherwise -> 'best'
    """
    if not language_code:
        return "best"
    lc = language_code.lower()
    if lc.startswith("en"):
        return "slam-1"
    return "best"

def create_transcript_and_wait(audio_url: str, language_code: str = None, speech_model: str = None, poll_interval=2):
    """
    Submit a transcript job and poll until completion.
    speech_model should be a string like 'slam-1', 'best', or None to let the API pick default.
    """
    create_url = "https://api.assemblyai.com/v2/transcript"
    headers = {"authorization": ASSEMBLYAI_API_KEY, "content-type": "application/json"}
    data = {"audio_url": audio_url}
    if language_code:
        data["language_code"] = language_code
    if speech_model:
        data["speech_model"] = speech_model

    resp = requests.post(create_url, headers=headers, json=data, timeout=60)
    resp.raise_for_status()
    job = resp.json()
    job_id = job.get("id")
    get_url = f"{create_url}/{job_id}"
    while True:
        r = requests.get(get_url, headers={"authorization": ASSEMBLYAI_API_KEY}, timeout=60)
        r.raise_for_status()
        status = r.json()
        st = status.get("status")
        if st == "completed":
            return status.get("text", "")
        if st == "failed":
            raise RuntimeError("Transcription failed: " + str(status.get("error", "unknown error")))
        time.sleep(poll_interval)

def telegram_file_stream(file_url, chunk_size=256*1024):
    with requests.get(file_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk

def telegram_file_info_and_url(file_id):
    f = bot.get_file(file_id)
    file_path = f.file_path
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    return f, file_url

def is_transcoding_like_error(msg: str) -> bool:
    if not msg:
        return False
    m = msg.lower()
    checks = [
        "transcoding failed",
        "file does not appear to contain audio",
        "text/html",
        "html document",
        "unsupported media type",
        "could not decode",
    ]
    return any(ch in m for ch in checks)

def build_lang_keyboard(callback_prefix: str, row_width: int = 3, message_id: str = None):
    markup = InlineKeyboardMarkup(row_width=row_width)
    buttons = []
    for label, code in LANG_OPTIONS:
        button_text = label
        if message_id is not None:
            cb = f"{callback_prefix}|{code}|{message_id}"
        else:
            cb = f"{callback_prefix}|{code}"
        buttons.append(InlineKeyboardButton(button_text, callback_data=cb))
    for i in range(0, len(buttons), row_width):
        markup.add(*buttons[i:i+row_width])
    return markup

def build_start_language_keyboard():
    return build_lang_keyboard("start_select_lang")

def build_stt_language_keyboard():
    return build_lang_keyboard("stt_lang")

def build_admin_menu():
    markup = InlineKeyboardMarkup(row_width=2)
    translate_state = "✅ Open" if admin_button_state["translate"] else "🚫 Closed"
    summarize_state = "✅ Open" if admin_button_state["summarize"] else "🚫 Closed"
    markup.add(
        InlineKeyboardButton("📊 Total Users", callback_data="admin_total_users"),
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
    )
    markup.add(
        InlineKeyboardButton(f"Translate Button: {translate_state}", callback_data="admin_toggle_translate"),
        InlineKeyboardButton(f"Summarize Button: {summarize_state}", callback_data="admin_toggle_summarize")
    )
    return markup

def signed_upload_token(chat_id: int, lang_code: str):
    payload = {"chat_id": chat_id, "lang": lang_code}
    return serializer.dumps(payload)

def unsign_upload_token(token: str, max_age_seconds: int = 3600):
    data = serializer.loads(token, max_age=max_age_seconds)
    return data

def animate_processing_message(chat_id, message_id, stop_event):
    dots = [".", "..", "..."]
    idx = 0
    while not stop_event():
        try:
            bot.edit_message_text(f"🔄 Processing{dots[idx % len(dots)]}", chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
        idx = (idx + 1) % len(dots)
        time.sleep(0.6)

def check_subscription(user_id: int) -> bool:
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Error checking subscription: {e}")
        return False

def send_subscription_message(chat_id: int):
    try:
        chat = bot.get_chat(chat_id)
    except Exception:
        chat = None
    if chat and chat.type == 'private':
        if not REQUIRED_CHANNEL or not REQUIRED_CHANNEL.strip():
            return
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton(
                "🔓 Join the Channel to unlock",
                url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"
            )
        )
        bot.send_message(
            chat_id,
            "🔒 Access Locked You cannot use this bot until you join the Channel",
            reply_markup=markup
        )

@bot.message_handler(commands=['start'])
def start_handler(message):
    try:
        update_user_activity(message.from_user.id)
        if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
            send_subscription_message(message.chat.id)
            return
        bot.send_message(
            message.chat.id,
            "Choose your file language for transcription using the below buttons:",
            reply_markup=build_start_language_keyboard()
        )
    except Exception:
        logging.exception("Error in start_handler")

@bot.message_handler(commands=['admin'])
def admin_handler(message):
    try:
        if message.from_user.id != ADMIN_ID:
            bot.send_message(message.chat.id, "🚫 You are not authorized to use this command.")
            return
        update_user_activity(message.from_user.id)
        bot.send_message(message.chat.id, "⚙️ Admin Panel", reply_markup=build_admin_menu())
    except Exception:
        logging.exception("Error in admin_handler")

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("start_select_lang|"))
def start_select_lang_callback(call):
    log_callback(call)
    try:
        uid = str(call.from_user.id)
        _, lang_code = call.data.split("|", 1)
        lang_label = CODE_TO_LABEL.get(lang_code, lang_code)
        set_stt_user_lang(uid, lang_code)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            logging.info(f"Could not delete language selection message: {e}")
        welcome_text = (
            f"👋 Salaam!    \n"
            "• Send me\n"
            "• voice message\n"
            "• audio file\n"
            "• video\n"
            "• to transcribe for free"
        )
        bot.send_message(call.message.chat.id, welcome_text)
        bot.answer_callback_query(call.id, f"✅ Language set to {lang_label}")
    except Exception:
        logging.exception("Error in start_select_lang_callback")
        try:
            bot.answer_callback_query(call.id, "❌ Error setting language", show_alert=True)
        except Exception:
            pass

@bot.message_handler(commands=['help'])
def handle_help(message):
    try:
        update_user_activity(message.from_user.id)
        if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
            send_subscription_message(message.chat.id)
            return
        text = (
            "Commands supported:\n"
            "/start - Show welcome message\n"
            "/lang  - Change language\n"
            "/help  - This help message\n\n"
            "Send a voice/audio/video (≤ 20MB for Telegram) and I will transcribe it.\n"
            "If it's larger than Telegram limits, you'll be provided a secure web upload link (supports up to 250MB) Need more help? Contact: @boyso20"
        )
        bot.send_message(message.chat.id, text)
    except Exception:
        logging.exception("Error in handle_help")

@bot.message_handler(commands=['lang'])
def handle_lang(message):
    try:
        kb = build_stt_language_keyboard()
        bot.send_message(message.chat.id, "Choose your file language for transcription using the below buttons:", reply_markup=kb)
    except Exception:
        logging.exception("Error in handle_lang")

@bot.callback_query_handler(lambda c: c.data and c.data.startswith("stt_lang|"))
def on_stt_language_select(call):
    log_callback(call)
    try:
        uid = str(call.from_user.id)
        _, lang_code = call.data.split("|", 1)
        lang_label = CODE_TO_LABEL.get(lang_code, lang_code)
        set_stt_user_lang(uid, lang_code)
        bot.answer_callback_query(call.id, f"✅ Language set: {lang_label}")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            logging.info(f"Could not delete STT language selection message: {e}")
    except Exception:
        logging.exception("Error in on_stt_language_select")
        try:
            bot.answer_callback_query(call.id, "❌ Error setting language", show_alert=True)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data in ("admin_total_users", "admin_broadcast", "admin_toggle_translate", "admin_toggle_summarize") and c.from_user.id == ADMIN_ID)
def admin_menu_callback(call):
    log_callback(call)
    try:
        chat_id = call.message.chat.id
        data = call.data.replace("admin_", "")
        if data == "total_users":
            total_registered = users_collection.count_documents({})
            bot.send_message(chat_id, f"👥 Total registered users: {total_registered}")
        elif data == "broadcast":
            admin_state[call.from_user.id] = {"state": "awaiting_broadcast_message"}
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel_broadcast"))
            bot.send_message(chat_id, "📢 Send the broadcast message now (text/photo/video/document). You can cancel anytime with the button below. After sending the content, you'll be asked to confirm before it is broadcast.", reply_markup=kb)
        elif data == "toggle_translate":
            admin_button_state["translate"] = not admin_button_state["translate"]
            status = "Open" if admin_button_state["translate"] else "Closed"
            bot.edit_message_text(f"⚙️ Admin Panel\n\nTranslate button is now: {status}",
                                  chat_id=chat_id, message_id=call.message.message_id,
                                  reply_markup=build_admin_menu())
            bot.answer_callback_query(call.id, f"Translate button is now {status}", show_alert=True)
        elif data == "toggle_summarize":
            admin_button_state["summarize"] = not admin_button_state["summarize"]
            status = "Open" if admin_button_state["summarize"] else "Closed"
            bot.edit_message_text(f"⚙️ Admin Panel\n\nSummarize button is now: {status}",
                                  chat_id=chat_id, message_id=call.message.message_id,
                                  reply_markup=build_admin_menu())
            bot.answer_callback_query(call.id, f"Summarize button is now {status}", show_alert=True)
        bot.answer_callback_query(call.id)
    except Exception:
        logging.exception("Error in admin_menu_callback")
        try:
            bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data == "admin_confirm_broadcast" and c.from_user.id == ADMIN_ID)
def admin_confirm_broadcast(call):
    log_callback(call)
    try:
        st = admin_state.get(call.from_user.id)
        if not st or "pending_broadcast" not in st:
            bot.answer_callback_query(call.id, "No pending broadcast message to send.", show_alert=True)
            return
        pending = st.get("pending_broadcast")
        preview_message_id = st.get("preview_message_id")
        admin_state.pop(call.from_user.id, None)
        if preview_message_id:
            try:
                bot.delete_message(chat_id=call.message.chat.id, message_id=preview_message_id)
            except Exception:
                logging.info("Could not delete preview message (maybe already deleted)")
        success = fail = 0
        user_ids = [doc["_id"] for doc in users_collection.find({}, {"_id": 1})]
        for uid in user_ids:
            if uid == str(ADMIN_ID):
                continue
            try:
                bot.copy_message(int(uid), pending.chat.id, pending.message_id)
                success += 1
            except telebot.apihelper.ApiTelegramException as e:
                logging.error(f"Failed to send broadcast to {uid}: {e}")
                fail += 1
            time.sleep(0.05)
        bot.send_message(call.message.chat.id, f"📊 Broadcast complete. ✅ Successful: {success}, ❌ Failed: {fail}")
        bot.answer_callback_query(call.id)
    except Exception:
        logging.exception("Error in admin_confirm_broadcast")
        try:
            bot.answer_callback_query(call.id, "❌ Error sending broadcast", show_alert=True)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data == "admin_cancel_broadcast" and c.from_user.id == ADMIN_ID)
def admin_cancel_broadcast(call):
    log_callback(call)
    try:
        st = admin_state.get(call.from_user.id)
        preview_message_id = None
        if st:
            preview_message_id = st.get("preview_message_id")
        admin_state.pop(call.from_user.id, None)
        if preview_message_id:
            try:
                bot.delete_message(chat_id=call.message.chat.id, message_id=preview_message_id)
            except Exception:
                logging.info("Could not delete preview message during cancel (maybe already deleted)")
        bot.send_message(call.message.chat.id, "❌ Broadcast cancelled.")
        bot.answer_callback_query(call.id)
    except Exception:
        logging.exception("Error in admin_cancel_broadcast")
        try:
            bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        except Exception:
            pass

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and isinstance(admin_state.get(m.from_user.id), dict) and admin_state.get(m.from_user.id, {}).get("state") == 'awaiting_broadcast_message', content_types=['text', 'photo', 'video', 'audio', 'document'])
def prepare_broadcast_message(message):
    try:
        admin_state[message.from_user.id] = {
            "state": "pending_confirmation",
            "pending_broadcast": message
        }
        total_users = users_collection.count_documents({})
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("📢 Confirm Send", callback_data="admin_confirm_broadcast"),
            InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel_broadcast")
        )
        preview_text = f"Preview saved. This will be sent to approximately {total_users - 1} users (excluding admin). Confirm?"
        preview_msg = bot.send_message(message.chat.id, preview_text, reply_markup=kb)
        admin_state[message.from_user.id]["preview_message_id"] = preview_msg.message_id
        logging.info(f"Admin {message.from_user.id} saved pending broadcast; preview_message_id={preview_msg.message_id}")
    except Exception:
        logging.exception("Error in prepare_broadcast_message")

def handle_media_common(message, target_bot: telebot.TeleBot):
    uid = str(message.from_user.id)
    update_user_activity(message.from_user.id)
    if message.chat.type == 'private' and str(message.from_user.id) != str(ADMIN_ID) and not check_subscription(message.from_user.id):
        send_subscription_message(message.chat.id)
        return
    file_id = None
    file_size = None
    if message.voice:
        file_id = message.voice.file_id
        file_size = message.voice.file_size
    elif message.audio:
        file_id = message.audio.file_id
        file_size = message.audio.file_size
    elif message.video:
        file_id = message.video.file_id
        file_size = message.video.file_size
    elif message.document:
        mime = message.document.mime_type
        if mime and ('audio' in mime or 'video' in mime):
            file_id = message.document.file_id
            file_size = message.document.file_size
        else:
            bot.send_message(message.chat.id, "Sorry, I can only transcribe audio or video files.")
            return
    lang = get_stt_user_lang(uid)
    if file_size and file_size > TELEGRAM_MAX_BYTES:
        token = signed_upload_token(message.chat.id, lang)
        upload_link = f"{WEBHOOK_BASE}/upload/{token}"
        pretty_size_mb = round(file_size / (1024*1024), 2)
        text = (
            "📁 <b>File Too Large for Telegram</b>\n"
            f"Your file is {pretty_size_mb}MB, which exceeds Telegram's 20MB limit.\n\n"
            "🌐 <b>Upload via Web Interface:</b>\n"
            "👆 Click the link below to upload your large file:\n\n"
            f"🔗 <a href=\"{upload_link}\">Upload Large File</a>\n\n"
            f"✅ Your language preference ({lang}) is already set!\n"
            "Link expires in 1 hour."
        )
        bot.send_message(message.chat.id, text, disable_web_page_preview=True, reply_to_message_id=message.message_id)
        return
    processing_msg = bot.send_message(message.chat.id, "🔄 Processing", reply_to_message_id=message.message_id)
    processing_msg_id = processing_msg.message_id
    stop_animation = {"stop": False}
    def stop_event():
        return stop_animation["stop"]
    animation_thread = threading.Thread(target=animate_processing_message, args=(message.chat.id, processing_msg_id, stop_event))
    animation_thread.start()
    try:
        tf, file_url = telegram_file_info_and_url(file_id)
        gen = telegram_file_stream(file_url)
        upload_url = assemblyai_upload_from_stream(gen)
        speech_model = select_speech_model_for_lang(lang)
        text = create_transcript_and_wait(upload_url, language_code=lang, speech_model=speech_model)
        # Build markup if needed
        if admin_button_state["translate"] or admin_button_state["summarize"]:
            markup = InlineKeyboardMarkup()
            if admin_button_state["translate"]:
                # store transcription keyed by string message_id
                # but first save transcription in memory
                user_transcriptions.setdefault(uid, {})[str(message.message_id)] = text
                # schedule deletion later
                threading.Thread(target=delete_transcription_later, args=(uid, str(message.message_id)), daemon=True).start()
                markup.add(InlineKeyboardButton("Translate", callback_data=f"btn_translate|{message.message_id}"))
            if admin_button_state["summarize"]:
                markup.add(InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{message.message_id}"))
        else:
            # still store transcription so translate/summarize works even if buttons closed later
            user_transcriptions.setdefault(uid, {})[str(message.message_id)] = text
            threading.Thread(target=delete_transcription_later, args=(uid, str(message.message_id)), daemon=True).start()
            markup = None

        if len(text) > 4000:
            # already stored above
            f = io.BytesIO(text.encode("utf-8"))
            f.name = "transcription.txt"
            bot.send_document(message.chat.id, f, caption="Transcription too long. Here's the complete text in a file.", reply_to_message_id=message.message_id, reply_markup=markup)
        else:
            bot.send_message(message.chat.id, text or "No transcription text was returned.", reply_to_message_id=message.message_id, reply_markup=markup)
        increment_processing_count(uid, "stt")
    except Exception as e:
        error_msg = str(e)
        logging.exception("Error in transcription process")
        if is_transcoding_like_error(error_msg):
            bot.send_message(message.chat.id, "⚠️ Transcription error: file is not audible. Please send a different file.", reply_to_message_id=message.message_id)
        else:
            bot.send_message(message.chat.id, f"Error during transcription: {error_msg}", reply_to_message_id=message.message_id)
    finally:
        stop_animation["stop"] = True
        animation_thread.join()
        try:
            bot.delete_message(message.chat.id, processing_msg_id)
        except Exception:
            pass

@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media_types(message):
    try:
        handle_media_common(message, bot)
    except Exception:
        logging.exception("Error in handle_media_types")

# ---------- HTML TEMPLATE (updated client upload progress) ----------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <title>Media to Text Bot</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"/>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet"/>
    <style>
        :root {
            --primary: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            --success: linear-gradient(135deg, #10b981, #059669);
            --danger: linear-gradient(135deg, #ef4444, #dc2626);
            --card-bg: rgba(255, 255, 255, 0.95);
            --shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: var(--primary);
            min-height: 100vh;
            overflow-x: hidden;
        }
        .app-container {
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .main-card {
            background: var(--card-bg);
            backdrop-filter: blur(20px);
            border-radius: 20px;
            box-shadow: var(--shadow);
            border: 1px solid rgba(255, 255, 255, 0.2);
            max-width: 600px;
            width: 100%;
            overflow: hidden;
            transition: all 0.3s ease;
        }
        .main-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 32px 64px -12px rgba(0, 0, 0, 0.3);
        }
        .header {
            background: var(--primary);
            color: white;
            padding: 2.5rem 2rem;
            text-align: center;
            position: relative;
            overflow: hidden;
        }
        .header h1 {
            font-size: 2rem;
            font-weight: 700;
            margin-bottom: 0.5rem;
            text-shadow: 0 2px 4px rgba(0,0,0,0.3);
        }
        .header p {
            opacity: 0.9;
            font-size: 1.1rem;
        }
        .card-body { padding: 2.5rem; }
        .form-group { margin-bottom: 2rem; }
        .form-label {
            font-weight: 600;
            color: #374151;
            margin-bottom: 0.8rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 1.1rem;
        }
        .form-select, .form-control {
            border: 2px solid #e5e7eb;
            border-radius: 15px;
            padding: 1rem 1.2rem;
            font-size: 1rem;
            transition: all 0.3s ease;
            background: white;
            box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        }
        .form-select:focus, .form-control:focus {
            border-color: #667eea;
            box-shadow: 0 0 0 4px rgba(102, 126, 234, 0.1);
            outline: none;
        }
        .upload-area {
            border: 3px dashed #d1d5db;
            border-radius: 20px;
            padding: 3rem 2rem;
            text-align: center;
            transition: all 0.3s ease;
            cursor: pointer;
            background: #f8fafc;
            position: relative;
        }
        .upload-area:hover {
            border-color: #667eea;
            background: #f0f9ff;
            transform: scale(1.02);
        }
        .upload-area.dragover {
            border-color: #667eea;
            background: #667eea;
            color: white;
        }
        .upload-icon {
            font-size: 4rem;
            color: #667eea;
            margin-bottom: 1.5rem;
            transition: all 0.3s ease;
        }
        .dragover .upload-icon { color: white; transform: scale(1.2); }
        .upload-text {
            font-size: 1.3rem;
            font-weight: 600;
            color: #374151;
            margin-bottom: 0.8rem;
        }
        .dragover .upload-text { color: white; }
        .upload-hint {
            color: #6b7280;
            font-size: 1rem;
        }
        .dragover .upload-hint { color: rgba(255, 255, 255, 0.9); }
        .btn-primary {
            background: var(--primary);
            border: none;
            border-radius: 15px;
            padding: 1rem 2.5rem;
            font-weight: 600;
            font-size: 1.1rem;
            transition: all 0.3s ease;
            position: relative;
            overflow: hidden;
        }
        .btn-primary:hover {
            transform: translateY(-3px);
            box-shadow: 0 15px 35px -5px rgba(102, 126, 234, 0.4);
        }
        .status-message {
            padding: 1.5rem;
            border-radius: 15px;
            margin: 2rem 0;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 1rem;
            font-size: 1.1rem;
        }
        .status-processing {
            background: linear-gradient(135deg, #3b82f6, #1d4ed8);
            color: white;
        }
        .status-success {
            background: var(--success);
            color: white;
        }
        .status-error {
            background: var(--danger);
            color: white;
        }
        .result-container {
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 15px;
            padding: 2rem;
            margin-top: 2rem;
        }
        .result-text {
            font-family: 'Georgia', serif;
            line-height: 1.8;
            color: #1f2937;
            font-size: 1.1rem;
        }
        .close-notice {
            background: linear-gradient(135deg, #10b981, #059669);
            color: white;
            padding: 1.5rem;
            border-radius: 15px;
            margin: 2rem 0;
            text-align: center;
            font-weight: 600;
            font-size: 1.1rem;
        }
        .progress-wrap { margin-top: 1rem; text-align: left; }
        .progress-bar-outer {
            width: 100%;
            background: #e6eefc;
            border-radius: 12px;
            overflow: hidden;
            height: 18px;
        }
        .progress-bar-inner {
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg,#6ee7b7,#3b82f6);
            transition: width 0.2s ease;
        }
        .bytes-info {
            margin-top: 0.5rem;
            font-size: 0.95rem;
            color: #374151;
        }
        @keyframes pulse {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.1); }
        }
        .pulse-icon { animation: pulse 2s infinite; }
        .hidden { display: none !important; }
        @media (max-width: 768px) {
            .app-container { padding: 15px; }
            .main-card { margin: 0; }
            .header h1 { font-size: 1.8rem; }
            .card-body { padding: 2rem; }
            .upload-area { padding: 2.5rem 1.5rem; }
            .upload-icon { font-size: 3rem; }
        }
    </style>
</head>
<body>
    <div class="app-container">
        <div class="main-card">
            <div class="header">
                <h1><i class="fas fa-microphone-alt"></i> Media to Text Bot</h1>
                <p>Transform your media files into accurate text</p>
            </div>
            <div class="card-body">
                <form id="transcriptionForm" enctype="multipart/form-data" method="post">
                    <div class="form-group">
                        <label class="form-label" for="language">
                            <i class="fas fa-globe-americas"></i> Language
                        </label>
                        <select class="form-select" id="language" name="language" required>
                            {% for label, code in lang_options %}
                            <option value="{{ code }}" {% if code == selected_lang %}selected{% endif %}>{{ label }}</option>
                            {% endfor %}
                        </select>
                    </div>
                    <div class="form-group">
                        <label class="form-label">
                            <i class="fas fa-file-audio"></i> Media File
                        </label>
                        <div class="upload-area" id="uploadArea">
                            <div class="upload-icon">
                                <i class="fas fa-cloud-upload-alt"></i>
                            </div>
                            <div class="upload-text">Drop your media here</div>
                            <div class="upload-hint">MP3, WAV, M4A, OGG, WEBM, FLAC, MP4 • Max {{ max_mb }}MB</div>
                            <input type="file" id="audioFile" name="file" accept=".mp3,.wav,.m4a,.ogg,.webm,.flac,.mp4" class="d-none" required>
                        </div>
                    </div>
                    <button type="button" id="uploadButton" class="btn btn-primary w-100">
                        <i class="fas fa-magic"></i> Upload & Start
                    </button>
                </form>
                <div id="statusContainer"></div>
                <div id="resultContainer"></div>
            </div>
        </div>
    </div>
    <script>
        class TranscriptionApp {
            constructor() {
                this.initializeEventListeners();
            }
            initializeEventListeners() {
                this.uploadArea = document.getElementById('uploadArea');
                this.fileInput = document.getElementById('audioFile');
                this.uploadButton = document.getElementById('uploadButton');
                this.statusContainer = document.getElementById('statusContainer');
                this.resultContainer = document.getElementById('resultContainer');

                this.uploadArea.addEventListener('click', () => this.fileInput.click());
                this.fileInput.addEventListener('change', (e) => this.handleFileSelect(e));
                this.uploadArea.addEventListener('dragover', (e) => {
                    e.preventDefault();
                    this.uploadArea.classList.add('dragover');
                });
                this.uploadArea.addEventListener('dragleave', () => {
                    this.uploadArea.classList.remove('dragover');
                });
                this.uploadArea.addEventListener('drop', (e) => {
                    e.preventDefault();
                    this.uploadArea.classList.remove('dragover');
                    const files = e.dataTransfer.files;
                    if (files.length > 0) {
                        this.fileInput.files = files;
                        this.handleFileSelect({ target: this.fileInput });
                    }
                });
                this.uploadButton.addEventListener('click', (e) => this.handleSubmit(e));
            }
            humanFileSize(bytes) {
                if (bytes === 0) return '0 B';
                const k = 1024;
                const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
                const i = Math.floor(Math.log(bytes) / Math.log(k));
                return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
            }
            handleFileSelect(e) {
                const file = e.target.files[0];
                if (file) {
                    const uploadText = document.querySelector('.upload-text');
                    uploadText.textContent = `Selected: ${file.name} (${this.humanFileSize(file.size)})`;
                }
            }
            showUploadingUI() {
                this.statusContainer.innerHTML = `
                    <div class="status-message status-processing">
                        <i class="fas fa-spinner fa-spin pulse-icon"></i>
                        <div>
                            <div id="uploadStatusText">Upload Processing..</div>
                            <div class="progress-wrap">
                                <div class="progress-bar-outer"><div id="progressInner" class="progress-bar-inner"></div></div>
                                <div id="bytesInfo" class="bytes-info"></div>
                            </div>
                        </div>
                    </div>
                `;
            }
            async handleSubmit(e) {
                e.preventDefault();
                const file = this.fileInput.files[0];
                if (!file) {
                    alert("Please choose a file to upload.");
                    return;
                }
                if (file.size > {{ max_mb }} * 1024 * 1024) {
                    alert("File is too large. Max allowed is {{ max_mb }}MB.");
                    return;
                }
                const formData = new FormData();
                formData.append('file', file);
                formData.append('language', document.getElementById('language').value);

                this.showUploadingUI();
                const progressInner = document.getElementById('progressInner');
                const bytesInfo = document.getElementById('bytesInfo');
                const uploadStatusText = document.getElementById('uploadStatusText');

                const xhr = new XMLHttpRequest();
                xhr.open('POST', window.location.pathname, true);

                xhr.upload.onprogress = (event) => {
                    if (event.lengthComputable) {
                        const percent = Math.round((event.loaded / event.total) * 100);
                        progressInner.style.width = percent + '%';
                        bytesInfo.textContent = `${(event.loaded/1024/1024).toFixed(2)} MB / ${(event.total/1024/1024).toFixed(2)} MB (${percent}%)`;
                        uploadStatusText.textContent = `Uploading... ${percent}%`;
                    } else {
                        // Fallback if total size unknown
                        progressInner.style.width = '50%';
                        bytesInfo.textContent = `${(event.loaded/1024/1024).toFixed(2)} MB uploaded`;
                        uploadStatusText.textContent = `Uploading...`;
                    }
                };

                xhr.onload = () => {
                    if (xhr.status >= 200 && xhr.status < 300) {
                        let respText = "Upload accepted. Processing started. You may close this tab.";
                        try {
                            const j = JSON.parse(xhr.responseText);
                            if (j && j.message) respText = j.message;
                        } catch (err) {
                            // plain text response
                            respText = xhr.responseText || respText;
                        }
                        this.statusContainer.innerHTML = `
                            <div class="close-notice">
                                <i class="fas fa-check-circle"></i>
                                ${respText}
                            </div>
                        `;
                    } else {
                        let text = xhr.responseText || 'Upload failed';
                        this.statusContainer.innerHTML = `
                            <div class="status-message status-error">
                                <i class="fas fa-exclamation-triangle"></i>
                                <span>Upload failed. ${text}</span>
                            </div>
                        `;
                    }
                };

                xhr.onerror = () => {
                    this.statusContainer.innerHTML = `
                        <div class="status-message status-error">
                            <i class="fas fa-exclamation-triangle"></i>
                            <span>Upload failed. Please try again.</span>
                        </div>
                    `;
                };

                xhr.send(formData);
            }
        }
        document.addEventListener('DOMContentLoaded', () => {
            new TranscriptionApp();
        });
    </script>
</body>
</html>
"""

@app.route("/upload/<token>", methods=['GET', 'POST'])
def upload_large_file(token):
    try:
        data = unsign_upload_token(token, max_age_seconds=3600)
    except SignatureExpired:
        return "<h3>Link expired</h3>", 400
    except BadSignature:
        return "<h3>Invalid link</h3>", 400
    chat_id = data.get("chat_id")
    lang = data.get("lang", "en")
    if request.method == 'GET':
        return render_template_string(HTML_TEMPLATE, lang_options=LANG_OPTIONS, selected_lang=lang, max_mb=MAX_WEB_UPLOAD_MB)
    file = request.files.get('file')
    if not file:
        return "No file uploaded", 400

    # Save uploaded file to a temporary file on disk immediately (no full read into memory)
    try:
        # Determine a safe suffix if filename present
        suffix = ''
        try:
            if file.filename and '.' in file.filename:
                suffix = os.path.splitext(file.filename)[1]
        except Exception:
            suffix = ''
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp_name = tmp.name
        tmp.close()
        # FileStorage.save will stream to disk (fast disk copy) without loading whole file into memory
        file.save(tmp_name)
        file_size = os.path.getsize(tmp_name)
        if file_size > MAX_WEB_UPLOAD_MB * 1024 * 1024:
            try:
                os.remove(tmp_name)
            except Exception:
                pass
            return f"File too large. Max allowed is {MAX_WEB_UPLOAD_MB}MB.", 400
    except Exception as e:
        logging.exception("Failed to save uploaded file to temp")
        try:
            if tmp and os.path.exists(tmp.name):
                os.remove(tmp.name)
        except Exception:
            pass
        return f"Failed to save uploaded file: {e}", 500

    # We will process the saved file in a background thread; return immediately so upload is fast.
    def file_chunk_generator_from_path(path, chunk_size=256*1024):
        try:
            with open(path, 'rb') as fh:
                while True:
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        except Exception:
            logging.exception("Error yielding chunks from temp file")
            raise

    def process_uploaded_file(chat_id_inner, lang_inner, file_path):
        try:
            # Stream file to AssemblyAI from disk
            upload_url = assemblyai_upload_from_stream(file_chunk_generator_from_path(file_path))
            speech_model = select_speech_model_for_lang(lang_inner)
            text = create_transcript_and_wait(upload_url, language_code=lang_inner, speech_model=speech_model)

            # For web-uploaded files we create a token so translate/summarize buttons refer to this token
            token = uuid.uuid4().hex
            chat_id_str = str(chat_id_inner)
            user_transcriptions.setdefault(chat_id_str, {})[token] = text
            # schedule deletion later
            threading.Thread(target=delete_transcription_later, args=(chat_id_str, token), daemon=True).start()

            # Build markup referencing the token
            if admin_button_state["translate"] or admin_button_state["summarize"]:
                markup = InlineKeyboardMarkup()
                if admin_button_state["translate"]:
                    markup.add(InlineKeyboardButton("Translate", callback_data=f"btn_translate|{token}"))
                if admin_button_state["summarize"]:
                    markup.add(InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{token}"))
            else:
                markup = None

            # Send result to user (if long -> as document with the exact caption you requested)
            if len(text) > 4000:
                fobj = io.BytesIO(text.encode("utf-8"))
                fobj.name = "transcription.txt"
                bot.send_document(chat_id_inner, fobj, caption="Transcription too long. Here's the complete text in a file.", reply_markup=markup)
            else:
                bot.send_message(chat_id_inner, text or "No transcription text was returned.", reply_markup=markup)
            increment_processing_count(str(chat_id_inner), "stt")
        except Exception:
            logging.exception("Error transcribing uploaded file")
            try:
                bot.send_message(chat_id_inner, "Error occurred while transcribing the uploaded file.")
            except Exception:
                pass
        finally:
            # Clean up temp file
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                logging.exception("Failed to delete temp file after processing")

    # Launch background thread to handle transcription
    threading.Thread(target=process_uploaded_file, args=(chat_id, lang, tmp_name), daemon=True).start()

    # Return JSON so the web client can show a nicer message.
    return jsonify({"status": "accepted", "message": "Upload accepted. Processing started. Your transcription will be sent to your Telegram chat when ready."})

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("btn_translate|"))
def button_translate_handler(call):
    log_callback(call)
    try:
        uid = str(call.from_user.id)
        _, message_id_str = call.data.split("|", 1)
        # message_id_str is now the original message id (as string) or a token (string)
        if uid not in user_transcriptions or message_id_str not in user_transcriptions[uid]:
            bot.answer_callback_query(call.id, "❌ Transcription not available or expired")
            return
        # Important: pass the SAME message_id_str (token or numeric string) into language selection callbacks
        markup = build_lang_keyboard("translate_to", message_id=message_id_str)
        try:
            bot.send_message(call.message.chat.id, "Select target language for translation:", reply_markup=markup, reply_to_message_id=None)
        except Exception:
            bot.send_message(call.message.chat.id, "Select target language for translation:", reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception:
        logging.exception("Error in button_translate_handler")
        try:
            bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("btn_summarize|"))
def button_summarize_handler(call):
    log_callback(call)
    try:
        uid = str(call.from_user.id)
        _, message_id_str = call.data.split("|", 1)
        if uid not in user_transcriptions or message_id_str not in user_transcriptions[uid]:
            bot.answer_callback_query(call.id, "❌ Transcription expired")
            return
        markup = build_lang_keyboard("summarize_in", message_id=message_id_str)
        try:
            bot.send_message(call.message.chat.id, "Select language for summary:", reply_markup=markup, reply_to_message_id=None)
        except Exception:
            bot.send_message(call.message.chat.id, "Select language for summary:", reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception:
        logging.exception("Error in button_summarize_handler")
        try:
            bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("translate_to|"))
def callback_translate_to(call):
    log_callback(call)
    try:
        uid = str(call.from_user.id)
        parts = call.data.split("|")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ Missing message reference. Please try again.", show_alert=True)
            return
        lang_code = parts[1]
        message_id = parts[2]  # keep as string (could be numeric string or token)
        lang_label = CODE_TO_LABEL.get(lang_code, lang_code)
        if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
            bot.answer_callback_query(call.id, "❌ Transcription expired")
            return
        try:
            bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception as e:
            logging.info(f"Could not delete language selection message: {e}")
        progress_msg = None
        try:
            progress_msg = bot.send_message(call.message.chat.id, f"🔄 Translating")
        except Exception:
            progress_msg = None
        bot.answer_callback_query(call.id)
        def do_translate(chat_id, orig_message_id, progress_message_id):
            stop_flag = {"stop": False}
            def stop_event():
                return stop_flag["stop"]
            anim_thread = None
            if progress_message_id:
                anim_thread = threading.Thread(target=animate_processing_message, args=(chat_id, progress_message_id, stop_event))
                anim_thread.start()
            try:
                transcription = user_transcriptions[uid][orig_message_id]
                target_lang_name = CODE_TO_LABEL.get(lang_code, lang_code)
                translated = translate_large_text_with_gemini(transcription, target_lang_name)
                if not translated:
                    raise RuntimeError("Empty translation returned by Gemini.")
                # reply_to_message_id: use original numeric message id if orig_message_id is digits
                reply_to = int(orig_message_id) if orig_message_id.isdigit() else None
                if len(translated) > 4000:
                    f = io.BytesIO(translated.encode("utf-8"))
                    f.name = f"translation_{orig_message_id}.txt"
                    if reply_to:
                        bot.send_document(chat_id, f, reply_to_message_id=reply_to)
                    else:
                        bot.send_document(chat_id, f)
                else:
                    if reply_to:
                        bot.send_message(chat_id, translated, reply_to_message_id=reply_to)
                    else:
                        bot.send_message(chat_id, translated)
            except Exception as e:
                logging.exception("Error during do_translate")
                try:
                    bot.send_message(chat_id, f"❌ An error occurred during translation: {e}")
                except Exception:
                    pass
            finally:
                stop_flag["stop"] = True
                if anim_thread:
                    anim_thread.join()
                if progress_message_id:
                    try:
                        bot.delete_message(chat_id, progress_message_id)
                    except Exception:
                        pass
        threading.Thread(target=lambda: do_translate(call.message.chat.id, message_id, progress_msg.message_id if progress_msg else None), daemon=True).start()
    except Exception:
        logging.exception("Error in callback_translate_to")
        try:
            bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("summarize_in|"))
def callback_summarize_in(call):
    log_callback(call)
    try:
        uid = str(call.from_user.id)
        parts = call.data.split("|")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ Missing message reference. Please try again.", show_alert=True)
            return
        lang_code = parts[1]
        message_id = parts[2]
        lang_label = CODE_TO_LABEL.get(lang_code, lang_code)
        if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
            bot.answer_callback_query(call.id, "❌ Transcription expired")
            return
        try:
            bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        except Exception as e:
            logging.info(f"Could not delete language selection message: {e}")
        progress_msg = None
        try:
            progress_msg = bot.send_message(call.message.chat.id, f"🔄 Summarizing")
        except Exception:
            progress_msg = None
        bot.answer_callback_query(call.id)
        def do_summarize(chat_id, orig_message_id, progress_message_id):
            stop_flag = {"stop": False}
            def stop_event():
                return stop_flag["stop"]
            anim_thread = None
            if progress_message_id:
                anim_thread = threading.Thread(target=animate_processing_message, args=(chat_id, progress_message_id, stop_event))
                anim_thread.start()
            try:
                transcription = user_transcriptions[uid][orig_message_id]
                target_lang_name = CODE_TO_LABEL.get(lang_code, lang_code)
                summary = summarize_large_text_with_gemini(transcription, target_lang_name)
                if not summary:
                    raise RuntimeError("Empty summary returned by Gemini.")
                reply_to = int(orig_message_id) if orig_message_id.isdigit() else None
                if len(summary) > 4000:
                    f = io.BytesIO(summary.encode("utf-8"))
                    f.name = f"summary_{orig_message_id}.txt"
                    if reply_to:
                        bot.send_document(chat_id, f, reply_to_message_id=reply_to)
                    else:
                        bot.send_document(chat_id, f)
                else:
                    if reply_to:
                        bot.send_message(chat_id, summary, reply_to_message_id=reply_to)
                    else:
                        bot.send_message(chat_id, summary)
            except Exception as e:
                logging.exception("Error during do_summarize")
                try:
                    bot.send_message(chat_id, f"❌ An error occurred during summarization: {e}")
                except Exception:
                    pass
            finally:
                stop_flag["stop"] = True
                if anim_thread:
                    anim_thread.join()
                if progress_message_id:
                    try:
                        bot.delete_message(chat_id, progress_message_id)
                    except Exception:
                        pass
        threading.Thread(target=lambda: do_summarize(call.message.chat.id, message_id, progress_msg.message_id if progress_msg else None), daemon=True).start()
    except Exception:
        logging.exception("Error in callback_summarize_in")
        try:
            bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        except Exception:
            pass

@app.route("/telegram_webhook", methods=['POST'])
def telegram_webhook():
    update_json = request.get_json(force=True)
    try:
        update = telebot.types.Update.de_json(update_json)
        bot.process_new_updates([update])
    except Exception:
        logging.exception("Error processing incoming webhook update")
    return "OK"

@app.route("/healthz")
def healthz():
    return "OK"

if __name__ == "__main__":
    webhook_url = WEBHOOK_BASE.rstrip("/") + "/telegram_webhook"
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        bot.set_webhook(url=webhook_url)
        print("Webhook set to:", webhook_url)
        try:
            client.admin.command('ping')
            logging.info("Successfully connected to MongoDB!")
        except Exception as e:
            logging.error(f"Could not connect to MongoDB: {e}")
    except Exception:
        logging.exception("Failed to set webhook")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
