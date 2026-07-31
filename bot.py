import os
import json
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

def delete_message(chat_id, message_id):
    return api_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

def send_photo(chat_id, photo_url, caption, keyboard=None):
    try:
        img_data = requests.get(photo_url, timeout=5).content
        payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
        if keyboard: payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        r = requests.post(f"{BALE_API}/sendPhoto", data=payload, files={"photo": ("image.jpg", img_data)}, verify=False)
        if not r.ok: raise Exception("Upload Failed")
    except Exception: 
        # Fallback if image is too large or fails to download
        send_message(chat_id, f"🖼 [لینک اصلی تصویر]({photo_url})\n\n{caption}", keyboard)

def answer_callback(callback_query_id):
    return api_call("answerCallbackQuery", {"callback_query_id": callback_query_id})

def btn(text, data): return {"text": text, "callback_data": data}
def url_btn(text, url): return {"text": text, "url": url}

# ── 🌐 The Omni-Search Engine ──────────────────────────────────────────────
def fetch_search_results(query, search_type="web"):
    results = []
    
    try:
        with DDGS() as ddgs:
            if search_type == "web":
                raw_results = list(ddgs.text(query, max_results=30))
                if raw_results: return raw_results
            elif search_type == "images":
                return list(ddgs.images(query, max_results=30))
            elif search_type == "news":
                return list(ddgs.news(query, max_results=30))
    except Exception as e:
        logging.warning(f"DuckDuckGo {search_type} failed: {e}")

    # Fallback to SearxNG ONLY for web text search
    if search_type == "web":
        instances = [
            "https://searx.tiekoetter.com/search",
            "https://searx.work/search",
            "https://searx.ro/search",
            "https://paulgo.io/search"
        ]
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        for url in instances:
            try:
                r = requests.get(url, params={"q": query, "format": "json"}, headers=headers, timeout=6)
                if r.ok:
                    try:
                        data = r.json()
                        for item in data.get("results", [])[:30]:
                            results.append({"title": item.get("title", ""), "body": item.get("content", "")[:120], "href": item.get("url", "")})
                        if results: return results
                    except Exception: pass
            except Exception: pass
            
    return results

# ── 🎨 UI Renderers ─────────────────────────────────────────────────────────
def render_web_search(chat_id, message_id=None, page_num=1):
    results, query = SESSIONS[chat_id].get("results", []), SESSIONS[chat_id].get("search_query", "")
    if not results: return edit_message(chat_id, message_id, "❌ متاسفانه نتیجه‌ای در بخش وب پیدا نشد.", [[btn("🔙 بازگشت", "do_search:menu")]])

    page_items = results[(page_num - 1) * 5 : (page_num - 1) * 5 + 5]
    lines = [f"🌐 **نتایج وب برای:** `{query}`", f"📄 **صفحه:** {page_num}\n"]
    
    row_links = []
    for i, item in enumerate(page_items):
        title, snippet, link = item.get("title", "بدون عنوان")[:50], item.get("body", "")[:120] + "...", item.get("href", "")
        lines.extend([f"{['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣'][i]} **{title}**", f"📝 {snippet}\n"])
        row_links.append(url_btn(str(i+1), link))
        
    kb = [row_links]
    nav_row = []
    if (page_num * 5) < len(results): nav_row.append(btn("⬅️ بعدی", f"wpage:next:{page_num+1}"))
    if page_num > 1: nav_row.append(btn("➡️ قبلی", f"wpage:prev:{page_num-1}"))
    if nav_row: kb.append(nav_row)
    kb.append([btn("🔙 تغییر نوع جستجو", "do_search:menu")])
    
    edit_message(chat_id, message_id, "\n".join(lines), kb)

def render_news_search(chat_id, message_id=None, page_num=1):
    results, query = SESSIONS[chat_id].get("results", []), SESSIONS[chat_id].get("search_query", "")
    if not results: return edit_message(chat_id, message_id, "❌ متاسفانه هیچ خبر جدیدی پیدا نشد.", [[btn("🔙 بازگشت", "do_search:menu")]])

    page_items = results[(page_num - 1) * 5 : (page_num - 1) * 5 + 5]
    lines = [f"📰 **اخبار جدید برای:** `{query}`", f"📄 **صفحه:** {page_num}\n"]
    
    row_links = []
    for i, item in enumerate(page_items):
        title = item.get("title", "بدون عنوان")[:50]
        date = item.get("date", "")[:10]
        source = item.get("source", "منبع ناشناس")
        link = item.get("url", item.get("href", ""))
        
        lines.extend([f"{['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣'][i]} **{title}**", f"🗞 {source} | 📅 {date}\n"])
        row_links.append(url_btn(str(i+1), link))
        
    kb = [row_links]
    nav_row = []
    if (page_num * 5) < len(results): nav_row.append(btn("⬅️ بعدی", f"npage:next:{page_num+1}"))
    if page_num > 1: nav_row.append(btn("➡️ قبلی", f"npage:prev:{page_num-1}"))
    if nav_row: kb.append(nav_row)
    kb.append([btn("🔙 تغییر نوع جستجو", "do_search:menu")])
    
    edit_message(chat_id, message_id, "\n".join(lines), kb)

