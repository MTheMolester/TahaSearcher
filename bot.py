import os
import logging
import requests
from flask import Flask, request, jsonify

# Safely import the new library name
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Environment Variables ──────────────────────────────────────────────────
BALE_TOKEN = os.environ["BALE_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "tahasearcher-secret")
BALE_API = f"https://tapi.bale.ai/bot{BALE_TOKEN}"
app = Flask(__name__)

SESSIONS = {}

# ── Bale API Helpers ────────────────────────────────────────────────────────
def api_call(method, payload):
    try:
        r = requests.post(f"{BALE_API}/{method}", json=payload, timeout=20)
        return r.json() if r.content else {}
    except Exception: return {}

def send_message(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard: payload["reply_markup"] = {"inline_keyboard": keyboard}
    return api_call("sendMessage", payload)

def edit_message(chat_id, message_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if keyboard is not None: payload["reply_markup"] = {"inline_keyboard": keyboard}
    return api_call("editMessageText", payload)

def answer_callback(callback_query_id):
    return api_call("answerCallbackQuery", {"callback_query_id": callback_query_id})

def btn(text, data): return {"text": text, "callback_data": data}
def url_btn(text, url): return {"text": text, "url": url}

# ── 🌐 The Unbreakable Multi-Engine Search ─────────────────────────────────
def fetch_search_results(query):
    results = []
    
    # Engine 1: DuckDuckGo
    try:
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, max_results=30))
            if raw_results:
                return raw_results
    except Exception as e:
        logging.warning(f"DuckDuckGo failed/blocked: {e}")

    # Engine 2: The Rotating SearxNG Fallback
    # If one server blocks us, we instantly pivot to the next one
    instances = [
        "https://searx.tiekoetter.com/search",
        "https://searx.work/search",
        "https://searx.ro/search",
        "https://paulgo.io/search",
        "https://search.mdosch.de/search"
    ]
    
    # Fake a real Google Chrome browser so we don't get blocked
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    for url in instances:
        try:
            logging.info(f"Attempting fallback to {url}...")
            params = {"q": query, "format": "json"}
            r = requests.get(url, params=params, headers=headers, timeout=6)
            
            if r.ok:
                # Safely try to parse JSON. If it is HTML (Cloudflare block), it skips gracefully.
                try:
                    data = r.json()
                    for item in data.get("results", [])[:30]:
                        results.append({
                            "title": item.get("title", ""),
                            "body": item.get("content", "")[:120],
                            "href": item.get("url", "")
                        })
                    if results:
                        logging.info(f"✅ Successfully extracted data from {url}")
                        return results
                except Exception:
                    logging.warning(f"⚠️ {url} sent HTML instead of JSON. Skipping to next server...")
        except Exception as e:
            logging.warning(f"❌ Failed to connect to {url}: {e}")
            
    return results

def render_web_search(chat_id, message_id=None, page_num=1):
    results = SESSIONS.get(chat_id, {}).get("web_results", [])
    query = SESSIONS.get(chat_id, {}).get("search_query", "")
    
    if not results:
        text = "❌ متاسفانه هیچ نتیجه‌ای پیدا نشد. لطفاً چند دقیقه دیگر دوباره تلاش کنید."
        if message_id: return edit_message(chat_id, message_id, text)
        else: return send_message(chat_id, text)

    start_idx = (page_num - 1) * 5
    end_idx = start_idx + 5
    page_items = results[start_idx:end_idx]
    
    lines = [f"🌐 **نتایج جستجو برای:** `{query}`", f"📄 **صفحه:** {page_num}\n"]
    numbers = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    
    kb = []
    row_links = []
    
    for i, item in enumerate(page_items):
        title = item.get("title", "بدون عنوان")[:50]
        snippet = item.get("body", "")[:120] + "..."
        link = item.get("href", "")
        
        lines.append(f"{numbers[i]} **{title}**")
        lines.append(f"📝 {snippet}\n")
        
        row_links.append(url_btn(str(i+1), link))
        
    kb.append(row_links)
    nav_row = []
    
    if end_idx < len(results): nav_row.append(btn("⬅️ بعدی", f"wpage:next:{page_num+1}"))
    if page_num > 1: nav_row.append(btn("➡️ قبلی", f"wpage:prev:{page_num-1}"))
        
    if nav_row: kb.append(nav_row)
    
    text = "\n".join(lines)
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

# ── Core Handlers ───────────────────────────────────────────────────────────
def handle_message(msg):
    if msg.get("chat", {}).get("type", "") != "private": return 
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()

    if text in ("/start", "/help"):
        SESSIONS[chat_id] = {}
        welcome_text = (
            "👋 **به ربات TahaSearcher خوش آمدید!**\n\n"
            "من یک موتور جستجوی هوشمند و بدون محدودیت هستم. 🌐\n\n"
            "لطفاً هر کلمه، جمله یا سوالی که دارید را مستقیماً برای من بفرستید تا کل اینترنت را برای شما جستجو کنم:"
        )
        send_message(chat_id, welcome_text)
        return

    send_message(chat_id, "⏳ در حال جستجو در وب...")
    SESSIONS.setdefault(chat_id, {})["search_query"] = text
    
    try:
        results = fetch_search_results(text)
        SESSIONS[chat_id]["web_results"] = results
        render_web_search(chat_id, None, page_num=1)
    except Exception as e:
        logging.error(f"Critical Search Error: {e}")
        send_message(chat_id, "❌ خطایی در ارتباط با سرور جستجو رخ داد. لطفاً چند لحظه دیگر تلاش کنید.")

def handle_callback(cq):
    chat_id = str(cq["message"]["chat"]["id"])
    message_id = cq["message"]["message_id"]
    data = cq.get("data", "")
    
    answer_callback(cq["id"])

    s = SESSIONS.get(chat_id)
    if not s: 
        send_message(chat_id, "⚠️ نشست شما منقضی شده است. لطفاً دوباره کلمه مورد نظر را جستجو کنید.")
        return

    kind, _, value = data.partition(":")
    
    if kind == "wpage":
        direction, _, p_num = value.partition(":")
        render_web_search(chat_id, message_id, page_num=int(p_num))

@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    try:
        if "callback_query" in update: handle_callback(update["callback_query"])
        elif "message" in update: handle_message(update["message"])
    except Exception: pass
    return jsonify({"ok": True})

@app.route("/")
def health(): return "TahaSearcher Engine is online and running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
