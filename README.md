PMB Reaction Bot

A Discord bot for scheduling RSVPs with per-time-slot voting. Posts one message per time slot, lets people react ✅ / ❌ / ❓, and can ping anyone on a roster who hasn't responded to every slot.

Commands
/rsvp <title> [date]

Posts one message per time slot, formatted as:

**Movie Night — 8:00 PM EDT**  ·  your local time: 5:00 PM

The server's timezone leads, so everyone sees the same time when comparing slots or quoting one back to each other. The trailing time is a Discord timestamp (<t:...:t>), which Discord renders in each reader's own timezone.

title — what people are RSVPing to (e.g. "Movie Night")
date (optional) — YYYY-MM-DD, defaults to today

Each message gets ✅ (yes), ❌ (no), and ❓ (maybe) reactions — or three buttons instead, depending on /setvoting.

Up to 3 RSVPs can run at once per server. Creating a fourth closes the oldest, and you're told which one. Two running at the same time can't share a title, because the title is how /summary and /reactping tell them apart.

/settimes <times>

Customize the time slots used by /rsvp. Comma-separated, written however you like:

/settimes times:8pm, 8:30pm, 9pm, 9:30pm, 10pm

12-hour (8pm, 8:30 PM, 8 p.m.) and 24-hour (20:00) are both accepted, and the two can be mixed. A bare number is read as 24-hour, so 9 means 9:00 AM and 21 means 9:00 PM — the confirmation echoes back what was parsed, so a mistake is obvious. Duplicate times are ignored.

Replaces the default slots (20:00, 20:30, 21:00, 21:30, 22:00, 22:30, 23:00 — i.e. 8:00 PM through 11:00 PM) for this server. Requires Manage Server permission.

/settimezone <zone>

Sets the timezone used to interpret the times given in /settimes and /rsvp. Defaults to America/New_York (US Eastern), so most servers never need to run this.

/settimezone zone:America/New_York

Must be an IANA zone name — for the US that's America/New_York, America/Chicago, America/Denver or America/Los_Angeles. These follow daylight saving on their own, so 8:00 PM stays 8:00 PM year-round.

Fixed UTC offsets are deliberately not accepted: an offset can't know about daylight saving, so -5 would be an hour wrong from March to November, silently.

This is a server-wide setting, not per-user — one admin sets it and it applies to everyone in the guild. Requires Manage Server permission.

/setvoting <mode>

Chooses how people answer an RSVP. Requires Manage Server permission.

Reactions, clear the bot's own (default) — the bot removes its own reaction the moment a real person picks that option, so the count on the message is exactly the number of people. The cost: when the last vote on an option is withdrawn, that option briefly disappears from the message and has to be re-added, which puts it at the end, so the bot has to re-lay all three to keep them in order.

Reactions, keep the bot's own — the bot leaves all three reactions in place permanently. Every count reads one higher than the real number, but the options can never vanish and so can never fall out of ✅ ❌ ❓ order.

Buttons — no reactions at all. Three buttons sit under each message and the tally is written into the message itself, updated on every press:

**Movie Night — 8:00 PM EDT**  ·  your local time: 5:00 PM
✅ 4   ❌ 1   ❓ 2
[ ✅ Yes ] [ ❌ No ] [ ❓ Maybe ]

Counts are exact, the options can't move or disappear, and a press is exclusive — one answer per person per slot, and pressing the one you already chose clears it. (With reactions a person can sit in both Yes and Maybe at once.)

The tradeoff is that button votes live only in memory, so a bot restart loses them. Reactions at least survive on the message and get read back on the next /summary. After a restart, pressing a button on an old RSVP shows Discord's generic "interaction failed" rather than a message.

In every mode the bot is excluded from the tally, so it never appears in the summary image or counts toward /reactping.

The mode is recorded on each RSVP when it's created — a message posted with buttons can't become a reaction message — so changing this affects new RSVPs. Switching between the two reaction modes does apply to RSVPs already running.

/summary [title]

Renders a shareable PNG of every time slot with each responder's avatar and name, grouped into Yes / No / Maybe, and badges the slot with the most yes votes.

title picks which RSVP when more than one is running — it autocompletes, and matches case-insensitively on a partial name. Leave it blank when only one is running.

Because it's an image rather than Discord markdown, it cannot localize per viewer the way /rsvp messages do — so it states its timezone in the header ("all times EDT"). Everyone sees the server's timezone.

/setroster <role>

Sets which role counts as "the roster" for /reactping. Requires Manage Server permission.

/reactping [title]

Pings everyone in the roster who hasn't reacted to every time slot. Requires a roster to be set first. title works the same as on /summary.

Setup
Install dependencies:
   pip install -r requirements.txt

Summary images need a real font installed. The Docker image handles this (fonts-dejavu-core and fonts-noto-color-emoji); on a bare host, install DejaVu or set SUMMARY_FONT / SUMMARY_FONT_BOLD to a .ttf path. Without one, Pillow falls back to a tiny bitmap face and non-ASCII text renders as boxes — the bot prints a warning if this happens.
Set the DISCORD_TOKEN environment variable with your bot's token (never hardcode it in the file).
Run:
   python bot.py
Discord Developer Portal setup
Privileged Gateway Intents: enable Message Content Intent and Server Members Intent (Bot settings page).
OAuth2 → URL Generator scopes: bot, applications.commands
Bot Permissions: Send Messages, Add Reactions, Read Message History, View Channel, Mention Everyone
Deployment

Designed to run continuously on a host like Railway:

Push bot.py, requirements.txt, and a Procfile (worker: python bot.py) to a GitHub repo.
Connect the repo to a Railway service.
Add DISCORD_TOKEN under the service's Variables tab.
Railway auto-redeploys on every push to the connected branch.
Notes
Settings (/settimes, /settimezone, /setroster, /setvoting) and RSVP data are stored in memory — they reset if the bot restarts.
Up to 3 RSVPs are tracked per server at once; creating a fourth closes the oldest and stops tracking reactions on its messages.
