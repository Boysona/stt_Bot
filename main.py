# main.py
import os
import time
import json
import requests
import traceback
from flask import Flask, request, render_template_string
import telebot
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from threading import Thread
from datetime import datetime
from pymongo import MongoClient

# ----------------------------
# --- CONFIG (replace or keep) ---
# ----------------------------
ASSEMBLYAI_API_KEY = "b07239215b60433b8e225e7fd8ef6576"
WEBHOOK_BASE = "https://stt-bot-ckt1.onrender.com"   # your webhook base (render URL)
BOT_TOKEN = "7790991731:AAF4NHGm0BJCf08JTdBaUWKzwfs82_Y9Ecw"

# MongoDB (use provided URI and DB name)
MONGO_URI = "mongodb+srv://hoskasii:GHyCdwpI0PvNuLTg@cluster0.dy7oe7t.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME = "telegram_bot_db"

# ADMIN PASSWORD stored in code (change this to your own secret)
ADMIN_PASSWORD = "admin_secret_123"

# secret for signing upload links (change to a strong random string in production)
SECRET_KEY = "super-secret-please-change"

# Max telegram direct download size
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024  # 20MB

# Flask & TeleBot init
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML', threaded=False)

# serializer for generating signed upload links that expire
serializer = URLSafeTimedSerializer(SECRET_KEY)

# Connect to MongoDB
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client[DB_NAME]
    users_col = db.users
    admins_col = db.admins
    tokens_col = db.tokens  # extra collection if you want to store tokens (not strictly used now)
    # Try a server_info call to confirm connection (may raise)
    mongo_client.server_info()
    print("Connected to MongoDB.")
except Exception as e:
    mongo_client = None
    db = None
    users_col = admins_col = tokens_col = None
    print("Failed to connect to MongoDB:", e)

# Languages as requested in the image (partial list).
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

# Default language if none chosen
DEFAULT_LANG = "en"

# A simple in-memory store for user languages. In production use a DB. (we also persist to Mongo)
user_langs = {}

# States for admin flows (in-memory)
awaiting_admin_password = {}      # chat_id -> True when waiting for password
awaiting_broadcast_text = {}      # chat_id -> True when waiting for broadcast message

# A simple in-memory store for tracking animation state.
animation_states = {}

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

def make_admin_panel_keyboard():
    from telebot import types
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(text="Send Broadcast", callback_data="admin:send_broadcast"),
        types.InlineKeyboardButton(text="Total Users", callback_data="admin:total_users")
    )
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
    # requests will stream the provided generator as request body
    resp = requests.post(upload_url, headers=headers, data=stream_iterable, timeout=3600)
    resp.raise_for_status()
    return resp.json().get("upload_url")

def create_transcript_and_wait(audio_url: str, language_code: str = None, status_callback=None, poll_interval=2):
    """
    Create AssemblyAI transcript job and poll until completion.
    status_callback: optional function(status_dict) called after each poll (useful for updating UI)
    Returns transcript text on success, raises on failure.
    """
    create_url = "https://api.assemblyai.com/v2/transcript"
    headers = {"authorization": ASSEMBLYAI_API_KEY, "content-type": "application/json"}
    data = {"audio_url": audio_url}
    if language_code:
        # AssemblyAI accepts language_code (BCP-47-ish); adjust as needed.
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

def send_welcome_message(message):
    chat_id = message.chat.id
    first_name = message.from_user.first_name if message.from_user else "Friend"
    text = (
        f"👋 Salaam {first_name}!\n\n"
        "• Send me a voice message\n"
        "• audio file\n"
        "• video\n"
        "• to transcribe for free"
    )
    bot.send_message(chat_id, text)

def safe_upsert_user(message, lang=None, is_admin=False):
    """
    Save or update user in MongoDB. Called whenever user interacts.
    """
    try:
        if users_col is None:
            return
        user = message.from_user or message.chat
        chat_id = message.chat.id
        doc = {
            "chat_id": chat_id,
            "user_id": getattr(user, "id", None),
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
            "username": getattr(user, "username", None),
            "lang": lang or user_langs.get(str(chat_id), DEFAULT_LANG),
            "is_admin": bool(is_admin),
            "updated_at": datetime.utcnow()
        }
        users_col.update_one({"chat_id": chat_id}, {"$set": doc, "$setOnInsert": {"created_at": datetime.utcnow()}}, upsert=True)
    except Exception as e:
        print("Failed to upsert user:", e)

