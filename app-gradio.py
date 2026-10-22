"""A customer service agent for a small music café (Gradio app).

Every question goes to the agent first. The agent decides which tool to use:
café information, the song list and the drinks come from a SQLite database,
the code adds up the bill, the code writes song wishes into a queue,
and anything the tools cannot answer is handed to a human.
The panel on the right shows which tools were called and what they returned.

Run it with:  python app-gradio.py
"""
import csv
import json
import os
import re
import sqlite3
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv
from openai import OpenAI

HERE = Path(__file__).parent

CAFE_NAME = "Vinyl Music Café"
CAFE_NAME_ZH = "黑膠音樂咖啡館"

# ---------------------------------------------------------------------------
# 1. API setup
# ---------------------------------------------------------------------------
load_dotenv(HERE / ".env")
API_KEY = os.getenv("API_KEY")
API_ENDPOINT = os.getenv("LLM_API_URL")
CHAT_MODEL = os.getenv("MODEL_NAME")

if not (API_KEY and API_ENDPOINT and CHAT_MODEL):
    print("WARNING: API_KEY, LLM_API_URL or MODEL_NAME is missing. Check your .env file.")

client = OpenAI(api_key=API_KEY or "missing", base_url=API_ENDPOINT, timeout=60.0)


# ---------------------------------------------------------------------------
# 2. The café database: three tables from CSV files, plus the song queue (SQLite)
# ---------------------------------------------------------------------------
# The tables are built from the CSV files every time the program starts.
# The queue is empty at every start, so each test begins fresh.
DB_FILE = HERE / "cafe_app.db"

# check_same_thread=False because Gradio answers requests on worker threads.
# timeout=10: wait up to 10 seconds if another program is using the file.
connection = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
connection.row_factory = sqlite3.Row


def query(sql, params=()):
    """Run one SELECT and return all rows. A new cursor for every call is safe for many users."""
    return connection.execute(sql, params).fetchall()


