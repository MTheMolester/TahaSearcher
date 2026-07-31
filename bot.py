import os
import json
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify

# Safely import the ddgs scraper library
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
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CX = os.environ.get("GOOGLE_CX")

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

# Tier 1: General Bot Approval
def is_approved(user_id):
    if str(user_id) == str(ADMIN_ID): return True
    return db_cmd("SISMEMBER", "approved_users", str(user_id)) == 1

def approve_user(user_id): db_cmd("SADD", "approved_users", str(user_id))
def revoke_user(user_id): db_cmd("SREM", "approved_users", str(user_id))
def get_all_users(): return db_cmd("SMEMBERS", "approved_users") or []

# Tier 2: Google Engine Approval
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

def log_history(user_id, engine, category, query):
    ir_time = (datetime.utcnow() + timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d %H:%M")
    entry = {
        "engine": engine,
        "category": category,
        "query": query,
        "time": ir_time
    }
    db_cmd("LPUSH", f"shist:{user_id}", json.dumps(entry, ensure_ascii=False))
    db_cmd("LTRIM", f"shist:{user_id}", "0", "19") # Keep last 20 searches

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
        if not r.ok: raise Exception()
    except Exception:
        send_message(chat_id, f"🖼 [لینک تصویر]({photo_url})\n\n{caption}", keyboard)

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
    text = f"⛔️ **دسترسی شما فعال نیست!**\n\nلطفاً برای دریافت مجوز، شناسه عددی زیر را برای مدیر ارسال کنید:\n\n🆔 **شناسه عددی شما:** `{user_id}`\n\n👤 **ارتباط با مدیر:** {ADMIN_USERNAME}"
    if message_id: edit_message(chat_id, message_id, text)
    else: send_message(chat_id, text)

# ── 🎯 Search Engine Core ──────────────────────────────────────────────────
def google_official_search(query):
    if not GOOGLE_API_KEY or not GOOGLE_CX: return []
    try:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "num": 10}
        r = requests.get(url, params=params, timeout=10)
        if r.ok:
            return [{"title": i.get("title",""), "body": i.get("snippet","")[:120], "href": i.get("link","")} for i in r.json().get("items", [])]
    except Exception as e:
        logging.error(f"Google Search Error: {e}")
    return []

