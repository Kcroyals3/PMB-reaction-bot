PMB Reaction Bot

A Discord bot for scheduling RSVPs with per-time-slot voting. Posts one message per time slot, lets people react ✅ / ❌ / ❓, and can ping anyone on a roster who hasn't responded to every slot.

Commands
/rsvp <title> [date]

Posts one message per time slot, formatted as:

**Movie Night — Fri Sep 18, 8:00 PM EDT**  ·  your local time: 18 September 2026 17:00

The date is on every slot message, so it doesn't need to go in the title. The server's timezone leads, so everyone sees the same time when comparing slots or quoting one back to each other. The trailing half is a Discord timestamp (<t:...:f>), which Discord renders in each reader's own timezone — date included, because a 10:00 PM Eastern slot is 3:00 AM the next day in the UK and a bare time would quietly say 3:00 AM today.

title — what people are RSVPing to (e.g. "Movie Night")
date (optional) — defaults to today. Written however you like:

  today, tonight, tomorrow, tmr
  friday, fri, next friday
  in 3 days, +5, 10 days
  9/12, 9-12, 9/12/26, 2026-09-12
  sep 12, september 12th, 12 sept, Dec 25 2027

A date with no year rolls forward — typing 1/5 in December means next January, not ten months ago. Slash dates are read US-style (month first), except where the first number can't be a month, which makes 18/9 unambiguous. /rsvp echoes back the date it settled on, so a misread is visible rather than silent.

Each message gets ✅ (yes), ❌ (no), and ❓ (maybe) reactions — or three buttons instead, depending on /setvoting.

Up to 3 RSVPs can run at once per server. Creating a fourth closes the oldest, and you're told which one. Two running at the same time can't share a title, because the title is how /summary and /reactping tell them apart.

/settimes <times>

Customize the time slots used by /rsvp. Comma-separated, written however you like:

/settimes times:8pm, 8:30pm, 9pm, 9:30pm, 10pm

12-hour (8pm, 8:30 PM, 8 p.m.) and 24-hour (20:00) are both accepted, and the two can be mixed. A bare number is read as 24-hour, so 9 means 9:00 AM and 21 means 9:00 PM — the confirmation echoes back what was parsed, so a mistake is obvious. Duplicate times are ignored.

Replaces the default slots (20:00, 20:30, 21:00, 21:30, 22:00, 22:30, 23:00 — i.e. 8:00 PM through 11:00 PM) for this server. Requires Manage Server permission.

/settimestamp <style>

Chooses how each reader's own local time appears on RSVP messages. Requires Manage Server permission.

  Date and time (default) — 18 September 2026 17:00
  Time only               — 17:00
  Full                    — Friday, 18 September 2026 17:00
  Relative                — in 3 hours
  Off                     — server time only

The command replies with a live preview rendered in your own timezone, so you see the actual result rather than a description of it. Turning it off means nothing on the message adapts per reader — anyone outside the server's timezone has to convert it themselves.

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

Titles may contain your server's own emoji. They're downloaded and drawn inline at the right size, rather than appearing as the raw <:name:id> text Discord stores them as. Unicode emoji work too. Either kind is skipped rather than drawn as a broken box if it can't be fetched or rendered.

/rsvps

Lists what's running on this server: each title, its date, how many slots and how many people have answered, which voting mode it uses, whether it has a live summary pinned, and the best turnout so far. Use a title from here with /summary or /reactping.

/setlivesummary <enabled> [channel] [mode] [pin]

Posts a summary image that redraws itself as people answer, so nobody has to run /summary to see where things stand. Requires Manage Server permission.

channel — where to post it. Leave blank and each summary appears in whichever channel its RSVP was created in. Give a channel and they all go there instead, each captioned with a link back to where to actually answer. The bot's permissions there (View Channel, Send Messages, Attach Files) are checked when you set it, rather than failing silently later.

mode — "One summary per RSVP" (default), or "A single summary that follows the newest RSVP". The second suits a dedicated summary channel: each new RSVP takes over the same message, so there's always exactly one and it's always current. Older RSVPs stop writing to it.

pin — whether to pin it. On by default. Pinning needs Manage Messages; if the bot doesn't have it the summary still posts, just unpinned, and the command tells you so up front.

It uses the compact grid layout (people down the side, slots across the top) rather than the detailed card layout, because a live image lives in the channel permanently — the grid says the same thing in about a quarter of the height.

It redraws at most once every 4 seconds. Replacing a message's image means re-uploading it, so a burst of twenty answers becomes one redraw rather than twenty.

Turning it off leaves the image in place, marks it as no longer updating, and unpins it.

/closersvp <title> [delete_messages]

Stops tracking an RSVP before the 3-at-once cap pushes it out, freeing a slot. Requires Manage Server permission.

By default its messages stay in the channel, marked "Closed — no longer counting answers" with any buttons removed, so the result is still readable but obviously final. Pass delete_messages to remove them instead. Its live summary is stopped and unpinned either way.

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
Persistence

Settings and running RSVPs are saved to a JSON file and reloaded on startup, so a restart or redeploy doesn't lose them. Set STATE_FILE to choose the path; it defaults to state.json next to bot.py.

In Docker this needs a volume, or the file lives inside the container and disappears when it's recreated. docker-compose.yml already mounts one at /data and points STATE_FILE there.

Writes are atomic (written to a temp file, then renamed) so a crash mid-write can't leave a truncated file, and they're batched — a burst of answers is one write, not twenty. A state file that's corrupt or written by a different version is reported and ignored rather than crashing the bot.

Notes
Up to 3 RSVPs are tracked per server at once; creating a fourth closes the oldest and stops tracking reactions on its messages. Use /closersvp to close one yourself.
Button RSVPs have their views re-registered on startup, so their buttons keep working across a restart.
