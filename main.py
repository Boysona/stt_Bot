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

# Expanded list of languages. Map button label -> AssemblyAI language code hint.
LANG_OPTIONS = [
    ("🇺🇸 English", "en"),
    ("🇪🇸 Español", "es"),
    ("🇫🇷 Français", "fr"),
    ("🇩🇪 Deutsch", "de"),
    ("🇮🇩 Indonesia", "id"),
    ("🇮🇹 Italiano", "it"),
    ("🇵🇱 Polski", "pl"),
    ("🇺🇦 Українська", "uk"),
    ("🇨🇳 中文", "zh"),
    ("🇹🇷 Türkçe", "tr"),
    ("🇯🇵 日本語", "ja"),
    ("🇷🇺 Русский", "ru"),
    ("🇸🇴 Soomaali", "so"),
    ("🇦🇪 العربية", "ar"),
    ("🇮🇳 हिन्दी", "hi"),
    ("🇵🇹 Português", "pt"),
    ("🇰🇷 한국어", "ko"),
    ("🇬🇷 Ελληνικά", "el"),
    ("🇳🇱 Nederlands", "nl"),
    ("🇹🇭 ไทย", "th"),
    ("🇻🇳 Tiếng Việt", "vi"),
    ("🇷🇴 Română", "ro"),
    ("🇸🇪 Svenska", "sv"),
    ("🇲🇾 Melayu", "ms"),
    ("🇮🇱 עברית", "he"),
    ("🇭🇺 Magyar", "hu"),
    ("🇹🇼 繁體中文", "zh-TW"),
    ("🇵🇭 Tagalog", "tl"),
]

# Default language if none chosen
DEFAULT_LANG = "en"

# Maximum characters for a direct message before sending as a file
MAX_MESSAGE_LENGTH = 4000

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

# ----------------------------
# --- TELEGRAM HANDLERS -----
# ----------------------------

@bot.message_handler(commands=['start'])
def handle_start(message):
    kb = make_lang_keyboard()
    bot.send_message(message.chat.id,
                     "Welcome! Please choose your language:",
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

# Main handler for media messages
@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    chat_id = message.chat.id
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
        file_size = message.video.video_size
        mime_type = message.video.mime_type
    elif message.document:
        file_id = message.document.file_id
        file_size = message.document.file_size
        mime_type = message.document.mime_type

    lang = getattr(bot, "user_langs", {}).get(str(chat_id), DEFAULT_LANG)

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

    processing_msg = send_processing_reply_and_get_message(chat_id, message.message_id)
    processing_msg_id = processing_msg.message_id

    try:
        tf, file_url = telegram_file_info_and_url(file_id)
    except Exception as e:
        bot.send_message(chat_id, "Failed to retrieve file from Telegram: " + str(e), reply_to_message_id=message.message_id)
        try:
            bot.delete_message(chat_id, processing_msg_id)
        except Exception:
            pass
        return

    def status_callback(status):
        try:
            bot.edit_message_text("🔄 Processing" + "...", chat_id=chat_id, message_id=processing_msg_id)
        except Exception:
            pass

    try:
        gen = telegram_file_stream(file_url)
        upload_url = assemblyai_upload_from_stream(gen)
        text = create_transcript_and_wait(upload_url, language_code=lang, status_callback=status_callback)

        if len(text) > MAX_MESSAGE_LENGTH:
            # Send as a .txt file
            with open("transcription.txt", "w", encoding="utf-8") as f:
                f.write(text)
            with open("transcription.txt", "rb") as f:
                bot.send_document(chat_id, f, caption="✅ Your transcription is ready (as a file due to length).", reply_to_message_id=message.message_id)
            os.remove("transcription.txt") # Clean up the file
        else:
            # Send as a direct message
            bot.send_message(chat_id, "✅ Your transcription is ready:", reply_to_message_id=message.message_id)
            bot.send_message(chat_id, text or "(No text returned)")
            
    except Exception as e:
        bot.send_message(chat_id, "Error during transcription: " + str(e), reply_to_message_id=message.message_id)
    finally:
        try:
            bot.delete_message(chat_id, processing_msg_id)
        except Exception:
            try:
                bot.edit_message_text("Done.", chat_id=chat_id, message_id=processing_msg_id)
            except Exception:
                pass

# keep a tiny in-memory store for user languages (simple). In production use DB.
bot.user_langs = {}

# Track language selection via CallbackQuery and persist, and edit the message
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("lang:"))
def callback_lang_store(call):
    lang_code = call.data.split(":", 1)[1]
    bot.user_langs[str(call.message.chat.id)] = lang_code
    
    # Get the language label to show in the message
    lang_label = next((label for label, code in LANG_OPTIONS if code == lang_code), lang_code)
    
    # Edit the message with the selected language text
    try:
        new_text = f"✅ Language selected: {lang_label}"
        bot.edit_message_text(new_text, chat_id=call.message.chat.id, message_id=call.message.message_id)
    except Exception:
        pass
    
    # Answer the callback query to remove the loading animation
    try:
        bot.answer_callback_query(call.id, text=f"Language set to {lang_label}")
    except Exception:
        pass

# ----------------------------
# --- FLASK ROUTES ----------
# ----------------------------

@app.route("/telegram_webhook", methods=['POST'])
def telegram_webhook():
    update_json = request.get_json(force=True)
    try:
        update = telebot.types.Update.de_json(update_json)
        bot.process_new_updates([update])
    except Exception as e:
        print("Error processing update:", e)
    return "OK"

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

    file = request.files.get('file')
    if not file:
        return "No file uploaded", 400

    def file_gen():
        chunk_size = 256*1024
        while True:
            chunk = file.stream.read(chunk_size)
            if not chunk:
                break
            yield chunk

    try:
        bot.send_message(chat_id, f"📥 Received large upload from web interface. Starting transcription (language: {lang}).")
        upload_url = assemblyai_upload_from_stream(file_gen())
        def simple_status_cb(status):
            pass
        text = create_transcript_and_wait(upload_url, language_code=lang, status_callback=simple_status_cb)
        
        if len(text) > MAX_MESSAGE_LENGTH:
            with open("transcription.txt", "w", encoding="utf-8") as f:
                f.write(text)
            with open("transcription.txt", "rb") as f:
                bot.send_document(chat_id, f, caption="✅ Your transcription is ready (as a file due to length).", reply_to_message_id=None)
            os.remove("transcription.txt")
        else:
            bot.send_message(chat_id, "✅ Your transcription is ready:", reply_to_message_id=None)
            bot.send_message(chat_id, text)
            
    except Exception as e:
        bot.send_message(chat_id, "Error transcribing uploaded file: " + str(e))
        return f"Error during transcription: {e}", 500

    return "<h3>Upload complete. Transcription will be sent to your Telegram chat.</h3>"

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