def render_image_carousel(chat_id, message_id=None, index=0):
    results, query = SESSIONS[chat_id].get("results", []), SESSIONS[chat_id].get("search_query", "")
    if not results: 
        delete_message(chat_id, message_id)
        return send_message(chat_id, "❌ متاسفانه تصویری پیدا نشد.", [[btn("🔙 بازگشت", "do_search:menu")]])

    item = results[index]
    img_url = item.get("image", item.get("url", ""))
    title = item.get("title", "تصویر")[:100]
    source = item.get("source", "نامشخص")
    
    caption = f"🖼 **نتیجه {index + 1} از {len(results)}**\n\n📝 **{title}**\n🌐 منبع: {source}"
    
    nav_row = []
    if index < len(results) - 1: nav_row.append(btn("⬅️ بعدی", f"ipage:next:{index+1}"))
    if index > 0: nav_row.append(btn("➡️ قبلی", f"ipage:prev:{index-1}"))
        
    kb = [nav_row, [btn("🔙 تغییر نوع جستجو", "do_search:menu")]]
    
    if message_id: delete_message(chat_id, message_id) # Delete old text/image to replace with new image
    send_photo(chat_id, img_url, caption, kb)

# ── Core Handlers ───────────────────────────────────────────────────────────
def handle_message(msg):
    if msg.get("chat", {}).get("type", "") != "private": return 
    chat_id, text = str(msg["chat"]["id"]), (msg.get("text") or "").strip()

    if text in ("/start", "/help"):
        SESSIONS[chat_id] = {}
        return send_message(chat_id, "👋 **به ربات TahaSearcher خوش آمدید!**\n\nمن یک موتور جستجوی چندمنظوره هستم. 🌐\n\nلطفاً کلمه یا جمله‌ای که دارید را مستقیماً برای من بفرستید:")

    SESSIONS.setdefault(chat_id, {})["search_query"] = text
    kb = [
        [btn("📄 جستجوی وب (Web)", "do_search:web")],
        [btn("🖼 تصاویر (Images)", "do_search:images")],
        [btn("📰 اخبار (News)", "do_search:news")]
    ]
    send_message(chat_id, f"🔍 **کلمه جستجو شده:** `{text}`\nلطفاً نوع جستجو را انتخاب کنید:", kb)

def handle_callback(cq):
    chat_id, message_id, data = str(cq["message"]["chat"]["id"]), cq["message"]["message_id"], cq.get("data", "")
    answer_callback(cq["id"])

    s = SESSIONS.get(chat_id)
    if not s: return send_message(chat_id, "⚠️ نشست شما منقضی شده است. لطفاً دوباره جستجو کنید.")

    kind, _, value = data.partition(":")
    
    if kind == "do_search":
        if value == "menu":
            kb = [[btn("📄 جستجوی وب (Web)", "do_search:web")], [btn("🖼 تصاویر (Images)", "do_search:images")], [btn("📰 اخبار (News)", "do_search:news")]]
            delete_message(chat_id, message_id)
            return send_message(chat_id, f"🔍 **کلمه جستجو شده:** `{s.get('search_query', '')}`\nلطفاً نوع جستجو را انتخاب کنید:", kb)
            
        query = s.get("search_query")
        
        # Need to handle deleting if the previous message was a photo
        delete_message(chat_id, message_id)
        loading_msg = send_message(chat_id, f"⏳ در حال جستجو در دیتابیس...")
        new_msg_id = loading_msg.get("result", {}).get("message_id")
        
        s["search_type"], s["results"] = value, fetch_search_results(query, value)
        
        if value == "web": render_web_search(chat_id, new_msg_id, 1)
        elif value == "news": render_news_search(chat_id, new_msg_id, 1)
        elif value == "images": render_image_carousel(chat_id, new_msg_id, 0)

    elif kind == "wpage": render_web_search(chat_id, message_id, int(value.partition(":")[2]))
    elif kind == "npage": render_news_search(chat_id, message_id, int(value.partition(":")[2]))
    elif kind == "ipage": render_image_carousel(chat_id, message_id, int(value.partition(":")[2]))

@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    try:
        if "callback_query" in update: handle_callback(update["callback_query"])
        elif "message" in update: handle_message(update["message"])
    except Exception: pass
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