def is_user_admin(chat_id):
    try:
        if admins_col is None:
            return False
        return admins_col.find_one({"chat_id": chat_id}) is not None
    except Exception:
        return False

def add_admin(chat_id):
    try:
        if admins_col is None:
            return
        admins_col.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id, "granted_at": datetime.utcnow()}}, upsert=True)
        # also mark user record
        users_col.update_one({"chat_id": chat_id}, {"$set": {"is_admin": True}}, upsert=True)
    except Exception as e:
        print("Failed to add admin:", e)

def count_total_users():
    try:
        if users_col is None:
            return 0
        return users_col.count_documents({})
    except Exception as e:
        print("Failed to count users:", e)
        return 0

def get_all_user_chat_ids():
    try:
        if users_col is None:
            return []
        docs = users_col.find({}, {"chat_id": 1})
        return [d["chat_id"] for d in docs if "chat_id" in d]
    except Exception as e:
        print("Failed to fetch user chat ids:", e)
        return []

# ----------------------------
# --- TELEGRAM HANDLERS -----
# ----------------------------

@bot.message_handler(commands=['start'])
def handle_start(message):
    # Save user to DB
    safe_upsert_user(message)
    kb = make_lang_keyboard()
    bot.send_message(message.chat.id, "Please choose your Transcription language", reply_markup=kb)

@bot.message_handler(commands=['help'])
def handle_help(message):
    safe_upsert_user(message)
    text = (
        "Commands supported:\n"
        "/start - Show welcome message\n"
        "/lang  - Change language\n"
        "/help  - This help message\n"
        "/admin - Admin login (requires secret password)\n\n"
        "Send a voice/audio/video (≤ 20MB) and I will transcribe it.\n"
        "If it's larger than 20MB, I'll give you a secure upload link."
    )
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=['lang'])
def handle_lang(message):
    safe_upsert_user(message)
    kb = make_lang_keyboard()
    bot.send_message(message.chat.id, "Choose a language:", reply_markup=kb)

@bot.message_handler(commands=['admin'])
def handle_admin(message):
    chat_id = message.chat.id
    safe_upsert_user(message)
    # If already admin, show panel directly
    if is_user_admin(chat_id):
        kb = make_admin_panel_keyboard()
        bot.send_message(chat_id, "You are already an admin. Admin panel:", reply_markup=kb)
        return

    awaiting_admin_password[chat_id] = True
    bot.send_message(chat_id, "🔐 Please enter the admin password to access the admin panel:")

@bot.message_handler(func=lambda m: awaiting_admin_password.get(m.chat.id, False))
def handle_admin_password_entry(message):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    # stop waiting (one-time)
    awaiting_admin_password.pop(chat_id, None)

    if text == ADMIN_PASSWORD:
        add_admin(chat_id)
        bot.send_message(chat_id, "✅ Password accepted. You are now an admin.")
        kb = make_admin_panel_keyboard()
        bot.send_message(chat_id, "Admin panel:", reply_markup=kb)
    else:
        bot.send_message(chat_id, "❌ Incorrect password. Access denied.")

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("admin:"))
def handle_admin_callbacks(call):
    chat_id = call.message.chat.id
    data = call.data.split(":", 1)[1]

    # Verify caller is admin
    if not is_user_admin(chat_id):
        bot.answer_callback_query(call.id, "You are not authorized.", show_alert=True)
        return

    if data == "send_broadcast":
        # Ask admin to send broadcast message
        awaiting_broadcast_text[chat_id] = True
        bot.send_message(chat_id, "✉️ Please send the message text you want to broadcast to all users.")
        bot.answer_callback_query(call.id)
    elif data == "total_users":
        total = count_total_users()
        bot.send_message(chat_id, f"👥 Total users in database: {total}")
        bot.answer_callback_query(call.id)
    else:
        bot.answer_callback_query(call.id, "Unknown admin action.")

