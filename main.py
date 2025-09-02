# main.py
import os
import time
import json
import requests
import logging
import io
import threading
from flask import Flask, request, render_template_string, abort
import telebot
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from threading import Thread
from datetime import datetime
from pymongo import MongoClient

# ----------------------------
# --- CONFIG (replace or keep) ---
# ----------------------------
# Bot 1 values kept, plus Bot 2 DB/Gemini details (as requested)
ASSEMBLYAI_API_KEY = "b07239215b60433b8e225e7fd8ef6576"
WEBHOOK_BASE = "https://stt-bot-ckt1.onrender.com"   # your webhook base (render URL)
BOT_TOKEN = "7790991731:AAF4NHGm0BJCf08JTdBaUWKzwfs82_Y9Ecw"
SECRET_KEY = "super-secret-please-change"  # signing for upload links (change in prod)

# Bot 2 additions
GEMINI_API_KEY = "AIzaSyDpb3UvnrRgk6Fu61za_VrRN8byZRSyq_I"
MONGO_URI = "mongodb+srv://hoskasii:GHyCdwpI0PvNuLTg@cluster0.dy7oe7t.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "telegram_bot_db"
ADMIN_ID = 6964068910  # set admin id (from Bot2)

# Limits
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024  # 20MB

# Flask & TeleBot init
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML', threaded=True)

# serializer for generating signed upload links that expire
serializer = URLSafeTimedSerializer(SECRET_KEY)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------
# --- DATABASE (Mongo) ------
# ----------------------------
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
users_collection = db["users"]
tokens_collection = db.get("tokens")  # optional for storing tokens if needed

# ----------------------------
# --- IN-MEMORY TRANSCRIPTS ---
# ----------------------------
# store ephemeral transcriptions for quick translate/summarize buttons
user_transcriptions = {}  # { user_id_str: { message_id: text } }
# auto-expire threads will remove stored transcripts after some minutes
TRANSCRIPT_TTL_SECONDS = 600  # 10 minutes

# ----------------------------
# --- LANGUAGES (from Bot1 + Bot2) ---
# ----------------------------
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
    ("🇵🇭 Tagalog", "tl"),
    ("🇸🇴 Soomaali", "so"),
]
DEFAULT_LANG = "en"

# ----------------------------
# --- UTIL FUNCTIONS -------
# ----------------------------
def make_lang_keyboard():
    from telebot import types
    kb = types.InlineKeyboardMarkup()
    buttons = []
    for label, code in LANG_OPTIONS:
        buttons.append(types.InlineKeyboardButton(text=label, callback_data=f"lang:{code}"))
    # arrange in three per row
    row = []
    for i, btn in enumerate(buttons, 1):
        row.append(btn)
        if i % 3 == 0:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    return kb

def signed_upload_token(chat_id: int, lang_code: str):
    payload = {"chat_id": chat_id, "lang": lang_code}
    return serializer.dumps(payload)

def unsign_upload_token(token: str, max_age_seconds: int = 3600):
    # raises SignatureExpired or BadSignature
    data = serializer.loads(token, max_age=max_age_seconds)
    return data

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

def create_transcript_and_wait(audio_url: str, language_code: str = None, status_callback=None, poll_interval=2):
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
    if not job_id:
        raise RuntimeError("No transcription job id returned")
    get_url = f"{create_url}/{job_id}"

    while True:
        r = requests.get(get_url, headers={"authorization": ASSEMBLYAI_API_KEY}, timeout=60)
        r.raise_for_status()
        status = r.json()
        if status_callback:
            try:
                status_callback(status)
            except Exception:
                pass
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

def send_welcome_message(chat_id, first_name="Friend"):
    text = (
        f"👋 Salaam {first_name}!\n\n"
        "Send me a voice message, audio file, or video and I will transcribe it for free.\n"
        "Use the language buttons first if you want a specific transcription language."
    )
    bot.send_message(chat_id, text)

# ----------------------------
# --- DB helpers -------------
# ----------------------------
def register_user(user_id: int, username: str = None, first_name: str = None):
    now = datetime.utcnow()
    users_collection.update_one(
        {"_id": str(user_id)},
        {"$set": {"username": username, "first_name": first_name, "last_seen": now}, "$setOnInsert": {"first_seen": now, "stt_conversion_count": 0, "tts_conversion_count": 0}},
        upsert=True
    )

def update_user_activity(user_id: int):
    now = datetime.utcnow()
    users_collection.update_one({"_id": str(user_id)}, {"$set": {"last_seen": now}}, upsert=True)