def read_csv(name):
    with open(HERE / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


for table in ("cafe_info", "drinks", "songs", "queue"):
    connection.execute(f"DROP TABLE IF EXISTS {table}")

# Table 1: information about the café, one row per topic.
connection.execute("""
    CREATE TABLE cafe_info (
        topic   TEXT PRIMARY KEY,
        content TEXT NOT NULL          -- English and Traditional Chinese in one text
    )
""")
# Table 2: the drinks, one row per drink.
connection.execute("""
    CREATE TABLE drinks (
        item_id   INTEGER PRIMARY KEY,
        name      TEXT NOT NULL,
        name_zh   TEXT NOT NULL,       -- Chinese name, for questions in Chinese
        category  TEXT NOT NULL,
        price     INTEGER NOT NULL,    -- New Taiwan dollars
        available INTEGER NOT NULL
    )
""")
# Table 3: the songs a customer can wish for, one row per song.
connection.execute("""
    CREATE TABLE songs (
        song_id   INTEGER PRIMARY KEY,
        artist    TEXT NOT NULL,
        artist_zh TEXT NOT NULL,
        title     TEXT NOT NULL,       -- English translation of the title
        title_zh  TEXT NOT NULL,       -- the original title
        available INTEGER NOT NULL
    )
""")
# Table 4: the song queue. The tool request_song writes here. Staff can change it too.
connection.execute("""
    CREATE TABLE queue (
        queue_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        song_id      INTEGER NOT NULL,
        requested_at TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'waiting'
    )
""")

connection.executemany(
    "INSERT INTO cafe_info VALUES (?, ?)",
    [(r["topic"], r["content"]) for r in read_csv("cafe_info.csv")],
)
connection.executemany(
    "INSERT INTO drinks VALUES (?, ?, ?, ?, ?, ?)",
    [(int(r["item_id"]), r["name"], r["name_zh"], r["category"], int(r["price"]), int(r["available"]))
     for r in read_csv("drinks.csv")],
)
connection.executemany(
    "INSERT INTO songs VALUES (?, ?, ?, ?, ?, ?)",
    [(int(r["song_id"]), r["artist"], r["artist_zh"], r["title"], r["title_zh"], int(r["available"]))
     for r in read_csv("songs.csv")],
)
connection.commit()

CAFE_TOPICS = [r["topic"] for r in query("SELECT topic FROM cafe_info ORDER BY rowid")]
print(f"Ready: {len(CAFE_TOPICS)} café topics, "
      f"{query('SELECT COUNT(*) AS n FROM drinks')[0]['n']} drinks, "
      f"{query('SELECT COUNT(*) AS n FROM songs')[0]['n']} songs.")


# ---------------------------------------------------------------------------
# 3. The tools, as plain functions
# ---------------------------------------------------------------------------
# Tool 1: look up information.
def get_cafe_info(topic):
    """Return one piece of café information, such as the opening hours or the address."""
    rows = query("SELECT topic, content FROM cafe_info WHERE topic = ? COLLATE NOCASE", (topic.strip(),))
    if not rows:
        return {"found": False, "note": f"There is no café information about '{topic}'.",
                "topics_available": CAFE_TOPICS}
    return {"found": True, "topic": rows[0]["topic"], "content": rows[0]["content"]}


# --- Helpers to compare words ----------------------------------------------
CJK = re.compile(r"[\u4e00-\u9fff]")


def zh_chars(text):
    """Only the Chinese characters: '再見!我的愛人' -> '再見我的愛人'."""
    return "".join(CJK.findall(text))


# Used for the DRINKS.
ALIASES = {"cold": "iced", "ice": "iced"}       # what customers say -> words in the drink names
SKIP_ZH = set("杯份個的一二兩三四五六七八九十")      # measure words and numbers: "兩杯咖啡" = "咖啡"
ALL_DRINK_WORDS = {"drink", "beverage", "all", "menu", "everything"}
ALL_DRINK_ZH = ("飲料", "飲品", "菜單", "全部")


def words(text):
    """'Two Iced Teas!' -> {'two', 'iced', 'tea'};  '冰奶茶' -> {'冰', '奶', '茶'}.

    English: lower case, no punctuation, no plural s.  Chinese: each character is one word.
    """
    result = set()
    for word in re.sub(r"[^a-z0-9]+", " ", text.lower()).split():
        if len(word) > 3 and word.endswith("s"):
            word = word[:-1]                                  # teas -> tea
        result.update(ALIASES.get(word, word).split())
    result.update(ch for ch in text if CJK.match(ch) and ch not in SKIP_ZH)
    return result


def find_drinks(text):
    """Drinks that match the text: the English name, the Chinese name, or a category.

    Customers type "milk tea" or "冰奶茶", so we compare words, not whole strings.
    "drinks", "menu" or "飲料" means the whole drink list.
    """
    rows = query("SELECT name, name_zh, category, price, available FROM drinks ORDER BY item_id")
    text = text.strip()
    wanted = words(text)
    if not wanted or wanted <= ALL_DRINK_WORDS or any(z in text for z in ALL_DRINK_ZH):
        return rows
    exact = [r for r in rows if text.lower() in (r["name"].lower(), r["name_zh"])]
    if exact:                                                 # "coffee" is the Coffee, nothing else
        return exact
    return [r for r in rows
            if wanted <= words(r["name"]) | words(r["name_zh"]) or wanted == words(r["category"])]


# Used for the SONGS. Chinese titles must match as a whole piece (not character by character),
# otherwise "愛我" would find every title with 愛 or 我 in it.
STOP_EN = {"song", "songs", "by", "music", "singer", "artist", "some", "any", "play",
           "all", "everything", "list"}            # words that do not name a song: "all songs", "songs by ..."
ALL_SONG_ZH = {"全部", "所有", "歌單", "歌曲", "所有歌曲", "全部歌曲", "歌"}   # Chinese for "all songs"
FILLER_ZH = ("的歌曲", "的歌", "歌曲", "的", "歌", "首")


def en_words(text):
    """"Don't Fall" -> {'dont', 'fall'}; "Teng's" -> {'teng'}: lower case, no apostrophes, no punctuation."""
    text = re.sub(r"['’]s\b", "", text.lower()).replace("'", "").replace("’", "")
    return set(re.sub(r"[^a-z0-9]+", " ", text).split())


def zh_match(query_zh, row):
    """Does a Chinese query fit this song? It may be a part of the singer, a part of the title,
    or the singer plus the title ('鄧麗君的在水一方')."""
    artist, title = zh_chars(row["artist_zh"]), zh_chars(row["title_zh"])
    if query_zh in artist or query_zh in title:
        return True
    if artist and artist in query_zh:
        rest = query_zh.replace(artist, "")
        for filler in FILLER_ZH:
            rest = rest.replace(filler, "")
        return rest == "" or rest in title
    return False


def find_songs(text):
    """Songs that match the text: singer or title, in English or Chinese. 'all' lists every song."""
    rows = query("SELECT * FROM songs ORDER BY song_id")
    wanted_en = en_words(CJK.sub(" ", text)) - STOP_EN       # the English part of the query
    wanted_zh = zh_chars(text)                                 # the Chinese part of the query
    if not wanted_en and (not wanted_zh or wanted_zh in ALL_SONG_ZH):
        return rows                                            # nothing specific asked: every song
    hits = []
    for row in rows:
        have_en = en_words(row["artist"]) | en_words(row["title"])
        if wanted_en <= have_en and (zh_match(wanted_zh, row) if wanted_zh else True):
            hits.append(row)
    return hits


def song_dict(row):
    return {"song_id": row["song_id"], "artist": row["artist"], "artist_zh": row["artist_zh"],
            "title": row["title"], "title_zh": row["title_zh"], "available": bool(row["available"])}


# Tool 2: look up records (songs).
def search_songs(keyword):
    """Find songs by singer or title ('Teresa Teng', '鄧麗君', '在水一方'). 'all' lists every song."""
    rows = find_songs(keyword)
    if not rows:
        return {"found": 0, "songs": [],
                "note": "No song or singer on our list matches this keyword. "
                        "Tell the customer, then use transfer_to_human."}
    return {"found": len(rows), "songs": [song_dict(r) for r in rows]}


# Tool 3 and 4: look up records (drinks).
def search_drinks(keyword):
    """Find drinks by part of a name ('milk tea', '奶茶'), by a word ('tea') or list all of them."""
    rows = find_drinks(keyword)
    if not rows:
        return {"found": 0, "items": [], "note": "No drink matches this keyword."}
    return {"found": len(rows),
            "items": [{"name": r["name"], "name_zh": r["name_zh"], "category": r["category"],
                       "price": r["price"], "available": bool(r["available"])} for r in rows]}


def get_price(item_name):
    """Return the price of one drink. The name does not have to be exact, and may be Chinese."""
    rows = find_drinks(item_name)
    if len(rows) == 1:
        r = rows[0]
        return {"found": True, "name": r["name"], "name_zh": r["name_zh"], "price": r["price"],
                "available": bool(r["available"])}
    if len(rows) > 1:
        return {"found": False, "note": f"'{item_name}' matches several drinks. Ask the customer which one.",
                "candidates": [f"{r['name']} ({r['name_zh']})" for r in rows]}
    return {"found": False, "note": f"'{item_name}' is not on our drink list."}


# Tool 5: calculate total.
def calculate_total(items):
    """Add up a drink order. The prices come from the database and the maths happens here.

    No total unless every drink is clear: a total of only some drinks is a wrong bill.
    """
    lines, problems, total = [], [], 0
    for entry in items:
        found = get_price(entry["name"])
        if not found["found"]:
            problems.append({"item": entry["name"], **found})   # not on the list, or several matches
            continue
        if not found["available"]:
            problems.append({"item": entry["name"], **found,
                             "note": f"{found['name']} is not available today."})
            continue
        quantity = int(entry.get("quantity", 1))
        subtotal = round(found["price"] * quantity, 2)
        lines.append({"name": found["name"], "name_zh": found["name_zh"], "unit_price": found["price"],
                      "quantity": quantity, "subtotal": subtotal})
        total += subtotal
    if problems:
        return {"total": None, "problems": problems,
                "note": "No total yet. Ask the customer about these drinks, then call calculate_total again."}
    return {"lines": lines, "total": round(total, 2)}


# Tool 6: change data. Only after the customer has said yes.
def request_song(song_id, confirmed=False):
    """Put a song into the queue. Nothing is written until confirmed is true."""
    if isinstance(confirmed, str):                       # some models send "true" as text
        confirmed = confirmed.strip().lower() == "true"
    rows = query("SELECT * FROM songs WHERE song_id = ?", (int(song_id),))
    if not rows:
        return {"queued": False, "note": "There is no song with this song_id. Use search_songs first."}
    song = song_dict(rows[0])
    if not song["available"]:
        return {"queued": False, "song": song, "note": "This song is not available today. Tell the customer."}

    waiting = query("SELECT queue_id FROM queue WHERE song_id = ? AND status = 'waiting'", (song["song_id"],))
    if waiting:
        position = query("SELECT COUNT(*) AS n FROM queue WHERE status = 'waiting' AND queue_id <= ?",
                         (waiting[0]["queue_id"],))[0]["n"]
        return {"queued": False, "already_in_queue": True, "position": position, "song": song,
                "note": "This song is already waiting in the queue. Tell the customer its position."}

    if not confirmed:
        return {"queued": False, "song": song,
                "note": ("Nothing was written. Show this song to the customer and ask if you should put it "
                         "in the queue. Only after the customer says yes, call request_song again "
                         "with confirmed=true.")}

    cursor = connection.execute(
        "INSERT INTO queue (song_id, requested_at) VALUES (?, datetime('now', 'localtime'))",
        (song["song_id"],))
    connection.commit()
    position = query("SELECT COUNT(*) AS n FROM queue WHERE status = 'waiting' AND queue_id <= ?",
                     (cursor.lastrowid,))[0]["n"]
    return {"queued": True, "position": position, "song": song,
            "note": "The song is in the queue. Tell the customer its position. It plays after all earlier songs."}


# Tool 7: hand over to a person.
def transfer_to_human():
    """Hand the conversation to a person."""
    rows = query("SELECT content FROM cafe_info WHERE topic = 'phone'")
    contact = rows[0]["content"] if rows else "Please ask our staff in the café."
    return {"message": f"Please ask our staff in the café, or call us. {contact}",
            "note": ("The hand-over is done. First tell the customer in one short sentence why: "
                     "you do not have that information, the song is not on our list, or the request needs a person. "
                     "Then give this message in the customer's language. Do not say that you will connect them "
                     "or pass them on: you cannot. Do not call this tool again.")}


TOOL_FUNCTIONS = {
    "get_cafe_info": get_cafe_info,
    "search_songs": search_songs,
    "search_drinks": search_drinks,
    "get_price": get_price,
    "calculate_total": calculate_total,
    "request_song": request_song,
    "transfer_to_human": transfer_to_human,
}

# What the model sees: names, purpose, arguments. Most "it did not use my tool"
# problems are really "my description did not say when to use it".
TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_cafe_info",
        "description": ("Get one piece of café information: opening hours (營業時間), address (地址), phone (電話), "
                        "payment (付款方式), buying records (購買唱片), delivery (外送), parking (停車), "
                        "how song requests work (點歌方式), Wi-Fi or reservation (訂位)."),
        "parameters": {"type": "object",
                       "properties": {"topic": {"type": "string", "enum": CAFE_TOPICS}},
                       "required": ["topic"]}}},
    {"type": "function", "function": {
        "name": "search_songs",
        "description": ("Search our song list by singer or title, in English or Chinese, for example "
                        "'Teresa Teng', '鄧麗君' or '在水一方'. Use 'all' to list every song. "
                        "Use the customer's own words and do not translate them."),
        "parameters": {"type": "object",
                       "properties": {"keyword": {"type": "string"}},
                       "required": ["keyword"]}}},
    {"type": "function", "function": {
        "name": "search_drinks",
        "description": ("Search the drink list by keyword (water, tea, milk tea, coffee) or list all drinks with 'all'. "
                        "Use it when the customer asks what drinks we have, or names a drink only partly."),
        "parameters": {"type": "object",
                       "properties": {"keyword": {"type": "string"}},
                       "required": ["keyword"]}}},
    {"type": "function", "function": {
        "name": "get_price",
        "description": ("Get the price of ONE drink, and whether it is available today. "
                        "Use the customer's own words and do not translate them, for example 'coffee' or '冰奶茶'."),
        "parameters": {"type": "object",
                       "properties": {"item_name": {"type": "string"}},
                       "required": ["item_name"]}}},
    {"type": "function", "function": {
        "name": "calculate_total",
        "description": ("Add up an order of several drinks. Always use this for any total. "
                        "Never add prices yourself."),
        "parameters": {"type": "object",
                       "properties": {"items": {"type": "array", "items": {
                           "type": "object",
                           "properties": {"name": {"type": "string",
                                                   "description": "The drink in the customer's own words. Do not translate it."},
                                          "quantity": {"type": "integer"}},
                           "required": ["name", "quantity"]}}},
                       "required": ["items"]}}},
    {"type": "function", "function": {
        "name": "request_song",
        "description": ("Put a song into the play queue. First find the song with search_songs and use its song_id. "
                        "Set confirmed=true ONLY when the customer has said yes to this song in their last message. "
                        "With confirmed=false nothing is written."),
        "parameters": {"type": "object",
                       "properties": {"song_id": {"type": "integer"},
                                      "confirmed": {"type": "boolean"}},
                       "required": ["song_id", "confirmed"]}}},
    {"type": "function", "function": {
        "name": "transfer_to_human",
        "description": ("Use this when the customer wants a song that is not on our list, wants to buy a record, "
                        "makes a complaint, asks for a manager, or asks about the café something no other tool can answer."),
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]

# The rules are sorted into four kinds:
#   Grounding: 1, 2   Routing: 3-8   Scope: 9   Language and format: 10
SYSTEM_PROMPT = f"""
You are the customer service agent of {CAFE_NAME} ({CAFE_NAME_ZH}), a small music café in Kaohsiung with many vinyl records.
Customers can wish for songs from our song list, and they can order drinks. All prices are in New Taiwan dollars. Write them like NT$70.

RULES
1. Answer only from tool results. Never answer from memory, and never invent anything.
2. If a tool reports that nothing was found, say so plainly. Do not guess. If a drink or a song is not available today, say so.
3. Café questions (opening hours, address, phone, payment, delivery, parking, Wi-Fi, reservation, how song requests work, buying records) -> get_cafe_info.
4. Questions about songs or singers -> search_songs.
5. Questions about drinks and prices -> search_drinks or get_price.
6. Use calculate_total for every total. Never add prices yourself.
7. Song wishes: first find the song with search_songs. Then show the song to the customer and ask if you should put it in the queue.
   Only after the customer says yes, call request_song with confirmed=true. Never say that a song is in the queue unless request_song returned queued=true.
8. A song that is not on our list, a request to buy a record, a complaint, a request for a manager, or any café question no tool can answer -> transfer_to_human.
9. Only talk about this café. For other topics (such as the weather, directions to other places or homework), do not use any tool: politely say that you can only help with questions about the café, its songs and its drinks.
10. Answer in the customer's language: English, or Traditional Chinese (as used in Taiwan) if the customer writes in Chinese.
   Write drink names and song titles in the language of the answer: in English answers 'English title (中文原名)', in Chinese answers the Chinese names (name_zh, title_zh, artist_zh).
   Be polite and short: three sentences or less, except when you list songs or drinks.
"""

MAX_STEPS = 6          # a confused model must not loop forever


# ---------------------------------------------------------------------------
# The agent loop
# ---------------------------------------------------------------------------
def as_text(content):
    """Gradio 6 may give the text of a message as a list of blocks, e.g. [{'type': 'text', 'text': 'Hi'}].
    The model and re.search need a plain string, so we turn every message into one."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return str(content)


def known_chinese_names():
    """Chinese names from our own data (songs, singers, drinks). Longest first."""
    names = set()
    for r in query("SELECT artist_zh, title_zh FROM songs"):
        names.update([r["artist_zh"], r["title_zh"], zh_chars(r["title_zh"])])
    names.update(r["name_zh"] for r in query("SELECT name_zh FROM drinks"))
    return sorted((n for n in names if n), key=len, reverse=True)


KNOWN_ZH = known_chinese_names()


def language_note(text):
    """The program, not the model, decides the reply language.

    We first remove the Chinese names from our own data, so that an English sentence like
    "Please play 在水一方" is still answered in English. If Chinese is left, or if the message
    is only a Chinese name, the reply is Traditional Chinese.
    """
    rest = text
    for name in KNOWN_ZH:
        rest = rest.replace(name, " ")
    only_names = not CJK.search(rest) and not re.search(r"[A-Za-z]", rest)
    chinese = bool(CJK.search(rest)) or (only_names and bool(CJK.search(text)))
    if chinese:
        return [{"role": "system", "content": "The customer wrote in Chinese. Reply in Traditional Chinese."}]
    return [{"role": "system", "content": "The customer wrote in English. Reply in English."}]


def run_agent(chat_history):
    """Answer the last question in chat_history. Returns (answer, trace)."""
    history = [{"role": m["role"], "content": as_text(m["content"])} for m in chat_history]
    messages = ([{"role": "system", "content": SYSTEM_PROMPT}] + history[:-1]
                + language_note(history[-1]["content"]) + history[-1:])
    trace = []

    for step in range(MAX_STEPS):
        response = client.chat.completions.create(
            model=CHAT_MODEL, messages=messages, tools=TOOL_SPECS, temperature=0.2,
        )
        message = response.choices[0].message

        if not message.tool_calls:                     # plain text: the turn is finished
            return (message.content or "").strip(), trace

#        messages.append({
 #           "role": "assistant",
  #          "content": message.content or "",
   #         "tool_calls": [{"id": c.id, "type": "function",
    #                        "function": {"name": c.function.name,
     #                                    "arguments": c.function.arguments}}
      #                     for c in message.tool_calls],
       # })

        messages.append(message)

        for call in message.tool_calls:
            name = call.function.name
            function = TOOL_FUNCTIONS.get(name)        # only our own names are allowed
            try:
                arguments = json.loads(call.function.arguments or "{}")
                result = function(**arguments) if function else {"error": f"unknown tool: {name}"}
            except Exception as error:
                arguments = {}
                result = {"error": f"{type(error).__name__}: {error}"}

            trace.append({"step": step + 1, "tool": name,
                          "arguments": arguments, "result": result})
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, ensure_ascii=False)})

    return "Sorry, I could not finish that. Please ask our staff in the café. 很抱歉，請洽現場店員。", trace


# ---------------------------------------------------------------------------
# 4. The Gradio page
# ---------------------------------------------------------------------------
GREETING = (f"Hello! I am the online agent of {CAFE_NAME}. "
            "Ask me about our opening hours, our songs, our drinks, or put a song on the queue.\n\n"
            f"您好！我是{CAFE_NAME_ZH}的線上客服，可以問營業資訊、點歌、點飲料，用中文或英文都可以。")

EMPTY_TRACE = "### Tools used\n\n(nothing yet)"

# Sample questions, grouped by what they test.
SAMPLE_GROUPS = {
    "Café information (get_cafe_info)": [
        "What time do you open?",
        "Where is the café?",
        "Can I pay by credit card?",          # we take only cash and LINE Pay: the answer is "no"
        "Do you deliver?",
        "Is there parking?",
        "How does the song wish work?",
        "Do you have Wi-Fi?",
    ],
    "Songs (search_songs)": [
        "Which songs do you have?",
        "Do you have songs by Teresa Teng?",
        "Do you have the song Brave by Mayday?",
        "Is 在水一方 on your list?",
    ],
    "Drinks and prices (search_drinks, get_price)": [
        "What drinks do you have?",
        "How much is a coffee?",
        "How much is a green tea?",           # two kinds: the agent should ask which one
        "Do you have iced milk tea?",
    ],
    "The bill (calculate_total)": [
        "I would like two coffees, one iced milk tea and a water. How much is that?",
        "One tea and one coffee, please. How much?",       # "tea" is not clear: no total until the customer says which
        "Four of us, each gets a plum green tea. How much?",
    ],
    "A song wish (request_song): click these in order, without Clear": [
        "I would like to hear 在水一方, please.",
        "Yes, please put it in the queue.",
    ],
    "Not on the list, records, complaint, not our job": [
        "Can you play Bohemian Rhapsody by Queen?",        # not on the list: transfer_to_human
        "I want to buy a record.",
        "My coffee was cold and I want to speak to the manager.",
        "Do you sell cake?",
        "Who is the owner of the café?",                   # not in the data
        "What is the weather in Kaohsiung tomorrow?",      # not our job
    ],
    "A conversation: click these in order, without Clear": [
        "How much is a coffee?",
        "And two of them with an iced milk tea?",
        "Great. What time do you close tonight?",
    ],
}

SAMPLE_GROUPS_ZH = {
    "店家資訊（get_cafe_info）": [
        "請問你們幾點開門？",
        "店在哪裡？",
        "可以刷信用卡嗎？",                       # we take only cash and LINE Pay
        "你們有外送嗎？",
        "附近有停車位嗎？",
        "怎麼點歌？",
        "有 Wi-Fi 嗎？",
    ],
    "歌曲（search_songs）": [
        "有哪些歌可以點？",
        "有鄧麗君的歌嗎？",
        "有五月天的勇敢嗎？",
        "有蕭煌奇的歌嗎？",
    ],
    "飲料與價格（search_drinks、get_price）": [
        "有什麼飲料？",
        "咖啡多少錢？",
        "綠茶多少錢？",                           # two kinds: the agent should ask which one
        "有冰奶茶嗎？",
    ],
    "點餐算錢（calculate_total）": [
        "我要兩杯咖啡、一杯冰奶茶和一杯水，多少錢？",
        "一杯茶和一杯咖啡多少錢？",                 # "茶" is not clear: no total until the customer says which
        "我們四個人，每人一杯梅子綠茶，多少錢？",
    ],
    "點歌（request_song）：請依序點選，中間不要按 Clear": [
        "我想聽在水一方。",
        "好，請幫我加入播放佇列。",
    ],
    "歌單沒有、買唱片、客訴、不是客服的事": [
        "可以播 Queen 的 Bohemian Rhapsody 嗎？",    # not on the list: transfer_to_human
        "我想買黑膠唱片。",
        "我的咖啡是冷的，我要找店長。",
        "有賣蛋糕嗎？",
        "老闆是誰？",
        "明天高雄天氣如何？",
    ],
    "多輪對話：依序點選，中間不要按 Clear": [
        "咖啡多少錢？",
        "那兩杯再加一杯冰奶茶呢？",
        "好，那你們今天幾點關門？",
    ],
}


def format_trace(trace):
    """Show the tool calls of the last answer, so the choice is visible."""
    if not trace:
        return "### Tools used\n\n(no tool was needed for this answer)"
    lines = ["### Tools used", ""]
    for entry in trace:
        lines.append(f"**{entry['step']}. {entry['tool']}**")
        lines.append("")
        lines.append(f"arguments: `{json.dumps(entry['arguments'], ensure_ascii=False)[:160]}`")
        lines.append("")
        lines.append(f"result: `{json.dumps(entry['result'], ensure_ascii=False)[:400]}`")
        lines.append("")
    return "\n".join(lines)


def start_chat():
    """The first state of the chat box: the greeting only.

    Used at start-up and when the visitor clears the chat, so the agent speaks first.
    """
    return [{"role": "assistant", "content": GREETING}]


def add_user_message(user_text, chat_history):
    """Show the question at once and empty the input box."""
    user_text = user_text.strip()
    if not user_text:
        return "", chat_history
    return "", chat_history + [{"role": "user", "content": user_text}]


def answer_question(chat_history):
    """Run the agent on the current conversation."""
    if not chat_history or chat_history[-1]["role"] != "user":
        return chat_history, EMPTY_TRACE

    try:
        answer, trace = run_agent(chat_history)
        panel = format_trace(trace)
    except Exception as error:
        answer = f"(system message) API error: {error}"
        panel = "### Tools used\n\n(the call failed)"

    return chat_history + [{"role": "assistant", "content": answer}], panel


with gr.Blocks(title=f"{CAFE_NAME} Agent") as demo:
    gr.Markdown(
        f"## {CAFE_NAME}（{CAFE_NAME_ZH}）: customer service agent\n"
        "Every question goes to the agent. Café information, songs and prices come from a SQLite database, "
        "the code adds up the bill and puts your song wish into the queue, and anything else goes to a human. "
        "The panel on the right shows which tool was used."
    )

    with gr.Row():
        with gr.Column(scale=3):
            chatbot = gr.Chatbot(value=start_chat(), height=430, label="Agent")
            with gr.Row():
                user_box = gr.Textbox(placeholder="Ask about the café, a song or a drink... 歡迎用中文提問",
                                      show_label=False, scale=8)
                send_button = gr.Button("Send", variant="primary", scale=1)
                clear_button = gr.Button("Clear", scale=1)

            gr.Markdown("**Sample questions** (click one, then press Send)")
            with gr.Tabs():
                with gr.Tab("English"):
                    for group, questions in SAMPLE_GROUPS.items():
                        gr.Examples(examples=questions, inputs=user_box, label=group)
                with gr.Tab("中文"):
                    for group, questions in SAMPLE_GROUPS_ZH.items():
                        gr.Examples(examples=questions, inputs=user_box, label=group)

        with gr.Column(scale=2):
            trace_panel = gr.Markdown(EMPTY_TRACE)

    # One turn: show the question, then let the agent work.
    send_button.click(
        add_user_message, [user_box, chatbot], [user_box, chatbot]
    ).then(
        answer_question, chatbot, [chatbot, trace_panel]
    )

    user_box.submit(
        add_user_message, [user_box, chatbot], [user_box, chatbot]
    ).then(
        answer_question, chatbot, [chatbot, trace_panel]
    )

    # Clearing brings the greeting back and empties the trace panel.
    clear_button.click(start_chat, outputs=chatbot).then(
        lambda: ("", EMPTY_TRACE), outputs=[user_box, trace_panel]
    )


if __name__ == "__main__":
    demo.launch()
