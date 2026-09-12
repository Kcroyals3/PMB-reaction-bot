PMB Reaction Bot

A Discord bot for scheduling RSVPs with per-time-slot voting. Posts one message per time slot, lets people react ✅ / ❌ / ❓, and can ping anyone on a roster who hasn't responded to every slot.

Commands
/rsvp <title> [date]

Posts one message per time slot. Each message shows the time as a Discord timestamp (<t:...:t>), so it automatically displays in each reader's own local timezone — no confusion across time zones.

title — what people are RSVPing to (e.g. "Movie Night")
date (optional) — YYYY-MM-DD, defaults to today

Each message gets ✅ (yes), ❌ (no), and ❓ (maybe) reactions. The bot seeds all three so people can click them, and automatically removes/re-adds its own seed reaction so the count always reflects real responses, not the bot's own placeholder.

/settimes <times>

Customize the time slots used by /rsvp. Comma-separated, 24-hour format:

/settimes times:18:00,18:30,19:00,19:30,20:00

Replaces the default slots (8:00, 8:30, 9:00, 9:30, 10:00, 10:30, 11:00) for this server. Requires Manage Server permission.

/settimezone <offset>

Sets the UTC offset used to interpret the times given in /settimes and /rsvp. Run once per server so the bot knows what timezone your times are written in:

/settimezone offset:-4

(-4 = EDT, -5 = EST, 5.5 = IST, etc.) Requires Manage Server permission.

/setroster <role>

Sets which role counts as "the roster" for /reactping. Requires Manage Server permission.

/reactping

Pings everyone in the roster who hasn't reacted to every time slot of the most recent /rsvp. Requires a roster to be set first.

Setup
Install dependencies:
   pip install discord.py
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
Settings (/settimes, /settimezone, /setroster) and RSVP data are stored in memory — they reset if the bot restarts.
One RSVP "event" is tracked at a time per server for /reactping (the most recent /rsvp run).