def set_user_lang(user_id: int, lang_code: str):
    users_collection.update_one({"_id": str(user_id)}, {"$set": {"stt_language": lang_code}}, upsert=True)

def get_user_lang(user_id: int) -> str:
    doc = users_collection.find_one({"_id": str(user_id)})
    if doc and "stt_language" in doc:
        return doc["stt_language"]
    return DEFAULT_LANG

def increment_processing_count_db(user_id: int, service_type: str = "stt"):
    field = f"{service_type}_conversion_count"
    users_collection.update_one({"_id": str(user_id)}, {"$inc": {field: 1}}, upsert=True)

# ----------------------------
# --- GEMINI helpers (from Bot2) ---
# ----------------------------
def ask_gemini(text: str, instruction: str) -> str:
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
    try:
        resp = requests.post(url, headers={'Content-Type': "application/json"}, json=payload, timeout=60)
        result = resp.json()
        if "candidates" in result and result['candidates']:
            # try to extract safely
            candidate = result['candidates'][0]
            parts = candidate.get('content', {}).get('parts', [])
            if parts:
                return parts[0].get('text', '')
        # fallback: stringify
        return "Error: " + json.dumps(result)
    except Exception as e:
        return f"Error: {str(e)}"

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
        if res.startswith("Error:"):
            return res
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
        if res.startswith("Error:"):
            return res
        partial_summaries.append(res)
    combined = "\n\n".join(partial_summaries)
    final_instr = f"Combine and polish these partial summaries into a single concise summary in {target_lang_name}. Only provide the summary:"
    final = ask_gemini(combined, final_instr)
    return final

# ----------------------------
# --- HELPERS for transcripts storage/expiry
# ----------------------------
def store_transcript_temporarily(user_id_str: str, message_id: int, text: str):
    user_transcriptions.setdefault(user_id_str, {})[message_id] = text
    # start TTL thread
    t = threading.Thread(target=_delete_transcript_later, args=(user_id_str, message_id), daemon=True)
    t.start()

def _delete_transcript_later(user_id_str: str, message_id: int):
    time.sleep(TRANSCRIPT_TTL_SECONDS)
    if user_id_str in user_transcriptions:
        user_transcriptions[user_id_str].pop(message_id, None)
        if not user_transcriptions[user_id_str]:
            user_transcriptions.pop(user_id_str, None)

# ----------------------------
# --- TELEGRAM HANDLERS -----
# ----------------------------
@bot.message_handler(commands=['start'])
def handle_start(message):
    register_user(message.from_user.id, username=getattr(message.from_user, 'username', None), first_name=getattr(message.from_user, 'first_name', None))
    kb = make_lang_keyboard()
    bot.send_message(message.chat.id, "Please choose your Transcription language", reply_markup=kb)

@bot.message_handler(commands=['help'])
def handle_help(message):
    update_user_activity(message.from_user.id)
    text = (
        "Commands supported:\n"
        "/start - Show welcome message\n"
        "/lang  - Change language\n"
        "/help  - This help message\n\n"
        "Send a voice/audio/video (≤ 20MB) and I will transcribe it.\n"
        "If it's larger than 20MB, I'll give you a secure upload link."
    )
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=['lang'])
def handle_lang(message):
    kb = make_lang_keyboard()
    bot.send_message(message.chat.id, "Choose a language:", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("lang:"))
def handle_lang_callback(call):
    lang_code = call.data.split(":", 1)[1]
    set_user_lang(call.message.chat.id if call.message else call.from_user.id, lang_code)
    try:
        bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    except Exception:
        pass
    # Send welcome
    first_name = call.from_user.first_name if call.from_user else "Friend"
    send_welcome_message(call.message.chat.id, first_name=first_name)
    bot.answer_callback_query(call.id, f"✅ you set to {lang_code}")

