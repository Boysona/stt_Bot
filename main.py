# bot.py
import os
import uuid
import logging
import requests
import telebot
import json
from flask import Flask, request, abort, render_template_string
from datetime import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
import threading
import time
import io
import re
from pymongo import MongoClient
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
import traceback

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# === CONFIG ===
# Replace these with your actual keys and URLs (you provided these in the prompt)
GEMINI_API_KEY = "AIzaSyDpb3UvnrRgk6Fu61za_VrRN8byZRSyq_I"
ASSEMBLYAI_API_KEY = "b07239215b60433b8e225e7fd8ef6576"
BOT_TOKEN = "7790991731:AAF4NHGm0BJCf08JTdBaUWKzwfs82_Y9Ecw"
WEBHOOK_BASE = "https://stt-bot-ckt1.onrender.com"   # your webhook base (render URL)
ADMIN_ID = 6964068910 # Replace with your Telegram User ID
REQUIRED_CHANNEL = "" # Optional: Add channel username (e.g., @mychannel) if you require subscription

# secret for signing upload links (change to a strong random string in production)
SECRET_KEY = "super-secret-please-change"

# Max telegram direct download size
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024  # 20MB

# MongoDB Configuration
MONGO_URI = "mongodb+srv://hoskasii:GHyCdwpI0PvNuLTg@cluster0.dy7oe7t.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "telegram_bot_db"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
users_collection = db["users"]
tokens_collection = db["tokens"]
# ============

# Flask & TeleBot init
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, parse_mode='HTML')

# serializer for generating signed upload links that expire
serializer = URLSafeTimedSerializer(SECRET_KEY)

# A simple in-memory store for tracking admin state and user transcriptions.
admin_state = {}
user_transcriptions = {} # key = user_id_str -> {message_id: "transcription text"}

# --------------------
# Language options (as you provided)
# --------------------
LANG_OPTIONS = [
    ("🇺🇸 English", "en"),
    ("🇩🇪 Deutsch", "de"),
    ("🇮🇳 हिन्दी", "hi"),
    ("🇷🇺 Русский", "ru"),
    ("🇮🇩 Indonesia", "id"),
    ("🇰🇿 Қазақша", "kk"),
    ("🇦🇿 Azərbaycan", "az"),
    ("🇮🇹 Italiano", "it"),
    ("🇹🇷 Türkçe", "tr"),
    ("🇧🇬 Български", "bg"),
    ("🇷🇸 Srpski", "sr"),
    ("🇫🇷 Français", "fr"),
    ("🇸🇦 العربية", "ar"),
    ("🇪🇸 Español", "es"),
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
    ("🇹🇭 ไทย", "th"),
    ("🇻🇳 Tiếng Việt", "vi"),
    ("🇵🇹 Português", "pt"),
    ("🇲🇾 Melayu", "ms"),
    ("🇷🇴 Română", "ro"),
    ("🇺🇿 O'zbekcha", "uz"),
    ("🇵🇭 Tagalog", "tl")
]

# Build helper maps from LANG_OPTIONS
CODE_TO_LABEL = {code: label for (label, code) in LANG_OPTIONS}
LABEL_TO_CODE = {label: code for (label, code) in LANG_OPTIONS}

# Build STT_LANGUAGES dict used elsewhere (gives a unified place)
STT_LANGUAGES = {}
for label, code in LANG_OPTIONS:
    # try to split emoji and name (emoji is first char(s))
    STT_LANGUAGES[label.split(" ", 1)[-1]] = {
        "code": code,
        "emoji": label.split(" ", 1)[0],
        "native": label.split(" ", 1)[-1]
    }

# A simple in-memory store for pending media.
memory_lock = threading.Lock()
in_memory_data = {
    "pending_media": {},  # key = user_id_str -> pending dict
}

# --------------------
# Gemini helpers (translate & summarize)
# --------------------
def ask_gemini(text: str, instruction: str, timeout=60) -> str:
    """
    Calls the Gemini generateContent endpoint with a simple payload.
    Returns string result or raises Exception.
    """
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
    # Try to extract text in the structure you provided earlier
    if "candidates" in result and isinstance(result["candidates"], list) and len(result["candidates"]) > 0:
        # attempt to follow your expected structure
        cand = result['candidates'][0]
        # safe navigation
        try:
            return cand['content']['parts'][0]['text']
        except Exception:
            # fallback: return stringified candidate
            return json.dumps(cand)
    # fallback: try to return any 'output' fields or the raw json
    return json.dumps(result)

