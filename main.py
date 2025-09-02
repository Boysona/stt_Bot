import os
import time
import requests
import telebot
from telebot import types
from flask import Flask, request, redirect, render_template_string, url_for
import threading
import uuid

# ---------------- CONFIG ----------------
BOT_TOKEN = "7790991731:AAF4NHGm0BJCf08JTdBaUWKzwfs82_Y9Ecw"
ASSEMBLYAI_API_KEY = "b07239215b60433b8e225e7fd8ef6576"
WEBHOOK_URL = "https://stt-bot-ckt1.onrender.com"
MAX_TG_FILE_SIZE = 20 * 1024 * 1024  # 20MB

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
server = Flask(__name__)

# In-memory storage for language preferences
user_languages = {}
# Temporary store for large file uploads
pending_uploads = {}

# ---------------- HELPERS ----------------

LANGUAGES = {
    "🇺🇸 English": "en",
    "🇸🇦 Arabic": "ar",
    "🇫🇷 French": "fr",
    "🇪🇸 Spanish": "es",
    "🇹🇷 Turkish": "tr",
    "🇸🇴 Somali": "so"
}


def generate_language_keyboard():
    kb = types.InlineKeyboardMarkup()
    row = []
    for idx, (flag, code) in enumerate(LANGUAGES.items(), start=1):
        row.append(types.InlineKeyboardButton(flag, callback_data=f"lang_{code}"))
        if idx % 3 == 0:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    return kb


def transcribe_with_assemblyai(file_url, language_code):
    headers = {"authorization": ASSEMBLYAI_API_KEY}
    endpoint = "https://api.assemblyai.com/v2/transcribe"

    json_data = {
        "audio_url": file_url,
        "language_code": language_code
    }

    resp = requests.post(endpoint, headers=headers, json=json_data)
    resp.raise_for_status()
    transcription_id = resp.json()["id"]

    # Poll until completed
    while True:
        status = requests.get(f"https://api.assemblyai.com/v2/transcribe/{transcription_id}",
                              headers=headers).json()
        if status["status"] == "completed":
            return status["text"]
        elif status["status"] == "error":
            return f"❌ Error: {status['error']}"
        time.sleep(3)


def send_processing_message(chat_id, message_id):
    """Send animated Processing... message"""
    dots = [".", "..", "..."]
    msg = bot.reply_to(bot.get_message(chat_id, message_id), "🔄 Processing.")
    for i in range(6):
        time.sleep(1)
        bot.edit_message_text(f"🔄 Processing{dots[i % 3]}",
                              chat_id=chat_id,
                              message_id=msg.message_id)
    return msg


# ---------------- TELEGRAM HANDLERS ----------------

@bot.message_handler(commands=['start'])
def start_cmd(message):
    kb = generate_language_keyboard()
    bot.send_message(message.chat.id,
                     "🌍 Please select your language:",
                     reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data.startswith("lang_"))
def lang_callback(call):
    lang_code = call.data.split("_")[1]
    user_languages[call.message.chat.id] = lang_code
    lang_name = [k for k, v in LANGUAGES.items() if v == lang_code][0]

    bot.edit_message_text(chat_id=call.message.chat.id,
                          message_id=call.message.message_id,
                          text=f"✅ Language set to {lang_name}")
    bot.send_message(call.message.chat.id, f"👋 Welcome! Your language is {lang_name}")


@bot.message_handler(commands=['help'])
def help_cmd(message):
    bot.send_message(message.chat.id,
                     "🤖 I can transcribe your voice/video to text.\n"
                     "Commands:\n"
                     "/start - Choose language\n"
                     "/lang - Change language\n"
                     "/help - Show this help")


@bot.message_handler(commands=['lang'])
def lang_cmd(message):
    kb = generate_language_keyboard()
    bot.send_message(message.chat.id,
                     "🌍 Please select your language:",
                     reply_markup=kb)


@bot.message_handler(content_types=['voice', 'audio', 'video'])
def handle_media(message):
    chat_id = message.chat.id
    lang = user_languages.get(chat_id, "en")

    file_id = (message.voice or message.audio or message.video).file_id
    file_info = bot.get_file(file_id)
    file_size = file_info.file_size

    if file_size > MAX_TG_FILE_SIZE:
        # Generate upload link
        upload_id = str(uuid.uuid4())
        pending_uploads[upload_id] = {
            "chat_id": chat_id,
            "lang": lang,
            "expires": time.time() + 3600
        }
        upload_url = f"{WEBHOOK_URL}/upload/{upload_id}"

        bot.send_message(chat_id,
                         f"📁 File Too Large for Telegram\n"
                         f"Your file is {round(file_size/1024/1024,1)}MB, which exceeds 20MB.\n\n"
                         f"🌐 Upload via Web Interface:\n"
                         f"🔗 {upload_url}\n"
                         f"✅ Your language preference is set!")
        return

    # Download file as stream URL
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

    # Send processing message
    processing_msg = bot.reply_to(message, "🔄 Processing...")

    def worker():
        try:
            text = transcribe_with_assemblyai(file_url, lang)
            bot.send_message(chat_id, f"📝 Transcription:\n{text}", reply_to_message_id=message.message_id)
        finally:
            bot.delete_message(chat_id, processing_msg.message_id)

    threading.Thread(target=worker).start()


# ---------------- FLASK WEB UPLOAD ----------------

UPLOAD_FORM = """
<!DOCTYPE html>
<html>
<head><title>Upload Large File</title></head>
<body>
  <h2>Upload your large file</h2>
  <form action="{{ url_for('do_upload', upload_id=upload_id) }}" method="post" enctype="multipart/form-data">
    <input type="file" name="file" required />
    <button type="submit">Upload</button>
  </form>
</body>
</html>
"""

@server.route("/upload/<upload_id>", methods=["GET", "POST"])
def do_upload(upload_id):
    if upload_id not in pending_uploads or pending_uploads[upload_id]["expires"] < time.time():
        return "❌ Link expired", 400

    if request.method == "GET":
        return render_template_string(UPLOAD_FORM, upload_id=upload_id)

    # Handle file upload
    f = request.files["file"]
    filename = f"{uuid.uuid4()}_{f.filename}"
    filepath = os.path.join("/tmp", filename)
    f.save(filepath)

    # Upload to AssemblyAI
    headers = {"authorization": ASSEMBLYAI_API_KEY}
    with open(filepath, "rb") as upfile:
        upload_resp = requests.post("https://api.assemblyai.com/v2/upload",
                                    headers=headers,
                                    data=upfile)
    os.remove(filepath)

    file_url = upload_resp.json()["upload_url"]
    chat_id = pending_uploads[upload_id]["chat_id"]
    lang = pending_uploads[upload_id]["lang"]

    text = transcribe_with_assemblyai(file_url, lang)
    bot.send_message(chat_id, f"📝 Transcription:\n{text}")

    del pending_uploads[upload_id]
    return "✅ Upload successful, check your Telegram!"


# ---------------- WEBHOOK ----------------

@server.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    json_update = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_update)
    bot.process_new_updates([update])
    return "OK", 200


@server.route("/", methods=["GET"])
def index():
    return "🤖 Bot is running"


if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    server.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
