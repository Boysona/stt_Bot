# main.py
import os
import time
import json
import io
import requests
import logging
import threading
from threading import Thread
from datetime import datetime
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from flask import Flask, request, render_template_string, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand

from pymongo import MongoClient

# ----------------------------
# --- CONFIG (edit as needed)-
# ----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Fallback tokens/keys (kept from your provided code)
FALLBACK_BOT_TOKEN = "7790991731:AAF4NHGm0BJCf08JTdBaUWKzwfs82_Y9Ecw"
ASSEMBLYAI_API_KEY = "b07239215b60433b8e225e7fd8ef6576"
GEMINI_API_KEY = "AIzaSyDpb3UvnrRgk6Fu61za_VrRN8byZRSyq_I"

# Mongo DB (from your Bot2)
MONGO_URI = "mongodb+srv://hoskasii:GHyCdwpI0PvNuLTg@cluster0.dy7oe7t.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "telegram_bot_db"

# Admin (change to your admin id)
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6964068910"))

# Server / webhook
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE", "https://stt-bot-ckt1.onrender.com")
WEBHOOK_ROUTE = "/telegram_webhook"

# Secret for signed upload links
SECRET_KEY = os.environ.get("SECRET_KEY", "super-secret-please-change")
serializer = URLSafeTimedSerializer(SECRET_KEY)

# Telegram file size limit for direct Telegram upload
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024  # 20MB

# Language options (kept combined)
LANG_OPTIONS = [
    ("🇺🇸 English", "en"),
    ("🇸🇴 Soomaali", "so"),
    ("🇦🇪 العربية", "ar"),
    ("🇪🇸 Español", "es"),
    ("🇫🇷 Français", "fr"),
    ("🇩🇪 Deutsch", "de"),
    ("🇮🇳 हिन्दी", "hi"),
    ("🇷🇺 Русский", "ru"),
    ("🇮🇩 Indonesia", "id"),
    ("🇰🇿 Қазақша", "kk"),
    ("🇮🇹 Italiano", "it"),
    ("🇹🇷 Türkçe", "tr"),
    ("🇯🇵 日本語", "ja"),
    ("🇰🇷 한국어", "ko"),
    ("🇨🇳 中文", "zh"),
    ("🇵🇹 Português", "pt"),
    ("🇻🇳 Tiếng Việt", "vi"),
    ("🇵🇭 Tagalog", "tl"),
    ("🇸🇦 العربية", "ar"),
]

DEFAULT_LANG = "en"

# ----------------------------
# --- DATABASE SETUP -------
# ----------------------------
mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = mongo_client[DB_NAME]
users_collection = db["users"]
tokens_collection = db["tokens"]   # your Bot2 used this; we read token from here
# optional collections
logs_collection = db.get("logs")

def get_bot_token_from_db(fallback=FALLBACK_BOT_TOKEN):
    try:
        # try multiple query patterns for flexibility
        doc = tokens_collection.find_one({"bot": "bot1"}) or tokens_collection.find_one({"name":"bot1_token"}) or tokens_collection.find_one({})
        if doc and ("token" in doc or "value" in doc):
            token = doc.get("token") or doc.get("value")
            if token:
                logging.info("Using BOT token fetched from DB tokens collection.")
                return token
    except Exception as e:
        logging.warning(f"Could not read token from DB: {e}")
    logging.info("Using fallback BOT token.")
    return fallback

BOT_TOKEN = get_bot_token_from_db()
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML', threaded=True)

# ----------------------------
# --- FLASK APP ------------
# ----------------------------
app = Flask(__name__)

# ----------------------------
# --- IN-MEMORY STORAGE -----
# ----------------------------
# short-lived transcription storage to enable Translate/Summarize buttons
user_transcriptions = {}  # { user_id_str: { original_message_id: text } }
admin_state = {}  # store admin workflow states (e.g., awaiting broadcast)

