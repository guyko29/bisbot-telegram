# BisBot 🥗

A personal Telegram bot that helps one person follow the meal plan their
nutritionist wrote. You tell it what you ate (text or a photo), it converts
that into the plan's portion units with Claude, and it keeps score against
your daily targets. All bot messages are in Hebrew.

## What it does

- **Log a meal** by sending free text (`2 פרוסות לחם, ביצה וסלט`) or a photo
  with an optional caption. The bot replies with a short breakdown and three
  buttons: ✅ confirm, ✏️ fix, 🗑 cancel. Nothing is saved before you confirm.
  With ✏️ you describe the correction in free text and the meal is re-parsed.
- **Shortcuts**: `/save <name>` stores the last logged meal under a name.
  Sending exactly that name later logs it immediately, without calling Claude.
- **Ask anything**: dinner ideas that fit what is left today, substitutions,
  what to eat before a workout at 18:00. Claude answers with the plan, today's
  totals and your recent meals as context.
- **Summaries**: `/today`, `/week`, and automatic messages every day at 21:00
  and every Friday at 12:00.

| Command | What it does |
|---|---|
| `/today` | Totals per category against the daily targets, what is left, and this week's limited items |
| `/week` | Last 7 complete days: adherence per category, patterns, 1-2 suggestions |
| `/undo` | Removes the last logged meal |
| `/save <name>` | Saves the last logged meal as a shortcut |
| `/shortcuts` | Lists shortcuts |
| `/delshortcut <name>` | Deletes a shortcut |
| `/help` | Short guide |

## Setup

You need Python 3.11+ (or Docker), a Telegram bot token and an Anthropic API key.