# Main handler for media messages (keeps Bot1 logic, adds translate/summarize buttons)
@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    register_user(message.from_user.id, username=getattr(message.from_user,'username',None), first_name=getattr(message.from_user,'first_name',None))
    chat_id = message.chat.id
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
        # accept doc if it's an audio/video type
        mime = message.document.mime_type
        if mime and ('audio' in mime or 'video' in mime):
            file_id = message.document.file_id
            file_size = message.document.file_size
        else:
            bot.send_message(chat_id, "Sorry, I can only transcribe audio or video files.")
            return

    # language from DB
    lang = get_user_lang(message.from_user.id)

    if file_size and file_size > TELEGRAM_MAX_BYTES:
        token = signed_upload_token(chat_id, lang)
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
        bot.send_message(chat_id, text, disable_web_page_preview=True, reply_to_message_id=message.message_id)
        return

    # Send processing message and animate
    processing_msg = bot.send_message(chat_id, "🔄 Processing...", reply_to_message_id=message.message_id)
    processing_msg_id = processing_msg.message_id

    stop_animation = False
    def stop_event():
        return stop_animation
    animation_thread = Thread(target=animate_processing_message, args=(chat_id, processing_msg_id, stop_event))
    animation_thread.start()

    try:
        tf, file_url = telegram_file_info_and_url(file_id)
        gen = telegram_file_stream(file_url)
        upload_url = assemblyai_upload_from_stream(gen)

        # Create and wait for transcript
        text = create_transcript_and_wait(upload_url, language_code=lang)

        # store and present with Translate & Summarize buttons
        if len(text) > 4000:
            # send as file with inline markup that also includes buttons
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(telebot.types.InlineKeyboardButton("Translate", callback_data=f"btn_translate|{message.message_id}"))
            markup.add(telebot.types.InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{message.message_id}"))
            # write to bytesIO and send as document with reply markup
            b = io.BytesIO(text.encode("utf-8"))
            b.name = "transcription.txt"
            bot.send_document(chat_id, b, caption="Your transcription is ready.", reply_markup=markup, reply_to_message_id=message.message_id)
            # we still keep a copy in memory for translate/summarize
            store_transcript_temporarily(str(message.from_user.id), message.message_id, text)
        else:
            # small enough to send directly with buttons
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(
                telebot.types.InlineKeyboardButton("Translate", callback_data=f"btn_translate|{message.message_id}"),
                telebot.types.InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{message.message_id}")
            )
            sent = bot.send_message(chat_id, text or "No transcription text was returned.", reply_markup=markup, reply_to_message_id=message.message_id)
            # sometimes message ids differ; use original message id as key for continuity
            store_transcript_temporarily(str(message.from_user.id), message.message_id, text)
        # increment user's db count
        increment_processing_count_db(message.from_user.id, "stt")
    except Exception as e:
        logger.exception("Error during transcription")
        bot.send_message(chat_id, f"Error during transcription: {e}", reply_to_message_id=message.message_id)
    finally:
        stop_animation = True
        animation_thread.join()
        try:
            bot.delete_message(chat_id, processing_msg_id)
        except Exception:
            pass

# ----------------------------
# --- CALLBACKS for translate / summarize & admin ---
# ----------------------------
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("btn_translate|"))
def button_translate_handler(call):
    uid = str(call.from_user.id)
    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)
    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ Transcription not available or expired")
        return

    # Build language selection keyboard (use LANG_OPTIONS labels for convenience)
    markup = telebot.types.InlineKeyboardMarkup(row_width=3)
    buttons = []
    for label, code in LANG_OPTIONS:
        # Use the label (emoji + native) but Gemini needs a language name like "French"
        # We'll send language name as label without emoji for Gemini (simple heuristic)
        label_text = label
        # callback_data: translate_to|<lang_label>|<message_id>
        buttons.append(telebot.types.InlineKeyboardButton(label_text, callback_data=f"translate_to|{label}|{message_id}"))
    # add rows
    for i in range(0, len(buttons), 3):
        markup.add(*buttons[i:i+3])

    try:
        bot.send_message(call.message.chat.id, "🌍 Select target language for translation:", reply_markup=markup, reply_to_message_id=message_id)
    except Exception:
        bot.send_message(call.message.chat.id, "🌍 Select target language for translation:", reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("btn_summarize|"))
def button_summarize_handler(call):
    uid = str(call.from_user.id)
    _, message_id_str = call.data.split("|", 1)
    message_id = int(message_id_str)
    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ Transcription expired")
        return

    markup = telebot.types.InlineKeyboardMarkup(row_width=3)
    buttons = []
    for label, code in LANG_OPTIONS:
        buttons.append(telebot.types.InlineKeyboardButton(label, callback_data=f"summarize_in|{label}|{message_id}"))
    for i in range(0, len(buttons), 3):
        markup.add(*buttons[i:i+3])

    try:
        bot.send_message(call.message.chat.id, "🌍 Select language for summary:", reply_markup=markup, reply_to_message_id=message_id)
    except Exception:
        bot.send_message(call.message.chat.id, "🌍 Select language for summary:", reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("translate_to|"))