# ----------------------------
# --- UTIL FUNCTIONS -------
# ----------------------------
def update_user_activity(user_id: int):
    now = datetime.utcnow()
    users_collection.update_one(
        {"_id": str(user_id)},
        {"$set": {"last_active": now}, "$setOnInsert": {"first_seen": now, "stt_conversion_count": 0, "tts_conversion_count": 0}},
        upsert=True
    )

def increment_processing_count(user_id: str, service_type: str):
    field_to_inc = f"{service_type}_conversion_count"
    users_collection.update_one({"_id": str(user_id)}, {"$inc": {field_to_inc: 1}})

def set_user_lang(user_id: str, lang_code: str):
    users_collection.update_one({"_id": str(user_id)}, {"$set": {"stt_language": lang_code}}, upsert=True)

def get_user_lang(user_id: str):
    doc = users_collection.find_one({"_id": str(user_id)})
    if doc and "stt_language" in doc:
        return doc["stt_language"]
    return DEFAULT_LANG

def make_lang_keyboard():
    kb = InlineKeyboardMarkup()
    buttons = []
    from telebot import types
    for label, code in LANG_OPTIONS:
        b = InlineKeyboardButton(text=label, callback_data=f"lang:{code}")
        buttons.append(b)
    # arrange 3 per row
    row = []
    for i, btn in enumerate(buttons, 1):
        row.append(btn)
        if i % 3 == 0:
            kb.add(*row)
            row = []
    if row:
        kb.add(*row)
    return kb

def signed_upload_token(chat_id: int, lang_code: str):
    payload = {"chat_id": chat_id, "lang": lang_code}
    return serializer.dumps(payload)

def unsign_upload_token(token: str, max_age_seconds: int = 3600):
    data = serializer.loads(token, max_age=max_age_seconds)
    return data

def assemblyai_upload_from_stream(stream_iterable):
    upload_url = "https://api.assemblyai.com/v2/upload"
    headers = {"authorization": ASSEMBLYAI_API_KEY}
    resp = requests.post(upload_url, headers=headers, data=stream_iterable, timeout=3600)
    resp.raise_for_status()
    return resp.json().get("upload_url")

def create_transcript_and_wait(audio_url: str, language_code: str = None, status_callback=None, poll_interval=2):
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
    dots = [".", "..", "..."]
    idx = 0
    while not stop_event():
        try:
            bot.edit_message_text(f"🔄 Processing{dots[idx % len(dots)]}", chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
        idx = (idx + 1) % len(dots)
        time.sleep(0.6)

# ----------------------------
# --- Gemini helpers (Translate/Summarize) -------
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
        if "candidates" in result:
            return result['candidates'][0]['content']['parts'][0]['text']
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
# --- TELEGRAM HANDLERS -----
# ----------------------------
def send_welcome_message(chat_id, first_name="Friend"):
    lang = get_user_lang(str(chat_id))
    text = (
        f"👋 Salaam {first_name}!\n\n"
        "• Send me a voice message, audio, video or upload to transcribe.\n"
        "• Use /lang to change language.\n"
        "• Files ≤ 20MB are processed directly; larger files get a secure upload link."
    )
    bot.send_message(chat_id, text)

@bot.message_handler(commands=['start'])
def handle_start(message):
    update_user_activity(message.from_user.id)
    kb = make_lang_keyboard()
    bot.send_message(message.chat.id, "Please choose your Transcription language", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("lang:"))
def handle_lang_callback(call):
    lang_code = call.data.split(":", 1)[1]
    uid = str(call.from_user.id)
    set_user_lang(uid, lang_code)
    try:
        bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    except Exception:
        pass
    send_welcome_message(call.message.chat.id, first_name=call.from_user.first_name if call.from_user else "Friend")
    bot.answer_callback_query(call.id, f"✅ Language set to {lang_code}")

@bot.message_handler(commands=['help'])
def handle_help(message):
    update_user_activity(message.from_user.id)
    help_text = (
        "Commands supported:\n"
        "/start - Choose language\n"
        "/lang  - Change language\n"
        "/help  - This help message\n\n"
        "Send a voice/audio/video (≤ 20MB) and I will transcribe it.\n"
        "If larger than 20MB I'll give a secure upload link."
    )
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(commands=['admin'])
def admin_command(message):
    if message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "🚫 You are not authorized.")
        return
    # build admin keyboard
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📊 Total Users", callback_data="admin_total_users"),
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")
    )
    bot.send_message(message.chat.id, "⚙️ Admin Panel", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data in ["admin_total_users", "admin_broadcast"] and c.from_user.id == ADMIN_ID)