1. **Create the bot**: talk to [@BotFather](https://t.me/BotFather), send
   `/newbot`, and copy the token.
2. **Find your chat id**: send any message to [@userinfobot](https://t.me/userinfobot).
   (Alternatively start BisBot with `ALLOWED_CHAT_ID=0`, message it, and read
   the id from the log line `Ignoring update from chat <id>`.)
3. **Get an API key** at <https://console.anthropic.com>.
4. **Configure**:

   ```bash
   cp .env.example .env    # then fill in the three required values
   ```

5. **Check the meal plan** - `menu.md` and `menu_targets.json` already hold the plan; see the next section for how it is counted.

### The meal plan: `menu.md` and `menu_targets.json`

- `menu.md` is the nutritionist's plan as written. It is given to Claude
  verbatim, so any readable format works.
- `menu_targets.json` turns the plan into numbers. Totals and targets are
  computed from it in Python, never by the model.

The plan is written as meals with options and has no daily totals of its own,
so the targets are the **sum of its meals**:

| Category | Target | Where it comes from |
|---|---|---|
| פחמימות (`carbs`) | 3 to 5 a day | 1-2 at breakfast, 1 at lunch, 1-2 at dinner |
| חלבון (`protein`) | 3 a day | one protein portion per main meal |
| ירקות (`vegetables`) | at least 3 a day | vegetables at every main meal, no cap |
| ביניים (`snack`) | at most 2 a day | the two between-meal slots |
| פיצה, סושי | at most 1 a week each | "פעם בשבוע" |
| מעדן הגולן, אלכוהול | at most 2 a week each | "מקסימום פעמיים בשבוע", "עד 2 מנות אלכוהול בשבוע" |
| מחוץ לתפריט (`off_plan`) | tracked, no target | anything the plan does not include |

How food is converted into those numbers is spelled out in the `counting_rules`
of `menu_targets.json`, which are added to Claude's instructions. In short:

- **Carbs**: one portion is a slice of bread, a heaped *serving* spoon of a
  cooked side, a small ladle of legumes, half a pita, a third of a laffa or half
  a bun.
- **Protein**: one portion is whatever the plan prescribes for that meal - 2 eggs
  at breakfast, 200 g cooked chicken at lunch - scaled in proportion (100 g of
  chicken at lunch is 0.5).
- **Snacks**: one choice from the plan's snack lists is one snack, including the
  cappuccino. Its bread or cheese is not also counted as carbs or protein.
- **Whole-meal alternatives** (hamburger plate, shawarma, toast, pizza, sushi,
  yogurt bowl) take the place of the meal and are logged as that meal's usual
  protein and carbs.
- **Sport nutrition**: the pre-workout snack and the other workout extras are
  logged but stay out of the daily totals. `/today` lists them separately.
- **Weekly limits** are counted per calendar week, Sunday to Saturday.

To change a target or a rule, edit `menu_targets.json` and restart the bot:

```json
{
  "categories": [
    {"key": "carbs", "name": "פחמימות", "daily_min": 3, "daily_max": 5},
    {"key": "protein", "name": "חלבון", "daily_target": 3},
    {"key": "vegetables", "name": "ירקות", "daily_min": 3},
    {"key": "snack", "name": "ביניים", "unit": "", "unit_singular": "", "daily_max": 2},
    {"key": "pizza", "name": "פיצה", "weekly_limit": 1},
    {"key": "off_plan", "name": "מחוץ לתפריט"}
  ],
  "meal_types": [
    {"key": "lunch", "name": "ארוחת צהריים"},
    {"key": "pre_workout", "name": "לפני אימון", "extra": true}
  ],
  "counting_rules": ["One paragraph per rule, written for Claude."]
}
```

- `key` is the stable identifier stored in the database; `name` is what the bot
  shows. If you rename or remove a key, meals already logged under the old key
  drop out of the totals.
- Daily targets: `daily_target` for an exact number, `daily_min` + `daily_max`
  for a range, or just one of them for "at least" / "at most". A category with
  none of these is tracked without a target.
- `weekly_limit` makes a category a weekly counter instead of a daily one.
- A meal type with `"extra": true` is logged but excluded from every total.
- `"placeholder": true` at the top level makes the bot warn that the plan is not
  the real one (useful while trying the bot with a sample plan).

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m bisbot
```

The bot uses long polling, so no public URL or webhook is needed. The SQLite
file is created at `DB_PATH` (default `data/bisbot.db`).

Run the tests (they mock both Claude and Telegram, so no keys are needed):

```bash
pip install -r requirements-dev.txt
pytest
```

With Docker:

```bash
docker build -t bisbot .
docker run --rm --env-file .env -v bisbot_data:/data bisbot
```

## Deploy

Two things matter on any host:

- **Run exactly one instance.** Two processes polling the same bot token steal
  each other's updates.
- **Mount a persistent volume** and point `DB_PATH` at it, otherwise the log is
  lost on every deploy. The Dockerfile defaults to `DB_PATH=/data/bisbot.db`.

### Railway

1. Push this repository to GitHub and create a Railway project with
   *Deploy from GitHub repo*. Railway builds the `Dockerfile` automatically.
2. In the service's **Variables**, add `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`,
   `ALLOWED_CHAT_ID` and `TZ=Asia/Jerusalem` (plus `CLAUDE_MODEL` if you want a
   different model).
3. Add a **Volume** to the service and mount it at `/data`.
4. Keep the service at one replica. No port or domain is needed.

### Fly.io

`fly.toml` is included. Change the `app` name in it, then:

```bash
fly launch --no-deploy --copy-config        # registers the app, keeps fly.toml
fly volumes create bisbot_data --size 1 --region fra
fly secrets set TELEGRAM_BOT_TOKEN=... ANTHROPIC_API_KEY=... ALLOWED_CHAT_ID=...
fly deploy --ha=false                        # a single machine
fly logs
```

## Configuration

| Variable | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | | From @BotFather |
| `ANTHROPIC_API_KEY` | yes | | |
| `ALLOWED_CHAT_ID` | yes | | Updates from every other chat are dropped |
| `CLAUDE_MODEL` | no | `claude-sonnet-5-5` | Any current Claude model id |
| `CLAUDE_EFFORT` | no | `low` | `low` / `medium` / `high`; empty = model default |
| `CLAUDE_REFUSAL_FALLBACK` | no | `true` | See below |
| `TZ` | no | `Asia/Jerusalem` | Defines "today" and the schedule |
| `DB_PATH` | no | `data/bisbot.db` | |
| `MENU_PATH` | no | `menu.md` | |
| `MENU_TARGETS_PATH` | no | `menu_targets.json` | |
| `LOG_LEVEL` | no | `INFO` | |

## How it works

```
bisbot/
  app.py            builds the Application, registers handlers, entry point
  handlers.py       commands, the log / confirm / fix / cancel flow, buttons
  claude_client.py  every Claude API call, and error mapping
  prompts.py        system prompts and tool schemas
  parsing.py        validation of meal payloads
  summaries.py      daily / weekly numbers and their Hebrew formatting (pure)
  reports.py        combines DB + summaries + Claude into the final messages
  jobs.py           the 21:00 daily and Friday 12:00 schedule (JobQueue)
  db.py             SQLite data-access layer
  menu.py           loads menu.md and menu_targets.json
  texts.py          every Hebrew string
  config.py         environment settings
tests/              parsing, summaries, DB, Claude client, end-to-end bot flows
```

**One Claude call per message.** Each incoming message goes to Claude with two
tools: `log_meal` (structured `{meal_type, items: [{name, category, portions,
confidence}], notes}`) and `answer_user` (`{intent, text}`). Which tool Claude
calls is the intent classification. Both tools are `strict`, and the category
and meal-type enums are generated from `menu_targets.json`, so Claude can only
return categories that exist in your plan. The result is validated again in
`parsing.py` before it is shown. Items with confidence below 0.6 are marked ❓.

**Numbers never come from the model.** Totals, remaining portions and adherence
are computed in SQL/Python. For the scheduled daily summary and `/week`, the bot
prints its own numbers and Claude only writes the short note underneath; if
Claude is unavailable the numbers are still sent.

**Failures keep your input.** If a Claude call fails, the bot says so in Hebrew
and stores the raw text / photo reference with a 🔄 button that re-sends it.

**Prompt caching.** The system prompt (instructions + `menu.md`) is identical on
every request and marked cacheable; the clock, today's totals and recent meals
travel in the user turn.

### Things worth knowing

- **Week window**: `/week` and the Friday message cover the seven complete days
  ending yesterday. Today is excluded so a half-logged day does not drag the
  averages down.
- **Adherence**: per category and day, 100% when the amount is inside the target
  range, dropping linearly to 0% at 100% off the nearest bound; averaged over
  the days that have at least one logged meal. For an exact target (protein), a
  day still counts as "on target" within half a portion. Days with no log at all
  are reported separately rather than counted as zero.
- **`/undo`** soft-deletes: the row stays in the database with
  `status = 'deleted'` and leaves every total.
- **Refusal fallback**: requests are sent with `fallbacks: "default"` (beta
  `server-side-fallback-2026-07-01`), so if the model declines a message the API
  re-runs it on the model's default fallback. Set `CLAUDE_REFUSAL_FALLBACK=false`
  to send plain requests instead.
- **Conversation memory** for follow-up questions is the last few messages, kept
  in memory only; it resets when the bot restarts. Unconfirmed meals and failed
  requests are stored in the database and survive a restart.

### Database

```sql
meals     (id, logged_at /* UTC */, day /* local date */, meal_type,
           raw_text, photo_file_id, notes, status /* confirmed | deleted */)
items     (id, meal_id, name, category, portions)
shortcuts (name, items_json)
pending   (chat_id, payload_json)   -- unconfirmed parse + failed request kept for retry
```

All access goes through the `Database` class in `db.py`; replacing SQLite means
reimplementing that one class.
