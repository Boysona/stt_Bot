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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

GEMINI_API_KEY = "AIzaSyDpb3UvnrRgk6Fu61za_VrRN8byZRSyq_I"
ASSEMBLYAI_API_KEY = "b07239215b60433b8e225e7fd8ef6576"
BOT_TOKEN = "7790991731:AAF4NHGm0BJCf08JTdBaUWKzwfs82_Y9Ecw"
WEBHOOK_BASE = "https://stt-bot-ckt1.onrender.com"
ADMIN_ID = 6964068910
REQUIRED_CHANNEL = ""
SECRET_KEY = "super-secret-please-change"
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024
MONGO_URI = "mongodb+srv://hoskasii:GHyCdwpI0PvNuLTg@cluster0.dy7oe7t.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "telegram_bot_db"

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
users_collection = db["users"]
tokens_collection = db["tokens"]

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, parse_mode='HTML')
serializer = URLSafeTimedSerializer(SECRET_KEY)

admin_state = {}
user_transcriptions = {}
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
in_memory_data = {
    "pending_media": {},
}

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

def delete_transcription_later(user_id: str, message_id: int):
    time.sleep(600)
    with memory_lock:
        if user_id in user_transcriptions and message_id in user_transcriptions[user_id]:
            del user_transcriptions[user_id][message_id]

def assemblyai_upload_from_stream(stream_iterable):
    upload_url = "https://api.assemblyai.com/v2/upload"
    headers = {"authorization": ASSEMBLYAI_API_KEY}
    resp = requests.post(upload_url, headers=headers, data=stream_iterable, timeout=3600)
    resp.raise_for_status()
    return resp.json().get("upload_url")

def create_transcript_and_wait(audio_url: str, language_code: str = None, poll_interval=2):
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