def admin_menu_callback(call):
    if call.data == "admin_total_users":
        total = users_collection.count_documents({})
        bot.send_message(call.message.chat.id, f"👥 Total registered users: {total}")
    elif call.data == "admin_broadcast":
        admin_state[call.from_user.id] = 'awaiting_broadcast_message'
        bot.send_message(call.message.chat.id, "📢 Send the broadcast message now (text/photo/video/document)")
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
        except Exception as e:
            logging.error(f"Failed broadcast to {uid}: {e}")
            fail += 1
        time.sleep(0.05)
    bot.send_message(message.chat.id, f"📊 Broadcast complete. ✅ Success: {success}, ❌ Failed: {fail}")

# Main handler for media messages (voice/audio/video/document)
@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    update_user_activity(message.from_user.id)
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
        mime = message.document.mime_type
        if mime and ('audio' in mime or 'video' in mime):
            file_id = message.document.file_id
            file_size = message.document.file_size
        else:
            bot.send_message(chat_id, "Sorry, I can only transcribe audio or video files.")
            return

    lang = get_user_lang(str(message.from_user.id))

    # large-file handling
    if file_size and file_size > TELEGRAM_MAX_BYTES:
        token = signed_upload_token(chat_id, lang)
        upload_link = f"{WEBHOOK_BASE}/upload/{token}"
        pretty_size_mb = round(file_size / (1024*1024), 2)
        text = (
            "📁 <b>File Too Large for Telegram</b>\n"
            f"Your file is {pretty_size_mb}MB, exceeds 20MB.\n\n"
            f"🔗 <a href=\"{upload_link}\">Upload Large File</a>\n\n"
            f"✅ Language preference ({lang}) saved.\nLink expires in 1 hour."
        )
        bot.send_message(chat_id, text, disable_web_page_preview=True, reply_to_message_id=message.message_id)
        return

    # processing animation
    processing_msg = bot.send_message(chat_id, "🔄 Processing...", reply_to_message_id=message.message_id)
    processing_msg_id = processing_msg.message_id
    stop_flag = {"stop": False}
    def stop_event(): return stop_flag["stop"]
    anim_thread = Thread(target=animate_processing_message, args=(chat_id, processing_msg_id, stop_event))
    anim_thread.start()

    try:
        tf, file_url = telegram_file_info_and_url(file_id)
        gen = telegram_file_stream(file_url)
        upload_url = assemblyai_upload_from_stream(gen)
        text = create_transcript_and_wait(upload_url, language_code=lang)
        if not text:
            bot.send_message(chat_id, "No transcription text was returned.", reply_to_message_id=message.message_id)
        else:
            # store transcription temporarily for translate/summarize actions
            uid = str(message.from_user.id)
            user_transcriptions.setdefault(uid, {})[message.message_id] = text
            # delete later thread
            def delete_later():
                time.sleep(600)
                try:
                    if uid in user_transcriptions and message.message_id in user_transcriptions[uid]:
                        del user_transcriptions[uid][message.message_id]
                except Exception:
                    pass
            Thread(target=delete_later, daemon=True).start()

            # Create inline keyboard for Translate/Summarize
            kb = InlineKeyboardMarkup(row_width=2)
            kb.add(
                InlineKeyboardButton("🌍 Translate", callback_data=f"translate|{message.message_id}"),
                InlineKeyboardButton("📝 Summarize", callback_data=f"summarize|{message.message_id}")
            )
            # If text too long for message, send as document with keyboard (telebot supports reply_markup on send_document in a limited way)
            if len(text) > 4000:
                f = io.BytesIO(text.encode("utf-8"))
                f.name = "transcript.txt"
                try:
                    bot.send_document(chat_id, f, caption="Your transcription is ready.", reply_markup=kb, reply_to_message_id=message.message_id)
                except Exception:
                    # fallback: send file then send buttons as a message
                    try:
                        bot.send_document(chat_id, f, caption="Your transcription is ready.", reply_to_message_id=message.message_id)
                    except Exception:
                        bot.send_message(chat_id, "✅ Transcription complete (could not send file).", reply_to_message_id=message.message_id)
                    bot.send_message(chat_id, "Choose an action:", reply_markup=kb, reply_to_message_id=message.message_id)
            else:
                bot.send_message(chat_id, text, reply_to_message_id=message.message_id, reply_markup=kb)
        increment_processing_count(str(message.from_user.id), "stt")
    except Exception as e:
        logging.exception(f"Error during transcription: {e}")
        bot.send_message(chat_id, f"Error during transcription: {e}", reply_to_message_id=message.message_id)
    finally:
        stop_flag["stop"] = True
        anim_thread.join(timeout=1.0)
        try:
            bot.delete_message(chat_id, processing_msg_id)
        except Exception:
            pass

