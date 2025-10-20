import os, re, json, logging, requests
from serpapi import GoogleSearch
from bs4 import BeautifulSoup
from openai import OpenAI
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ConversationHandler, ContextTypes, CallbackQueryHandler, filters

# ====================== CONFIG ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8229049437:AAFnS8Q-OhBbfTEFax0zb4EK1b6FNxj4wlQ")
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "6bd3650d9de2d08b1c132ce28e1de3567604c6daebb742d147b68f3b87a61255")
PPLX_KEY = os.getenv("PPLX_KEY", "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")
PPLX_MODEL = "sonar-pro"

# ====================== LOGGING ======================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("fab-rag")

# ====================== GLOBALS ======================
GENDER, AGE, QUERY, CLARIFY = 0, 1, 2, 3
pplx = OpenAI(api_key=PPLX_KEY, base_url="https://api.perplexity.ai")

# ====================== AGENT 1: УТОЧНЕНИЯ (Perplexity) ======================
def try_parse_json(txt):
    cleaned = re.sub(r'``````', '', txt, flags=re.DOTALL).strip()
    try: return json.loads(cleaned)
    except:
        s, e = cleaned.find('{'), cleaned.rfind('}')
        if s != -1 and e != -1 and e > s:
            try: return json.loads(cleaned[s:e+1])
            except: pass
    return None

def clarification_agent(query, history):
    ql = query.lower(); entity = None
    if any(x in ql for x in ["духи","парфюм","аромат"]): entity = "perfume"
    elif any(x in ql for x in ["крем","уход за кожей","лицо"]): entity = "cream"
    elif any(x in ql for x in ["шампунь","волос","бальзам"]): entity = "hair"
    elif any(x in ql for x in ["уборк","кухн","ванн","туалет","поверх","мыть"]): entity = "cleaning"
    if not entity: return []

    system = "Ты помощник Faberlic. Верни вопросы в JSON без markdown."
    user_data = {"entity":entity,"query":query,"history":{k:v for k,v in history.items() if k in ['gender','age']}}
    try:
        resp = pplx.chat.completions.create(model=PPLX_MODEL, temperature=0.1, max_tokens=300,
            messages=[{"role":"system","content":system},
                      {"role":"user","content":("Сформируй 1-2 вопроса для entity. JSON: {\"questions\":[{\"key\":\"...\",\"question\":\"...\",\"options\":[[\"A\"],[\"B\"]]}]}.\n"
                                               + json.dumps(user_data, ensure_ascii=False))}])
        data = try_parse_json(resp.choices[0].message.content.strip())
        if not data or "questions" not in data: raise ValueError("JSON fail")
        qs = data.get("questions", [])[:2]
        log.info(f"[CLARIFY] entity={entity} questions={len(qs)}")
        return qs
    except Exception as e:
        log.warning(f"[CLARIFY] fallback: {e}")
        if entity == "perfume": return [{"key":"audience","question":"Для кого?","options":[['Мужской','Женский'],['Унисекс']]}]
        if entity == "cream": return [{"key":"skin_type","question":"Тип кожи?","options":[['Нормальная','Сухая'],['Жирная']]}]
        if entity == "hair": return [{"key":"hair_problem","question":"Проблема?","options":[['Перхоть','Выпадение'],['Жирность']]}]
        if entity == "cleaning": return [{"key":"room_type","question":"Помещение?","options":[['Кухня','Ванная'],['Туалет']]}]
        return []

def enrich_query(query, user_data):
    enriched = query
    for k in ['audience','skin_type','hair_problem','room_type']:
        v = user_data.get(k)
        if v and v not in ['Любая','Универсальное','Нет проблем']:
            enriched += f" {v}"
    return enriched

# ====================== AGENT 2: ПОИСК (SerpAPI) ======================
def retrieval_agent(query, max_results=10):
    log.info(f"[RETRIEVAL] query='{query}'")
    params = {"engine":"google", "q":f"{query} site:faberlic.com/ru/", "api_key":SERPAPI_KEY, "num":max_results, "gl":"ru", "hl":"ru"}
    try:
        search = GoogleSearch(params)
        results = search.get_dict()
        urls = [item.get("link") for item in results.get("organic_results", []) if item.get("link") and "faberlic.com" in item.get("link") and "/ru/" in item.get("link")]
        log.info(f"[RETRIEVAL] found={len(urls)}")
        return urls[:max_results]
    except Exception as e:
        log.error(f"[RETRIEVAL] error: {e}")
        return []

