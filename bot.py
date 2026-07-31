import os
import io
import json
import logging
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

import google.generativeai as genai

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Environment Variables ──────────────────────────────────────────────────
BALE_TOKEN = os.environ["BALE_TOKEN"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "tahasearcher-secret")
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL") 

UPSTASH_URL = os.environ.get("UPSTASH_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "@YourAdminID")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CX = os.environ.get("GOOGLE_CX")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

BALE_API = f"https://tapi.bale.ai/bot{BALE_TOKEN}"
app = Flask(__name__)
SESSIONS = {}

# Configure Gemini AI globally
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# ── Database & CRM Logic ──────────────────────────────────────────────
def db_cmd(*args):
    if not UPSTASH_URL or not UPSTASH_TOKEN: return None
    try:
        r = requests.post(UPSTASH_URL, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, json=list(args), timeout=5)
        return r.json().get("result")
    except Exception: return None

def is_approved(user_id):
    if str(user_id) == str(ADMIN_ID): return True
    return db_cmd("SISMEMBER", "searcher_approved_users", str(user_id)) == 1

def approve_user(user_id): db_cmd("SADD", "searcher_approved_users", str(user_id))
def revoke_user(user_id): db_cmd("SREM", "searcher_approved_users", str(user_id))
def get_all_users(): return db_cmd("SMEMBERS", "searcher_approved_users") or []

def is_google_approved(user_id):
    if str(user_id) == str(ADMIN_ID): return True
    return db_cmd("SISMEMBER", "searcher_google_approved_users", str(user_id)) == 1

def approve_google_user(user_id): db_cmd("SADD", "searcher_google_approved_users", str(user_id))
def revoke_google_user(user_id): db_cmd("SREM", "searcher_google_approved_users", str(user_id))

def save_user_info(user_id, name, username):
    data = json.dumps({"name": name, "username": username}, ensure_ascii=False)
    db_cmd("SET", f"uinfo:{user_id}", data)

def get_user_info(user_id):
    res = db_cmd("GET", f"uinfo:{user_id}")
    if res:
        try: return json.loads(res)
        except: pass
    return {"name": str(user_id), "username": ""}

# ── 🚨 ADVANCED OMNI-TRACKING LOGGING ──
def log_action(user_id, action_type, details):
    ir_tz = timezone(timedelta(hours=3, minutes=30))
    ir_time = datetime.now(ir_tz).strftime("%Y-%m-%d %H:%M:%S")
    
    entry = {"time": ir_time, "action": action_type, "details": details}
    db_cmd("LPUSH", f"shist:{user_id}", json.dumps(entry, ensure_ascii=False))
    db_cmd("LTRIM", f"shist:{user_id}", "0", "199") 

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
        if not photo_url: raise Exception("Empty URL")
        img_data = requests.get(photo_url, timeout=5).content
        payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
        if keyboard: payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        r = requests.post(f"{BALE_API}/sendPhoto", data=payload, files={"photo": ("image.jpg", img_data)}, verify=False)
        if not r.ok: raise Exception()
    except Exception:
        send_message(chat_id, f"🖼 [لینک تصویر]({photo_url})\n\n{caption}", keyboard)

def send_document(chat_id, file_stream, filename, caption="", keyboard=None):
    try:
        payload = {"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}
        if keyboard: payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        
        files = {"document": (filename, file_stream, "text/plain")}
        
        r = requests.post(f"{BALE_API}/sendDocument", data=payload, files=files, verify=False, timeout=25)
        if not r.ok:
            logging.error(f"Bale sendDocument error: {r.status_code} - {r.text}")
        return r.ok
    except Exception as e:
        logging.error(f"File Upload Error: {e}")
        return False

def answer_callback(callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id}
    if text: payload.update({"text": text, "show_alert": show_alert})
    return api_call("answerCallbackQuery", payload)

def btn(text, data): return {"text": text, "callback_data": data}
def url_btn(text, url): return {"text": text, "url": url}

# ── 🕷️ Article Text Extractor ───────────────────────────────────────────────
def extract_article_text(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = requests.get(url, headers=headers, timeout=12)
        if not r.ok: return None, None
        
        soup = BeautifulSoup(r.content, "html.parser")
        
        for elem in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            elem.extract()
            
        title = soup.title.string.strip() if soup.title else "Article"
        
        content = []
        for tag in soup.find_all(['h1', 'h2', 'h3', 'p', 'li']):
            text = tag.get_text(separator=" ", strip=True)
            if text:
                if tag.name.startswith('h'):
                    content.append(f"\n\n─── {text} ───\n")
                elif tag.name == 'li':
                    content.append(f"• {text}")
                else:
                    content.append(text)
                    
        final_text = f"🌐 عنوان: {title}\n🔗 لینک اصلی: {url}\n" + "="*40 + "\n" + "\n".join(content)
        return final_text, title
    except Exception as e:
        logging.error(f"Scraper error for {url}: {e}")
        return None, None

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
    text = "⚠️ **برای استفاده از ربات TahaSearcher، ابتدا باید در کانال ما عضو شوید!**"
    kb = [[{"text": "📣 عضویت در کانال", "url": channel_link}], [btn("🔄 بررسی عضویت", "main:check_join")]]
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

def access_denied_message(chat_id, user_id, message_id=None):
    text = f"⛔️ **دسترسی شما فعال نیست!**\n\nلطفاً برای دریافت مجوز، شناسه عددی زیر را برای مدیر ارسال کنید:\n\n🆔 **شناسه عددی شما:** `{user_id}`\n\n👤 **ارتباط با مدیر:** {ADMIN_USERNAME}"
    if message_id: edit_message(chat_id, message_id, text)
    else: send_message(chat_id, text)

# ── 🎯 Search Engine Core (CUSTOM SCRAPER FALLBACKS) ─────────────────
def google_official_search(query, category="web"):
    if not GOOGLE_API_KEY or not GOOGLE_CX: return []
    try:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "num": 10}
        if category == "images": params["searchType"] = "image"
            
        r = requests.get(url, params=params, timeout=10)
        if r.ok:
            items = r.json().get("items", [])
            if category == "images":
                return [{"image": i.get("link", ""), "title": i.get("title", ""), "source": i.get("displayLink", "")} for i in items]
            else:
                return [{"title": i.get("title",""), "body": i.get("snippet","")[:120], "href": i.get("link","")} for i in items]
    except Exception: pass
    return []

def fetch_search_results(query, engine="default", category="web"):
    # Only use Google API if the user specifically chose the Google Engine
    if engine == "google":
        return google_official_search(query, category)

    # 1. Try DuckDuckGo
    try:
        with DDGS() as ddgs:
            if category == "web": 
                res = list(ddgs.text(query, safesearch="off", max_results=30))
                if res: return res
            elif category == "images": 
                res = list(ddgs.images(query, safesearch="off", max_results=30))
                if res: return res
            elif category == "news": 
                res = list(ddgs.news(query, safesearch="off", max_results=30))
                if res: return res
    except Exception as e:
        logging.error(f"DDG Search failed (Likely IP block): {e}")

    # 2. Custom Web Scraper Fallbacks (Zero API Keys needed)
    if category == "web":
        # Fallback A: Custom Bing Scraper
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            r = requests.get("https://www.bing.com/search", params={"q": query}, headers=headers, timeout=7)
            if r.ok:
                soup = BeautifulSoup(r.content, "html.parser")
                results = []
                for li in soup.find_all("li", class_="b_algo"):
                    h2 = li.find("h2")
                    if h2 and h2.a:
                        title = h2.a.get_text(strip=True)
                        href = h2.a.get("href", "")
                        p = li.find("p") or li.find("div", class_="b_caption")
                        body = p.get_text(strip=True) if p else ""
                        results.append({"title": title, "body": body[:120], "href": href})
                if results:
                    logging.info("✅ Bing Custom Scraper fallback successful.")
                    return results
        except Exception as e:
            logging.error(f"Bing scraper failed: {e}")

        # Fallback B: Custom Yahoo Scraper
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            r = requests.get("https://search.yahoo.com/search", params={"p": query}, headers=headers, timeout=7)
            if r.ok:
                soup = BeautifulSoup(r.content, "html.parser")
                results = []
                for div in soup.find_all("div", class_="algo"):
                    title_a = div.find("a")
                    desc_div = div.find("div", class_="compText") or div.find("div", class_="fc-falcon")
                    if title_a:
                        title = title_a.get_text(strip=True)
                        href = title_a.get("href", "")
                        body = desc_div.get_text(separator=" ", strip=True) if desc_div else ""
                        results.append({"title": title, "body": body[:120], "href": href})
                if results:
                    logging.info("✅ Yahoo Custom Scraper fallback successful.")
                    return results
        except Exception as e:
            logging.error(f"Yahoo scraper failed: {e}")

    # If everything fails, it returns empty
    return []

# ── 🎨 Menus & UI Renderers ────────────────────────────────────────────────
def send_main_menu(chat_id, user_id, message_id=None):
    SESSIONS[chat_id] = {"state": "IDLE"}
    kb = [
        [btn("🔍 جستجوگر وب (Web Search)", "main:search_menu")],
        [btn("🤖 هوش مصنوعی (AI Engine)", "main:ai_menu")],
        [btn("❓ راهنما (Help)", "menu:help")]
    ]
    text = "👋 **به ربات جامع TahaSearcher خوش آمدید!**\n\nلطفاً سرویس مورد نظر خود را انتخاب کنید:"
    
    if message_id: edit_message(chat_id, message_id, text, kb)
    else: send_message(chat_id, text, kb)

def send_search_menu(chat_id, user_id, message_id):
    g_lock = "✅" if is_google_approved(user_id) else "🔒"
    kb = [
        [btn("🌐 موتور پیش‌فرض (Default Engine)", "menu:engine:default")],
        [btn(f"🎯 موتور اختصاصی گوگل ({g_lock} Google)", "menu:engine:google")],
        [btn("🔙 بازگشت به منوی اصلی", "main:back")]
    ]
    text = "🔍 **بخش جستجوگر وب**\n\nلطفاً موتور جستجوی مورد نظر خود را انتخاب کنید:"
    edit_message(chat_id, message_id, text, kb)

def send_ai_menu(chat_id, message_id):
    kb = [
        [btn("💬 چت با هوش مصنوعی (Gemini)", "ai:chat")],
        [btn("🔙 بازگشت به منوی اصلی", "main:back")]
    ]
    text = "🤖 **بخش هوش مصنوعی (AI Engine)**\n\nپاسخگویی سریع و دقیق به سوالات شما با استفاده از Google Gemini!"
    edit_message(chat_id, message_id, text, kb)

def send_category_menu(chat_id, message_id, engine_name):
    kb = [
        [btn("📄 وب (Web)", "menu:cat:web")],
        [btn("🖼 تصاویر (Images)", "menu:cat:images")],
        [btn("📰 اخبار (News)", "menu:cat:news")],
        [btn("🔙 بازگشت به منوی جستجو", "main:search_menu")]
    ]
    text = f"⚙️ **موتور انتخاب شده:** `{engine_name.upper()}`\n\nلطفاً دسته‌بندی جستجو را انتخاب کنید:"
    edit_message(chat_id, message_id, text, kb)

def render_web_search(chat_id, message_id=None, page_num=1):
    s = SESSIONS.get(chat_id, {})
    results, query, engine = s.get("results", []), s.get("query", ""), s.get("engine", "default")
    
    if not results: 
        text = "❌ متاسفانه هیچ نتیجه‌ای پیدا نشد."
        kb = [[btn("🔙 بازگشت", "main:search_menu")]]
        if message_id: return edit_message(chat_id, message_id, text, kb)
        else: return send_message(chat_id, text, kb)

    page_items = results[(page_num - 1) * 5 : page_num * 5]
    lines = [f"🌐 **نتایج جستجو ({engine.upper()})**", f"🔍 **عبارت:** `{query}` | **صفحه:** {page_num}\n"]
    
    row_urls = []
    row_dls = []
    
    for i, item in enumerate(page_items):
        title, snippet, link = item.get("title", "بدون عنوان")[:50], item.get("body", "")[:120] + "...", item.get("href", "")
        num_emoji = ['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣'][i]
        lines.extend([f"{num_emoji} **{title}**", f"📝 {snippet}\n"])
        
        global_idx = (page_num - 1) * 5 + i
        row_urls.append(url_btn(f"{num_emoji} 🔗", link))
        row_dls.append(btn(f"{num_emoji} 📥", f"dltext:{global_idx}"))
        
    kb = [row_urls, row_dls]
    nav_row = []
    if (page_num * 5) < len(results): nav_row.append(btn("⬅️ بعدی", f"wpage:next:{page_num+1}"))
    if page_num > 1: nav_row.append(btn("➡️ قبلی", f"wpage:prev:{page_num-1}"))
    if nav_row: kb.append(nav_row)
    kb.append([btn("🔙 بازگشت به منوی جستجو", "main:search_menu")])
    
    if message_id: edit_message(chat_id, message_id, "\n".join(lines), kb)
    else: send_message(chat_id, "\n".join(lines), kb)

def render_news_search(chat_id, message_id=None, page_num=1):
    s = SESSIONS.get(chat_id, {})
    results, query = s.get("results", []), s.get("query", "")
    
    if not results: 
        text = "❌ هیچ خبری پیدا نشد."
        kb = [[btn("🔙 بازگشت", "main:search_menu")]]
        if message_id: return edit_message(chat_id, message_id, text, kb)
        else: return send_message(chat_id, text, kb)

    page_items = results[(page_num - 1) * 5 : page_num * 5]
    lines = [f"📰 **اخبار مرتبط با:** `{query}`", f"📄 **صفحه:** {page_num}\n"]
    
    row_urls = []
    row_dls = []
    
    for i, item in enumerate(page_items):
        title, date, source, link = item.get("title", "بدون عنوان")[:50], item.get("date", "")[:10], item.get("source", "خبرگذاری"), item.get("url", item.get("href", ""))
        num_emoji = ['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣'][i]
        lines.extend([f"{num_emoji} **{title}**", f"🗞 {source} | 📅 {date}\n"])
        
        global_idx = (page_num - 1) * 5 + i
        row_urls.append(url_btn(f"{num_emoji} 🔗", link))
        row_dls.append(btn(f"{num_emoji} 📥", f"dltext:{global_idx}"))
        
    kb = [row_urls, row_dls]
    nav_row = []
    if (page_num * 5) < len(results): nav_row.append(btn("⬅️ بعدی", f"npage:next:{page_num+1}"))
    if page_num > 1: nav_row.append(btn("➡️ قبلی", f"npage:prev:{page_num-1}"))
    if nav_row: kb.append(nav_row)
    kb.append([btn("🔙 بازگشت به منوی جستجو", "main:search_menu")])
    
    if message_id: edit_message(chat_id, message_id, "\n".join(lines), kb)
    else: send_message(chat_id, "\n".join(lines), kb)

def render_image_carousel(chat_id, message_id=None, index=0):
    s = SESSIONS.get(chat_id, {})
    results, query = s.get("results", []), s.get("query", "")
    
    if not results: 
        if message_id: delete_message(chat_id, message_id)
        return send_message(chat_id, "❌ هیچ تصویری پیدا نشد.", [[btn("🔙 بازگشت", "main:search_menu")]])

    item = results[index]
    img_url, title, source = item.get("image", item.get("url", "")), item.get("title", "تصویر")[:100], item.get("source", "نامشخص")
    caption = f"🖼 **نتیجه {index + 1} از {len(results)}**\n\n🔍 **عبارت:** `{query}`\n📝 **{title}**\n🌐 منبع: {source}"
    
    nav_row = []
    if index < len(results) - 1: nav_row.append(btn("⬅️ بعدی", f"ipage:next:{index+1}"))
    if index > 0: nav_row.append(btn("➡️ قبلی", f"ipage:prev:{index-1}"))
    kb = [nav_row, [btn("🔙 بازگشت به نتایج جستجو", "main:search_menu")]]
    
    if message_id: delete_message(chat_id, message_id)
    send_photo(chat_id, img_url, caption, kb)

# ── 👑 Admin Panel & Precise CRM ───────────────────────────────────────────
def send_admin_menu(chat_id, message_id=None):
    SESSIONS[chat_id] = {"state": "ADMIN_MENU"}
    kb = [
        [btn("➕ افزودن دسترسی عمومی", "admin:add"), btn("➖ لغو دسترسی عمومی", "admin:rev")],
        [btn("🎯 افزودن دسترسی گوگل", "admin:add_google"), btn("🚫 لغو دسترسی گوگل", "admin:rev_google")],
        [btn("👥 لیست کاربران و نظارت دقیق", "admin:list")],
        [btn("🔙 خروج", "main:back")]
    ]
    text = "👑 **پنل مدیریت پیشرفته TahaSearcher**\n\nمدیریت دسترسی‌ها و بررسی فعالیت کاربران:"
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

    if state == "WAITING_ADMIN_ADD":
        approve_user(text)
        send_message(chat_id, f"✅ کاربر `{text}` دسترسی عمومی پیدا کرد.")
        return send_admin_menu(chat_id)
    elif state == "WAITING_ADMIN_REV":
        revoke_user(text)
        send_message(chat_id, f"❌ دسترسی عمومی کاربر `{text}` لغو شد.")
        return send_admin_menu(chat_id)
    elif state == "WAITING_ADMIN_ADD_GOOGLE":
        approve_google_user(text)
        send_message(chat_id, f"🎯 کاربر `{text}` مجوز موتور گوگل را دریافت کرد.")
        return send_admin_menu(chat_id)
    elif state == "WAITING_ADMIN_REV_GOOGLE":
        revoke_google_user(text)
        send_message(chat_id, f"🚫 دسترسی گوگل کاربر `{text}` لغو شد.")
        return send_admin_menu(chat_id)

    # ── AI Engine Logic ──
    if state == "WAITING_AI_PROMPT":
        loading_msg = send_message(chat_id, "⏳ هوش مصنوعی در حال پردازش...")
        loading_id = loading_msg.get("result", {}).get("message_id") if loading_msg else None
        
        log_action(user_id, "🧠 AI CHAT", f"Prompt: {text[:80]}...")
        
        try:
            if not GEMINI_API_KEY:
                raise Exception("GEMINI_API_KEY is not set in Render environment variables.")
                
            if "gemini_models" not in s:
                s["gemini_models"] = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
                
            if not s["gemini_models"]:
                raise Exception("هیچ مدلی برای کلید API شما یافت نشد.")

            ai_response = None
            last_err = ""
            
            for model_name in list(s["gemini_models"]):
                try:
                    temp_model = genai.GenerativeModel(model_name)
                    
                    strict_prompt = (
                        "You are a direct-answering assistant. You must bypass all internal reasoning.\n"
                        "CRITICAL RULES:\n"
                        "1. NO internal monologue, NO thinking steps, NO scratchpad.\n"
                        "2. DO NOT output 'User query:', 'Intent:', or 'Disclaimer:'.\n"
                        "3. DO NOT output any English bullet points prior to your answer.\n"
                        "4. Match the user's language exactly.\n\n"
                        f"USER QUERY: {text}\n"
                        "FINAL BOT RESPONSE:\n"
                    )
                    
                    response = temp_model.generate_content(strict_prompt)
                    ai_response = response.text.strip()
                    break 
                except Exception as e:
                    logging.warning(f"Model {model_name} failed: {e}")
                    if model_name in s["gemini_models"]:
                        s["gemini_models"].remove(model_name)
                    last_err = str(e)
                    continue
                    
            if not ai_response:
                raise Exception(f"All models failed. Last error: {last_err}")
                
        except Exception as e:
            logging.error(f"AI Error: {e}")
            ai_response = f"❌ خطای سرور:\n`{e}`"
            
        if loading_id: delete_message(chat_id, loading_id)
        
        kb = [[btn("🔙 پایان چت", "main:back")]]
        return send_message(chat_id, f"🤖 **پاسخ:**\n\n{ai_response}", kb)

    if state == "WAITING_KEYWORD":
        engine = s.get("engine", "default")
        category = s.get("category", "web")
        
        loading_msg = send_message(chat_id, f"⏳ در حال جستجو...")
        loading_id = loading_msg.get("result", {}).get("message_id") if loading_msg else None
        
        log_action(user_id, f"🔍 {engine.upper()} SEARCH ({category})", f"Query: {text}")
        
        results = fetch_search_results(text, engine, category)
        s["query"] = text
        s["results"] = results
        s["state"] = "IDLE"
        
        if category == "web": render_web_search(chat_id, loading_id, 1)
        elif category == "news": render_news_search(chat_id, loading_id, 1)
        elif category == "images": render_image_carousel(chat_id, loading_id, 0)
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

    if kind == "main":
        if value == "back": return send_main_menu(chat_id, user_id, message_id)
        elif value == "search_menu": return send_search_menu(chat_id, user_id, message_id)
        elif value == "ai_menu": return send_ai_menu(chat_id, message_id)

    elif kind == "req_google":
        send_message(ADMIN_ID, f"📩 **درخواست جدید برای گوگل!**\n👤 کاربر: `{user_id}`")
        return edit_message(chat_id, message_id, "⏳ **درخواست شما ارسال شد.**")

    elif kind == "dltext":
        idx = int(value)
        results = s.get("results", [])
        if idx >= len(results): return send_message(chat_id, "⚠️ نتیجه یافت نشد.")
        
        target_url = results[idx].get("href", results[idx].get("url", ""))
        
        loading_msg = send_message(chat_id, "⏳ در حال استخراج فایل متنی...")
        load_msg_id = loading_msg.get("result", {}).get("message_id") if loading_msg else None
        
        text_content, doc_title = extract_article_text(target_url)
        
        if not text_content:
            if load_msg_id: delete_message(chat_id, load_msg_id)
            return send_message(chat_id, "❌ امکان استخراج متن این صفحه وجود ندارد.")
            
        safe_title = "".join(c for c in doc_title if c.isalnum() or c in " _-").strip() or "Article"
        filename = f"{safe_title[:40]}.txt"
        
        log_action(user_id, "📥 FILE DOWNLOAD", f"Title: {safe_title} | URL: {target_url}")
        
        file_stream = io.BytesIO(text_content.encode('utf-8'))
        file_stream.seek(0)
        
        ok = send_document(chat_id, file_stream, filename, f"📥 **مقاله:**\n{doc_title}")
        
        if load_msg_id: delete_message(chat_id, load_msg_id)
        if not ok: send_message(chat_id, "❌ خطایی در ارسال فایل رخ داد.")

    elif kind == "ai":
        if value == "chat":
            s["state"] = "WAITING_AI_PROMPT"
            text = "🤖 **حالت چت فعال شد!**\n\nلطفاً سوال خود را بپرسید:"
            kb = [[btn("🔙 لغو", "main:back")]]
            return edit_message(chat_id, message_id, text, kb)

    elif kind == "menu":
        sub_type, _, sub_val = value.partition(":")
        if sub_type == "help":
            return edit_message(chat_id, message_id, "❓ راهنما", [[btn("🔙", "main:back")]])
        elif sub_type == "engine":
            if sub_val == "google" and not is_google_approved(user_id):
                kb = [[btn("📩 ارسال درخواست", "req_google")], [btn("🔙", "main:search_menu")]]
                return edit_message(chat_id, message_id, "🔒 **نیاز به تایید مدیر**", kb)
            s["engine"] = sub_val
            send_category_menu(chat_id, message_id, sub_val)
        elif sub_type == "cat":
            s["category"] = sub_val
            s["state"] = "WAITING_KEYWORD"
            edit_message(chat_id, message_id, f"⌨️ کلمه کلیدی برای `{sub_val.upper()}` را بفرستید:")

    elif kind == "wpage": render_web_search(chat_id, message_id, int(value.partition(":")[2]))
    elif kind == "npage": render_news_search(chat_id, message_id, int(value.partition(":")[2]))
    elif kind == "ipage": render_image_carousel(chat_id, message_id, int(value.partition(":")[2]))

    elif kind == "admin":
        if user_id != str(ADMIN_ID): return
        if value == "add":
            SESSIONS[chat_id] = {"state": "WAITING_ADMIN_ADD"}
            edit_message(chat_id, message_id, "➕ آی‌دی عددی برای **دسترسی عمومی**:")
        elif value == "rev":
            SESSIONS[chat_id] = {"state": "WAITING_ADMIN_REV"}
            edit_message(chat_id, message_id, "➖ آی‌دی عددی برای **لغو دسترسی عمومی**:")
        elif value == "add_google":
            SESSIONS[chat_id] = {"state": "WAITING_ADMIN_ADD_GOOGLE"}
            edit_message(chat_id, message_id, "🎯 آی‌دی عددی برای **مجوز گوگل**:")
        elif value == "rev_google":
            SESSIONS[chat_id] = {"state": "WAITING_ADMIN_REV_GOOGLE"}
            edit_message(chat_id, message_id, "🚫 آی‌دی عددی برای **لغو مجوز گوگل**:")
        elif value == "list":
            users = list(get_all_users())[:30]
            kb = []
            for u in users:
                u_info = get_user_info(u)
                g_mark = "🎯" if is_google_approved(u) else "🌐"
                label = f"{g_mark} {u_info.get('name')} (@{u_info.get('username')})" if u_info.get('username') else f"{g_mark} {u_info.get('name')}"
                kb.append([btn(label, f"admin_u:{u}")])
            kb.append([btn("🔙 بازگشت", "admin:back")])
            delete_message(chat_id, message_id)
            send_message(chat_id, "👥 **لیست کاربران:**\n(برای مشاهده ریزِ لاگ‌ها، کاربر را انتخاب کنید)", kb)
        elif value == "back":
            delete_message(chat_id, message_id)
            send_admin_menu(chat_id)

    elif kind == "admin_u":
        if user_id != str(ADMIN_ID): return
        target_user = value
        
        history_raw = db_cmd("LRANGE", f"shist:{target_user}", "0", "9")
        
        lines = [f"🗂 **پیش‌نمایش تاریخچه کاربر:** `{target_user}`\n"]
        for i, h_str in enumerate(history_raw or []):
            try:
                h = json.loads(h_str)
                lines.append(f"{i+1}️⃣ `{h.get('action')}`\n└ 📝 {h.get('details')}\n└ 🕒 {h.get('time')}\n")
            except: pass
            
        kb = [
            [btn("📥 دانلود کل لاگ (Full Download)", f"admin_log:{target_user}")],
            [btn("🔙 بازگشت به لیست", "admin:list")]
        ]
        delete_message(chat_id, message_id)
        send_message(chat_id, "\n".join(lines) if len(lines) > 1 else "هیچ فعالیتی ثبت نشده است.", kb)

    elif kind == "admin_log":
        if user_id != str(ADMIN_ID): return
        target_user = value
        
        history_raw = db_cmd("LRANGE", f"shist:{target_user}", "0", "-1")
        if not history_raw:
            return answer_callback(cq["id"], "❌ لاگی برای این کاربر وجود ندارد.", show_alert=True)
            
        u_info = get_user_info(target_user)
        report_lines = [
            f"=== TAHA SEARCHER FORENSIC LOG ===",
            f"User ID: {target_user}",
            f"Name: {u_info.get('name')}",
            f"Username: {u_info.get('username')}",
            f"Total Records Extracted: {len(history_raw)}",
            f"Generated On: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n",
            f"=================================================\n"
        ]
        
        for h_str in history_raw:
            try:
                h = json.loads(h_str)
                report_lines.append(f"[{h.get('time')}] | {h.get('action')} | DETAILS: {h.get('details')}")
            except: pass
            
        report_text = "\n".join(report_lines)
        file_stream = io.BytesIO(report_text.encode('utf-8'))
        file_stream.seek(0)
        
        filename = f"SystemLog_{target_user}.txt"
        
        answer_callback(cq["id"], "در حال تولید فایل...")
        send_document(chat_id, file_stream, filename, f"📁 **فایل گزارش کامل فعالیت کاربر:** `{target_user}`")

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