def chunk_text(text: str, max_chars: int = 24000):
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start+max_chars])
        start += max_chars
    return chunks

def translate_large_text_with_gemini(text: str, target_lang_name: str):
    """
    Splits the text into chunks, translates each chunk with Gemini, then requests a final polishing pass.
    Returns translation string or raises Exception.
    """
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
    """
    Splits text into chunks, summarizes each chunk, then asks Gemini to combine & polish the partial summaries.
    Returns summary string or raises Exception.
    """
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

# --------------------
# Database helpers
# --------------------
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

# --------------------
# Pending media helpers
# --------------------
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

def delete_transcription_later(user_id: str, message_id: int):
    time.sleep(600)  # Transcription valid for 10 minutes
    with memory_lock:
        if user_id in user_transcriptions and message_id in user_transcriptions[user_id]:
            del user_transcriptions[user_id][message_id]

# --------------------
# AssemblyAI helpers
# --------------------
def assemblyai_upload_from_stream(stream_iterable):
    """
    Streams data to AssemblyAI upload endpoint and returns the upload_url.
    stream_iterable: an iterator/generator that yields bytes
    """
    upload_url = "https://api.assemblyai.com/v2/upload"
    headers = {"authorization": ASSEMBLYAI_API_KEY}
    resp = requests.post(upload_url, headers=headers, data=stream_iterable, timeout=3600)
    resp.raise_for_status()
    return resp.json().get("upload_url")

def create_transcript_and_wait(audio_url: str, language_code: str = None, poll_interval=2):
    """
    Create AssemblyAI transcript job and poll until completion.
    Returns transcript text on success, raises on failure.
    """
    create_url = "https://api.assemblyai.com/v2/transcript"
    headers = {"authorization": ASSEMBLYAI_API_KEY, "content-type": "application/json"}
    data = {"audio_url": audio_url}
    if language_code:
        data["language_code"] = language_code

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
    """
    Generator that yields chunks from a remote file (telegram file URL).
    """
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

# --------------------
# Keyboards & Helpers (use LANG_OPTIONS)
# --------------------
def build_lang_keyboard(callback_prefix: str, row_width: int = 3, message_id: int = None):
    """
    Builds an InlineKeyboardMarkup for available languages.
    If message_id is provided, callback_data will be: "<prefix>|<code>|<message_id>"
    Otherwise: "<prefix>|<code>"
    """
    markup = InlineKeyboardMarkup(row_width=row_width)
    buttons = []
    for label, code in LANG_OPTIONS:
        # label already contains emoji and native name; show as-is
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
    markup.add(
        InlineKeyboardButton("📊 Total Users", callback_data="admin_total_users"),
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")
    )
    return markup

def signed_upload_token(chat_id: int, lang_code: str):
    payload = {"chat_id": chat_id, "lang": lang_code}
    return serializer.dumps(payload)

def unsign_upload_token(token: str, max_age_seconds: int = 3600):
    # raises SignatureExpired or BadSignature
    data = serializer.loads(token, max_age=max_age_seconds)
    return data

def animate_processing_message(chat_id, message_id, stop_event):
    """
    Edits a message to animate dots until stop_event() is true.
    """
    dots = [".", "..", "..."]
    idx = 0
    while not stop_event():
        try:
            bot.edit_message_text(f"🔄 Processing{dots[idx % len(dots)]}", chat_id=chat_id, message_id=message_id)
        except Exception:
            # Silently fail if message is deleted or inaccessible
            pass
        idx = (idx + 1) % len(dots)
        time.sleep(0.6)