def build_lang_keyboard(callback_prefix: str, row_width: int = 3, message_id: int = None):
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

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("admin_") and c.from_user.id == ADMIN_ID)
def admin_menu_callback(call):
    try:
        chat_id = call.message.chat.id
        data = call.data.replace("admin_", "")

        if data == "total_users":
            total_registered = users_collection.count_documents({})
            bot.send_message(chat_id, f"👥 Total registered users: {total_registered}")
        elif data == "broadcast":
            admin_state[call.from_user.id] = 'awaiting_broadcast_message'
            bot.send_message(chat_id, "📢 Send the broadcast message now:")
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
        
        if not data.startswith("toggle_"):
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
        
        if admin_button_state["translate"] or admin_button_state["summarize"]:
            markup = InlineKeyboardMarkup()
            if admin_button_state["translate"]:
                markup.add(InlineKeyboardButton("Translate", callback_data=f"btn_translate|{message.message_id}"))
            if admin_button_state["summarize"]:
                markup.add(InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{message.message_id}"))
        else:
            markup = None

        if len(text) > 4000:
            user_transcriptions.setdefault(uid, {})[message.message_id] = text
            threading.Thread(target=delete_transcription_later, args=(uid, message.message_id), daemon=True).start()

            f = io.BytesIO(text.encode("utf-8"))
            f.name = "transcription.txt"
            bot.send_document(message.chat.id, f, caption="Your transcription is ready.", reply_to_message_id=message.message_id, reply_markup=markup)
        else:
            user_transcriptions.setdefault(uid, {})[message.message_id] = text
            threading.Thread(target=delete_transcription_later, args=(uid, message.message_id), daemon=True).start()
            
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

UPLOAD_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Upload File</title>
    <style>
        :root {
            --primary: #0088cc;
            --primary-dark: #006699;
            --bg: #f5f5f5;
            --card-bg: #ffffff;
            --text: #333333;
            --text-light: #666666;
            --border: #e0e0e0;
            --success: #4caf50;
            --error: #f44336;
        }
        
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif;
        }
        
        body {
            background-color: var(--bg);
            color: var(--text);
            line-height: 1.6;
            padding: 20px;
            max-width: 500px;
            margin: 0 auto;
        }
        
        .card {
            background-color: var(--card-bg);
            border-radius: 16px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
            padding: 24px;
            margin-bottom: 20px;
        }
        
        .header {
            text-align: center;
            margin-bottom: 24px;
        }
        
        .header h1 {
            font-size: 24px;
            font-weight: 600;
            margin-bottom: 8px;
            color: var(--primary);
        }
        
        .header p {
            color: var(--text-light);
            font-size: 16px;
        }
        
        .info {
            background-color: rgba(0, 136, 204, 0.1);
            border-left: 4px solid var(--primary);
            padding: 12px 16px;
            margin-bottom: 20px;
            border-radius: 4px;
        }
        
        .info p {
            margin: 4px 0;
            font-size: 14px;
        }
        
        .info strong {
            color: var(--primary);
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .file-input {
            width: 100%;
            padding: 16px;
            border: 2px dashed var(--border);
            border-radius: 12px;
            text-align: center;
            cursor: pointer;
            transition: all 0.3s ease;
            background-color: rgba(0, 136, 204, 0.05);
        }
        
        .file-input:hover {
            border-color: var(--primary);
            background-color: rgba(0, 136, 204, 0.1);
        }
        
        .file-input input {
            display: none;
        }
        
        .file-input label {
            display: flex;
            flex-direction: column;
            align-items: center;
            cursor: pointer;
        }
        
        .file-input svg {
            width: 48px;
            height: 48px;
            margin-bottom: 12px;
            fill: var(--primary);
        }
        
        .file-input span {
            font-size: 16px;
            font-weight: 500;
            color: var(--primary);
        }
        
        .file-input small {
            color: var(--text-light);
            margin-top: 4px;
        }
        
        .btn {
            display: block;
            width: 100%;
            padding: 16px;
            background-color: var(--primary);
            color: white;
            border: none;
            border-radius: 12px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: background-color 0.3s ease;
        }
        
        .btn:disabled {
            background-color: var(--border);
            cursor: not-allowed;
        }
        
        .btn:hover:not(:disabled) {
            background-color: var(--primary-dark);
        }
        
        .progress {
            display: none;
            margin-top: 20px;
        }
        
        .progress-bar {
            height: 6px;
            background-color: var(--border);
            border-radius: 3px;
            overflow: hidden;
        }
        
        .progress-fill {
            height: 100%;
            background-color: var(--primary);
            width: 0%;
            transition: width 0.3s ease;
        }
        
        .progress-text {
            text-align: center;
            margin-top: 8px;
            font-size: 14px;
            color: var(--text-light);
        }
        
        .result {
            display: none;
            text-align: center;
            padding: 24px;
        }
        
        .result.success {
            color: var(--success);
        }
        
        .result.error {
            color: var(--error);
        }
        
        .result svg {
            width: 64px;
            height: 64px;
            margin-bottom: 16px;
        }
        
        .result h2 {
            font-size: 20px;
            margin-bottom: 8px;
        }
        
        .result p {
            color: var(--text-light);
        }
        
        .footer {
            text-align: center;
            margin-top: 24px;
            color: var(--text-light);
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <h1>Upload File for Transcription</h1>
            <p>Select your audio or video file to transcribe</p>
        </div>
        
        <div class="info">
            <p><strong>Chat ID:</strong> {{ chat_id }}</p>
            <p><strong>Language:</strong> {{ lang }}</p>
        </div>
        
        <form id="uploadForm" enctype="multipart/form-data">
            <div class="form-group">
                <div class="file-input">
                    <label for="file">
                        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
                            <path d="M14,2H6A2,2 0 0,0 4,4V20A2,2 0 0,0 6,22H18A2,2 0 0,0 20,20V8L14,2M18,20H6V4H13V9H18V20Z" />
                        </svg>
                        <span>Select a file</span>
                        <small>Audio or video files only</small>
                    </label>
                    <input type="file" name="file" id="file" required accept="audio/*,video/*">
                </div>
            </div>
            
            <button type="submit" class="btn" id="submitBtn">Upload & Transcribe</button>
        </form>
        
        <div class="progress" id="progress">
            <div class="progress-bar">
                <div class="progress-fill" id="progressFill"></div>
            </div>
            <div class="progress-text" id="progressText">Uploading... 0%</div>
        </div>
        
        <div class="result success" id="successResult">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
                <path fill="currentColor" d="M21,7L9,19L3.5,13.5L4.91,12.09L9,16.17L19.59,5.59L21,7Z" />
            </svg>
            <h2>Upload Complete!</h2>
            <p>Your file has been uploaded successfully. The transcription will be sent to your Telegram chat shortly.</p>
            <p>You can safely close this tab.</p>
        </div>
        
        <div class="result error" id="errorResult">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
                <path fill="currentColor" d="M13,13H11V7H13M13,17H11V15H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z" />
            </svg>
            <h2>Upload Failed</h2>
            <p id="errorMessage">An error occurred during upload. Please try again.</p>
        </div>
    </div>
    
    <div class="footer">
        <p>Link expires in 1 hour</p>
    </div>

    <script>
        document.addEventListener('DOMContentLoaded', function() {
            const form = document.getElementById('uploadForm');
            const fileInput = document.getElementById('file');
            const submitBtn = document.getElementById('submitBtn');
            const progress = document.getElementById('progress');
            const progressFill = document.getElementById('progressFill');
            const progressText = document.getElementById('progressText');
            const successResult = document.getElementById('successResult');
            const errorResult = document.getElementById('errorResult');
            const errorMessage = document.getElementById('errorMessage');
            
            fileInput.addEventListener('change', function() {
                if (this.files.length > 0) {
                    document.querySelector('.file-input span').textContent = this.files[0].name;
                }
            });
            
            form.addEventListener('submit', function(e) {
                e.preventDefault();
                
                const file = fileInput.files[0];
                if (!file) return;
                
                const formData = new FormData();
                formData.append('file', file);
                
                submitBtn.disabled = true;
                progress.style.display = 'block';
                
                const xhr = new XMLHttpRequest();
                
                xhr.upload.addEventListener('progress', function(e) {
                    if (e.lengthComputable) {
                        const percentComplete = (e.loaded / e.total) * 100;
                        progressFill.style.width = percentComplete + '%';
                        progressText.textContent = 'Uploading... ' + Math.round(percentComplete) + '%';
                    }
                });
                
                xhr.addEventListener('load', function() {
                    if (xhr.status === 200) {
                        progress.style.display = 'none';
                        successResult.style.display = 'block';
                        form.style.display = 'none';
                    } else {
                        showError('Upload failed: ' + xhr.statusText);
                    }
                });
                
                xhr.addEventListener('error', function() {
                    showError('Network error. Please check your connection and try again.');
                });
                
                xhr.open('POST', window.location.href, true);
                xhr.send(formData);
            });
            
            function showError(message) {
                submitBtn.disabled = false;
                progress.style.display = 'none';
                errorMessage.textContent = message;
                errorResult.style.display = 'block';
            }
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
        return render_template_string(UPLOAD_PAGE, chat_id=chat_id, lang=lang)

    file = request.files.get('file')
    if not file:
        return "No file uploaded", 400

    processing_msg = bot.send_message(chat_id, "🔄 Processing...")
    processing_msg_id = processing_msg.message_id

    def transcribe_in_background():
        stop_animation = False
        def stop_event():
            return stop_animation
        
        animation_thread = threading.Thread(
            target=animate_processing_message, 
            args=(chat_id, processing_msg_id, stop_event)
        )
        animation_thread.start()
        
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

            if admin_button_state["translate"] or admin_button_state["summarize"]:
                markup = InlineKeyboardMarkup()
                if admin_button_state["translate"]:
                    markup.add(InlineKeyboardButton("Translate", callback_data=f"btn_translate|{processing_msg_id}"))
                if admin_button_state["summarize"]:
                    markup.add(InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{processing_msg_id}"))
            else:
                markup = None

            if len(text) > 4000:
                user_transcriptions.setdefault(str(chat_id), {})[processing_msg_id] = text
                threading.Thread(target=delete_transcription_later, args=(str(chat_id), processing_msg_id), daemon=True).start()

                f = io.BytesIO(text.encode("utf-8"))
                f.name = "transcription.txt"
                bot.send_document(chat_id, f, caption="Your transcription is ready.", reply_markup=markup, reply_to_message_id=processing_msg_id)
            else:
                user_transcriptions.setdefault(str(chat_id), {})[processing_msg_id] = text
                threading.Thread(target=delete_transcription_later, args=(str(chat_id), processing_msg_id), daemon=True).start()

                bot.send_message(chat_id, text or "No transcription text was returned.", reply_markup=markup, reply_to_message_id=processing_msg_id)

        except Exception as e:
            logging.exception("Error transcribing uploaded file")
            bot.send_message(chat_id, f"Error occurred while transcribing: {str(e)}", reply_to_message_id=processing_msg_id)
        finally:
            stop_animation = True
            animation_thread.join()
            try:
                bot.delete_message(chat_id, processing_msg_id)
            except Exception:
                pass

    threading.Thread(target=transcribe_in_background, daemon=True).start()

    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Upload Complete</title>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif;
                background-color: #f5f5f5;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                padding: 20px;
            }
            .card {
                background: white;
                border-radius: 16px;
                padding: 24px;
                text-align: center;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
                max-width: 400px;
            }
            .icon {
                font-size: 64px;
                margin-bottom: 16px;
            }
            h1 {
                color: #0088cc;
                margin-bottom: 16px;
            }
            p {
                color: #666;
                line-height: 1.6;
                margin-bottom: 8px;
            }
        </style>
    </head>
    <body>
        <div class="card">
            <div class="icon">✅</div>
            <h1>Upload Complete</h1>
            <p>Your file has been uploaded successfully.</p>
            <p>The transcription will be sent to your Telegram chat once it's ready.</p>
            <p>You can safely close this tab.</p>
        </div>
    </body>
    </html>
    """

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("btn_translate|"))
def button_translate_handler(call):
    try:
        uid = str(call.from_user.id)
        _, message_id_str = call.data.split("|", 1)
        message_id = int(message_id_str)
        if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
            bot.answer_callback_query(call.id, "❌ Transcription not available or expired")
            return
        markup = build_lang_keyboard("translate_to", message_id=message_id)
        try:
            bot.send_message(call.message.chat.id, "Select target language for translation:", reply_markup=markup, reply_to_message_id=message_id)
        except Exception:
            bot.send_message(call.message.chat.id, " Select target language for translation:", reply_markup=markup)
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
        markup = build_lang_keyboard("summarize_in", message_id=message_id)
        try:
            bot.send_message(call.message.chat.id, "Select language for summary:", reply_markup=markup, reply_to_message_id=message_id)
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

        def do_translate(chat_id, orig_message_id, progress_message_id):
            stop_animation = False
            def stop_event():
                return stop_animation
            
            if progress_message_id:
                animation_thread = threading.Thread(
                    target=animate_processing_message, 
                    args=(chat_id, progress_message_id, stop_event)
                )
                animation_thread.start()
            
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
                stop_animation = True
                if progress_message_id:
                    try:
                        animation_thread.join()
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

        def do_summarize(chat_id, orig_message_id, progress_message_id):
            stop_animation = False
            def stop_event():
                return stop_animation
            
            if progress_message_id:
                animation_thread = threading.Thread(
                    target=animate_processing_message, 
                    args=(chat_id, progress_message_id, stop_event)
                )
                animation_thread.start()
            
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
                stop_animation = True
                if progress_message_id:
                    try:
                        animation_thread.join()
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