def callback_translate_to(call):
    uid = str(call.from_user.id)
    parts = call.data.split("|")
    lang_label = parts[1]
    message_id = int(parts[2])
    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ Transcription expired")
        return

    # delete the language selection message (tidy UI)
    try:
        bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    except Exception:
        pass

    # send progress message
    progress_msg = None
    try:
        progress_msg = bot.send_message(call.message.chat.id, f"🔄 Translating to {lang_label}...", reply_to_message_id=message_id)
    except Exception:
        progress_msg = None
    bot.answer_callback_query(call.id)

    def do_translate(chat_id, orig_message_id, progress_message_id):
        try:
            transcription = user_transcriptions[uid][orig_message_id]
            # Extract a readable language name from label, remove emoji if present
            readable_lang = lang_label.split(" ", 1)[-1] if " " in lang_label else lang_label
            translated = translate_large_text_with_gemini(transcription, readable_lang)
            if translated.startswith("Error:"):
                bot.send_message(chat_id, f"❌ Translation error: {translated}", reply_to_message_id=orig_message_id)
                return
            if len(translated) > 4000:
                f = io.BytesIO(translated.encode("utf-8"))
                f.name = f"translation_{orig_message_id}.txt"
                try:
                    bot.send_document(chat_id, f, caption=f"🌍 Translation to {readable_lang}:", reply_to_message_id=orig_message_id)
                except Exception as e:
                    logger.error(f"Failed to send translation file: {e}")
                    bot.send_message(chat_id, "✅ Translation complete (could not send file).", reply_to_message_id=orig_message_id)
            else:
                bot.send_message(chat_id, translated, reply_to_message_id=orig_message_id)
        except Exception as e:
            logger.exception(f"Error during translation: {e}")
            try:
                bot.send_message(chat_id, "❌ An error occurred during translation.", reply_to_message_id=orig_message_id)
            except Exception:
                pass
        finally:
            if progress_message_id:
                try:
                    bot.delete_message(chat_id, progress_message_id)
                except Exception:
                    pass

    threading.Thread(target=lambda: do_translate(call.message.chat.id, message_id, progress_msg.message_id if progress_msg else None), daemon=True).start()

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("summarize_in|"))
def callback_summarize_in(call):
    uid = str(call.from_user.id)
    parts = call.data.split("|")
    lang_label = parts[1]
    message_id = int(parts[2])
    if uid not in user_transcriptions or message_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ Transcription expired")
        return

    try:
        bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    except Exception:
        pass

    progress_msg = None
    try:
        progress_msg = bot.send_message(call.message.chat.id, f"🔄 Summarizing in {lang_label}...", reply_to_message_id=message_id)
    except Exception:
        progress_msg = None
    bot.answer_callback_query(call.id)

    def do_summarize(chat_id, orig_message_id, progress_message_id):
        try:
            transcription = user_transcriptions[uid][orig_message_id]
            readable_lang = lang_label.split(" ", 1)[-1] if " " in lang_label else lang_label
            summary = summarize_large_text_with_gemini(transcription, readable_lang)
            if summary.startswith("Error:"):
                bot.send_message(chat_id, f"❌ Summarization error: {summary}", reply_to_message_id=orig_message_id)
                return
            if len(summary) > 4000:
                f = io.BytesIO(summary.encode("utf-8"))
                f.name = f"summary_{orig_message_id}.txt"
                try:
                    bot.send_document(chat_id, f, caption=f"📝 Summary in {readable_lang}:", reply_to_message_id=orig_message_id)
                except Exception as e:
                    logger.error(f"Failed to send summary file: {e}")
                    bot.send_message(chat_id, "✅ Summary complete (could not send file).", reply_to_message_id=orig_message_id)
            else:
                bot.send_message(chat_id, summary, reply_to_message_id=orig_message_id)
        except Exception as e:
            logger.exception(f"Error during summarization: {e}")
            try:
                bot.send_message(chat_id, "❌ An error occurred during summarization.", reply_to_message_id=orig_message_id)
            except Exception:
                pass
        finally:
            if progress_message_id:
                try:
                    bot.delete_message(chat_id, progress_message_id)
                except Exception:
                    pass

    threading.Thread(target=lambda: do_summarize(call.message.chat.id, message_id, progress_msg.message_id if progress_msg else None), daemon=True).start()

# ----------------------------
# --- ADMIN PANEL & BROADCAST
# ----------------------------
admin_state = {}  # { admin_user_id: 'awaiting_broadcast_message' }

def build_admin_menu():
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("📊 Total Users", callback_data="admin_total_users"),
        telebot.types.InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")
    )
    return markup