# --------------------
# Bot handlers
# --------------------
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
                "🔓 Join the group to unlock",
                url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"
            )
        )
        bot.send_message(
            chat_id,
            "🔒 Access Locked. To use this Bot, please join our group first. Tap the button below to join and then send /start.",
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
            "Choose your Media (Voice, Audio, Video) file language for transcription using the below buttons:",
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
    try:
        uid = str(call.from_user.id)
        _, lang_code = call.data.split("|", 1)
        # Find label for language
        lang_label = CODE_TO_LABEL.get(lang_code, lang_code)
        set_stt_user_lang(uid, lang_code)

        # Delete the language selection message
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception as e:
            logging.info(f"Could not delete language selection message: {e}")

        # Send welcome message
        welcome_text = (
            f"👋 Salaam! dear\n"
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
            "Send a voice/audio/video (≤ 20MB) and I will transcribe it.\n"
            "If it's larger than 20MB, I'll give you a secure upload link."
        )
        bot.send_message(message.chat.id, text)
    except Exception:
        logging.exception("Error in handle_help")

@bot.message_handler(commands=['lang'])
def handle_lang(message):
    try:
        kb = build_stt_language_keyboard()
        bot.send_message(message.chat.id, "Choose a language:", reply_markup=kb)
    except Exception:
        logging.exception("Error in handle_lang")

@bot.callback_query_handler(lambda c: c.data and c.data.startswith("stt_lang|"))
def on_stt_language_select(call):
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

@bot.callback_query_handler(func=lambda c: c.data in ["admin_total_users", "admin_broadcast"] and c.from_user.id == ADMIN_ID)
def admin_menu_callback(call):
    try:
        chat_id = call.message.chat.id
        if call.data == "admin_total_users":
            total_registered = users_collection.count_documents({})
            bot.send_message(chat_id, f"👥 Total registered users: {total_registered}")
        elif call.data == "admin_broadcast":
            admin_state[call.from_user.id] = 'awaiting_broadcast_message'
            bot.send_message(chat_id, "📢 Send the broadcast message now:")
        bot.answer_callback_query(call.id)
    except Exception:
        logging.exception("Error in admin_menu_callback")
        try:
            bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        except Exception:
            pass

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast_message', content_types=['text', 'photo', 'video', 'audio', 'document'])
def broadcast_message(message):
    try:
        admin_state[message.from_user.id] = None
        success = fail = 0
        user_ids = [doc["_id"] for doc in users_collection.find({}, {"_id": 1})]
        for uid in user_ids:
            if uid == str(ADMIN_ID):
                continue
            try:
                bot.copy_message(int(uid), message.chat.id, message.message_id)
                success += 1
            except telebot.apihelper.ApiTelegramException as e:
                logging.error(f"Failed to send broadcast to {uid}: {e}")
                fail += 1
            time.sleep(0.05)
        bot.send_message(message.chat.id, f"📊 Broadcast complete. ✅ Successful: {success}, ❌ Failed: {fail}")
    except Exception:
        logging.exception("Error in broadcast_message")

# ----------------------------
# --- Main handler for media messages -----
# ----------------------------
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

    processing_msg = bot.send_message(message.chat.id, "🔄 Processing...", reply_to_message_id=message.message_id)
    processing_msg_id = processing_msg.message_id

    stop_animation = False
    def stop_event():
        return stop_animation
    animation_thread = threading.Thread(target=animate_processing_message, args=(message.chat.id, processing_msg_id, stop_event))
    animation_thread.start()

    try:
        tf, file_url = telegram_file_info_and_url(file_id)
        gen = telegram_file_stream(file_url)
        upload_url = assemblyai_upload_from_stream(gen)

        text = create_transcript_and_wait(upload_url, language_code=lang)

        if len(text) > 4000:
            user_transcriptions.setdefault(uid, {})[message.message_id] = text
            threading.Thread(target=delete_transcription_later, args=(uid, message.message_id), daemon=True).start()

            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("Translate", callback_data=f"btn_translate|{message.message_id}"),
                InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{message.message_id}")
            )

            f = io.BytesIO(text.encode("utf-8"))
            f.name = "transcription.txt"
            bot.send_document(message.chat.id, f, caption="Your transcription is ready.", reply_to_message_id=message.message_id, reply_markup=markup)
        else:
            user_transcriptions.setdefault(uid, {})[message.message_id] = text
            threading.Thread(target=delete_transcription_later, args=(uid, message.message_id), daemon=True).start()

            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("Translate", callback_data=f"btn_translate|{message.message_id}"),
                InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{message.message_id}")
            )
            bot.send_message(message.chat.id, text or "No transcription text was returned.", reply_to_message_id=message.message_id, reply_markup=markup)

        increment_processing_count(uid, "stt")

    except Exception as e:
        error_msg = str(e)
        logging.exception("Error in transcription process")
        if is_transcoding_like_error(error_msg):
            bot.send_message(message.chat.id, "❌ Transcription error: The file format is not supported or the file is not audible. Please send a different file.", reply_to_message_id=message.message_id)
        else:
            bot.send_message(message.chat.id, f"Error during transcription: {error_msg}", reply_to_message_id=message.message_id)
    finally:
        stop_animation = True
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

