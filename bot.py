"""Otaku Radar - a Telegram bot to discover anime, series and movies,
find legal places to watch them, and get new-episode alerts.

Data sources: AniList (anime), TMDB (series/movies, streaming availability),
Internet Archive (public-domain films). No piracy sites are used.
"""
import html
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
TMDB_KEY = os.environ["TMDB_KEY"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DB_PATH = os.environ.get("DB_PATH", "otaku_radar.db")
DEFAULT_COUNTRY = os.environ.get("DEFAULT_COUNTRY", "NG")

TMDB = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"
ANILIST = "https://graphql.anilist.co"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("otaku-radar")
http: httpx.AsyncClient = None  # created in post_init
e = html.escape


# ---------------------------------------------------------------- database
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY, first_name TEXT,
                country TEXT, joined INTEGER);
            CREATE TABLE IF NOT EXISTS follows(
                user_id INTEGER, source TEXT, media_id INTEGER, title TEXT,
                PRIMARY KEY(user_id, source, media_id));
            CREATE TABLE IF NOT EXISTS notified(
                source TEXT, media_id INTEGER, key TEXT,
                PRIMARY KEY(source, media_id, key));
            """
        )


def get_country(user_id):
    with db() as c:
        r = c.execute("SELECT country FROM users WHERE user_id=?", (user_id,)).fetchone()
    return (r[0] if r and r[0] else DEFAULT_COUNTRY).upper()


# ------------------------------------------------------------- tiny helpers
_last_call = {}


def throttled(user_id, gap=1.2):
    now = time.time()
    if now - _last_call.get(user_id, 0) < gap:
        return True
    _last_call[user_id] = now
    return False


async def anilist(query, variables):
    r = await http.post(ANILIST, json={"query": query, "variables": variables})
    r.raise_for_status()
    return r.json()["data"]


async def tmdb(path, **params):
    r = await http.get(f"{TMDB}{path}", params={"api_key": TMDB_KEY, **params})
    r.raise_for_status()
    return r.json()


def short(text, n=450):
    text = (text or "No synopsis available.").replace("<br>", " ").replace("<br/>", " ")
    text = html.unescape(text)
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "..."


# ---------------------------------------------------------------- content
WELCOME = (
    "👋 <b>Hey {name}, welcome to Otaku Radar!</b>\n\n"
    "Find any anime, series or movie, see where to watch it legally "
    "(including free options), and get an alert the moment a new episode drops.\n\n"
    "Just type a title to search, or use the buttons below."
)

TUTORIAL = [
    "🔎 <b>Step 1 of 3 - Search</b>\n\nType any title, like <code>Naruto</code> or "
    "<code>Breaking Bad</code>, or use /search. Tap a result to see its card.",
    "➕ <b>Step 2 of 3 - Follow</b>\n\nOn any anime or series card, tap "
    "<b>Follow</b>. Your list lives in /watchlist.",
    "🔔 <b>Step 3 of 3 - Get alerts</b>\n\nI check for new episodes every 30 minutes "
    "and message you when a followed show airs. Tap <b>Where to watch</b> for "
    "legal and free streaming links. Set your country with /settings so the "
    "results match where you live.",
]

HELP = (
    "<b>Commands</b>\n"
    "/search &lt;title&gt; - find anime, series, movies\n"
    "/trending - what's popular now\n"
    "/follow - tap Follow on any card\n"
    "/watchlist - shows you follow\n"
    "/archive &lt;title&gt; - free public-domain films\n"
    "/downloads &lt;title&gt; - legal free download sources\n"
    "/torrents &lt;title&gt; - legal torrents (public domain)\n"
    "/settings - choose your country\n"
    "/tutorial - quick tour\n"
    "/help - this message\n\n"
    "Tip: you can also just type a title."
)

COUNTRIES = ["NG", "US", "GB", "CA", "GH", "KE", "ZA", "IN", "PH", "AU", "DE", "FR"]


def home_buttons():
    return Markup(
        [
            [Btn("🔥 Trending", callback_data="menu:trending"), Btn("📋 Watchlist", callback_data="menu:watchlist")],
            [Btn("🎓 Quick tour", callback_data="tut:0"), Btn("⚙️ Settings", callback_data="menu:settings")],
            [Btn("❓ Help", callback_data="menu:help")],
        ]
    )


# --------------------------------------------------------------- commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO users(user_id, first_name, country, joined) VALUES(?,?,?,?)",
            (u.id, u.first_name, DEFAULT_COUNTRY, int(time.time())),
        )
    await update.message.reply_text(
        WELCOME.format(name=e(u.first_name or "there")),
        parse_mode=ParseMode.HTML,
        reply_markup=home_buttons(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def tutorial_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        TUTORIAL[0], parse_mode=ParseMode.HTML, reply_markup=tut_buttons(0)
    )


def tut_buttons(i):
    row = []
    if i > 0:
        row.append(Btn("⬅️ Back", callback_data=f"tut:{i-1}"))
    if i < len(TUTORIAL) - 1:
        row.append(Btn("Next ➡️", callback_data=f"tut:{i+1}"))
    else:
        row.append(Btn("✅ Done", callback_data="tut:done"))
    return Markup([row, [Btn("Skip", callback_data="tut:done")]])


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_settings(update.message.chat_id, update.effective_user.id, context)


async def send_settings(chat_id, user_id, context):
    cur = get_country(user_id)
    rows, row = [], []
    for code in COUNTRIES:
        row.append(Btn(("✅ " if code == cur else "") + code, callback_data=f"country:{code}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await context.bot.send_message(
        chat_id,
        f"⚙️ <b>Settings</b>\nCountry: <b>{cur}</b> (used for streaming links)\n"
        "Not listed? Send <code>/country XX</code> with your 2-letter code.",
        parse_mode=ParseMode.HTML,
        reply_markup=Markup(rows),
    )


async def country_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args[0]) != 2:
        await update.message.reply_text("Usage: /country NG")
        return
    code = context.args[0].upper()
    set_country(update.effective_user.id, update.effective_user.first_name, code)
    await update.message.reply_text(f"Country set to {code} ✅")


def set_country(user_id, name, code):
    with db() as c:
        c.execute(
            "INSERT INTO users(user_id, first_name, country, joined) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET country=excluded.country",
            (user_id, name, code, int(time.time())),
        )


# ----------------------------------------------------------------- search
SEARCH_Q = """
query ($s: String) { Page(perPage: 4) {
  media(search: $s, type: ANIME, sort: SEARCH_MATCH) {
    id seasonYear title { romaji english } } } }