# Callback: Translate / Summarize selection
@bot.callback_query_handler(func=lambda c: c.data and (c.data.startswith("translate|") or c.data.startswith("summarize|")))
def handle_translate_summarize_btn(call):
    parts = call.data.split("|")
    action = parts[0]
    orig_msg_id = int(parts[1])
    uid = str(call.from_user.id)
    if uid not in user_transcriptions or orig_msg_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ Transcription expired or not available.")
        return

    # present language keyboard (reuse LANG_OPTIONS but use native name if available)
    kb = InlineKeyboardMarkup(row_width=3)
    for label, code in LANG_OPTIONS:
        # label contains emoji + name; we want to pass short name, use code as identifier
        kb.add(InlineKeyboardButton(label, callback_data=f"{action}_to|{code}|{orig_msg_id}"))
    try:
        bot.send_message(call.message.chat.id, "🌍 Choose target language:", reply_markup=kb, reply_to_message_id=orig_msg_id)
    except Exception:
        bot.send_message(call.message.chat.id, "🌍 Choose target language:", reply_markup=kb)
    bot.answer_callback_query(call.id)

# Callback: translate_to|<code>|<orig_msg_id>
@bot.callback_query_handler(func=lambda c: c.data and (c.data.startswith("translate_to|") or c.data.startswith("summarize_in|") or c.data.startswith("summarize_to|") or c.data.startswith("translate_to|")))
def handle_translate_to(call):
    # Support both translate_to and summarize_to naming from different patterns
    data = call.data
    if data.startswith("translate_to|"):
        _prefix = "translate_to|"
    elif data.startswith("translate|"):
        _prefix = "translate|"
    elif data.startswith("summarize_in|") or data.startswith("summarize_to|"):
        _prefix = "summarize_to|"
    else:
        # expected pattern translate_to|code|orig
        _prefix = None

    parts = call.data.split("|")
    if len(parts) < 3:
        bot.answer_callback_query(call.id, "Invalid selection.")
        return
    # parts: [action_tag, lang_code, orig_msg_id]
    action_tag = parts[0]
    lang_code = parts[1]
    orig_msg_id = int(parts[2])
    uid = str(call.from_user.id)

    if uid not in user_transcriptions or orig_msg_id not in user_transcriptions[uid]:
        bot.answer_callback_query(call.id, "❌ Transcription expired or not available")
        return

    # delete language selection message to keep chat clean
    try:
        bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
    except Exception:
        pass

    # send progress message
    progress_msg = None
    try:
        if action_tag.startswith("translate"):
            progress_msg = bot.send_message(call.message.chat.id, f"🔄 Translating to {lang_code}...", reply_to_message_id=orig_msg_id)
        else:
            progress_msg = bot.send_message(call.message.chat.id, f"🔄 Summarizing in {lang_code}...", reply_to_message_id=orig_msg_id)
    except Exception:
        progress_msg = None

    def do_work(chat_id, orig_message_id, is_translate, target_lang_code, progress_message_id):
        try:
            original_text = user_transcriptions[uid][orig_message_id]
            # For user-friendly target language, we pass the code as name (Gemini instruction uses names; often code may be accepted)
            target_name = target_lang_code
            if is_translate:
                res = translate_large_text_with_gemini(original_text, target_name)
            else:
                res = summarize_large_text_with_gemini(original_text, target_name)
            if res.startswith("Error:"):
                bot.send_message(chat_id, f"❌ Error: {res}", reply_to_message_id=orig_message_id)
                return
            if len(res) > 4000:
                f = io.BytesIO(res.encode("utf-8"))
                f.name = ("translation.txt" if is_translate else "summary.txt")
                try:
                    bot.send_document(chat_id, f, caption=("🌍 Translation" if is_translate else "📝 Summary"), reply_to_message_id=orig_message_id)
                except Exception as e:
                    logging.error(f"Failed to send file: {e}")
                    bot.send_message(chat_id, "✅ Done (could not send file).", reply_to_message_id=orig_message_id)
            else:
                bot.send_message(chat_id, res, reply_to_message_id=orig_message_id)
        except Exception as e:
            logging.exception(f"Error in translate/summarize job: {e}")
            try:
                bot.send_message(chat_id, "❌ An error occurred during processing.", reply_to_message_id=orig_message_id)
            except Exception:
                pass
        finally:
            if progress_message_id:
                try:
                    bot.delete_message(chat_id, progress_message_id)
                except Exception:
                    pass

    is_translate = action_tag.startswith("translate")
    threading.Thread(target=lambda: do_work(call.message.chat.id, orig_msg_id, is_translate, lang_code, progress_msg.message_id if progress_msg else None), daemon=True).start()
    bot.answer_callback_query(call.id)

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

        if len(text) > 4000:
            with open("transcription_result.txt", "w", encoding="utf-8") as f:
                f.write(text)
            with open("transcription_result.txt", "rb") as f:
                bot.send_document(chat_id, f, caption="Your transcription is ready.")
            os.remove("transcription_result.txt")
        else:
            bot.send_message(chat_id, text or "No transcription text was returned.")
    except Exception as e:
        bot.send_message(chat_id, f"Error transcribing uploaded file: {e}")
        return f"Error during transcription: {e}", 500
    finally:
        try:
            bot.delete_message(chat_id, upload_msg_id)
        except Exception:
            pass

    return "<h3>Upload complete. Transcription will be sent to your Telegram chat.</h3>"