# ====================== AGENT 3: ПАРСИНГ (BeautifulSoup) ======================
def parsing_agent(url):
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code != 200: return None
        soup = BeautifulSoup(r.content, 'lxml')
        
        title = soup.select_one('h1, .product-title, h1[itemprop="name"]')
        price = soup.select_one('.price, .product-price, span[itemprop="price"]')
        article = soup.select_one('.sku, .product-sku, .product-code')
        desc = soup.select_one('.description, .product-description, div[itemprop="description"]')
        
        if "/category/" in url:
            links = [a.get('href') for a in soup.select('a[href*="/product/"]')[:8] if a.get('href')]
            log.info(f"[PARSING] category {len(links)} products")
            return {"type":"category", "url":url, "products":links}
        
        if title:
            product = {"type":"product", "url":url,
                       "title":title.get_text(strip=True) if title else "—",
                       "price":price.get_text(strip=True) if price else "—",
                       "article":article.get_text(strip=True) if article else "—",
                       "description":(desc.get_text(strip=True)[:150] if desc else "—")}
            log.info(f"[PARSING] product: {product['title']}")
            return product
        return None
    except Exception as e:
        log.debug(f"[PARSING] error {url}: {e}")
        return None

def collect_products(query):
    urls = retrieval_agent(query, max_results=12)
    if not urls: return []
    
    products = []
    for url in urls:
        parsed = parsing_agent(url)
        if parsed:
            if parsed.get("type") == "category":
                for prod_url in parsed.get("products", [])[:6]:
                    if not prod_url.startswith("http"): prod_url = "https://faberlic.com" + prod_url
                    prod = parsing_agent(prod_url)
                    if prod and prod.get("type") == "product": products.append(prod)
            elif parsed.get("type") == "product":
                products.append(parsed)
        if len(products) >= 10: break
    
    log.info(f"[COLLECT] final={len(products)}")
    return products[:10]

# ====================== AGENT 4: ФОРМАТИРОВАНИЕ (Perplexity) ======================
def formatter_agent(products, profile):
    if not products:
        return "❌ Не нашёл товары. Откройте каталог: https://faberlic.com/ru"
    
    system = "Ты консультант Faberlic. Форматируй список товаров без markdown."
    payload = {"profile":profile, "format_rules":["Для каждого: Название, Артикул, Цена, Описание (1 строка), Ссылка", "Нумерованный список БЕЗ ** и []"], "products":products}
    log.info(f"[FORMAT] formatting {len(products)} products")
    try:
        resp = pplx.chat.completions.create(model=PPLX_MODEL, temperature=0.1, max_tokens=1500, search_domain_filter=["faberlic.com"],
            messages=[{"role":"system","content":system}, {"role":"user","content":json.dumps(payload, ensure_ascii=False)}])
        out = resp.choices[0].message.content.strip()
        log.info(f"[FORMAT] out_len={len(out)}")
        return out
    except Exception as e:
        log.error(f"[FORMAT] error: {e}")
        result = "Нашёл товары Faberlic:\n\n"
        for i, p in enumerate(products[:10], 1):
            result += f"{i}. {p.get('title','—')}\n   Артикул: {p.get('article','—')} | Цена: {p.get('price','—')}\n   {p.get('url','')}\n\n"
        return result.strip()

# ====================== TELEGRAM HANDLERS ======================
async def cmd_start(u, c):
    c.user_data.clear()
    await u.message.reply_text("👋 Привет! Помогу найти товары Faberlic.\n\nУкажите пол:",
        reply_markup=ReplyKeyboardMarkup([['Мужской','Женский'],['Пропустить']], one_time_keyboard=True, resize_keyboard=True))
    return GENDER

async def cmd_new(u, c):
    c.user_data.pop('asked', None)
    await u.message.reply_text("Что ищете?\n\n💡 Примеры:\n• парфюм\n• крем для лица\n• шампунь от перхоти",
        reply_markup=ReplyKeyboardMarkup([['/help']], resize_keyboard=True))
    return QUERY

async def cmd_help(u, c):
    await u.message.reply_text("📚 Помощь: https://faberlic.com/ru/ru/help")
    return QUERY