"""


async def do_search(chat_id, query, context):
    query = query.strip()
    if not query:
        await context.bot.send_message(chat_id, "Type a title, e.g. /search Attack on Titan")
        return
    try:
        anime = (await anilist(SEARCH_Q, {"s": query}))["Page"]["media"]
        tm = (await tmdb("/search/multi", query=query)).get("results", [])
    except Exception:
        log.exception("search failed")
        await context.bot.send_message(chat_id, "😕 Search is having trouble right now. Try again in a minute.")
        return

    rows = []
    for m in anime[:3]:
        t = m["title"]["english"] or m["title"]["romaji"]
        rows.append([Btn(f"🎌 {t} ({m.get('seasonYear') or '?'})"[:60], callback_data=f"show:anime:{m['id']}")])
    count = 0
    for r in tm:
        kind = r.get("media_type")
        if kind not in ("tv", "movie"):
            continue
        t = r.get("name") or r.get("title")
        year = (r.get("first_air_date") or r.get("release_date") or "????")[:4]
        icon = "📺" if kind == "tv" else "🎬"
        rows.append([Btn(f"{icon} {t} ({year})"[:60], callback_data=f"show:{kind}:{r['id']}")])
        count += 1
        if count == 4:
            break
    if not rows:
        await context.bot.send_message(chat_id, f"No results for “{e(query)}”. Check the spelling?")
        return
    await context.bot.send_message(chat_id, "Here's what I found. Tap one:", reply_markup=Markup(rows))


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if throttled(update.effective_user.id):
        return
    await do_search(update.message.chat_id, " ".join(context.args), context)


async def text_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if throttled(update.effective_user.id):
        return
    await do_search(update.message.chat_id, update.message.text, context)


# ------------------------------------------------------------------ cards
ANIME_Q = """
query ($id: Int) { Media(id: $id, type: ANIME) {
  id title { romaji english } description(asHtml: false) episodes status
  averageScore genres seasonYear coverImage { large } siteUrl
  nextAiringEpisode { episode airingAt } } }
