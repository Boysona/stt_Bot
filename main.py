

import os
import time
import json
import requests
from flask import Flask, request, render_template_string, redirect, abort
import telebot
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# ----------------------------
# --- CONFIG (replace or keep) ---
# ----------------------------
ASSEMBLYAI_API_KEY = "b07239215b60433b8e225e7fd8ef6576"
WEBHOOK_BASE = "https://stt-bot-ckt1.onrender.com"   # your webhook base (render URL)
BOT_TOKEN = "7790991731:AAF4NHGm0BJCf08JTdBaUWKzwfs82_Y9Ecw"

# secret for signing upload links (change to a strong random string in production)
SECRET_KEY = "super-secret-please-change"  

# Max telegram direct download size
TELEGRAM_MAX_BYTES = 20 * 1024 * 1024  # 20MB

# Flask & TeleBot init
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML', threaded=False)

# serializer for generating signed upload links that expire
serializer = URLSafeTimedSerializer(SECRET_KEY)

# Some languages to show (partial list as requested). Map button label -> AssemblyAI language code hint.
LANG_OPTIONS = [
    ("🇺🇸 English", "en"),
    ("🇪🇸 Español", "es"),
    ("🇫🇷 Français", "fr"),
    ("🇸🇴 Soomaali", "so"),
    ("🇦🇪 العربية", "ar"),
    ("🇩🇪 Deutsch", "de"),
]

# Default language if none chosen
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
    # arrange in two per row
    row = []
    for i, btn in enumerate(buttons, 1):
        row.append(btn)
        if i % 2 == 0:
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

def send_processing_reply_and_get_message(chat_id, reply_to_message_id):
    """
    Send initial "Processing..." reply and return the sent message object
    """
    msg = bot.send_message(chat_id, "🔄 Processing.", reply_to_message_id=reply_to_message_id)
    return msg

def animate_processing_message(chat_id, message_id, stop_event, interval=0.6):
    """
    Edit message to animate dots until stop_event() returns True.
    stop_event: callable that returns True when animation should stop.
    NOTE: This function is used synchronously in the main processing loop via periodic calls.
    Kept as helper for readability.
    """
    dots = [" .", " ..", " ...", " …"]
    idx = 0
    while not stop_event():
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="🔄 Processing" + dots[idx % len(dots)])
        except Exception:
            pass
        idx += 1
        time.sleep(interval)

# ----------------------------
# --- TELEGRAM HANDLERS -----
# ----------------------------

@bot.message_handler(commands=['start'])
def handle_start(message):
    # show only some languages (as per requirement)
    kb = make_lang_keyboard()
    bot.send_message(message.chat.id,
                     "Welcome! Please choose your language / Fadlan dooro luqaddaada:",
                     reply_markup=kb)