def fetch_search_results(query, engine="default", category="web"):
    if engine == "google":
        return google_official_search(query)

    try:
        with DDGS() as ddgs:
            if category == "web": return list(ddgs.text(query, max_results=30))
            elif category == "images": return list(ddgs.images(query, max_results=30))
            elif category == "news": return list(ddgs.news(query, max_results=30))
    except Exception: pass

    # SearxNG Fallback for web search
    if category == "web":
        instances = ["https://searx.tiekoetter.com/search", "https://paulgo.io/search"]
        for url in instances:
            try:
                r = requests.get(url, params={"q": query, "format": "json"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                if r.ok and r.json().get("results"):
                    return [{"title": i.get("title",""), "body": i.get("content","")[:120], "href": i.get("url","")} for i in r.json()["results"][:30]]
            except Exception: pass

    return []

# ── 🎨 Menus & UI Renderers ────────────────────────────────────────────────
def send_main_menu(chat_id, user_id, message_id=None):
    SESSIONS[chat_id] = {"state": "IDLE"}
    g_lock = "✅" if is_google_approved(user_id) else "🔒"
    
    kb = [
        [btn("🌐 موتور پیش‌فرض (Default Engine)", "menu:engine:default")],
        [btn(f"🎯 موتور اختصاصی گوگل ({g_lock} Google)", "menu:engine:google")],
        [btn("❓ راهنما (Help)", "menu:help")]
    ]
    text = "👋 **به ربات TahaSearcher خوش آمدید!**\n\nلطفاً موتور جستجوی مورد نظر خود را انتخاب کنید:"
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

def send_category_menu(chat_id, message_id, engine_name):
    kb = [
        [btn("📄 وب (Web)", "menu:cat:web")],
        [btn("🖼 تصاویر (Images)", "menu:cat:images")],
        [btn("📰 اخبار (News)", "menu:cat:news")],
        [btn("🔙 بازگشت به منوی اصلی", "main:back")]
    ]
    text = f"⚙️ **موتور انتخاب شده:** `{engine_name.upper()}`\n\nلطفاً دسته‌بندی جستجو را انتخاب کنید:"
    edit_message(chat_id, message_id, text, kb)

def render_web_search(chat_id, message_id=None, page_num=1):
    s = SESSIONS.get(chat_id, {})
    results, query, engine = s.get("results", []), s.get("query", ""), s.get("engine", "default")
    
    if not results: return edit_message(chat_id, message_id, "❌ متاسفانه هیچ نتیجه‌ای پیدا نشد.", [[btn("🔙 بازگشت", "main:back")]])

    page_items = results[(page_num - 1) * 5 : page_num * 5]
    lines = [f"🌐 **نتایج جستجو ({engine.upper()})**", f"🔍 **عبارت:** `{query}` | **صفحه:** {page_num}\n"]
    
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
    kb.append([btn("🔙 بازگشت به منوی اصلی", "main:back")])
    
    edit_message(chat_id, message_id, "\n".join(lines), kb)

def render_news_search(chat_id, message_id=None, page_num=1):
    s = SESSIONS.get(chat_id, {})
    results, query = s.get("results", []), s.get("query", "")
    
    if not results: return edit_message(chat_id, message_id, "❌ هیچ خبری پیدا نشد.", [[btn("🔙 بازگشت", "main:back")]])

    page_items = results[(page_num - 1) * 5 : page_num * 5]
    lines = [f"📰 **اخبار مرتبط با:** `{query}`", f"📄 **صفحه:** {page_num}\n"]
    
    row_links = []
    for i, item in enumerate(page_items):
        title, date, source, link = item.get("title", "بدون عنوان")[:50], item.get("date", "")[:10], item.get("source", "خبرگذاری"), item.get("url", item.get("href", ""))
        lines.extend([f"{['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣'][i]} **{title}**", f"🗞 {source} | 📅 {date}\n"])
        row_links.append(url_btn(str(i+1), link))
        
    kb = [row_links]
    nav_row = []
    if (page_num * 5) < len(results): nav_row.append(btn("⬅️ بعدی", f"npage:next:{page_num+1}"))
    if page_num > 1: nav_row.append(btn("➡️ قبلی", f"npage:prev:{page_num-1}"))
    if nav_row: kb.append(nav_row)
    kb.append([btn("🔙 بازگشت به منوی اصلی", "main:back")])
    
    edit_message(chat_id, message_id, "\n".join(lines), kb)

def render_image_carousel(chat_id, message_id=None, index=0):
    s = SESSIONS.get(chat_id, {})
    results, query = s.get("results", []), s.get("query", "")
    
    if not results: 
        delete_message(chat_id, message_id)
        return send_message(chat_id, "❌ هیچ تصویری پیدا نشد.", [[btn("🔙 بازگشت به منو", "main:back")]])

    item = results[index]
    img_url, title, source = item.get("image", item.get("url", "")), item.get("title", "تصویر")[:100], item.get("source", "نامشخص")
    caption = f"🖼 **نتیجه {index + 1} از {len(results)}**\n\n🔍 **عبارت:** `{query}`\n📝 **{title}**\n🌐 منبع: {source}"
    
    nav_row = []
    if index < len(results) - 1: nav_row.append(btn("⬅️ بعدی", f"ipage:next:{index+1}"))
    if index > 0: nav_row.append(btn("➡️ قبلی", f"ipage:prev:{index-1}"))
    kb = [nav_row, [btn("🔙 بازگشت به منوی اصلی", "main:back")]]
    
    if message_id: delete_message(chat_id, message_id)
    send_photo(chat_id, img_url, caption, kb)

# ── 👑 Admin Panel & Precise CRM ───────────────────────────────────────────
def send_admin_menu(chat_id, message_id=None):
    SESSIONS[chat_id] = {"state": "ADMIN_MENU"}
    kb = [
        [btn("➕ افزودن دسترسی عمومی", "admin:add"), btn("➖ لغو دسترسی عمومی", "admin:rev")],
        [btn("🎯 افزودن دسترسی گوگل", "admin:add_google"), btn("🚫 لغو دسترسی گوگل", "admin:rev_google")],
        [btn("👥 لیست کاربران و تاریخچه دقیق", "admin:list")],
        [btn("🔙 خروج", "main:back")]
    ]
    text = "👑 **پنل مدیریت TahaSearcher**\nسطح دسترسی کاربران و تاریخچه دقیق فعالیت‌ها:"
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

    if text in ("/start", "/help"): return send_main_menu(chat_id, user_id)

    s = SESSIONS.get(chat_id, {})
    state = s.get("state")

    # Admin Inputs
    if state == "WAITING_ADMIN_ADD":
        approve_user(text)
        send_message(chat_id, f"✅ کاربر `{text}` دسترسی عمومی پیدا کرد.")
        send_message(text, "🎉 **دسترسی شما به ربات TahaSearcher فعال شد!**\nارسال کنید: /start")
        return send_admin_menu(chat_id)
    elif state == "WAITING_ADMIN_REV":
        revoke_user(text)
        send_message(chat_id, f"❌ دسترسی عمومی کاربر `{text}` لغو شد.")
        return send_admin_menu(chat_id)
    elif state == "WAITING_ADMIN_ADD_GOOGLE":
        approve_google_user(text)
        send_message(chat_id, f"🎯 کاربر `{text}` مجوز موتور گوگل را دریافت کرد.")
        send_message(text, "🎯 **مجوز استفاده از موتور گوگل برای شما فعال شد!**")
        return send_admin_menu(chat_id)
    elif state == "WAITING_ADMIN_REV_GOOGLE":
        revoke_google_user(text)
        send_message(chat_id, f"🚫 دسترسی گوگل کاربر `{text}` لغو شد.")
        return send_admin_menu(chat_id)

    # Search Keyword Prompt Input
    if state == "WAITING_KEYWORD":
        engine = s.get("engine", "default")
        category = s.get("category", "web")
        
        send_message(chat_id, f"⏳ در حال جستجوی عبارت `{text}` در موتور {engine.upper()}...")
        
        log_history(user_id, engine, category, text)
        
        results = fetch_search_results(text, engine, category)
        s["query"] = text
        s["results"] = results
        s["state"] = "IDLE"
        
        if category == "web": render_web_search(chat_id, None, 1)
        elif category == "news": render_news_search(chat_id, None, 1)
        elif category == "images": render_image_carousel(chat_id, None, 0)
        return

    send_main_menu(chat_id, user_id)

def handle_callback(cq):
    chat_id = str(cq["message"]["chat"]["id"])
    user_id = str(cq.get("from", {}).get("id", chat_id))
    message_id = cq["message"]["message_id"]
    data = cq.get("data", "")
    
    if data == "main:check_join":
        if check_membership(user_id):
            answer_callback(cq["id"], "✅ عضویت تایید شد!", show_alert=True)
            send_main_menu(chat_id, user_id, message_id)
        else: answer_callback(cq["id"], "❌ هنوز در کانال عضو نشده‌اید!", show_alert=True)
        return

    answer_callback(cq["id"])
    if not check_membership(user_id): return force_join_message(chat_id, message_id)
    if not is_approved(user_id): return access_denied_message(chat_id, user_id, message_id)

    s = SESSIONS.setdefault(chat_id, {})
    kind, _, value = data.partition(":")

    if kind == "main" and value == "back":
        return send_main_menu(chat_id, user_id, message_id)

    elif kind == "req_google":
        send_message(ADMIN_ID, f"📩 **درخواست جدید برای موتور گوگل!**\n\n👤 کاربر: `{user_id}`\nجهت تایید وارد `/admin` شوید.")
        return edit_message(chat_id, message_id, "⏳ **درخواست شما برای مدیر ارسال شد.**")

    elif kind == "menu":
        sub_type, _, sub_val = value.partition(":")
        
        if sub_type == "help":
            help_text = (
                "❓ **راهنمای استفاده از TahaSearcher:**\n\n"
                "1️⃣ **موتور پیش‌فرض (Default):** جستجوی کاملاً آزاد و رایگان در کل اینترنت (وب، عکس، خبر).\n\n"
                "2️⃣ **موتور گوگل (Google Engine):** نتایج دقیق مستقیم از API رسمی گوگل. این موتور نیاز به تایید مدیر دارد.\n\n"
                "💡 **روش کار:** ابتدا موتور و دسته‌بندی را انتخاب کنید، سپس کلمه کلیدی خود را بفرستید."
            )
            return edit_message(chat_id, message_id, help_text, [[btn("🔙 بازگشت", "main:back")]])
            
        elif sub_type == "engine":
            if sub_val == "google" and not is_google_approved(user_id):
                kb = [[btn("📩 ارسال درخواست مجوز به مدیر", "req_google")], [btn("🔙 بازگشت", "main:back")]]
                return edit_message(chat_id, message_id, "🔒 **موتور گوگل نیاز به مجوز اختصاصی مدیر دارد.**\nآیا می‌خواهید درخواست مجوز ارسال کنید؟", kb)
            
            s["engine"] = sub_val
            send_category_menu(chat_id, message_id, sub_val)
            
        elif sub_type == "cat":
            s["category"] = sub_val
            s["state"] = "WAITING_KEYWORD"
            edit_message(chat_id, message_id, f"⌨️ **دسته‌بندی `{sub_val.upper()}` انتخاب شد.**\n\nلطفاً کلمه یا عبارت کلیدی مورد نظر خود را ارسال کنید:")

    elif kind == "wpage": render_web_search(chat_id, message_id, int(value.partition(":")[2]))
    elif kind == "npage": render_news_search(chat_id, message_id, int(value.partition(":")[2]))
    elif kind == "ipage": render_image_carousel(chat_id, message_id, int(value.partition(":")[2]))

    # Admin Callback Actions
    elif kind == "admin":
        if user_id != str(ADMIN_ID): return
        if value == "add":
            SESSIONS[chat_id] = {"state": "WAITING_ADMIN_ADD"}
            edit_message(chat_id, message_id, "➕ آی‌دی عددی کاربر برای **دسترسی عمومی** را بفرستید:")
        elif value == "rev":
            SESSIONS[chat_id] = {"state": "WAITING_ADMIN_REV"}
            edit_message(chat_id, message_id, "➖ آی‌دی عددی کاربر برای **لغو دسترسی عمومی** را بفرستید:")
        elif value == "add_google":
            SESSIONS[chat_id] = {"state": "WAITING_ADMIN_ADD_GOOGLE"}
            edit_message(chat_id, message_id, "🎯 آی‌دی عددی کاربر برای **مجوز گوگل** را بفرستید:")
        elif value == "rev_google":
            SESSIONS[chat_id] = {"state": "WAITING_ADMIN_REV_GOOGLE"}
            edit_message(chat_id, message_id, "🚫 آی‌دی عددی کاربر برای **لغو مجوز گوگل** را بفرستید:")
        elif value == "list":
            users = list(get_all_users())[:30]
            kb = []
            for u in users:
                u_info = get_user_info(u)
                g_mark = "🎯" if is_google_approved(u) else "🌐"
                label = f"{g_mark} {u_info.get('name')} (@{u_info.get('username')})" if u_info.get('username') else f"{g_mark} {u_info.get('name')}"
                kb.append([btn(label, f"admin_u:{u}")])
            kb.append([btn("🔙 بازگشت به پنل اصلی", "admin:back")])
            delete_message(chat_id, message_id)
            send_message(chat_id, "👥 **لیست کاربران مجاز:**\n(علامت 🎯 به معنی داشتن مجوز گوگل است)\n\nروی کاربر کلیک کنید تا **تاریخچه دقیق جستجوها** را ببینید:", kb)
        elif value == "back":
            delete_message(chat_id, message_id)
            send_admin_menu(chat_id)

    elif kind == "admin_u":
        if user_id != str(ADMIN_ID): return
        target_user = value
        history_raw = db_cmd("LRANGE", f"shist:{target_user}", "0", "9")
        
        lines = [f"🗂 **تاریخچه دقیق جستجوهای کاربر:** `{target_user}`\n"]
        for i, h_str in enumerate(history_raw or []):
            try:
                h = json.loads(h_str)
                lines.append(f"{i+1}️⃣ **عبارت:** `{h.get('query')}`")
                lines.append(f"⚙️ موتور: `{h.get('engine').upper()}` | دسته: `{h.get('category').upper()}`")
                lines.append(f"🕒 زمان: `{h.get('time')}`\n")
            except: pass
            
        kb = [[btn("🔙 بازگشت به لیست کاربران", "admin:list")]]
        delete_message(chat_id, message_id)
        send_message(chat_id, "\n".join(lines) if len(lines) > 1 else "هیچ تاریخچه‌ای ثبت نشده است.", kb)

@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    try:
        if "callback_query" in update: handle_callback(update["callback_query"])
        elif "message" in update: handle_message(update["message"])
    except Exception: pass
    return jsonify({"ok": True})

@app.route("/")
def health(): return "TahaSearcher Engine 2.0 Running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
