# Otaku Radar (@OtakuRadarBot)

A Telegram bot to discover anime, series and movies, see where to watch them
legally (including free options), follow shows, and get new-episode alerts.

Data: AniList (anime), TMDB (series/movies + streaming availability, data by
JustWatch), Internet Archive (public-domain films).

## Features
- Welcome message, 3-step tutorial, /help
- Search by command or just by typing a title
- Title cards with poster, score, genres, synopsis
- Where to watch (free, ad-supported, subscription) by country
- Follow shows and get alerts every 30 minutes check
- /trending, /watchlist, /archive, /settings
- Admin: /stats and /broadcast (set ADMIN_ID)

## Setup from your phone
1. Create the bot with @BotFather (`/newbot`) and copy the token.
2. Get a free TMDB API key (themoviedb.org > Settings > API).
3. Upload these files to a GitHub repo.
4. On Koyeb or Render, create a **web service** from the repo and set:
   - `BOT_TOKEN` = your BotFather token
   - `TMDB_KEY` = your TMDB key
   - `ADMIN_ID` = your Telegram user ID (message @userinfobot to find it)
   - optional: `DEFAULT_COUNTRY` (default NG)
5. Start command: `python bot.py` (the Procfile already says this).
6. Message your bot `/start`.

## Notes
- Never commit your tokens. Keep them in environment variables only.
- Free hosts may sleep or wipe local files. If users' follows vanish after a
  redeploy, move the database to a free Supabase/Postgres or a persistent
  volume and point `DB_PATH` at it.
- Add the TMDB credit "This product uses the TMDB API but is not endorsed or
  certified by TMDB" in your bot's /start or description.