@app.route(WEBHOOK_ROUTE, methods=['POST'])
def telegram_webhook():
    update_json = request.get_json(force=True)
    try:
        update = telebot.types.Update.de_json(update_json)
        bot.process_new_updates([update])
    except Exception as e:
        logging.exception(f"Error processing update: {e}")
    return "OK"

@app.route("/healthz")
def healthz():
    return "OK"

# ----------------------------
# --- BOOT (set webhook) -----
# ----------------------------
def set_webhook_on_startup():
    webhook_url = WEBHOOK_BASE.rstrip("/") + WEBHOOK_ROUTE
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        bot.set_webhook(url=webhook_url)
        logging.info("Webhook set to: %s", webhook_url)
    except Exception as e:
        logging.error("Failed to set webhook: %s", e)

def set_bot_commands():
    commands = [
        BotCommand("start", "Get Started"),
        BotCommand("lang", "Change transcription language"),
        BotCommand("help", "How to use"),
        BotCommand("admin", "Admin panel (admin only)")
    ]
    try:
        bot.set_my_commands(commands)
    except Exception as e:
        logging.error("Failed to set bot commands: %s", e)

if __name__ == "__main__":
    try:
        # ensure DB connection
        mongo_client.admin.command('ping')
        logging.info("Connected to MongoDB successfully.")
    except Exception as e:
        logging.error(f"MongoDB connection issue: {e}")

    set_bot_commands()
    set_webhook_on_startup()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