@bot.message_handler(func=lambda m: awaiting_broadcast_text.get(m.chat.id, False))
def handle_broadcast_message_entry(message):
    chat_id = message.chat.id
    text = message.text or ""
    awaiting_broadcast_text.pop(chat_id, None)

    if not text.strip():
        bot.send_message(chat_id, "Broadcast cancelled — empty message.")
        return

    bot.send_message(chat_id, "🚀 Starting broadcast. I'll attempt to send the message to all users.")
    # Fetch all users from DB
    targets = get_all_user_chat_ids()
    success = 0
    failed = 0
    for tid in targets:
        try:
            bot.send_message(tid, f"📢 Broadcast from admin:\n\n{text}")
            success += 1
            time.sleep(0.05)  # small delay to avoid flood limits
        except Exception as e:
            failed += 1
            # optionally log the error
            print(f"Failed to send to {tid}: {e}")

    bot.send_message(chat_id, f"Broadcast complete. Sent: {success} • Failed: {failed}")

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("lang:"))
def handle_lang_callback(call):
    lang_code = call.data.split(":", 1)[1]
    chat_id = call.message.chat.id
    user_langs[str(chat_id)] = lang_code

    # Persist to DB
    try:
        if users_col:
            users_col.update_one({"chat_id": chat_id}, {"$set": {"lang": lang_code}}, upsert=True)
    except Exception as e:
        print("Failed to update language in DB:", e)

    # Tirtir fariintii hore ee xulashada luqada
    try:
        bot.delete_message(chat_id, call.message.message_id)
    except Exception as e:
        print(f"Failed to delete message: {e}")

    # Dir fariinta soo dhoweynta ee cusub
    send_welcome_message(call.message)

    # Answer the callback query to remove the "loading" state on the button
    try:
        bot.answer_callback_query(call.id, f"✅ you set to {lang_code}")
    except Exception:
        pass

# Main handler for media messages
@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    chat_id = message.chat.id
    safe_upsert_user(message)  # ensure user is stored
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

    lang = user_langs.get(str(chat_id), DEFAULT_LANG)

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

    # Send processing message
    processing_msg = bot.send_message(chat_id, "🔄 Processing...", reply_to_message_id=message.message_id)
    processing_msg_id = processing_msg.message_id

    # Start animation thread
    stop_animation = False
    def stop_event():
        return stop_animation
    animation_thread = Thread(target=animate_processing_message, args=(chat_id, processing_msg_id, stop_event))
    animation_thread.start()

    try:
        tf, file_url = telegram_file_info_and_url(file_id)
        gen = telegram_file_stream(file_url)
        upload_url = assemblyai_upload_from_stream(gen)

        # We don't need a status_callback here since we have our own animation thread
        text = create_transcript_and_wait(upload_url, language_code=lang)

        if len(text) > 4000:
            with open("transcription_result.txt", "w", encoding="utf-8") as f:
                f.write(text)
            with open("transcription_result.txt", "rb") as f:
                bot.send_document(chat_id, f, caption="Your transcription is ready.", reply_to_message_id=message.message_id)
            os.remove("transcription_result.txt")
        else:
            bot.send_message(chat_id, text or "No transcription text was returned.", reply_to_message_id=message.message_id)
    except Exception as e:
        tb = traceback.format_exc()
        print("Transcription error:", tb)
        try:
            bot.send_message(chat_id, f"Error during transcription: {e}", reply_to_message_id=message.message_id)
        except Exception:
            pass
    finally:
        # Stop animation thread and delete the processing message
        stop_animation = True
        animation_thread.join()
        try:
            bot.delete_message(chat_id, processing_msg_id)
        except Exception:
            pass

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
    except Exception as e:
        print("Error processing update:", e)
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
            with open("transcription_result.txt", "w", encoding="utf-8") as f:
                f.write(text)
            with open("transcription_result.txt", "rb") as f:
                bot.send_document(chat_id, f, caption="Your transcription is ready.")
            os.remove("transcription_result.txt")
        else:
            bot.send_message(chat_id, text or "No transcription text was returned.")

    except Exception as e:
        tb = traceback.format_exc()
        print("Error transcribing uploaded file:", tb)
        try:
            bot.send_message(chat_id, f"Error transcribing uploaded file: {e}")
        except Exception:
            pass
        return f"Error during transcription: {e}", 500
    finally:
        # Delete the initial message about receiving the upload
        try:
            bot.delete_message(chat_id, upload_msg_id)
        except Exception:
            pass

    return "<h3>Upload complete. Transcription will be sent to your Telegram chat.</h3>"

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
    except Exception as e:
        print("Failed to set webhook:", e)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