# ----------------------------
# --- FLASK ROUTES ----------
# ----------------------------

# route for Telegram webhook
@app.route("/telegram_webhook", methods=['POST'])
def telegram_webhook():
    update_json = request.get_json(force=True)
    try:
        update = telebot.types.Update.de_json(update_json)
        bot.process_new_updates([update])
    except Exception:
        logging.exception("Error processing incoming webhook update")
    return "OK"

# upload form for large files
UPLOAD_PAGE = """
<!doctype html>
<title>Upload large file</title>
<style>
  body { font-family: sans-serif; text-align: center; margin-top: 50px; }
  h3 { color: #333; }
  form { margin-top: 20px; padding: 20px; border: 1px solid #ddd; border-radius: 8px; display: inline-block; }
  input[type="file"] { display: block; margin-bottom: 10px; }
  input[type="submit"] { background-color: #0088cc; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; }
  p { color: #666; }
</style>
<h3>Upload file for transcription</h3>
<p>Chat ID: <b>{{ chat_id }}</b> • Language: <b>{{ lang }}</b></p>
<form method=post enctype=multipart/form-data>
  <input type=file name=file required>
  <input type=submit value="Upload & Transcribe">
</form>
<p>Link expires in 1 hour.</p>
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
        return render_template_string(UPLOAD_PAGE, chat_id=chat_id, lang=lang)

    file = request.files.get('file')
    if not file:
        return "No file uploaded", 400

    upload_msg = bot.send_message(chat_id, f"📥 Received large upload from web interface. Starting transcription (language: {lang}).")
    upload_msg_id = upload_msg.message_id

    try:
        def file_gen():
            chunk_size = 256*1024
            while True:
                chunk = file.stream.read(chunk_size)
                if not chunk:
                    break
                yield chunk

        upload_url = assemblyai_upload_from_stream(file_gen())
        text = create_transcript_and_wait(upload_url, language_code=lang)

        # We need a message ID to attach the buttons to
        final_msg = bot.send_message(chat_id, "Your transcription is ready.", reply_to_message_id=upload_msg_id)
        final_msg_id = final_msg.message_id

        if len(text) > 4000:
            user_transcriptions.setdefault(str(chat_id), {})[final_msg_id] = text
            threading.Thread(target=delete_transcription_later, args=(str(chat_id), final_msg_id), daemon=True).start()

            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("Translate", callback_data=f"btn_translate|{final_msg_id}"),
                InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{final_msg_id}")
            )

            f = io.BytesIO(text.encode("utf-8"))
            f.name = "transcription.txt"
            bot.send_document(chat_id, f, caption="Your transcription is ready.", reply_markup=markup, reply_to_message_id=upload_msg_id)
        else:
            user_transcriptions.setdefault(str(chat_id), {})[final_msg_id] = text
            threading.Thread(target=delete_transcription_later, args=(str(chat_id), final_msg_id), daemon=True).start()

            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("Translate", callback_data=f"btn_translate|{final_msg_id}"),
                InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{final_msg_id}")
            )
            bot.send_message(chat_id, text or "No transcription text was returned.", reply_markup=markup, reply_to_message_id=upload_msg_id)

    except Exception:
        logging.exception("Error transcribing uploaded file")
        bot.send_message(chat_id, "Error occurred while transcribing the uploaded file.", reply_to_message_id=upload_msg_id)
        return "Error during transcription", 500
    finally:
        try:
            bot.delete_message(chat_id, upload_msg_id)
        except Exception:
            pass
    return "<h3>Upload complete. Transcription will be sent to your Telegram chat.</h3>"

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("btn_translate|"))
def button_translate_handler(call):
    try:
        uid = str(call.from_user.id)
        _, message_id_str = call.data.split("|", 1)
        message_id = int(message_id_str)
        if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
            bot.answer_callback_query(call.id, "❌ Transcription not available or expired")
            return
        # Build keyboard that includes message_id in callback_data
        markup = build_lang_keyboard("translate_to", message_id=message_id)
        try:
            bot.send_message(call.message.chat.id, "🌍 Select target language for translation:", reply_markup=markup, reply_to_message_id=message_id)
        except Exception:
            bot.send_message(call.message.chat.id, "🌍 Select target language for translation:", reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception:
        logging.exception("Error in button_translate_handler")
        try:
            bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("btn_summarize|"))
def button_summarize_handler(call):
    try:
        uid = str(call.from_user.id)
        _, message_id_str = call.data.split("|", 1)
        message_id = int(message_id_str)
        if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
            bot.answer_callback_query(call.id, "❌ Transcription expired")
            return
        # Build keyboard that includes message_id in callback_data
        markup = build_lang_keyboard("summarize_in", message_id=message_id)
        try:
            bot.send_message(call.message.chat.id, "🌍 Select language for summary:", reply_markup=markup, reply_to_message_id=message_id)
        except Exception:
            bot.send_message(call.message.chat.id, "🌍 Select language for summary:", reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception:
        logging.exception("Error in button_summarize_handler")
        try:
            bot.answer_callback_query(call.id, "❌ Error", show_alert=True)
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("translate_to|"))
def callback_translate_to(call):
    try:
        uid = str(call.from_user.id)
        parts = call.data.split("|")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ Missing message reference. Please try again.", show_alert=True)
            return
        lang_code = parts[1]
        message_id = int(parts[2])
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
            progress_msg = bot.send_message(call.message.chat.id, f"🔄 Translating...", reply_to_message_id=message_id)
        except Exception:
            progress_msg = None
        bot.answer_callback_query(call.id)

        # Real translation using Gemini
        def do_translate(chat_id, orig_message_id, progress_message_id):
            try:
                transcription = user_transcriptions[uid][orig_message_id]
                target_lang_name = CODE_TO_LABEL.get(lang_code, lang_code)
                translated = translate_large_text_with_gemini(transcription, target_lang_name)
                if not translated:
                    raise RuntimeError("Empty translation returned by Gemini.")
                if len(translated) > 4000:
                    f = io.BytesIO(translated.encode("utf-8"))
                    f.name = f"translation_{orig_message_id}.txt"
                    bot.send_document(chat_id, f, reply_to_message_id=orig_message_id)
                else:
                    bot.send_message(chat_id, translated, reply_to_message_id=orig_message_id)
            except Exception as e:
                logging.exception("Error during do_translate")
                bot.send_message(chat_id, f"❌ An error occurred during translation: {e}", reply_to_message_id=orig_message_id)
            finally:
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
    try:
        uid = str(call.from_user.id)
        parts = call.data.split("|")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "❌ Missing message reference. Please try again.", show_alert=True)
            return
        lang_code = parts[1]
        message_id = int(parts[2])
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
            progress_msg = bot.send_message(call.message.chat.id, f"🔄 Summarizing...", reply_to_message_id=message_id)
        except Exception:
            progress_msg = None
        bot.answer_callback_query(call.id)

        # Real summarization using Gemini
        def do_summarize(chat_id, orig_message_id, progress_message_id):
            try:
                transcription = user_transcriptions[uid][orig_message_id]
                target_lang_name = CODE_TO_LABEL.get(lang_code, lang_code)
                summary = summarize_large_text_with_gemini(transcription, target_lang_name)
                if not summary:
                    raise RuntimeError("Empty summary returned by Gemini.")
                if len(summary) > 4000:
                    f = io.BytesIO(summary.encode("utf-8"))
                    f.name = f"summary_{orig_message_id}.txt"
                    bot.send_document(chat_id, f, reply_to_message_id=orig_message_id)
                else:
                    bot.send_message(chat_id, summary, reply_to_message_id=orig_message_id)
            except Exception as e:
                logging.exception("Error during do_summarize")
                bot.send_message(chat_id, f"❌ An error occurred during summarization: {e}", reply_to_message_id=orig_message_id)
            finally:
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

# health check
@app.route("/healthz")
def healthz():
    return "OK"

# ----------------------------
# --- BOOT (set webhook) ---
# ----------------------------
if __name__ == "__main__":
    webhook_url = WEBHOOK_BASE.rstrip("/") + "/telegram_webhook"
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        bot.set_webhook(url=webhook_url)
        print("Webhook set to:", webhook_url)
        # Check MongoDB connection on startup
        try:
            client.admin.command('ping')
            logging.info("Successfully connected to MongoDB!")
        except Exception as e:
            logging.error(f"Could not connect to MongoDB: {e}")

    except Exception:
        logging.exception("Failed to set webhook")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