"""


async def get_details(kind, mid):
    if kind == "anime":
        m = (await anilist(ANIME_Q, {"id": mid}))["Media"]
        title = m["title"]["english"] or m["title"]["romaji"]
        nxt = m.get("nextAiringEpisode")
        nxt_txt = ""
        if nxt:
            nxt_txt = "\n⏭ Next: ep " + str(nxt["episode"]) + " on " + time.strftime(
                "%a %d %b, %H:%M UTC", time.gmtime(nxt["airingAt"]))
        cap = (
            f"<b>{e(title)}</b> ({m.get('seasonYear') or '?'})\n"
            f"⭐ {m.get('averageScore') or '?'}/100 · {m.get('episodes') or '?'} eps · "
            f"{e(str(m.get('status')).title())}\n"
            f"🏷 {e(', '.join(m.get('genres') or [])) or '-'}{nxt_txt}\n\n"
            f"{e(short(m.get('description')))}"
        )
        return title, cap, m["coverImage"]["large"], True
    d = await tmdb(f"/{kind}/{mid}")
    title = d.get("name") or d.get("title")
    year = (d.get("first_air_date") or d.get("release_date") or "????")[:4]
    extra = ""
    if kind == "tv":
        extra = f" · {d.get('number_of_seasons', '?')} seasons"
    else:
        extra = f" · {d.get('runtime', '?')} min"
    cap = (
        f"<b>{e(title)}</b> ({year})\n"
        f"⭐ {round(d.get('vote_average', 0), 1)}/10{extra}\n"
        f"🏷 {e(', '.join(g['name'] for g in d.get('genres', []))) or '-'}\n\n"
        f"{e(short(d.get('overview')))}"
    )
    poster = IMG + d["poster_path"] if d.get("poster_path") else None
    return title, cap, poster, kind == "tv"


def is_following(user_id, kind, mid):
    with db() as c:
        return bool(c.execute(
            "SELECT 1 FROM follows WHERE user_id=? AND source=? AND media_id=?",
            (user_id, kind, mid)).fetchone())


async def send_card(chat_id, user_id, kind, mid, context):
    try:
        title, cap, poster, followable = await get_details(kind, mid)
    except Exception:
        log.exception("details failed")
        await context.bot.send_message(chat_id, "😕 Couldn't load that title. Try again soon.")
        return
    rows = [[Btn("📍 Where to watch", callback_data=f"watch:{kind}:{mid}")]]
    if followable:
        if is_following(user_id, kind, mid):
            rows.append([Btn("✅ Following (tap to unfollow)", callback_data=f"unfollow:{kind}:{mid}")])
        else:
            rows.append([Btn("➕ Follow for alerts", callback_data=f"follow:{kind}:{mid}")])
    markup = Markup(rows)
    if poster:
        try:
            await context.bot.send_photo(chat_id, poster, caption=cap[:1024],
                                         parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        except Exception:
            log.warning("photo failed, sending text")
    await context.bot.send_message(chat_id, cap[:4000], parse_mode=ParseMode.HTML, reply_markup=markup)


# ----------------------------------------------------------- where to watch
LINKS_Q = """
query ($id: Int) { Media(id: $id, type: ANIME) {
  title { romaji english } siteUrl externalLinks { site url type } } }