@bot.message_handler(commands=['help'])
def handle_help(message):
    text = (
        "Commands supported:\n"
        "/start - Show language selection\n"
        "/lang  - Change language\n"
        "/help  - This help message\n\n"
        "Send a voice/audio/video/document (≤ 20MB) and I will transcribe it.\n"
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
    # edit previous message to show selection
    try:
        new_text = f"✅ Language selected: {lang_code}"
        bot.edit_message_text(new_text, chat_id=call.message.chat.id, message_id=call.message.message_id)
    except Exception:
        pass
    # also send welcome message
    bot.send_message(call.message.chat.id, f"Welcome! Your language is set to {lang_code}. Send audio/video/document to transcribe.")

# Main handler for media messages
@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    chat_id = message.chat.id
    # find file id and size if available
    file_id = None
    file_size = None
    mime_type = None

    if message.voice:
        file_id = message.voice.file_id
        file_size = message.voice.file_size
        mime_type = "audio/ogg"
    elif message.audio:
        file_id = message.audio.file_id
        file_size = message.audio.file_size
        mime_type = message.audio.mime_type
    elif message.video:
        file_id = message.video.file_id
        file_size = message.video.file_size
        mime_type = message.video.mime_type
    elif message.document:
        # accept doc if mime is audio/video or unknown (user may send .mp3 as document)
        file_id = message.document.file_id
        file_size = message.document.file_size
        mime_type = message.document.mime_type

    # determine chosen language (we'll just look for recent language selection by chat - simple approach:
    # for simplicity, store last selected language in-memory per chat in a dict; in production use persistent store)
    # We'll create a simple attribute on bot (dictionary)
    lang = getattr(bot, "user_langs", {}).get(str(chat_id), DEFAULT_LANG)

    # If document > 20MB => send upload link
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
        bot.send_message(chat_id, text, disable_web_page_preview=True)
        return

    # For <=20MB, proceed to stream from Telegram to AssemblyAI
    # send processing reply as a reply to the original message
    processing_msg = send_processing_reply_and_get_message(chat_id, message.message_id)
    processing_msg_id = processing_msg.message_id

    stop_animation = {"stop": False}
    def stop_event():
        return stop_animation["stop"]

    # Prepare generator for streaming data from Telegram file_url -> chunks
    try:
        tf, file_url = telegram_file_info_and_url(file_id)
    except Exception as e:
        bot.send_message(chat_id, "Failed to retrieve file from Telegram: " + str(e), reply_to_message_id=message.message_id)
        # clean up processing msg
        try:
            bot.delete_message(chat_id, processing_msg_id)
        except Exception:
            pass
        return

    def status_callback(status):
        # Called each poll from create_transcript_and_wait: animate the processing text
        try:
            # rotate animation a bit when called
            bot.edit_message_text("🔄 Processing" + " ...", chat_id=chat_id, message_id=processing_msg_id)
        except Exception:
            pass

    # Stream Telegram -> AssemblyAI upload
    try:
        gen = telegram_file_stream(file_url)
        upload_url = assemblyai_upload_from_stream(gen)
        # create transcript and wait for completion; animate by calling status_callback on each poll
        text = create_transcript_and_wait(upload_url, language_code=lang, status_callback=status_callback)
        # send result as reply to original message
        bot.send_message(chat_id, text or "(No text returned)", reply_to_message_id=message.message_id)
    except Exception as e:
        bot.send_message(chat_id, "Error during transcription: " + str(e), reply_to_message_id=message.message_id)
    finally:
        # stop animation and delete processing message (if possible)
        stop_animation["stop"] = True
        try:
            bot.delete_message(chat_id, processing_msg_id)
        except Exception:
            # fallback: try to edit to completed
            try:
                bot.edit_message_text("Done.", chat_id=chat_id, message_id=processing_msg_id)
            except Exception:
                pass

# keep a tiny in-memory store for user languages (simple). In production use DB.
bot.user_langs = {}

# Track language selection via CallbackQuery and persist
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("lang:"))
def callback_lang_store(call):
    lang_code = call.data.split(":", 1)[1]
    bot.user_langs[str(call.message.chat.id)] = lang_code
    try:
        bot.answer_callback_query(call.id, f"Language set to {lang_code}")
    except Exception:
        pass

# ----------------------------
# --- FLASK ROUTES ----------
# ----------------------------

# route for Telegram webhook
@app.route("/telegram_webhook", methods=['POST'])
def telegram_webhook():
    # Telegram will POST update JSON here
    update_json = request.get_json(force=True)
    try:
        update = telebot.types.Update.de_json(update_json)
        bot.process_new_updates([update])
    except Exception as e:
        # log and ignore
        print("Error processing update:", e)
    return "OK"

# upload form for large files
UPLOAD_PAGE = """
<!doctype html>
<title>Upload large file</title>
<h3>Upload file for transcription</h3>
<p>Chat ID: {{ chat_id }} • Language: {{ lang }}</p>
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

    # Stream file.stream (werkzeug FileStorage) to AssemblyAI without loading into memory
    # We'll stream the file in chunks
    def file_gen():
        chunk_size = 256*1024
        while True:
            chunk = file.stream.read(chunk_size)
            if not chunk:
                break
            yield chunk

    try:
        # notify user on telegram that upload started
        bot.send_message(chat_id, f"📥 Received large upload from web interface. Starting transcription (language: {lang}).")
        upload_url = assemblyai_upload_from_stream(file_gen())
        # Poll for transcript (with a simple status callback that sends periodic edits is omitted here)
        def simple_status_cb(status):
            # we could implement progress notify; keep quiet to avoid flooding
            pass
        text = create_transcript_and_wait(upload_url, language_code=lang, status_callback=simple_status_cb)
        bot.send_message(chat_id, "✅ Your transcription is ready:", reply_to_message_id=None)
        bot.send_message(chat_id, text)
    except Exception as e:
        bot.send_message(chat_id, "Error transcribing uploaded file: " + str(e))
        return f"Error during transcription: {e}", 500

    # Optionally redirect to a success page
    return "<h3>Upload complete. Transcription will be sent to your Telegram chat.</h3>"

# health check
@app.route("/healthz")
def healthz():
    return "OK"

# ----------------------------
# --- BOOT (set webhook) ---
# ----------------------------
if __name__ == "__main__":
    # Set webhook (remove previous)
    webhook_url = WEBHOOK_BASE.rstrip("/") + "/telegram_webhook"
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        bot.set_webhook(url=webhook_url)
        print("Webhook set to:", webhook_url)
    except Exception as e:
        print("Failed to set webhook:", e)
    # Run flask app
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