async def cmd_filters(u, c):
    ud = c.user_data
    current = [f"{k}: {ud[k]}" for k in ['audience','skin_type','hair_problem','room_type'] if k in ud]
    cur_txt = ", ".join(current) if current else "не заданы"
    btns = [[InlineKeyboardButton("🗑 Очистить", callback_data="clear_filters")]]
    await u.message.reply_text(f"🔧 Фильтры: {cur_txt}", reply_markup=InlineKeyboardMarkup(btns))
    return QUERY

async def on_callback(u, c):
    q = u.callback_query; await q.answer()
    if q.data == "clear_filters":
        for k in ['audience','skin_type','hair_problem','room_type']: c.user_data.pop(k, None)
        await q.edit_message_text("✅ Фильтры очищены")

async def on_gender(u, c):
    t = u.message.text.strip()
    if t == 'Пропустить':
        c.user_data['gender'] = None
    elif t in ['Мужской','Женский']:
        c.user_data['gender'] = t
    else:
        return GENDER
    
    await u.message.reply_text("Возраст:",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Пропустить")]], one_time_keyboard=True, resize_keyboard=True))
    return AGE

async def on_age(u, c):
    t = u.message.text.strip()
    if t == "Пропустить": c.user_data['age'] = None
    elif t.isdigit(): c.user_data['age'] = t
    else: return AGE
    
    await u.message.reply_text("Что ищете?\n\n💡 Примеры:\n• парфюм\n• крем для лица\n• шампунь",
        reply_markup=ReplyKeyboardMarkup([['/new','/filters'],['/help']], resize_keyboard=True))
    return QUERY

async def on_query(u, c):
    text = u.message.text.strip()
    if text.startswith('/'): return QUERY
    if len(text) < 2:
        await u.message.reply_text("Опишите подробнее 🙏")
        return QUERY
    
    c.user_data['original_query'] = text
    
    if 'asked' not in c.user_data:
        qs = clarification_agent(text, c.user_data)
        if qs:
            c.user_data['asked'] = True
            c.user_data['clarify_queue'] = qs
            first = qs[0]
            c.user_data['pending_key'] = first["key"]
            await u.message.reply_text(first["question"],
                reply_markup=ReplyKeyboardMarkup(first["options"], one_time_keyboard=True, resize_keyboard=True))
            c.user_data['clarify_queue'] = qs[1:]
            return CLARIFY
    
    await run_search(u, c, text)
    return QUERY

async def on_clarify(u, c):
    ans = u.message.text.strip()
    key = c.user_data.get('pending_key')
    if key: c.user_data[key] = ans
    
    queue = c.user_data.get('clarify_queue', [])
    if queue:
        nxt = queue[0]
        c.user_data['pending_key'] = nxt["key"]
        c.user_data['clarify_queue'] = queue[1:]
        await u.message.reply_text(nxt["question"],
            reply_markup=ReplyKeyboardMarkup(nxt["options"], one_time_keyboard=True, resize_keyboard=True))
        return CLARIFY
    
    await run_search(u, c, c.user_data.get('original_query', ''))
    return QUERY

async def run_search(u, c, user_query):
    await u.message.reply_text("🔍 Ищу товары Faberlic...", reply_markup=ReplyKeyboardRemove())
    enriched = enrich_query(user_query, c.user_data)
    profile = {"gender":c.user_data.get('gender'), "age":c.user_data.get('age')}
    log.info(f"[PIPELINE] query='{user_query}' enriched='{enriched}'")
    
    products = collect_products(enriched)
    formatted = formatter_agent(products, profile)
    
    kb = ReplyKeyboardMarkup([['/new','/filters'],['/help']], resize_keyboard=True)
    await u.message.reply_text(formatted, reply_markup=kb, disable_web_page_preview=True)

async def error_handler(u, c):
    log.error("Error", exc_info=c.error)

# ====================== RUN ======================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_gender)],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_age)],
            QUERY: [CommandHandler("new", cmd_new), CommandHandler("help", cmd_help), CommandHandler("filters", cmd_filters),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, on_query)],
            CLARIFY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_clarify)],
        },
        fallbacks=[CommandHandler("start", cmd_start)], allow_reentry=True)
    
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(CommandHandler("ping", lambda u,c: u.message.reply_text("🏓 pong")))
    app.add_error_handler(error_handler)
    
    log.info("🚀 Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
