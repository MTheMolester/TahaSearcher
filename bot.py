import os
import json
import logging
import requests
from flask import Flask, request, jsonify

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Environment Variables ──────────────────────────────────────────────────
BALE_TOKEN = os.environ["BALE_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "tahasearcher-secret")
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL") 

UPSTASH_URL = os.environ.get("UPSTASH_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "@YourAdminID")

# Official Google API Configs
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY") # Google Cloud API Key
GOOGLE_CX = os.environ.get("GOOGLE_CX")          # Google Custom Search Engine ID

BALE_API = f"https://tapi.bale.ai/bot{BALE_TOKEN}"
app = Flask(__name__)
SESSIONS = {}

# ── Database & CRM Logic ──────────────────────────────────────────────────
def db_cmd(*args):
    if not UPSTASH_URL or not UPSTASH_TOKEN: return None
    try:
        r = requests.post(UPSTASH_URL, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, json=list(args), timeout=5)
        return r.json().get("result")
    except Exception: return None

# Tier 1: General Approval
def is_approved(user_id):
    if str(user_id) == str(ADMIN_ID): return True
    return db_cmd("SISMEMBER", "approved_users", str(user_id)) == 1

def approve_user(user_id): db_cmd("SADD", "approved_users", str(user_id))
def revoke_user(user_id): db_cmd("SREM", "approved_users", str(user_id))
def get_all_users(): return db_cmd("SMEMBERS", "approved_users") or []

# Tier 2: Google Search Approval
def is_google_approved(user_id):
    if str(user_id) == str(ADMIN_ID): return True
    return db_cmd("SISMEMBER", "google_approved_users", str(user_id)) == 1

def approve_google_user(user_id): db_cmd("SADD", "google_approved_users", str(user_id))
def revoke_google_user(user_id): db_cmd("SREM", "google_approved_users", str(user_id))

def save_user_info(user_id, name, username):
    data = json.dumps({"name": name, "username": username}, ensure_ascii=False)
    db_cmd("SET", f"uinfo:{user_id}", data)

def get_user_info(user_id):
    res = db_cmd("GET", f"uinfo:{user_id}")
    if res:
        try: return json.loads(res)
        except: pass
    return {"name": str(user_id), "username": ""}

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

def answer_callback(callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id}
    if text: payload.update({"text": text, "show_alert": show_alert})
    return api_call("answerCallbackQuery", payload)

def btn(text, data): return {"text": text, "callback_data": data}
def url_btn(text, url): return {"text": text, "url": url}

# ── Security Blockers ───────────────────────────────────────────────────────
def check_membership(user_id):
    if not REQUIRED_CHANNEL: return True 
    try:
        r = requests.post(f"{BALE_API}/getChatMember", json={"chat_id": REQUIRED_CHANNEL, "user_id": user_id}, timeout=5).json()
        if r.get("ok") and r["result"]["status"] in ["member", "administrator", "creator"]: return True
    except Exception: pass
    return False

def force_join_message(chat_id, message_id=None):
    channel_link = f"https://ble.ir/{REQUIRED_CHANNEL.replace('@', '')}"
    text = "⚠️ **برای استفاده از ربات TahaSearcher، ابتدا باید در کانال ما عضو شوید!**\n\nپس از عضویت، روی «بررسی عضویت» کلیک کنید."
    kb = [[{"text": "📣 عضویت در کانال", "url": channel_link}], [btn("🔄 بررسی عضویت", "main:check_join")]]
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

def access_denied_message(chat_id, user_id, message_id=None):
    text = f"⛔️ **دسترسی شما به ربات فعال نیست!**\n\n🆔 **شناسه عددی شما:** `{user_id}`\n\nلطفاً این شناسه را برای مدیر ارسال کنید تا دسترسی شما فعال شود:\n👤 **ارتباط با مدیر:** {ADMIN_USERNAME}"
    if message_id: edit_message(chat_id, message_id, text)
    else: send_message(chat_id, text)

# ── 🎯 Search Engines (Default vs Official Google) ──────────────────────────
def google_official_search(query):
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return []
    try:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "num": 10}
        r = requests.get(url, params=params, timeout=10)
        if r.ok:
            items = r.json().get("items", [])
            results = []
            for item in items:
                results.append({
                    "title": item.get("title", ""),
                    "body": item.get("snippet", "")[:120],
                    "href": item.get("link", "")
                })
            return results
    except Exception as e:
        logging.error(f"Google Official API Error: {e}")
    return []

def fetch_search_results(query, search_type="web"):
    results = []
    
    # Official Google Engine (Tier 2 Permission)
    if search_type == "google":
        return google_official_search(query)

    # Free Default Engine (DDG / SearxNG)
    try:
        with DDGS() as ddgs:
            if search_type == "web":
                raw = list(ddgs.text(query, max_results=30))
                if raw: return raw
            elif search_type == "images":
                return list(ddgs.images(query, max_results=30))
            elif search_type == "news":
                return list(ddgs.news(query, max_results=30))
    except Exception: pass

    # Fallback to SearxNG if DDG fails
    if search_type == "web":
        instances = ["https://searx.tiekoetter.com/search", "https://paulgo.io/search"]
        headers = {"User-Agent": "Mozilla/5.0"}
        for url in instances:
            try:
                r = requests.get(url, params={"q": query, "format": "json"}, headers=headers, timeout=5)
                if r.ok and r.json().get("results"):
                    return [{"title": item.get("title",""), "body": item.get("content","")[:120], "href": item.get("url","")} for item in r.json()["results"][:30]]
            except Exception: pass
            
    return results

# ── 🎨 UI Renderers ─────────────────────────────────────────────────────────
def render_web_search(chat_id, message_id=None, page_num=1, is_google=False):
    results = SESSIONS[chat_id].get("results", [])
    query = SESSIONS[chat_id].get("search_query", "")
    
    engine_name = "🎯 **گوگل (Google Official)**" if is_google else "🌐 **پیش‌فرض (Default Engine)**"
    
    if not results: 
        return edit_message(chat_id, message_id, f"❌ هیچ نتیجه‌ای در {engine_name} پیدا نشد.", [[btn("🔙 بازگشت", "do_search:menu")]])

    page_items = results[(page_num - 1) * 5 : page_num * 5]
    lines = [f"{engine_name}\n🔍 **جستجو برای:** `{query}`", f"📄 **صفحه:** {page_num}\n"]
    
    row_links = []
    for i, item in enumerate(page_items):
        title = item.get("title", "بدون عنوان")[:50]
        snippet = item.get("body", "")[:120] + "..."
        link = item.get("href", "")
        lines.extend([f"{['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣'][i]} **{title}**", f"📝 {snippet}\n"])
        row_links.append(url_btn(str(i+1), link))
        
    kb = [row_links]
    nav_row = []
    prefix = "gpage" if is_google else "wpage"
    if (page_num * 5) < len(results): nav_row.append(btn("⬅️ بعدی", f"{prefix}:next:{page_num+1}"))
    if page_num > 1: nav_row.append(btn("➡️ قبلی", f"{prefix}:prev:{page_num-1}"))
    if nav_row: kb.append(nav_row)
    kb.append([btn("🔙 تغییر نوع جستجو", "do_search:menu")])
    
    edit_message(chat_id, message_id, "\n".join(lines), kb)

# ── Admin Panel ─────────────────────────────────────────────────────────────
def send_admin_menu(chat_id, message_id=None):
    kb = [
        [btn("➕ تایید دسترسی عمومی", "admin:add"), btn("➖ لغو دسترسی عمومی", "admin:rev")],
        [btn("🎯 تایید دسترسی به گوگل", "admin:add_google"), btn("🚫 لغو دسترسی به گوگل", "admin:rev_google")],
        [btn("👥 لیست کاربران", "admin:list")]
    ]
    text = "👑 **پنل مدیریت TahaSearcher**\nمدیریت سطح دسترسی کاربران:"
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

# ── Core Handlers ───────────────────────────────────────────────────────────
def handle_message(msg):
    if msg.get("chat", {}).get("type", "") != "private": return 
    chat_id = str(msg["chat"]["id"])
    user_id = str(msg.get("from", {}).get("id", chat_id))
    text = (msg.get("text") or "").strip()

    save_user_info(user_id, msg.get("from", {}).get("first_name", "کاربر"), msg.get("from", {}).get("username", ""))

    if not check_membership(user_id): return force_join_message(chat_id)
    if text == "/admin" and user_id == str(ADMIN_ID): return send_admin_menu(chat_id)
    if not is_approved(user_id): return access_denied_message(chat_id, user_id)

    if text in ("/start", "/help"):
        SESSIONS[chat_id] = {}
        return send_message(chat_id, "👋 **به ربات TahaSearcher خوش آمدید!**\n\nعبارت مورد نظر خود را ارسال کنید تا برای شما جستجو کنم:")

    s = SESSIONS.get(chat_id)
    state = s.get("state") if s else None

    # Handle Admin Inputs
    if state == "WAITING_ADMIN_ADD":
        approve_user(text)
        send_message(chat_id, f"✅ کاربر `{text}` دسترسی عمومی پیدا کرد.")
        send_message(text, "🎉 **دسترسی شما به TahaSearcher فعال شد!**")
        return send_admin_menu(chat_id)
    elif state == "WAITING_ADMIN_ADD_GOOGLE":
        approve_google_user(text)
        send_message(chat_id, f"🎯 کاربر `{text}` مجوز موتور گوگل را دریافت کرد.")
        send_message(text, "🎯 **مجوز استفاده از موتور جستجوی رسمی گوگل برای شما فعال شد!**")
        return send_admin_menu(chat_id)

    # General Search Input
    SESSIONS[chat_id] = {"search_query": text}
    
    g_status = "✅ فعال" if is_google_approved(user_id) else "🔒 نیاز به مجوز"
    kb = [
        [btn("🌐 جستجوی پیش‌فرض (رایگان)", "do_search:web")],
        [btn(f"🎯 موتور اختصاصی گوگل ({g_status})", "do_search:google")],
        [btn("🖼 تصاویر", "do_search:images"), btn("📰 اخبار", "do_search:news")]
    ]
    send_message(chat_id, f"🔍 **عبارت جستجو:** `{text}`\nلطفاً موتور جستجو را انتخاب کنید:", kb)

def handle_callback(cq):
    chat_id = str(cq["message"]["chat"]["id"])
    user_id = str(cq.get("from", {}).get("id", chat_id))
    message_id = cq["message"]["message_id"]
    data = cq.get("data", "")
    
    if data == "main:check_join":
        if check_membership(user_id):
            answer_callback(cq["id"], "✅ عضویت تایید شد!", show_alert=True)
            send_message(chat_id, "برای شروع یک کلمه بفرستید.")
        else: answer_callback(cq["id"], "❌ هنوز در کانال عضو نشده‌اید!", show_alert=True)
        return

    answer_callback(cq["id"])
    if not check_membership(user_id): return force_join_message(chat_id, message_id)
    if not is_approved(user_id): return access_denied_message(chat_id, user_id, message_id)

    s = SESSIONS.get(chat_id, {})
    kind, _, value = data.partition(":")

    if kind == "req_google":
        # User requested Google permission -> Notify Admin!
        send_message(ADMIN_ID, f"📩 **درخواست جدید برای موتور گوگل!**\n\n👤 **کاربر:** `{user_id}`\nبرای تایید، وارد `/admin` شوید و آی‌دی بالا را در بخش گوگل وارد کنید.")
        return edit_message(chat_id, message_id, "⏳ **درخواست شما برای مدیر ارسال شد.**\nبه محض تایید، اطلاع داده خواهد شد.")

    elif kind == "do_search":
        if value == "google" and not is_google_approved(user_id):
            kb = [[btn("📩 ارسال درخواست مجوز به مدیر", "req_google")],[btn("🔙 بازگشت", "do_search:menu")]]
            return edit_message(chat_id, message_id, "🔒 **شما هنوز مجوز استفاده از موتور اختصاصی گوگل را ندارید!**\n\nجهت صرفه‌جویی در سهمیه API، استفاده از گوگل نیاز به تایید مدیر دارد.", kb)

        query = s.get("search_query")
        edit_message(chat_id, message_id, "⏳ در حال استعلام از موتور جستجو...")
        
        s["search_type"] = value
        s["results"] = fetch_search_results(query, value)
        
        if value == "web": render_web_search(chat_id, message_id, 1, is_google=False)
        elif value == "google": render_web_search(chat_id, message_id, 1, is_google=True)

    elif kind == "wpage": render_web_search(chat_id, message_id, int(value.partition(":")[2]), is_google=False)
    elif kind == "gpage": render_web_search(chat_id, message_id, int(value.partition(":")[2]), is_google=True)
    
    elif kind == "admin":
        if user_id != str(ADMIN_ID): return
        if value == "add":
            SESSIONS[chat_id] = {"state": "WAITING_ADMIN_ADD"}
            edit_message(chat_id, message_id, "➕ آی‌دی عددی کاربر برای **دسترسی عمومی** را بفرستید:")
        elif value == "add_google":
            SESSIONS[chat_id] = {"state": "WAITING_ADMIN_ADD_GOOGLE"}
            edit_message(chat_id, message_id, "🎯 آی‌دی عددی کاربر برای **دسترسی به گوگل** را بفرستید:")

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