"""


async def where_to_watch(chat_id, user_id, kind, mid, context):
    country = get_country(user_id)
    try:
        if kind == "anime":
            m = (await anilist(LINKS_Q, {"id": mid}))["Media"]
            links = [x for x in m["externalLinks"] if x.get("type") == "STREAMING"]
            lines = [f"• <a href=\"{e(x['url'])}\">{e(x['site'])}</a>" for x in links]
            text = "📍 <b>Legal streaming links</b>\n" + (
                "\n".join(lines) if lines else "No official streaming links listed yet.")
            text += f"\n\nMore info: <a href=\"{e(m['siteUrl'])}\">AniList page</a>"
        else:
            d = await tmdb(f"/{kind}/{mid}/watch/providers")
            res = d.get("results", {}).get(country)
            if not res:
                text = f"📍 No streaming data for <b>{country}</b>. Change country in /settings."
            else:
                def names(k):
                    return ", ".join(p["provider_name"] for p in res.get(k, [])) or None
                parts = [f"📍 <b>Where to watch in {country}</b>"]
                for label, k in (("🆓 Free", "free"), ("📢 Free with ads", "ads"),
                                 ("💳 Subscription", "flatrate"), ("🛒 Rent", "rent"), ("🛒 Buy", "buy")):
                    n = names(k)
                    if n:
                        parts.append(f"{label}: {e(n)}")
                if len(parts) == 1:
                    parts.append("Not available on any listed service.")
                if res.get("link"):
                    parts.append(f"\n<a href=\"{e(res['link'])}\">Open full list</a>")
                parts.append("\n<i>Streaming data by JustWatch via TMDB.</i>")
                text = "\n".join(parts)
    except Exception:
        log.exception("watch failed")
        text = "😕 Couldn't fetch streaming info right now."
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)


# ----------------------------------------------------------- follow / list
async def watchlist(chat_id, user_id, context):
    with db() as c:
        rows = c.execute(
            "SELECT source, media_id, title FROM follows WHERE user_id=? ORDER BY title",
            (user_id,)).fetchall()
    if not rows:
        await context.bot.send_message(chat_id, "Your watchlist is empty. Search a title and tap Follow ➕")
        return
    buttons = [[Btn(f"{'🎌' if s == 'anime' else '📺'} {t}"[:60], callback_data=f"show:{s}:{i}")]
               for s, i, t in rows]
    await context.bot.send_message(chat_id, f"📋 <b>You follow {len(rows)} show(s)</b>",
                                   parse_mode=ParseMode.HTML, reply_markup=Markup(buttons))


async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await watchlist(update.message.chat_id, update.effective_user.id, context)


async def trending(chat_id, context):
    try:
        d = await tmdb("/trending/all/week")
        a = (await anilist(
            "query { Page(perPage: 4) { media(type: ANIME, sort: TRENDING_DESC) "
            "{ id seasonYear title { romaji english } } } }", {}))["Page"]["media"]
    except Exception:
        log.exception("trending failed")
        await context.bot.send_message(chat_id, "😕 Couldn't load trending right now.")
        return
    rows = [[Btn(f"🎌 {m['title']['english'] or m['title']['romaji']}"[:60],
                 callback_data=f"show:anime:{m['id']}")] for m in a]
    n = 0
    for r in d.get("results", []):
        if r.get("media_type") in ("tv", "movie"):
            t = r.get("name") or r.get("title")
            icon = "📺" if r["media_type"] == "tv" else "🎬"
            rows.append([Btn(f"{icon} {t}"[:60], callback_data=f"show:{r['media_type']}:{r['id']}")])
            n += 1
            if n == 4:
                break
    await context.bot.send_message(chat_id, "🔥 <b>Trending this week</b>",
                                   parse_mode=ParseMode.HTML, reply_markup=Markup(rows))


async def trending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trending(update.message.chat_id, context)


# ----------------------------------------------------------- internet archive
async def archive_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if throttled(update.effective_user.id):
        return
    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("Usage: /archive <title>\nSearches free public-domain films and cartoons.")
        return
    params = {
        "q": f'title:({q}) AND mediatype:movies AND collection:(feature_films OR classic_tv OR animationandcartoons)',
        "fl[]": ["identifier", "title", "year"],
        "rows": 6, "output": "json",
    }
    try:
        r = await http.get("https://archive.org/advancedsearch.php", params=params)
        docs = r.json()["response"]["docs"]
    except Exception:
        log.exception("archive failed")
        await update.message.reply_text("😕 Internet Archive isn't responding. Try again later.")
        return
    if not docs:
        await update.message.reply_text("Nothing found. This only covers public-domain titles.")
        return
    lines = [f"• <a href=\"https://archive.org/details/{e(d['identifier'])}\">{e(str(d.get('title')))}</a> "
             f"({e(str(d.get('year', '?')))})" for d in docs]
    await update.message.reply_text(
        "🏛 <b>Free from the Internet Archive</b>\n" + "\n".join(lines) +
        "\n\nOpen a link, then use the Download options on the page.",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ------------------------------------------------ legal download sources
UA = {"User-Agent": "OtakuRadarBot/1.0"}


async def src_archive(q):
    params = {
        "q": f'title:({q}) AND mediatype:movies',
        "fl[]": ["identifier", "title"], "rows": 5, "output": "json",
    }
    r = await http.get("https://archive.org/advancedsearch.php", params=params, headers=UA)
    return [(str(d.get("title")), f"https://archive.org/details/{d['identifier']}")
            for d in r.json()["response"]["docs"]]


async def src_commons(q):
    params = {"action": "query", "list": "search", "srsearch": f"{q} filetype:video",
              "srnamespace": 6, "srlimit": 5, "format": "json"}
    r = await http.get("https://commons.wikimedia.org/w/api.php", params=params, headers=UA)
    out = []
    for i in r.json()["query"]["search"]:
        name = i["title"]
        out.append((name.replace("File:", ""),
                    "https://commons.wikimedia.org/wiki/" + name.replace(" ", "_")))
    return out


# To add a site: write a function like the ones above, then add it here.
SOURCES = {
    "Internet Archive": src_archive,
    "Wikimedia Commons": src_commons,
}


async def downloads_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if throttled(update.effective_user.id, 3):
        return
    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("Usage: /downloads <title>")
        return
    parts = []
    for name, fn in SOURCES.items():
        try:
            items = await fn(q)
        except Exception:
            log.exception("source %s failed", name)
            continue
        if items:
            lines = [f"• <a href=\"{e(u)}\">{e(t)}</a>" for t, u in items]
            parts.append(f"<b>{e(name)}</b>\n" + "\n".join(lines))
    await update.message.reply_text(
        "\n\n".join(parts) if parts else "Nothing found on the legal sources.",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ------------------------------------------------------- legal torrents
BLENDER_FILMS = [
    ("Big Buck Bunny", "https://peach.blender.org/download/"),
    ("Elephants Dream", "https://orange.blender.org/download/"),
    ("Sintel", "https://durian.blender.org/download/"),
    ("Tears of Steel", "https://mango.blender.org/download/"),
]


async def torrents_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if throttled(update.effective_user.id, 3):
        return
    q = " ".join(context.args).strip()
    if not q:
        lines = [f"• <a href=\"{e(u)}\">{e(t)}</a>" for t, u in BLENDER_FILMS]
        await update.message.reply_text(
            "🌀 <b>Blender open movies</b> (free, Creative Commons)\n" + "\n".join(lines) +
            "\n\nSearch the Internet Archive too: <code>/torrents &lt;title&gt;</code>",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    params = {
        "q": f"title:({q}) AND mediatype:movies",
        "fl[]": ["identifier", "title", "year"],
        "rows": 6, "output": "json",
    }
    try:
        r = await http.get("https://archive.org/advancedsearch.php", params=params,
                           headers={"User-Agent": "OtakuRadarBot/1.0"})
        docs = r.json()["response"]["docs"]
    except Exception:
        log.exception("torrent search failed")
        await update.message.reply_text("😕 Internet Archive isn't responding. Try again later.")
        return
    matches = [f"• {e(str(d.get('title')))} ({e(str(d.get('year', '?')))})\n"
               f"  <a href=\"https://archive.org/download/{e(d['identifier'])}/{e(d['identifier'])}_archive.torrent\">torrent</a>"
               f" · <a href=\"https://archive.org/details/{e(d['identifier'])}\">page</a>"
               for d in docs]
    blender = [f"• <a href=\"{e(u)}\">{e(t)}</a>" for t, u in BLENDER_FILMS if q.lower() in t.lower()]
    parts = []
    if blender:
        parts.append("🌀 <b>Blender open movies</b>\n" + "\n".join(blender))
    if matches:
        parts.append("🏛 <b>Internet Archive</b>\n" + "\n".join(matches))
    if not parts:
        await update.message.reply_text("Nothing found. Only public-domain and open-license titles are covered.")
        return
    await update.message.reply_text(
        "\n\n".join(parts) +
        "\n\nOpen the torrent link in your torrent app. If a torrent link fails, open the page and use its Download options. "
        "Check each item's license before sharing.",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# --------------------------------------------------------------- callbacks
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid, chat = q.from_user.id, q.message.chat_id
    parts = q.data.split(":")
    action = parts[0]

    if action == "tut":
        if parts[1] == "done":
            await q.edit_message_text("🎉 You're all set! Type a title to start.")
        else:
            i = int(parts[1])
            await q.edit_message_text(TUTORIAL[i], parse_mode=ParseMode.HTML, reply_markup=tut_buttons(i))
    elif action == "menu":
        if parts[1] == "trending":
            await trending(chat, context)
        elif parts[1] == "watchlist":
            await watchlist(chat, uid, context)
        elif parts[1] == "settings":
            await send_settings(chat, uid, context)
        elif parts[1] == "help":
            await context.bot.send_message(chat, HELP, parse_mode=ParseMode.HTML)
    elif action == "country":
        set_country(uid, q.from_user.first_name, parts[1])
        await q.edit_message_text(f"Country set to {parts[1]} ✅")
    elif action == "show":
        if not throttled(uid, 0.8):
            await send_card(chat, uid, parts[1], int(parts[2]), context)
    elif action == "watch":
        await where_to_watch(chat, uid, parts[1], int(parts[2]), context)
    elif action in ("follow", "unfollow"):
        kind, mid = parts[1], int(parts[2])
        if action == "follow":
            try:
                title = (await get_details(kind, mid))[0]
            except Exception:
                await q.answer("Couldn't follow right now.", show_alert=True)
                return
            with db() as c:
                c.execute("INSERT OR REPLACE INTO follows VALUES(?,?,?,?)", (uid, kind, mid, title))
            await q.answer("Following! I'll alert you on new episodes 🔔", show_alert=False)
            btn = Btn("✅ Following (tap to unfollow)", callback_data=f"unfollow:{kind}:{mid}")
        else:
            with db() as c:
                c.execute("DELETE FROM follows WHERE user_id=? AND source=? AND media_id=?", (uid, kind, mid))
            btn = Btn("➕ Follow for alerts", callback_data=f"follow:{kind}:{mid}")
        await q.edit_message_reply_markup(Markup([
            [Btn("📍 Where to watch", callback_data=f"watch:{kind}:{mid}")], [btn]]))


# ----------------------------------------------------------------- alerts
AIRING_Q = """
query ($id: Int, $from: Int, $to: Int) { Page(perPage: 5) {
  airingSchedules(mediaId: $id, airingAt_greater: $from, airingAt_lesser: $to) {
    episode airingAt } } }