@bot.message_handler(commands=['admin'])
def admin_handler(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "🚫 You are not authorized to use this command.")
        return
    update_user_activity(message.from_user.id)
    bot.send_message(message.chat.id, "⚙️ Admin Panel", reply_markup=build_admin_menu())

@bot.callback_query_handler(func=lambda c: c.data in ["admin_total_users", "admin_broadcast"] and c.from_user.id == ADMIN_ID)
def admin_menu_callback(call):
    chat_id = call.message.chat.id
    if call.data == "admin_total_users":
        total_registered = users_collection.count_documents({})
        bot.send_message(chat_id, f"👥 Total registered users: {total_registered}")
    elif call.data == "admin_broadcast":
        admin_state[call.from_user.id] = 'awaiting_broadcast_message'
        bot.send_message(chat_id, "📢 Send the broadcast message now:")
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.from_user.id == ADMIN_ID and admin_state.get(m.from_user.id) == 'awaiting_broadcast_message', content_types=['text', 'photo', 'video', 'audio', 'document'])
def broadcast_message(message):
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
            logger.error(f"Failed to send broadcast to {uid}: {e}")
            fail += 1
        time.sleep(0.05)
    bot.send_message(message.chat.id, f"📊 Broadcast complete. ✅ Successful: {success}, ❌ Failed: {fail}")

# ----------------------------
# --- FLASK ROUTES ----------
# ----------------------------
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
    lang = data.get("lang", DEFAULT_LANG)

    if request.method == 'GET':
        return render_template_string(UPLOAD_PAGE, chat_id=chat_id, lang=lang)

    # POST: user uploaded a file
    file = request.files.get('file')
    if not file:
        return "No file uploaded", 400

    upload_msg = bot.send_message(chat_id, f"📥 Received large upload from web interface. Starting transcription (language: {lang}).")
    upload_msg_id = upload_msg.message_id

    try:
        # Stream file.stream (werkzeug FileStorage) to AssemblyAI without loading into memory
        def file_gen():
            chunk_size = 256*1024
            while True:
                chunk = file.stream.read(chunk_size)
                if not chunk:
                    break
                yield chunk

        upload_url = assemblyai_upload_from_stream(file_gen())
        text = create_transcript_and_wait(upload_url, language_code=lang)

        # Check text length and send as file if needed
        if len(text) > 4000:
            # send file with buttons
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(telebot.types.InlineKeyboardButton("Translate", callback_data=f"btn_translate|{upload_msg_id}"))
            markup.add(telebot.types.InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{upload_msg_id}"))
            b = io.BytesIO(text.encode("utf-8"))
            b.name = "transcription.txt"
            bot.send_document(chat_id, b, caption="Your transcription is ready.", reply_markup=markup)
            store_transcript_temporarily(str(chat_id), upload_msg_id, text)
        else:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(
                telebot.types.InlineKeyboardButton("Translate", callback_data=f"btn_translate|{upload_msg_id}"),
                telebot.types.InlineKeyboardButton("Summarize", callback_data=f"btn_summarize|{upload_msg_id}")
            )
            bot.send_message(chat_id, text or "No transcription text was returned.", reply_markup=markup)
            store_transcript_temporarily(str(chat_id), upload_msg_id, text)

    except Exception as e:
        logger.exception("Error transcribing uploaded file")
        bot.send_message(chat_id, f"Error transcribing uploaded file: {e}")
        return f"Error during transcription: {e}", 500
    finally:
        try:
            bot.delete_message(chat_id, upload_msg_id)
        except Exception:
            pass

    return "<h3>Upload complete. Transcription will be sent to your Telegram chat.</h3>"

# health check
@app.route("/healthz")
def healthz():
    return "OK"

# Telegram webhook route
@app.route("/telegram_webhook", methods=['POST'])
def telegram_webhook():
    update_json = request.get_json(force=True)
    try:
        update = telebot.types.Update.de_json(update_json)
        bot.process_new_updates([update])
    except Exception as e:
        logger.exception("Error processing update:")
    return "OK"

# ----------------------------
# --- BOOT (set webhook) -----
# ----------------------------
def set_webhook_on_startup():
    webhook_url = WEBHOOK_BASE.rstrip("/") + "/telegram_webhook"
    try:
        bot.delete_webhook()
        time.sleep(0.5)
        bot.set_webhook(url=webhook_url)
        logger.info("Webhook set to: %s", webhook_url)
        return True
    except Exception as e:
        logger.exception("Failed to set webhook:")
        return False

if __name__ == "__main__":
    # attempt to set webhook and start flask
    set_webhook_on_startup()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