"""


def followers(kind, mid):
    with db() as c:
        return [r[0] for r in c.execute(
            "SELECT user_id FROM follows WHERE source=? AND media_id=?", (kind, mid))]


def mark_notified(kind, mid, key):
    """Return True if this is new (and record it)."""
    with db() as c:
        cur = c.execute("INSERT OR IGNORE INTO notified VALUES(?,?,?)", (kind, mid, key))
        return cur.rowcount == 1


async def notify(context, kind, mid, text):
    markup = Markup([[Btn("📍 Where to watch", callback_data=f"watch:{kind}:{mid}")]])
    for uid in followers(kind, mid):
        try:
            await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception:
            log.warning("could not alert %s", uid)


async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time())
    with db() as c:
        shows = c.execute("SELECT DISTINCT source, media_id, title FROM follows").fetchall()
    for kind, mid, title in shows:
        try:
            if kind == "anime":
                data = await anilist(AIRING_Q, {"id": mid, "from": now - 7200, "to": now})
                for ep in data["Page"]["airingSchedules"]:
                    if mark_notified(kind, mid, f"ep{ep['episode']}"):
                        await notify(context, kind, mid,
                                     f"🔔 <b>{e(title)}</b>\nEpisode {ep['episode']} just aired!")
            elif kind == "tv":
                d = await tmdb(f"/tv/{mid}")
                last = d.get("last_episode_to_air")
                if not last:
                    continue
                key = f"s{last['season_number']}e{last['episode_number']}"
                with db() as c:
                    seen = c.execute("SELECT 1 FROM notified WHERE source=? AND media_id=?",
                                     (kind, mid)).fetchone()
                is_new = mark_notified(kind, mid, key)
                if is_new and seen:  # first run only sets a baseline
                    await notify(context, kind, mid,
                                 f"🔔 <b>{e(title)}</b>\nS{last['season_number']}E{last['episode_number']} "
                                 f"“{e(last.get('name') or '')}” is out!")
        except Exception:
            log.exception("alert check failed for %s %s", kind, mid)


# ------------------------------------------------------------------ admin
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with db() as c:
        u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        f = c.execute("SELECT COUNT(*) FROM follows").fetchone()[0]
    await update.message.reply_text(f"👥 Users: {u}\n📋 Follows: {f}")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not context.args:
        return
    msg = " ".join(context.args)
    with db() as c:
        ids = [r[0] for r in c.execute("SELECT user_id FROM users")]
    sent = 0
    for uid in ids:
        try:
            await context.bot.send_message(uid, msg)
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"Sent to {sent}/{len(ids)} users.")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


# ------------------------------------------------------------------- main
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def start_health_server():
    """Free hosts want a web port; this keeps them happy."""
    port = int(os.environ.get("PORT", "8000"))
    threading.Thread(target=HTTPServer(("0.0.0.0", port), Health).serve_forever, daemon=True).start()


async def post_init(app: Application):
    global http
    http = httpx.AsyncClient(timeout=15)
    await app.bot.set_my_commands([
        BotCommand("start", "Welcome"), BotCommand("search", "Find a title"),
        BotCommand("trending", "What's popular"), BotCommand("watchlist", "Shows you follow"),
        BotCommand("archive", "Free public-domain films"), BotCommand("downloads", "Legal free downloads"), BotCommand("torrents", "Legal torrents"), BotCommand("settings", "Choose country"),
        BotCommand("tutorial", "Quick tour"), BotCommand("help", "All commands"),
    ])


async def post_shutdown(app: Application):
    await http.aclose()


def main():
    init_db()
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    for name, fn in [("start", start), ("help", help_cmd), ("tutorial", tutorial_cmd),
                     ("settings", settings_cmd), ("country", country_cmd), ("search", search_cmd),
                     ("trending", trending_cmd), ("watchlist", watchlist_cmd),
                     ("archive", archive_cmd), ("torrents", torrents_cmd), ("downloads", downloads_cmd), ("stats", stats), ("broadcast", broadcast)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_search))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(check_alerts, interval=1800, first=60)
    app.run_polling()


if __name__ == "__main__":
    main()
