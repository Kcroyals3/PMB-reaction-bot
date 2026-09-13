import asyncio
import json
import os
import io
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, ImageOps

intents = discord.Intents.default()
intents.reactions = True
intents.message_content = True
intents.members = True  # needed to iterate role members for /reactping

bot = commands.Bot(command_prefix="!", intents=intents)

DEFAULT_TIME_SLOTS = ["20:00", "20:30", "21:00", "21:30", "22:00", "22:30", "23:00"]

STATUS_EMOJIS = {
    "✅": "Yes",
    "❌": "No",
    "❓": "Maybe",
}

# The order options must appear in on a message. Discord lays reactions out by
# when each emoji was first added, so anything that adds one has to follow this
# sequence. Kept explicit rather than leaning on dict ordering, because the
# display order is a deliberate choice and shouldn't hinge on how the dict above
# happens to be written.
STATUS_ORDER = tuple(STATUS_EMOJIS)

STATUS_COLORS = {
    "✅": (67, 181, 129),
    "❌": (240, 71, 71),
    "❓": (250, 166, 26),
}

# event_id -> {
#   "title": str,
#   "guild_id": int,
#   "channel_id": int,
#   "tz": tzinfo,
#   "slots": {
#       time_label: {
#           "message_id": int,
#           "timestamp": int,
#           "votes": {emoji: set(user_id)}
#       }
#   }
# }
active_events = {}

# message_id -> (event_id, time_label)   -- fast lookup for reaction events
message_index = {}

# guild_id -> role_id (the "roster")
rosters = {}

# guild_id -> [event_id, ...], oldest first. A server can have several RSVPs
# running at once; /summary and /reactping pick between them by title.
guild_events = {}

# Creating one past this closes the oldest. A rolling window rather than a hard
# refusal, so /rsvp never fails on a cap the person wasn't thinking about.
MAX_ACTIVE_RSVPS = 3

# set of (message_id, emoji) where the bot currently holds a seed reaction
bot_seeded = set()

# guild_id -> list of "HH:MM" (24hr) time strings, custom per server
guild_times = {}

# How people answer an RSVP.
#   CLEAR   — reactions; the bot drops its own as soon as a real person picks
#             that option, so the visible count is exactly the people.
#   KEEP    — reactions; the bot leaves all three in place forever. Counts read
#             one high, but options can never vanish and so never fall out of
#             ✅ ❌ ❓ order.
#   BUTTONS — no reactions at all. Three buttons under the message, with the
#             tally written into the message itself.
# In every mode the bot is excluded from the tally, so it never appears in the
# summary image or counts toward /reactping.
KEEP_PLACEHOLDERS = "keep"
CLEAR_PLACEHOLDERS = "clear"
BUTTON_MODE = "buttons"
RSVP_MODES = (CLEAR_PLACEHOLDERS, KEEP_PLACEHOLDERS, BUTTON_MODE)

# guild_id -> mode. This is the default for RSVPs created from now on; each
# event records the mode it was built with, because a button message and a
# reaction message aren't interchangeable once posted.
guild_seed_mode = {}


# guild_id -> bool. Whether a new RSVP gets a pinned, self-updating summary.
guild_live_summary = {}

# guild_id -> channel_id to post live summaries in. Absent means "wherever the
# RSVP itself was created", which is the default.
guild_live_channel = {}


def guild_mode(guild_id) -> str:
    return guild_seed_mode.get(guild_id, CLEAR_PLACEHOLDERS)


def wants_live_summary(guild_id) -> bool:
    return bool(guild_live_summary.get(guild_id, False))


def live_summary_channel(guild_id, fallback):
    """Where this guild's live summaries go.

    Falls back to the RSVP's own channel if none is configured, or if the
    configured one has since been deleted or hidden from the bot — better a
    summary in the wrong place than none at all.
    """
    channel_id = guild_live_channel.get(guild_id)
    if channel_id is None:
        return fallback
    return bot.get_channel(channel_id) or fallback


# Whether each RSVP gets its own live summary, or one message follows whichever
# RSVP is newest. The latter suits a dedicated summary channel: one pinned
# message that's always current, instead of a growing pile of them.
LIVE_EACH = "each"
LIVE_LATEST = "latest"
LIVE_MODES = (LIVE_EACH, LIVE_LATEST)

# guild_id -> one of the above
guild_live_mode = {}

# guild_id -> the shared message id, in "latest" mode only
guild_live_message = {}

# guild_id -> bool. Whether to pin the live summary. Defaults to pinning.
guild_live_pin = {}


def wants_pinned_summary(guild_id) -> bool:
    return bool(guild_live_pin.get(guild_id, True))


def events_for_guild(guild_id: int) -> list:
    """This guild's live RSVPs as (event_id, event), oldest first."""
    return [
        (event_id, active_events[event_id])
        for event_id in guild_events.get(guild_id, [])
        if event_id in active_events
    ]


def forget_event(event_id: str):
    """Stop tracking an RSVP and drop every trace of its messages."""
    event = active_events.pop(event_id, None)
    if event is None:
        return None

    pending = _live_refresh.pop(event_id, None)
    if pending is not None and not pending.done():
        pending.cancel()

    for slot in event["slots"].values():
        message_id = slot["message_id"]
        message_index.pop(message_id, None)
        _order_locks.pop(message_id, None)
        for emoji in STATUS_ORDER:
            bot_seeded.discard((message_id, emoji))
    return event


def register_event(guild_id: int, event_id: str) -> list:
    """Track a new RSVP, closing the oldest if that puts the guild over the cap.

    Returns the events that were closed. This is also what keeps message_index
    and bot_seeded from growing forever in a long-running process.
    """
    event_ids = guild_events.setdefault(guild_id, [])
    event_ids.append(event_id)

    closed = []
    while len(event_ids) > MAX_ACTIVE_RSVPS:
        evicted = forget_event(event_ids.pop(0))
        if evicted is not None:
            closed.append(evicted)
    return closed


def close_event(guild_id: int, event_id: str):
    """Close an RSVP on purpose, rather than because the cap pushed it out."""
    event_ids = guild_events.get(guild_id)
    if event_ids and event_id in event_ids:
        event_ids.remove(event_id)
    return forget_event(event_id)


def resolve_event(guild_id: int, title):
    """Pick the RSVP a command means. Returns ((event_id, event), None) or
    (None, message) explaining what to do instead."""
    live = events_for_guild(guild_id)
    if not live:
        return None, "No RSVP found. Run `/rsvp` first."

    listing = ", ".join(f"`{event['title']}`" for _, event in live)

    if title is None:
        if len(live) == 1:
            return live[0], None
        return None, (
            f"There are {len(live)} RSVPs running — say which one you mean: {listing}"
        )

    needle = title.strip().casefold()
    exact = [pair for pair in live if pair[1]["title"].casefold() == needle]
    if len(exact) == 1:
        return exact[0], None

    partial = [pair for pair in live if needle in pair[1]["title"].casefold()]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        matches = ", ".join(f"`{event['title']}`" for _, event in partial)
        return None, f"`{title}` matches more than one RSVP: {matches}"

    return None, f"No RSVP called `{title}`. Running now: {listing}"


async def rsvp_title_autocomplete(interaction: discord.Interaction, current: str):
    """Offer the guild's live RSVP titles as you type."""
    needle = (current or "").casefold()
    return [
        app_commands.Choice(name=event["title"][:100], value=event["title"][:100])
        for _, event in events_for_guild(interaction.guild_id)
        if needle in event["title"].casefold()
    ][:25]

# guild_id -> tzinfo. Named IANA zones rather than fixed offsets, so a server
# set to Eastern stays correct across the EST/EDT changeover instead of
# silently drifting an hour every spring.
guild_timezones = {}

DEFAULT_TIMEZONE = "America/New_York"

try:
    DEFAULT_TZ = ZoneInfo(DEFAULT_TIMEZONE)
except (ZoneInfoNotFoundError, ValueError):
    # No IANA database available. Fall back to a fixed offset so the bot still
    # runs, but this WILL be an hour off during daylight saving.
    print(
        f"WARNING: timezone database unavailable, falling back to a fixed "
        f"UTC-5 offset for {DEFAULT_TIMEZONE}. Daylight saving will be wrong. "
        f"Install the 'tzdata' package.",
        flush=True,
    )
    # Deliberately NOT named "EST": in this state the offset is frozen, so
    # calling it EST in July would be a plausible-looking lie. "UTC-5" on a
    # summary image is the visible sign that this fallback is active.
    DEFAULT_TZ = timezone(timedelta(hours=-5), "UTC-5")


def get_time_slots(guild_id: int) -> list:
    return guild_times.get(guild_id, DEFAULT_TIME_SLOTS)


def get_timezone(guild_id: int):
    return guild_timezones.get(guild_id, DEFAULT_TZ)


def resolve_timezone(value: str):
    """Resolve an IANA zone name, or None if it isn't one.

    Fixed UTC offsets are deliberately not accepted. An offset can't know about
    daylight saving, so '-5' would be an hour wrong from March to November —
    and silently, which is the worst way to be wrong about a meeting time.
    """
    try:
        return ZoneInfo(value.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def describe_timezone(tz) -> str:
    key = getattr(tz, "key", None)
    if key:
        return key
    return datetime.now(tz).strftime("UTC%z")


def parse_time(value: str):
    """Parse a time into (hour, minute), or None if it can't be read.

    Accepts 24-hour ('20:00', '9:30') and the way people actually write times
    ('9pm', '8:30 PM', '8 p.m.'). Bare numbers are read as 24-hour, so '9' is
    9 AM and '21' is 9 PM — which is why /settimes echoes what it parsed.
    """
    text = value.strip().lower().replace(".", "").replace(" ", "")

    meridiem = None
    if text.endswith("am") or text.endswith("pm"):
        meridiem, text = text[-2:], text[:-2]
    elif text.endswith("a") or text.endswith("p"):
        meridiem, text = text[-1] + "m", text[:-1]

    hour_text, _, minute_text = text.partition(":")
    try:
        hour = int(hour_text)
        minute = int(minute_text) if minute_text else 0
    except ValueError:
        return None

    if not 0 <= minute <= 59:
        return None

    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    elif not 0 <= hour <= 23:
        return None

    return hour, minute


WEEKDAY_NAMES = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "tuesday's": 1,
    "wednesday": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def _build_date(year, month, day, today):
    """Assemble a date, rolling a yearless one forward if it's already gone.

    Someone typing "1/5" in December means next January, not ten months ago.
    """
    try:
        if year is not None:
            return datetime(year, month, day).date()
        candidate = datetime(today.year, month, day).date()
        if candidate < today:
            candidate = datetime(today.year + 1, month, day).date()
        return candidate
    except ValueError:
        return None  # 31 February, and friends


def parse_date(value: str, today):
    """Read a date the way people write one, or None if it can't be read.

    Handles "today", "tomorrow", "friday", "next friday", "in 3 days",
    "2026-09-12", "9/12", "9/12/26", "sep 12", "12 sept", "September 12 2026".

    Slash dates are read US-style (month first), since that's what this bot's
    default timezone implies — except when the first number can't be a month,
    which makes "13/5" unambiguous. /rsvp echoes back the date it settled on,
    so a misread is visible rather than silent.
    """
    text = re.sub(r"\s+", " ", value.strip().lower().replace(",", " ")).strip()
    if not text:
        return None

    if text in ("today", "tonight", "tonite", "now"):
        return today
    if text in ("tomorrow", "tmr", "tmrw", "tom", "tomorow"):
        return today + timedelta(days=1)
    if text in ("day after tomorrow", "overmorrow"):
        return today + timedelta(days=2)

    match = re.fullmatch(r"(?:in )?\+?(\d{1,3})(?: ?d| ?days?)?", text)
    if match and (text.startswith("in ") or text.startswith("+") or text[-1] in "sd"):
        return today + timedelta(days=int(match.group(1)))

    match = re.fullmatch(r"(next|this|coming)? ?([a-z']+)", text)
    if match and match.group(2) in WEEKDAY_NAMES:
        ahead = (WEEKDAY_NAMES[match.group(2)] - today.weekday()) % 7
        if match.group(1) == "next":
            # "next friday" means the one after this week's, even mid-week.
            ahead += 7 if ahead else 7
        return today + timedelta(days=ahead)

    match = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if match:
        return _build_date(
            int(match.group(1)), int(match.group(2)), int(match.group(3)), today
        )

    match = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})(?:[-/.](\d{2,4}))?", text)
    if match:
        first, second = int(match.group(1)), int(match.group(2))
        month, day = first, second
        if first > 12 >= second:
            month, day = second, first  # can only be day-first
        year = match.group(3)
        if year is not None:
            year = int(year)
            if year < 100:
                year += 2000
        return _build_date(year, month, day, today)

    month = day = year = None
    for token in text.replace(" of ", " ").split(" "):
        token = token.strip(".")
        if token in MONTH_NAMES:
            month = MONTH_NAMES[token]
            continue
        digits = re.sub(r"(st|nd|rd|th)$", "", token)
        if not digits.isdigit():
            return None
        number = int(digits)
        if number >= 1000 or (day is not None and year is None):
            year = number
        elif day is None:
            day = number
        else:
            return None

    if month is None or day is None:
        return None
    return _build_date(year, month, day, today)


def format_label(label: str) -> str:
    """Render a stored 'HH:MM' slot label the way people read times."""
    parsed = parse_time(label)
    if parsed is None:
        return label
    hour, minute = parsed
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def build_timestamp(time_label: str, date_str: str, tz) -> int:
    """Convert a time label + date string + timezone into a Unix timestamp."""
    hour, minute = parse_time(time_label)
    year, month, day = (int(p) for p in date_str.split("-"))
    dt = datetime(year, month, day, hour, minute, tzinfo=tz)
    return int(dt.timestamp())


def slot_datetime(ts: int, tz) -> datetime:
    return datetime.fromtimestamp(ts, tz=tz)


def format_slot_time(ts: int, tz) -> str:
    """12-hour time with no leading zero, e.g. '7:00 PM'.

    Built by hand because the no-pad hour directive is platform specific
    ('%-I' on glibc, '%#I' on Windows) and neither is portable.
    """
    dt = slot_datetime(ts, tz)
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {dt.strftime('%p')}"


def format_slot_date(ts: int, tz) -> str:
    dt = slot_datetime(ts, tz)
    return f"{dt.strftime('%A, %B')} {dt.day}"


def format_zone_label(ts: int, tz) -> str:
    """Short zone name for the given instant, e.g. 'EST' or 'EDT'."""
    return slot_datetime(ts, tz).strftime("%Z")


# Plain-text labels instead of unicode emoji for the column headers — keeps
# them legible even when no color-emoji font is installed.
STATUS_LABELS = {
    "✅": "YES",
    "❌": "NO",
    "❓": "MAYBE",
}

# ---------------------------------------------------------------------------
# Font loading
#
# python:*-slim ships no fonts at all. The old code silently fell back to
# ImageFont.load_default() — a fixed ~11px bitmap face — which made every
# requested size a no-op and rendered anything outside basic Latin as a tofu
# box. Install fonts-dejavu-core (and fonts-noto-color-emoji) in the image;
# this loader now shouts if it still can't find a real font.
# ---------------------------------------------------------------------------

FONT_DIRS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts"),
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "C:/Windows/Fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
]

REGULAR_FONT_NAMES = ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "arial.ttf"]
BOLD_FONT_NAMES = ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf"]
EMOJI_FONT_NAMES = ["NotoColorEmoji.ttf", "seguiemj.ttf", "AppleColorEmoji.ttc"]

_path_cache = {}
_font_cache = {}
_warned = set()


def _locate_font(filenames: list):
    """Find the first of `filenames` present in any known font directory."""
    key = tuple(filenames)
    if key in _path_cache:
        return _path_cache[key]

    found = None
    for name in filenames:
        for directory in FONT_DIRS:
            candidate = os.path.join(directory, name)
            if os.path.exists(candidate):
                found = candidate
                break
        if found:
            break

    if found is None:  # nothing in the usual spots — sweep subdirectories
        wanted = set(filenames)
        for directory in FONT_DIRS:
            if not os.path.isdir(directory):
                continue
            for root, _dirs, files in os.walk(directory):
                hit = wanted.intersection(files)
                if hit:
                    found = os.path.join(root, sorted(hit)[0])
                    break
            if found:
                break

    _path_cache[key] = found
    return found


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    cache_key = (size, bold)
    if cache_key in _font_cache:
        return _font_cache[cache_key]

    override = os.environ.get("SUMMARY_FONT_BOLD" if bold else "SUMMARY_FONT")
    path = override if override and os.path.exists(override) else None
    if path is None:
        path = _locate_font(BOLD_FONT_NAMES if bold else REGULAR_FONT_NAMES)
    if path is None and bold:  # bold missing but regular present is fine
        path = _locate_font(REGULAR_FONT_NAMES)

    if path is None:
        if "body" not in _warned:
            print(
                "WARNING: no TrueType font found — summary images will use the "
                "tiny bitmap fallback and non-ASCII text will render as boxes. "
                "Install fonts-dejavu-core in the image.",
                flush=True,
            )
            _warned.add("body")
        font = ImageFont.load_default()
    else:
        font = ImageFont.truetype(path, size)

    _font_cache[cache_key] = font
    return font


def load_emoji_font(size: int):
    """Return (font, native_size) for color emoji, or (None, 0) if unavailable.

    Noto Color Emoji is a CBDT bitmap face that only accepts one specific pixel
    size (109); Segoe UI Emoji and Apple Color Emoji scale freely. Callers use
    native_size to decide whether the glyph needs resizing after rendering.
    """
    cache_key = ("emoji", size)
    if cache_key in _font_cache:
        return _font_cache[cache_key]

    path = _locate_font(EMOJI_FONT_NAMES)
    result = (None, 0)
    if path:
        try:
            result = (ImageFont.truetype(path, size), size)
        except OSError:
            try:
                result = (ImageFont.truetype(path, 109), 109)
            except OSError:
                result = (None, 0)
    elif "emoji" not in _warned:
        print(
            "NOTE: no color emoji font found — emoji in RSVP titles will be "
            "omitted from the summary image. Install fonts-noto-color-emoji.",
            flush=True,
        )
        _warned.add("emoji")

    _font_cache[cache_key] = result
    return result


# Consecutive emoji codepoints are grouped so ZWJ sequences (e.g. 👨‍👩‍👧)
# render as the single glyph they're meant to be.
EMOJI_RUN = re.compile(
    "([\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3"
    "\U0001F1E6-\U0001F1FF]+)"
)


def draw_rich_text(img, draw, xy, text, font, fill, px):
    """Draw `text`, rendering any emoji runs with the color emoji font.

    Falls back to skipping emoji entirely when no emoji font is installed,
    which looks far better than a row of tofu boxes.
    """
    x, y = xy
    for part in EMOJI_RUN.split(text):
        if not part:
            continue
        if not EMOJI_RUN.fullmatch(part):
            draw.text((x, y), part, font=font, fill=fill)
            x += draw.textlength(part, font=font)
            continue

        emoji_font, native = load_emoji_font(px)
        if emoji_font is None:
            continue
        if native == px:
            draw.text((x, y), part, font=emoji_font, fill=fill, embedded_color=True)
            x += draw.textlength(part, font=emoji_font)
            continue

        # Render at the face's native size, then scale down to the text size.
        scratch = Image.new("RGBA", (native * 4, native * 2), (0, 0, 0, 0))
        ImageDraw.Draw(scratch).text((0, 0), part, font=emoji_font, embedded_color=True)
        bbox = scratch.getbbox()
        if not bbox:
            continue
        glyph = scratch.crop(bbox)
        ratio = px / glyph.height
        glyph = glyph.resize(
            (max(1, int(glyph.width * ratio)), max(1, int(glyph.height * ratio))),
            Image.LANCZOS,
        )
        img.paste(glyph, (int(x), int(y)), glyph)
        x += glyph.width
    return x


def truncate_to_width(text: str, font, max_width: float) -> str:
    if font.getlength(text) <= max_width:
        return text
    cut = len(text)
    while cut > 1 and font.getlength(text[:cut] + "…") > max_width:
        cut -= 1
    return text[:cut] + "…"


# ---------------------------------------------------------------------------
# Avatars
# ---------------------------------------------------------------------------

# user_id -> (avatar_key, image_bytes). Discord avatar URLs are content
# addressed, so a cached entry stays valid until the member changes their
# picture — which the key comparison below catches.
_avatar_cache = {}

PLACEHOLDER_COLORS = [
    (114, 137, 218),
    (87, 174, 148),
    (217, 138, 82),
    (176, 108, 191),
    (197, 91, 106),
]


# Avatars download concurrently, but not unboundedly — a large roster
# shouldn't fire a hundred simultaneous requests at the CDN.
AVATAR_FETCH_LIMIT = 10


async def fetch_avatars(guild: discord.Guild, user_ids) -> dict:
    """Download each member's avatar once. Returns {user_id: image_bytes}.

    Runs concurrently: fetched one at a time, a full roster added a visible
    delay to /summary before rendering could begin.
    """
    limit = asyncio.Semaphore(AVATAR_FETCH_LIMIT)

    async def fetch_one(uid):
        member = guild.get_member(uid)
        if member is None:
            return uid, None

        asset = member.display_avatar
        try:
            asset = asset.with_size(128).with_static_format("png")
        except (ValueError, AttributeError):
            pass

        cached = _avatar_cache.get(uid)
        if cached and cached[0] == asset.key:
            return uid, cached[1]

        async with limit:
            try:
                data = await asset.read()
            except (discord.HTTPException, discord.NotFound):
                return uid, None

        _avatar_cache[uid] = (asset.key, data)
        return uid, data

    results = await asyncio.gather(
        *(fetch_one(uid) for uid in user_ids), return_exceptions=True
    )

    avatars = {}
    for result in results:
        if isinstance(result, BaseException):
            continue
        uid, data = result
        if data is not None:
            avatars[uid] = data
    return avatars


_mask_cache = {}


def _circle_mask(size: int) -> Image.Image:
    """Antialiased circular mask. Identical for a given size, so built once."""
    mask = _mask_cache.get(size)
    if mask is None:
        # Built oversized and downsampled so the circle edge is antialiased.
        mask = Image.new("L", (size * 4, size * 4), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size * 4 - 1, size * 4 - 1], fill=255)
        mask = mask.resize((size, size), Image.LANCZOS)
        _mask_cache[size] = mask
    return mask


def build_avatar(raw: bytes, name: str, size: int) -> Image.Image:
    """A circular avatar, or a colored initial when there's no usable image."""
    if raw:
        try:
            source = Image.open(io.BytesIO(raw)).convert("RGBA")
            avatar = ImageOps.fit(
                source, (size, size), Image.LANCZOS, centering=(0.5, 0.5)
            )
            avatar.putalpha(_circle_mask(size))
            return avatar
        except OSError:
            pass  # unreadable/corrupt download — fall through to the initial

    color = PLACEHOLDER_COLORS[sum(name.encode("utf-8")) % len(PLACEHOLDER_COLORS)]
    avatar = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    plate = Image.new("RGBA", (size, size), color + (255,))
    plate.putalpha(_circle_mask(size))
    avatar.alpha_composite(plate)

    initial = name.strip()[:1].upper() or "?"
    font = load_font(int(size * 0.5), bold=True)
    scratch = ImageDraw.Draw(avatar)
    box = scratch.textbbox((0, 0), initial, font=font)
    scratch.text(
        (
            (size - (box[2] - box[0])) / 2 - box[0],
            (size - (box[3] - box[1])) / 2 - box[1],
        ),
        initial,
        font=font,
        fill=(255, 255, 255),
    )
    return avatar


# A letter goes in every grid cell, not just a colour. Checked rather than
# assumed: the worst adjacent pair here is ❌ vs ✅ at ΔE 7.7 under deuteranopia,
# which is inside the band where colour alone is not enough. Normal-vision
# separation (21.1) and contrast against the card both pass, so the Discord
# colours themselves stay — they just never carry meaning on their own.
STATUS_MARKS = {
    "✅": "Y",
    "❌": "N",
    "❓": "?",
}

# Rendered at 2x and downsampled at the end, which keeps text crisp on the
# high-DPI displays Discord is usually viewed on.
SCALE = 2

PAGE_BG = (30, 31, 34)
CARD_BG = (43, 45, 49)
CARD_BG_BEST = (40, 52, 45)
CARD_BORDER_BEST = (59, 165, 93)
TEXT = (242, 243, 245)
TEXT_MUTED = (148, 155, 164)
TEXT_FAINT = (106, 111, 120)
DIVIDER = (58, 61, 66)

# Past this many people a column gets a "+N more" tail instead of growing the
# image without bound on busy servers.
MAX_NAMES_PER_COLUMN = 12


def _s(value: int) -> int:
    return int(value) * SCALE


CELL_INK = (16, 20, 18)
CELL_EMPTY = (52, 55, 60)
GRID_MAX_ROWS = 25


def build_grid_image(
    event: dict, guild: discord.Guild, avatars: dict = None
) -> io.BytesIO:
    """A compact who-by-when matrix: people down the side, slots across the top.

    The detailed card layout runs to ~2000px for a full roster, which is a wall
    when it lives permanently in a channel. This says the same thing in about a
    quarter of the height, at the cost of names appearing once instead of once
    per slot.
    """
    avatars = avatars or {}
    title_font = load_font(_s(26), bold=True)
    subtitle_font = load_font(_s(13))
    column_font = load_font(_s(12), bold=True)
    name_font = load_font(_s(13))
    mark_font = load_font(_s(13), bold=True)
    total_font = load_font(_s(12), bold=True)
    legend_font = load_font(_s(12))

    PAD = _s(28)
    NAME_W = _s(206)
    PITCH = _s(84)
    CELL_W = _s(74)
    CELL_H = _s(22)
    ROW_H = _s(30)
    AVATAR = _s(20)
    RADIUS = _s(6)

    slots = list(event["slots"].values())
    WIDTH = PAD * 2 + NAME_W + PITCH * len(slots)

    # --- Measure ------------------------------------------------------------
    names = {}
    for slot in slots:
        for emoji in STATUS_ORDER:
            for uid in slot["votes"][emoji]:
                if uid not in names:
                    member = guild.get_member(uid)
                    names[uid] = member.display_name if member else f"User {uid}"

    people = sorted(names.items(), key=lambda item: item[1].casefold())
    hidden = max(0, len(people) - GRID_MAX_ROWS)
    people = people[:GRID_MAX_ROWS]

    def status_of(slot, uid):
        for emoji in STATUS_ORDER:
            if uid in slot["votes"][emoji]:
                return emoji
        return None

    header_h = PAD + _s(32) + _s(6) + _s(17) + _s(18)
    columns_h = _s(24)
    rows_h = len(people) * ROW_H
    hidden_h = _s(20) if hidden else 0
    totals_h = _s(34)
    legend_h = _s(28)
    height = header_h + columns_h + rows_h + hidden_h + totals_h + legend_h + PAD

    # --- Draw ---------------------------------------------------------------
    img = Image.new("RGB", (WIDTH, height), PAGE_BG)
    draw = ImageDraw.Draw(img)

    draw_rich_text(img, draw, (PAD, PAD), event["title"], title_font, TEXT, _s(26))

    first_ts = slots[0]["timestamp"]
    person_word = "person" if len(names) == 1 else "people"
    draw.text(
        (PAD, PAD + _s(32) + _s(6)),
        f"{format_slot_date(first_ts, event['tz'])}"
        f"  ·  all times {format_zone_label(first_ts, event['tz'])}"
        f"  ·  {len(names)} {person_word} responded",
        font=subtitle_font,
        fill=TEXT_MUTED,
    )

    def column_x(index):
        return PAD + NAME_W + index * PITCH

    # Column headers
    for index, slot in enumerate(slots):
        draw.text(
            (column_x(index), header_h),
            format_slot_time(slot["timestamp"], event["tz"]),
            font=column_font,
            fill=TEXT_MUTED,
        )

    rule_y = header_h + columns_h - _s(6)
    draw.line([PAD, rule_y, WIDTH - PAD, rule_y], fill=DIVIDER, width=SCALE)

    # One row per person
    avatar_images = {}
    row_y = header_h + columns_h
    for uid, display_name in people:
        avatar = avatar_images.get(uid)
        if avatar is None:
            avatar = build_avatar(avatars.get(uid), display_name, AVATAR)
            avatar_images[uid] = avatar
        img.paste(avatar, (PAD, row_y + _s(3)), avatar)

        draw.text(
            (PAD + AVATAR + _s(8), row_y + _s(6)),
            truncate_to_width(display_name, name_font, NAME_W - AVATAR - _s(16)),
            font=name_font,
            fill=TEXT,
        )

        for index, slot in enumerate(slots):
            status = status_of(slot, uid)
            x = column_x(index)
            box = [x, row_y + _s(4), x + CELL_W, row_y + _s(4) + CELL_H]

            if status is None:
                # Didn't answer this slot. Deliberately flat and unlabelled so
                # it reads as absence rather than as a fourth answer.
                draw.rounded_rectangle(box, radius=RADIUS, fill=CELL_EMPTY)
                continue

            draw.rounded_rectangle(box, radius=RADIUS, fill=STATUS_COLORS[status])
            mark = STATUS_MARKS[status]
            mark_w = draw.textlength(mark, font=mark_font)
            draw.text(
                (x + (CELL_W - mark_w) / 2, row_y + _s(7)),
                mark,
                font=mark_font,
                fill=CELL_INK,
            )

        row_y += ROW_H

    if hidden:
        draw.text(
            (PAD, row_y + _s(2)),
            f"+{hidden} more",
            font=legend_font,
            fill=TEXT_FAINT,
        )
        row_y += hidden_h

    # Yes-count per slot, with the best turnout called out
    totals = [len(slot["votes"]["✅"]) for slot in slots]
    best = max(totals, default=0)

    draw.line([PAD, row_y + _s(4), WIDTH - PAD, row_y + _s(4)], fill=DIVIDER, width=SCALE)
    draw.text(
        (PAD + AVATAR + _s(8), row_y + _s(14)),
        "YES",
        font=total_font,
        fill=STATUS_COLORS["✅"],
    )
    for index, count in enumerate(totals):
        winning = best > 0 and count == best
        label = str(count)
        label_w = draw.textlength(label, font=total_font)
        x = column_x(index) + (CELL_W - label_w) / 2
        draw.text(
            (x, row_y + _s(14)),
            label,
            font=total_font,
            fill=STATUS_COLORS["✅"] if winning else TEXT_MUTED,
        )
        if winning:
            draw.line(
                [column_x(index), row_y + _s(10), column_x(index) + CELL_W, row_y + _s(10)],
                fill=STATUS_COLORS["✅"],
                width=_s(2),
            )

    # Legend — the letters are what carry the meaning, the colour reinforces it
    legend_y = row_y + totals_h
    x = PAD
    for emoji in STATUS_ORDER:
        draw.rounded_rectangle(
            [x, legend_y, x + _s(18), legend_y + _s(14)],
            radius=_s(4),
            fill=STATUS_COLORS[emoji],
        )
        mark = STATUS_MARKS[emoji]
        draw.text(
            (x + (_s(18) - draw.textlength(mark, font=legend_font)) / 2, legend_y + _s(1)),
            mark,
            font=legend_font,
            fill=CELL_INK,
        )
        label = STATUS_EMOJIS[emoji]
        draw.text((x + _s(24), legend_y + _s(1)), label, font=legend_font, fill=TEXT_MUTED)
        x += _s(24) + draw.textlength(label, font=legend_font) + _s(18)

    draw.rounded_rectangle(
        [x, legend_y, x + _s(18), legend_y + _s(14)], radius=_s(4), fill=CELL_EMPTY
    )
    draw.text((x + _s(24), legend_y + _s(1)), "No response", font=legend_font, fill=TEXT_MUTED)

    img = img.resize((WIDTH // SCALE, height // SCALE), Image.LANCZOS)
    img = img.quantize(colors=256, method=Image.FASTOCTREE, dither=Image.Dither.NONE)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer


def build_summary_image(
    event: dict, guild: discord.Guild, avatars: dict = None
) -> io.BytesIO:
    avatars = avatars or {}
    title_font = load_font(_s(30), bold=True)
    subtitle_font = load_font(_s(14))
    time_font = load_font(_s(19), bold=True)
    badge_font = load_font(_s(11), bold=True)
    status_font = load_font(_s(14), bold=True)
    name_font = load_font(_s(13))
    footer_font = load_font(_s(12))

    WIDTH = _s(880)
    PAD = _s(28)
    CARD_PAD = _s(16)
    CARD_GAP = _s(10)
    COL_GAP = _s(14)
    ROW_H = _s(30)
    AVATAR = _s(24)
    RADIUS = _s(10)
    DOT_R = _s(4)

    TITLE_H = _s(36)
    SUBTITLE_H = _s(18)
    TIME_H = _s(24)
    STATUS_H = _s(22)
    GAP_TIME_TO_COLS = _s(12)

    col_width = (WIDTH - 2 * PAD - 2 * CARD_PAD - 2 * COL_GAP) // 3

    name_width = col_width - AVATAR - _s(8)

    def resolve_people(uids):
        people = []
        for uid in uids:
            member = guild.get_member(uid)
            people.append((uid, member.display_name if member else f"User {uid}"))
        return sorted(people, key=lambda person: person[1].casefold())

    # --- Measure ------------------------------------------------------------
    # Every card's height and final y is computed here, and the draw pass below
    # consumes those exact values. Nothing advances y on its own during drawing,
    # so the canvas can never come up short of the content again.
    cards = []
    responders = set()
    for slot in event["slots"].values():
        columns = []
        max_rows = 1
        for emoji in STATUS_ORDER:
            voters = slot["votes"][emoji]
            responders.update(voters)
            people = resolve_people(voters)
            count = len(people)

            shown = people[:MAX_NAMES_PER_COLUMN]
            overflow = count - len(shown)
            rows = max(1, len(shown) + (1 if overflow else 0))
            max_rows = max(max_rows, rows)

            columns.append(
                {
                    "label": STATUS_LABELS[emoji],
                    "color": STATUS_COLORS[emoji],
                    "count": count,
                    "people": shown,
                    "overflow": overflow,
                }
            )

        cards.append(
            {
                "time": format_slot_time(slot["timestamp"], event["tz"]),
                "columns": columns,
                "yes": len(slot["votes"]["✅"]),
                "height": (
                    CARD_PAD * 2
                    + TIME_H
                    + GAP_TIME_TO_COLS
                    + STATUS_H
                    + max_rows * ROW_H
                ),
            }
        )

    best_yes = max((c["yes"] for c in cards), default=0)

    header_h = PAD + TITLE_H + _s(8) + SUBTITLE_H + _s(20)
    y = header_h
    for card in cards:
        card["y"] = y
        y += card["height"] + CARD_GAP
    cards_bottom = y - CARD_GAP if cards else header_h

    footer_y = cards_bottom + _s(18)
    height = footer_y + _s(14) + PAD

    # --- Draw ---------------------------------------------------------------
    img = Image.new("RGB", (WIDTH, height), PAGE_BG)
    draw = ImageDraw.Draw(img)

    draw_rich_text(img, draw, (PAD, PAD), event["title"], title_font, TEXT, _s(30))

    first_ts = next(iter(event["slots"].values()))["timestamp"]
    slot_word = "slot" if len(cards) == 1 else "slots"
    person_word = "person" if len(responders) == 1 else "people"
    # The zone has to be stated: unlike Discord's <t:...> markdown, a rendered
    # image shows every viewer the same clock regardless of where they are.
    zone = format_zone_label(first_ts, event["tz"])
    subtitle = (
        f"{format_slot_date(first_ts, event['tz'])}"
        f"  ·  all times {zone}"
        f"  ·  {len(responders)} {person_word} responded"
        f"  ·  {len(cards)} time {slot_word}"
    )
    draw.text((PAD, PAD + TITLE_H + _s(8)), subtitle, font=subtitle_font, fill=TEXT_MUTED)

    rule_y = header_h - _s(10)
    draw.line([PAD, rule_y, WIDTH - PAD, rule_y], fill=DIVIDER, width=SCALE)

    avatar_images = {}

    for card in cards:
        top = card["y"]
        bottom = top + card["height"]
        is_best = best_yes > 0 and card["yes"] == best_yes

        draw.rounded_rectangle(
            [PAD, top, WIDTH - PAD, bottom],
            radius=RADIUS,
            fill=CARD_BG_BEST if is_best else CARD_BG,
            outline=CARD_BORDER_BEST if is_best else None,
            width=SCALE if is_best else 0,
        )

        text_x = PAD + CARD_PAD
        draw.text((text_x, top + CARD_PAD), card["time"], font=time_font, fill=TEXT)

        if is_best:
            label = "BEST TURNOUT"
            text_w = draw.textlength(label, font=badge_font)
            pill_w = text_w + _s(20)
            pill_h = _s(20)
            pill_x = WIDTH - PAD - CARD_PAD - pill_w
            pill_y = top + CARD_PAD + _s(2)
            draw.rounded_rectangle(
                [pill_x, pill_y, pill_x + pill_w, pill_y + pill_h],
                radius=pill_h // 2,
                fill=CARD_BORDER_BEST,
            )
            draw.text(
                (pill_x + _s(10), pill_y + _s(4)),
                label,
                font=badge_font,
                fill=(14, 24, 18),
            )

        col_y = top + CARD_PAD + TIME_H + GAP_TIME_TO_COLS
        for index, column in enumerate(card["columns"]):
            col_x = text_x + index * (col_width + COL_GAP)

            dot_cy = col_y + _s(7)
            draw.ellipse(
                [col_x, dot_cy - DOT_R, col_x + 2 * DOT_R, dot_cy + DOT_R],
                fill=column["color"],
            )
            draw.text(
                (col_x + 2 * DOT_R + _s(8), col_y),
                f"{column['label']}  {column['count']}",
                font=status_font,
                fill=column["color"],
            )

            row_y = col_y + STATUS_H
            if not column["people"]:
                draw.text(
                    (col_x, row_y + _s(4)), "—", font=name_font, fill=TEXT_FAINT
                )

            for uid, display_name in column["people"]:
                # Someone who answered every slot would otherwise have their
                # avatar composited once per slot instead of once.
                avatar = avatar_images.get(uid)
                if avatar is None:
                    avatar = build_avatar(avatars.get(uid), display_name, AVATAR)
                    avatar_images[uid] = avatar
                img.paste(avatar, (col_x, row_y), avatar)
                draw.text(
                    (col_x + AVATAR + _s(8), row_y + _s(5)),
                    truncate_to_width(display_name, name_font, name_width),
                    font=name_font,
                    fill=TEXT,
                )
                row_y += ROW_H

            if column["overflow"]:
                draw.text(
                    (col_x, row_y + _s(5)),
                    f"+{column['overflow']} more",
                    font=name_font,
                    fill=TEXT_FAINT,
                )

    draw.text(
        (PAD, footer_y),
        "React Yes / No / Maybe on a time slot to update this summary.",
        font=footer_font,
        fill=TEXT_FAINT,
    )

    img = img.resize((WIDTH // SCALE, height // SCALE), Image.LANCZOS)

    # The palette here is a handful of flat UI colors plus small avatar photos,
    # so 256 colors is visually indistinguishable and cuts the file to roughly
    # a quarter — a quarter of the bytes Discord has to accept on upload.
    img = img.quantize(colors=256, method=Image.FASTOCTREE, dither=Image.Dither.NONE)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer


# ---------------------------------------------------------------------------
# Persistence
#
# Everything above lives in dictionaries, so a redeploy used to lose every
# setting and every running RSVP. That was merely annoying until the live
# summary arrived — a pinned image that stops updating looks current and isn't,
# which is worse than not being there.
# ---------------------------------------------------------------------------

STATE_VERSION = 1
STATE_SAVE_DELAY = 2.0
_state_save_task = None
_state_loaded = False


def state_path() -> str:
    return os.environ.get(
        "STATE_FILE",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
    )


def serialize_state() -> dict:
    return {
        "version": STATE_VERSION,
        "guild_times": {str(k): v for k, v in guild_times.items()},
        "guild_timezones": {
            str(k): describe_timezone(v) for k, v in guild_timezones.items()
        },
        "guild_seed_mode": {str(k): v for k, v in guild_seed_mode.items()},
        "guild_live_summary": {str(k): bool(v) for k, v in guild_live_summary.items()},
        "guild_live_channel": {str(k): v for k, v in guild_live_channel.items()},
        "guild_live_mode": {str(k): v for k, v in guild_live_mode.items()},
        "guild_live_message": {str(k): v for k, v in guild_live_message.items()},
        "guild_live_pin": {str(k): bool(v) for k, v in guild_live_pin.items()},
        "rosters": {str(k): v for k, v in rosters.items()},
        "guild_events": {str(k): list(v) for k, v in guild_events.items()},
        "bot_seeded": [[message_id, emoji] for message_id, emoji in bot_seeded],
        "events": {
            event_id: {
                "title": event["title"],
                "guild_id": event["guild_id"],
                "channel_id": event["channel_id"],
                "tz": describe_timezone(event["tz"]),
                "mode": event["mode"],
                "live_message_id": event.get("live_message_id"),
                "live_channel_id": event.get("live_channel_id"),
                "slots": {
                    label: {
                        "message_id": slot["message_id"],
                        "timestamp": slot["timestamp"],
                        "votes": {
                            emoji: sorted(slot["votes"][emoji])
                            for emoji in STATUS_ORDER
                        },
                    }
                    for label, slot in event["slots"].items()
                },
            }
            for event_id, event in active_events.items()
        },
    }


def save_state() -> None:
    path = state_path()
    try:
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(serialize_state(), handle)
        # Swap it in atomically, so a crash mid-write can't leave a truncated
        # file that then fails to load on the way back up.
        os.replace(temporary, path)
    except OSError as error:
        print(f"WARNING: couldn't save state to {path}: {error}", flush=True)


def save_state_soon() -> None:
    """Coalesce writes — a burst of answers shouldn't be a burst of disk I/O."""
    global _state_save_task

    if _state_save_task is not None and not _state_save_task.done():
        return

    async def save_after_delay():
        await asyncio.sleep(STATE_SAVE_DELAY)
        await asyncio.to_thread(save_state)

    try:
        _state_save_task = asyncio.create_task(save_after_delay())
        _state_save_task.add_done_callback(_background_done)
    except RuntimeError:
        save_state()  # no event loop running — just write it


def load_state() -> None:
    path = state_path()
    if not os.path.exists(path):
        return

    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        print(
            f"WARNING: couldn't read saved state from {path} ({error}); "
            "starting empty.",
            flush=True,
        )
        return

    if data.get("version") != STATE_VERSION:
        print(
            f"NOTE: {path} was written by a different version of this bot; "
            "ignoring it rather than guessing at the format.",
            flush=True,
        )
        return

    guild_times.update({int(k): v for k, v in data.get("guild_times", {}).items()})
    rosters.update({int(k): v for k, v in data.get("rosters", {}).items()})
    guild_live_summary.update(
        {int(k): bool(v) for k, v in data.get("guild_live_summary", {}).items()}
    )
    guild_live_channel.update(
        {int(k): int(v) for k, v in data.get("guild_live_channel", {}).items()}
    )
    guild_live_message.update(
        {int(k): int(v) for k, v in data.get("guild_live_message", {}).items()}
    )
    guild_live_pin.update(
        {int(k): bool(v) for k, v in data.get("guild_live_pin", {}).items()}
    )
    guild_live_mode.update(
        {
            int(k): v
            for k, v in data.get("guild_live_mode", {}).items()
            if v in LIVE_MODES
        }
    )
    guild_seed_mode.update(
        {
            int(k): v
            for k, v in data.get("guild_seed_mode", {}).items()
            if v in RSVP_MODES
        }
    )

    for key, name in data.get("guild_timezones", {}).items():
        zone = resolve_timezone(name)
        if zone is not None:
            guild_timezones[int(key)] = zone

    for event_id, raw in data.get("events", {}).items():
        slots = {}
        for label, saved in raw.get("slots", {}).items():
            slots[label] = {
                "message_id": saved["message_id"],
                "timestamp": saved["timestamp"],
                "votes": {
                    emoji: set(saved.get("votes", {}).get(emoji, []))
                    for emoji in STATUS_ORDER
                },
            }
            message_index[saved["message_id"]] = (event_id, label)

        active_events[event_id] = {
            "id": event_id,
            "title": raw["title"],
            "guild_id": raw["guild_id"],
            "channel_id": raw["channel_id"],
            # A zone that no longer resolves (a renamed IANA entry, or a fixed
            # offset from before those were dropped) falls back rather than
            # taking the whole RSVP down with it.
            "tz": resolve_timezone(raw.get("tz", "")) or DEFAULT_TZ,
            "mode": raw.get("mode", CLEAR_PLACEHOLDERS),
            "live_message_id": raw.get("live_message_id"),
            "live_channel_id": raw.get("live_channel_id"),
            "slots": slots,
        }

    for key, event_ids in data.get("guild_events", {}).items():
        guild_events[int(key)] = [
            event_id for event_id in event_ids if event_id in active_events
        ]

    for entry in data.get("bot_seeded", []):
        if isinstance(entry, list) and len(entry) == 2:
            bot_seeded.add((entry[0], entry[1]))

    if active_events:
        print(
            f"Restored {len(active_events)} RSVP(s) from {path}",
            flush=True,
        )


@bot.event
async def on_ready():
    global _state_loaded

    # on_ready fires again on every reconnect; only read the file once.
    if not _state_loaded:
        _state_loaded = True
        load_state()

        # Buttons stop responding across a restart unless their view is
        # re-registered against the message it was posted on.
        for event_id, event in active_events.items():
            if event["mode"] != BUTTON_MODE:
                continue
            for label, slot in event["slots"].items():
                bot.add_view(
                    RSVPView(event_id, label), message_id=slot["message_id"]
                )

    await bot.tree.sync()
    # State the resolved timezone at startup, so "is it on Eastern?" is
    # answerable from the logs instead of by posting an RSVP to find out.
    # Anything other than EST/EDT here means the tzdata fallback is active.
    now = int(datetime.now(timezone.utc).timestamp())
    print(
        f"Logged in as {bot.user} — default timezone "
        f"{describe_timezone(DEFAULT_TZ)}, currently "
        f"{format_zone_label(now, DEFAULT_TZ)} ({format_slot_time(now, DEFAULT_TZ)})",
        flush=True,
    )


@bot.tree.command(name="setroster", description="Set the role used as the roster for /reactping")
@app_commands.describe(role="The role whose members count as the roster")
@app_commands.checks.has_permissions(manage_guild=True)
async def setroster(interaction: discord.Interaction, role: discord.Role):
    rosters[interaction.guild_id] = role.id
    save_state_soon()
    await interaction.response.send_message(
        f"Roster set to {role.mention}. `/reactping` will check its members.",
        ephemeral=True,
    )


@bot.tree.command(name="settimes", description="Customize the time slots /rsvp uses")
@app_commands.describe(times="Comma-separated times, e.g. 8pm, 8:30pm, 9pm (24-hour also works)")
@app_commands.checks.has_permissions(manage_guild=True)
async def settimes(interaction: discord.Interaction, times: str):
    entries = [t.strip() for t in times.split(",") if t.strip()]
    if not entries:
        await interaction.response.send_message("Give at least one time.", ephemeral=True)
        return

    slots = []
    for entry in entries:
        parsed = parse_time(entry)
        if parsed is None:
            await interaction.response.send_message(
                f"Couldn't read `{entry}`. Write times like `8pm`, `8:30pm` or "
                "`9 PM` — 24-hour (`20:00`) works too.\n"
                "For example: `/settimes times:8pm, 8:30pm, 9pm, 9:30pm`",
                ephemeral=True,
            )
            return

        # Stored 24-hour so the label is canonical, and deduplicated because a
        # slot's data is keyed by this label — two identical entries would post
        # two messages that then fought over one slot.
        label = f"{parsed[0]:02d}:{parsed[1]:02d}"
        if label not in slots:
            slots.append(label)

    guild_times[interaction.guild_id] = slots
    save_state_soon()

    readable = ", ".join(format_label(label) for label in slots)
    dropped = len(entries) - len(slots)
    note = f"\n(Ignored {dropped} duplicate{'' if dropped == 1 else 's'}.)" if dropped else ""
    await interaction.response.send_message(
        f"Time slots updated to: **{readable}**{note}", ephemeral=True
    )


@bot.tree.command(name="settimezone", description="Set the timezone /rsvp times are interpreted in")
@app_commands.describe(zone="Timezone name, e.g. America/New_York")
@app_commands.checks.has_permissions(manage_guild=True)
async def settimezone(interaction: discord.Interaction, zone: str):
    resolved = resolve_timezone(zone)
    if resolved is None:
        await interaction.response.send_message(
            f"`{zone}` isn't a timezone name I recognize. Use an IANA name — "
            "for the US that's `America/New_York`, `America/Chicago`, "
            "`America/Denver` or `America/Los_Angeles`.\n"
            "These follow daylight saving on their own, so 8:00 PM stays "
            "8:00 PM year round. The server already defaults to "
            f"`{DEFAULT_TIMEZONE}`, so you may not need this at all.",
            ephemeral=True,
        )
        return

    guild_timezones[interaction.guild_id] = resolved
    save_state_soon()
    now = int(datetime.now(timezone.utc).timestamp())
    await interaction.response.send_message(
        f"Timezone set to **{describe_timezone(resolved)}** "
        f"(currently {format_zone_label(now, resolved)}, "
        f"{format_slot_time(now, resolved)}).",
        ephemeral=True,
    )


MODE_LABELS = {
    CLEAR_PLACEHOLDERS: "Reactions",
    KEEP_PLACEHOLDERS: "Reactions, placeholders kept",
    BUTTON_MODE: "Buttons",
}

MODE_EXPLANATIONS = {
    CLEAR_PLACEHOLDERS: (
        "New RSVPs use **reactions**, and the bot removes its own as soon as "
        "someone picks that option.\n"
        "• Counts on the message are exactly the number of people.\n"
        "• An option whose last vote is withdrawn vanishes for a moment and is "
        "re-added, so the bot has to put the three back in order."
    ),
    KEEP_PLACEHOLDERS: (
        "New RSVPs use **reactions**, and the bot keeps its own ✅ ❌ ❓ on "
        "every message.\n"
        "• All three stay visible and can never fall out of order.\n"
        "• Each count reads one higher than the number of people, since the "
        "bot's own reaction is in it."
    ),
    BUTTON_MODE: (
        "New RSVPs use **buttons** instead of reactions.\n"
        "• Three buttons under each message, with the tally written into the "
        "message itself and updated on every press.\n"
        "• Counts are exact and the options can't move or disappear.\n"
        "• One answer per person per slot — pressing the one you already chose "
        "clears it.\n"
        "• Votes live only in memory, so a restart loses them. So does "
        "everything else here, but reactions at least survive on the message."
    ),
}


@bot.tree.command(
    name="setvoting",
    description="Choose how people answer an RSVP: reactions or buttons",
)
@app_commands.describe(mode="How people answer, and what the bot does with its own reaction")
@app_commands.choices(
    mode=[
        app_commands.Choice(
            name="Reactions, clear the bot's own — exact counts (default)",
            value=CLEAR_PLACEHOLDERS,
        ),
        app_commands.Choice(
            name="Reactions, keep the bot's own — order never shifts, counts read +1",
            value=KEEP_PLACEHOLDERS,
        ),
        app_commands.Choice(
            name="Buttons — exact counts, fixed order, one answer per person",
            value=BUTTON_MODE,
        ),
    ]
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setvoting(interaction: discord.Interaction, mode: str):
    # discord.py hands back either the raw value or the Choice wrapping it,
    # depending on how the parameter is annotated. Accept either.
    value = getattr(mode, "value", mode)
    if value not in RSVP_MODES:
        await interaction.response.send_message(
            "Pick one of the offered options.", ephemeral=True
        )
        return

    guild_seed_mode[interaction.guild_id] = value
    save_state_soon()

    # Each RSVP keeps the mode it was created with — a message posted with
    # buttons can't become a reaction message, or the other way round.
    running = len(events_for_guild(interaction.guild_id))
    note = (
        f"\n\nThe {running} RSVP{'' if running == 1 else 's'} already running "
        "keep the style they were created with."
        if running
        else ""
    )

    await interaction.response.send_message(
        MODE_EXPLANATIONS[value] + note + "\n\nIn every mode the bot is left out "
        "of the tally, so it never shows up in the summary image or counts "
        "toward `/reactping`.",
        ephemeral=True,
    )

    # Bring running reaction RSVPs into line with a keep/clear switch rather
    # than waiting for whatever happens to trigger the next repair.
    for _, event in events_for_guild(interaction.guild_id):
        if event["mode"] != BUTTON_MODE:
            event["mode"] = value if value != BUTTON_MODE else event["mode"]
            run_in_background(
                repair_event(event, bot.get_channel(event["channel_id"]))
            )


# ---------------------------------------------------------------------------
# Live summary
#
# A pinned image that redraws itself as people answer. Editing a message's text
# is cheap; replacing its attachment is a fresh upload every time, so this is
# throttled rather than run per reaction — a burst of twenty clicks produces one
# redraw, and the redraw reflects all twenty.
# ---------------------------------------------------------------------------

LIVE_REFRESH_DELAY = 4.0

# event_id -> pending refresh task
_live_refresh = {}


def schedule_live_refresh(event_id: str) -> None:
    """Queue a redraw, unless one is already queued.

    Deliberately not cancel-and-reschedule: under a steady stream of answers
    that would keep pushing the redraw into the future and never draw anything.
    Letting the pending one stand means at most one redraw per window, and it
    always renders the state as of when it fires.
    """
    event = active_events.get(event_id)
    if event is None or not event.get("live_message_id"):
        return

    pending = _live_refresh.get(event_id)
    if pending is not None and not pending.done():
        return

    async def refresh_after_delay():
        await asyncio.sleep(LIVE_REFRESH_DELAY)
        await refresh_live_summary(event_id)

    _live_refresh[event_id] = asyncio.create_task(refresh_after_delay())
    _live_refresh[event_id].add_done_callback(_background_done)


async def render_live_image(event: dict, guild):
    voter_ids = {
        uid
        for slot in event["slots"].values()
        for voters in slot["votes"].values()
        for uid in voters
    }
    avatars = await fetch_avatars(guild, voter_ids)
    return await asyncio.to_thread(build_grid_image, event, guild, avatars)


def live_summary_content(event: dict, channel_id: int) -> str:
    """The caption above the image.

    Rewritten on every refresh rather than only on the first post, because in
    "latest" mode one message changes which RSVP it's describing.
    """
    # Posted somewhere other than the RSVP itself, the summary has to say where
    # to actually answer — otherwise it's a scoreboard with no game.
    elsewhere = (
        f"  ·  answer in <#{event['channel_id']}>"
        if channel_id != event["channel_id"]
        else ""
    )
    return (
        f"**Live summary — {event['title']}**{elsewhere}"
        "  ·  updates as people answer"
    )


async def refresh_live_summary(event_id: str) -> None:
    event = active_events.get(event_id)
    if event is None or not event.get("live_message_id"):
        return

    guild = bot.get_guild(event["guild_id"])
    channel = bot.get_channel(event.get("live_channel_id") or event["channel_id"])
    if guild is None or channel is None:
        return

    buffer = await render_live_image(event, guild)
    try:
        message = channel.get_partial_message(event["live_message_id"])
        await message.edit(
            content=live_summary_content(event, channel.id),
            attachments=[discord.File(buffer, filename="rsvp_live.png")],
        )
    except (discord.HTTPException, discord.NotFound):
        # Someone deleted it, or we lost access. Stop trying to update it.
        if guild_live_message.get(event["guild_id"]) == event["live_message_id"]:
            guild_live_message.pop(event["guild_id"], None)
        event["live_message_id"] = None


async def start_live_summary(event: dict, fallback_channel, guild) -> None:
    """Give an event a live summary — a new message, or the shared one."""
    if event.get("live_message_id"):
        return

    guild_id = event["guild_id"]
    channel = live_summary_channel(guild_id, fallback_channel)
    if channel is None:
        return

    if guild_live_mode.get(guild_id, LIVE_EACH) == LIVE_LATEST:
        shared = guild_live_message.get(guild_id)
        if shared is not None:
            # Hand the one pinned message to the newest RSVP, and stop whatever
            # was using it from writing over the top.
            for _, other in events_for_guild(guild_id):
                if other is not event and other.get("live_message_id") == shared:
                    other["live_message_id"] = None

            event["live_message_id"] = shared
            event["live_channel_id"] = channel.id
            await refresh_live_summary(event["id"])

            # refresh clears the id if the message turned out to be gone; only
            # then do we fall through and post a replacement.
            if event.get("live_message_id"):
                return
            guild_live_message.pop(guild_id, None)

    buffer = await render_live_image(event, guild)
    try:
        message = await channel.send(
            live_summary_content(event, channel.id),
            file=discord.File(buffer, filename="rsvp_live.png"),
        )
    except discord.HTTPException:
        return

    event["live_message_id"] = message.id
    event["live_channel_id"] = channel.id
    if guild_live_mode.get(guild_id, LIVE_EACH) == LIVE_LATEST:
        guild_live_message[guild_id] = message.id

    if wants_pinned_summary(guild_id):
        try:
            await message.pin()
        except discord.HTTPException:
            pass  # needs Manage Messages, and a channel caps out at 50 pins


async def stop_live_summary(event: dict) -> None:
    """Leave the image in place but mark it as no longer live, and unpin it."""
    message_id = event.get("live_message_id")
    if not message_id:
        return
    event["live_message_id"] = None

    # In "latest" mode the next RSVP should post a fresh one rather than adopt
    # a message that now says it's finished.
    if guild_live_message.get(event["guild_id"]) == message_id:
        guild_live_message.pop(event["guild_id"], None)

    channel = bot.get_channel(event.get("live_channel_id") or event["channel_id"])
    if channel is None:
        return

    message = channel.get_partial_message(message_id)
    try:
        await message.edit(
            content=f"**Summary — {event['title']}**  ·  no longer updating"
        )
        await message.unpin()
    except (discord.HTTPException, discord.NotFound):
        pass


# ---------------------------------------------------------------------------
# Buttons
#
# A button RSVP keeps no state on Discord's side at all: there are no reactions
# to count, so the tally lives in memory and is written into the message text.
# That makes the count exact, the three options fixed in place, and the whole
# placeholder dance unnecessary — at the cost of the votes being gone if the
# bot restarts, the same as every other setting here.
# ---------------------------------------------------------------------------

BUTTON_STYLES = {
    "✅": discord.ButtonStyle.success,
    "❌": discord.ButtonStyle.danger,
    "❓": discord.ButtonStyle.secondary,
}


def slot_headline(event: dict, slot: dict) -> str:
    """Bold server-time headline, with the viewer-localized time trailing it."""
    ts = slot["timestamp"]
    tz = event["tz"]
    return (
        f"**{event['title']} — {format_slot_time(ts, tz)} {format_zone_label(ts, tz)}**"
        f"  ·  your local time: <t:{ts}:t>"
    )


def button_message_text(event: dict, slot: dict) -> str:
    """Headline plus the tally, since buttons carry no count of their own."""
    tally = "   ".join(f"{emoji} {len(slot['votes'][emoji])}" for emoji in STATUS_ORDER)
    return f"{slot_headline(event, slot)}\n{tally}"


class RSVPButton(discord.ui.Button):
    def __init__(self, event_id: str, time_label: str, status: str):
        super().__init__(
            style=BUTTON_STYLES[status],
            label=STATUS_EMOJIS[status],
            emoji=status,
            custom_id=f"rsvp:{event_id}:{time_label}:{status}",
        )
        self.event_id = event_id
        self.time_label = time_label
        self.status = status

    async def callback(self, interaction: discord.Interaction):
        await record_button_vote(
            interaction, self.event_id, self.time_label, self.status
        )


class RSVPView(discord.ui.View):
    def __init__(self, event_id: str, time_label: str):
        super().__init__(timeout=None)
        for status in STATUS_ORDER:
            self.add_item(RSVPButton(event_id, time_label, status))


async def record_button_vote(
    interaction: discord.Interaction, event_id: str, time_label: str, status: str
) -> None:
    """Apply a button press and rewrite the tally on the message."""
    event = active_events.get(event_id)
    slot = event["slots"].get(time_label) if event else None
    if slot is None:
        await interaction.response.send_message(
            "That RSVP has closed — it isn't being tracked any more.", ephemeral=True
        )
        return

    user_id = interaction.user.id
    clearing = user_id in slot["votes"][status]

    # A button answer is exclusive, unlike reactions: clear the other two
    # rather than letting one person sit in both Yes and Maybe. Pressing the
    # button you already chose clears your answer entirely.
    for emoji in STATUS_ORDER:
        slot["votes"][emoji].discard(user_id)
    if not clearing:
        slot["votes"][status].add(user_id)

    when = format_slot_time(slot["timestamp"], event["tz"])
    note = (
        f"Cleared your answer for {when}."
        if clearing
        else f"You're down as **{STATUS_EMOJIS[status]}** for {when}."
    )

    # Editing the message updates the tally and acknowledges the click at once.
    await interaction.response.edit_message(content=button_message_text(event, slot))
    await interaction.followup.send(note, ephemeral=True)
    schedule_live_refresh(event_id)
    save_state_soon()

    if not clearing and user_id not in _avatar_cache and interaction.guild is not None:
        run_in_background(fetch_avatars(interaction.guild, [user_id]))


@bot.tree.command(
    name="setlivesummary",
    description="Pin a summary image that redraws itself as people answer",
)
@app_commands.describe(
    enabled="Whether new RSVPs get a live summary",
    channel="Where to post it — leave blank to use whichever channel the RSVP is in",
    mode="One summary per RSVP, or a single one that follows the newest",
    pin="Whether to pin it (default: yes)",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="One summary per RSVP", value=LIVE_EACH),
        app_commands.Choice(
            name="A single summary that follows the newest RSVP", value=LIVE_LATEST
        ),
    ]
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setlivesummary(
    interaction: discord.Interaction,
    enabled: bool,
    channel: discord.TextChannel = None,
    mode: str = None,
    pin: bool = None,
):
    # Check up front rather than letting the post fail quietly in the
    # background, where nobody would ever see the error.
    if enabled and channel is not None:
        allowed = channel.permissions_for(interaction.guild.me)
        missing = [
            name
            for name, granted in (
                ("View Channel", allowed.view_channel),
                ("Send Messages", allowed.send_messages),
                ("Attach Files", allowed.attach_files),
            )
            if not granted
        ]
        if missing:
            await interaction.response.send_message(
                f"I can't post live summaries in {channel.mention} — I'm missing "
                f"**{'**, **'.join(missing)}** there.",
                ephemeral=True,
            )
            return

    guild_id = interaction.guild_id
    guild_live_summary[guild_id] = enabled
    if channel is not None:
        guild_live_channel[guild_id] = channel.id

    chosen_mode = getattr(mode, "value", mode)
    if chosen_mode in LIVE_MODES:
        # Switching away from "latest" releases the shared message, so the next
        # RSVP posts its own instead of taking that one over.
        if chosen_mode != guild_live_mode.get(guild_id, LIVE_EACH):
            guild_live_message.pop(guild_id, None)
        guild_live_mode[guild_id] = chosen_mode
    if pin is not None:
        guild_live_pin[guild_id] = pin

    save_state_soon()
    running = events_for_guild(guild_id)

    if enabled:
        configured = guild_live_channel.get(guild_id)
        where = (
            f"in <#{configured}>"
            if configured
            else "in whichever channel the RSVP is created in"
        )

        if guild_live_mode.get(guild_id, LIVE_EACH) == LIVE_LATEST:
            mode_note = (
                "• One summary, reused: each new RSVP takes over the same "
                "message, so there's always exactly one and it's always the "
                "newest. Older ones stop updating it."
            )
        else:
            mode_note = "• Each RSVP gets its own summary."

        if not wants_pinned_summary(guild_id):
            pin_note = "• It won't be pinned."
        else:
            pin_note = (
                "• Pinning needs Manage Messages; without it the summary still "
                "works, it just won't be pinned."
            )
            target = bot.get_channel(configured) if configured else None
            if target is not None and not target.permissions_for(
                interaction.guild.me
            ).manage_messages:
                pin_note = (
                    f"• I can't pin in <#{configured}> (no Manage Messages), so "
                    "the summary will post but stay unpinned."
                )

        await interaction.response.send_message(
            f"New RSVPs will get a summary image {where} that redraws itself as "
            "people answer.\n"
            f"{mode_note}\n"
            f"• It redraws at most once every {LIVE_REFRESH_DELAY:.0f} seconds. "
            "Replacing an image means re-uploading it, so a burst of answers "
            "becomes one redraw rather than twenty.\n"
            "• It uses the compact grid layout — `/summary` still gives you the "
            "detailed one.\n"
            f"{pin_note}"
            + (
                f"\n\nStarting one for the {len(running)} already running."
                if running
                else ""
            ),
            ephemeral=True,
        )
        for _, event in running:
            run_in_background(
                start_live_summary(
                    event, bot.get_channel(event["channel_id"]), interaction.guild
                )
            )
    else:
        await interaction.response.send_message(
            "Live summaries are off. Any already posted stay in the channel but "
            "stop updating, and get unpinned.",
            ephemeral=True,
        )
        for _, event in running:
            run_in_background(stop_live_summary(event))


@bot.tree.command(
    name="closersvp",
    description="Stop tracking an RSVP, freeing up one of the slots",
)
@app_commands.describe(
    title="Which RSVP to close",
    delete_messages="Also delete its messages from the channel (off by default)",
)
@app_commands.autocomplete(title=rsvp_title_autocomplete)
@app_commands.checks.has_permissions(manage_guild=True)
async def closersvp(
    interaction: discord.Interaction, title: str, delete_messages: bool = False
):
    found, problem = resolve_event(interaction.guild_id, title)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    event_id, event = found

    await interaction.response.defer(ephemeral=True)
    await stop_live_summary(event)

    channel = bot.get_channel(event["channel_id"])
    touched = 0
    if channel is not None:
        for slot in event["slots"].values():
            message = channel.get_partial_message(slot["message_id"])
            try:
                if delete_messages:
                    await message.delete()
                else:
                    # Leave the result visible, but make it obvious that
                    # reacting to it now does nothing.
                    base = (
                        button_message_text(event, slot)
                        if event["mode"] == BUTTON_MODE
                        else slot_headline(event, slot)
                    )
                    await message.edit(
                        content=f"{base}\n**Closed** — no longer counting answers.",
                        view=None,
                    )
            except (discord.HTTPException, discord.NotFound):
                continue
            touched += 1

    close_event(interaction.guild_id, event_id)
    save_state_soon()

    remaining = len(events_for_guild(interaction.guild_id))
    outcome = (
        f"deleted {touched} message{'' if touched == 1 else 's'}"
        if delete_messages
        else f"marked {touched} message{'' if touched == 1 else 's'} as closed"
    )
    await interaction.followup.send(
        f"Closed **{event['title']}** and {outcome}.\n"
        f"{remaining} of {MAX_ACTIVE_RSVPS} RSVPs still running.",
        ephemeral=True,
    )


@bot.tree.command(name="rsvps", description="List the RSVPs running on this server")
async def rsvps(interaction: discord.Interaction):
    running = events_for_guild(interaction.guild_id)
    if not running:
        await interaction.response.send_message(
            "No RSVPs running. Start one with `/rsvp`.", ephemeral=True
        )
        return

    blocks = []
    for _, event in running:
        slots = event["slots"]
        responders = {
            uid
            for slot in slots.values()
            for voters in slot["votes"].values()
            for uid in voters
        }
        first_ts = next(iter(slots.values()))["timestamp"]

        yes_counts = [len(slot["votes"]["✅"]) for slot in slots.values()]
        best = max(yes_counts, default=0)
        best_times = [
            format_slot_time(slot["timestamp"], event["tz"])
            for slot in slots.values()
            if len(slot["votes"]["✅"]) == best
        ]

        details = [MODE_LABELS.get(event["mode"], event["mode"])]
        if event.get("live_message_id"):
            details.append("live summary pinned")

        lines = [
            f"**{event['title']}**",
            f"{format_slot_date(first_ts, event['tz'])}  ·  "
            f"{len(slots)} slot{'' if len(slots) == 1 else 's'}  ·  "
            f"{len(responders)} responded",
            "  ·  ".join(details),
        ]
        if best:
            shown = ", ".join(best_times[:3])
            more = f" (+{len(best_times) - 3} more)" if len(best_times) > 3 else ""
            lines.append(f"Best so far: {shown}{more} — {best} yes")

        blocks.append("\n".join(lines))

    await interaction.response.send_message(
        f"**{len(running)} of {MAX_ACTIVE_RSVPS} RSVPs running**\n\n"
        + "\n\n".join(blocks)
        + "\n\nUse the title with `/summary` or `/reactping` to pick one.",
        ephemeral=True,
    )


@bot.tree.command(name="rsvp", description="Create an RSVP — one message per time slot")
@app_commands.describe(
    title="What are people RSVPing to?",
    date="When — today, tomorrow, friday, in 3 days, 9/12, sep 12, 2026-09-12 (default: today)",
)
async def rsvp(interaction: discord.Interaction, title: str, date: str = None):
    tz = get_timezone(interaction.guild_id)
    today = datetime.now(tz).date()

    if date is None:
        target = today
    else:
        target = parse_date(date, today)
        if target is None:
            await interaction.response.send_message(
                f"Couldn't read `{date}` as a date. Any of these work:\n"
                "`today` · `tomorrow` · `friday` · `next friday` · `in 3 days`\n"
                "`9/12` · `9/12/26` · `sep 12` · `12 sept` · `2026-09-12`",
                ephemeral=True,
            )
            return

    date_str = target.strftime("%Y-%m-%d")
    readable_date = f"{target.strftime('%A, %B')} {target.day}"

    time_slots = get_time_slots(interaction.guild_id)

    # The title is how /summary and /reactping tell RSVPs apart, so two running
    # at once can't share one.
    for _, existing in events_for_guild(interaction.guild_id):
        if existing["title"].casefold() == title.strip().casefold():
            await interaction.response.send_message(
                f"There's already an RSVP called **{existing['title']}** running. "
                "Give this one a different title so they can be told apart.",
                ephemeral=True,
            )
            return

    try:
        # The resolved date is echoed back because the parser accepts loose
        # input — "9/12" read the wrong way round should be visible, not silent.
        await interaction.response.send_message(
            f"Creating RSVP for **{title}** on **{readable_date}**...",
            ephemeral=True,
        )

        event_id = str(uuid.uuid4())
        slots = {}
        mode = guild_mode(interaction.guild_id)

        # Register the event up front. Seeding 3 reactions across every slot
        # takes many rate-limited round trips, and until this dict existed any
        # reaction arriving mid-creation raised a KeyError in the handler.
        # The mode is recorded here rather than read later, so an RSVP keeps
        # behaving the way it was built even if the server setting changes.
        event = {
            "id": event_id,
            "title": title,
            "guild_id": interaction.guild_id,
            "channel_id": interaction.channel_id,
            "tz": tz,
            "mode": mode,
            "live_message_id": None,
            "live_channel_id": None,
            "slots": slots,
        }
        active_events[event_id] = event
        closed = register_event(interaction.guild_id, event_id)

        for time_label in time_slots:
            slot = {
                "message_id": None,
                "timestamp": build_timestamp(time_label, date_str, tz),
                "votes": {emoji: set() for emoji in STATUS_EMOJIS},
            }

            # The server's own clock leads, so everyone reads the same time
            # when comparing slots or quoting one back. The <t:...> timestamp
            # trails it and is rendered by Discord in each reader's timezone,
            # which is the one thing an image summary can never do.
            if mode == BUTTON_MODE:
                message = await interaction.channel.send(
                    button_message_text(event, slot),
                    view=RSVPView(event_id, time_label),
                )
            else:
                message = await interaction.channel.send(slot_headline(event, slot))

            # Index the slot BEFORE seeding its reactions, so someone clicking
            # the instant the message appears is recorded rather than dropped.
            slot["message_id"] = message.id
            slots[time_label] = slot
            message_index[message.id] = (event_id, time_label)

            # Buttons arrive live with the message; there's nothing to seed.
            if mode == BUTTON_MODE:
                continue

            for emoji in STATUS_ORDER:
                try:
                    await message.add_reaction(emoji)
                except discord.HTTPException:
                    continue
                bot_seeded.add((message.id, emoji))

        # Narrow races remain between sending a message and indexing it, so
        # read the true state back from Discord and merge in anything missed.
        await reconcile_event(active_events[event_id], interaction.channel)

        if wants_live_summary(interaction.guild_id):
            await start_live_summary(event, interaction.channel, interaction.guild)

        save_state_soon()

        if closed:
            names = ", ".join(f"**{old['title']}**" for old in closed)
            await interaction.followup.send(
                f"Closed {names} to stay within {MAX_ACTIVE_RSVPS} running RSVPs. "
                "Those messages are still in the channel, but reactions on them "
                "no longer count.",
                ephemeral=True,
            )
            for old in closed:
                run_in_background(stop_live_summary(old))
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to send messages or add reactions in this channel. "
            "Ask a server admin to check my role permissions.",
            ephemeral=True,
        )


def partial_message(channel_id: int, message_id: int):
    """A message handle that costs no HTTP request.

    The reaction handlers only ever add or remove a reaction, which needs an
    id rather than the message body — fetching it made every click a second
    API call and made rapid reacting hit rate limits much sooner.
    """
    channel = bot.get_channel(channel_id)
    if channel is None or not hasattr(channel, "get_partial_message"):
        return None
    return channel.get_partial_message(message_id)


def locate_slot(message_id: int):
    """Resolve a message id to (event, slot), or None if it isn't a live RSVP."""
    lookup = message_index.get(message_id)
    if not lookup:
        return None
    event_id, time_label = lookup
    event = active_events.get(event_id)
    if event is None:
        return None
    slot = event["slots"].get(time_label)
    if slot is None:
        return None
    return event, slot


# Re-laying the options is a remove-then-add sequence, and Discord rate limits
# reaction writes hard. A rate-limited add would otherwise leave that option
# missing until something else happened to re-add it — and since the options go
# back in STATUS_ORDER, the casualty was almost always the last one, ❓.
REACTION_RETRY_DELAY = 1.0
REACTION_ADD_ATTEMPTS = 3

# message_id -> lock. Two reactions withdrawn at once would otherwise run two
# re-lays over each other, each acting on a snapshot the other had invalidated.
_order_locks = {}


def order_lock(message_id: int) -> asyncio.Lock:
    lock = _order_locks.get(message_id)
    if lock is None:
        lock = asyncio.Lock()
        _order_locks[message_id] = lock
    return lock


async def add_placeholders(message, emojis) -> None:
    """Put the bot's placeholder on each emoji, in the order given.

    Afterwards bot_seeded tells the truth for every one: present if the
    placeholder actually landed, absent if it didn't, so a failure is retried
    later rather than leaving the option permanently unclickable.
    """
    for emoji in emojis:
        key = (message.id, emoji)
        bot_seeded.discard(key)
        for attempt in range(REACTION_ADD_ATTEMPTS):
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                if attempt + 1 < REACTION_ADD_ATTEMPTS:
                    await asyncio.sleep(REACTION_RETRY_DELAY)
                continue
            bot_seeded.add(key)
            break


async def ensure_option_order(channel, slot, keep: bool = False) -> None:
    """Lay the three options out on the slot's message in STATUS_ORDER.

    Discord orders reactions by when each emoji was first added, and an emoji
    whose count falls to zero disappears from the message entirely — so the
    placeholder the bot re-adds afterwards lands at the end, and ✅ ❌ ❓ drifts
    into ❌ ❓ ✅.

    Only the bot's own placeholders are ever moved. An option carrying real
    votes is left exactly where it is, because the sole way to reposition it
    would be to clear the reaction, which would delete people's responses. So
    when every option is empty — including a freshly created RSVP — the order
    is exactly STATUS_ORDER; when some already hold votes, those keep their
    places and the placeholders are laid out in order after them.

    The message is fetched inside the lock rather than passed in, so a caller
    that waited on the lock acts on the current state instead of the snapshot
    it took before waiting.
    """
    if bot.user is None or channel is None:
        return

    async with order_lock(slot["message_id"]):
        try:
            message = await channel.fetch_message(slot["message_id"])
        except (discord.HTTPException, discord.NotFound):
            return

        current = [
            str(reaction.emoji)
            for reaction in message.reactions
            if str(reaction.emoji) in STATUS_EMOJIS
        ]

        if keep:
            # The placeholders never come off, so no option can drop to zero
            # and vanish, and the order can't drift. Nothing to re-lay — just
            # replace anything that went missing (a failed add, or a manual
            # removal by an admin).
            missing = [emoji for emoji in STATUS_ORDER if emoji not in current]
            if missing:
                await add_placeholders(message, missing)
            return

        # An option with real votes is anchored; the rest are ours to re-lay.
        movable = [emoji for emoji in STATUS_ORDER if not slot["votes"].get(emoji)]
        anchored = [emoji for emoji in current if emoji not in movable]

        if current == anchored + movable:
            return  # already the best arrangement available

        for emoji in movable:
            if emoji not in current:
                continue
            try:
                await message.remove_reaction(emoji, bot.user)
            except discord.HTTPException:
                pass
            bot_seeded.discard((message.id, emoji))

        # Every option this dropped must come back, in order.
        await add_placeholders(message, movable)


async def read_reaction_voters(reaction) -> set:
    """Page through a reaction's users, excluding the bot's own placeholder."""
    voters = set()
    async for user in reaction.users():
        if bot.user is None or user.id != bot.user.id:
            voters.add(user.id)
    return voters


_background_tasks = set()


def _background_done(task) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        print(f"background task failed: {error!r}", flush=True)


def run_in_background(coro) -> None:
    """Fire and forget, holding a reference so the task isn't GC'd mid-flight."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_done)


async def reconcile_slot(channel, slot: dict) -> None:
    """Read-only: refresh this slot's tally from what Discord actually holds.

    Paging a reaction's users is among the most aggressively rate limited calls
    in the API, and a seven-slot RSVP needed 21 of them — which discord.py
    serializes, and which is where a 23-second /summary was going.

    message.reactions already carries a count, and it comes free with the fetch.
    So compare that against what we hold and page only the ones that disagree,
    which is normally none of them.
    """
    try:
        message = await channel.fetch_message(slot["message_id"])
    except (discord.HTTPException, discord.NotFound):
        return

    by_emoji = {
        str(reaction.emoji): reaction
        for reaction in message.reactions
        if str(reaction.emoji) in STATUS_EMOJIS
    }

    votes = {}
    disputed = []
    for emoji in STATUS_ORDER:
        known = set(slot["votes"].get(emoji, ()))
        reaction = by_emoji.get(emoji)

        if reaction is None:
            # The option isn't on the message at all, so nobody holds it.
            votes[emoji] = set()
            continue

        # The bot's own placeholder is in Discord's count but not in ours.
        seeded = 1 if (slot["message_id"], emoji) in bot_seeded else 0
        if reaction.count == len(known) + seeded:
            votes[emoji] = known
        else:
            disputed.append(emoji)

    if disputed:
        pages = await asyncio.gather(
            *(read_reaction_voters(by_emoji[emoji]) for emoji in disputed),
            return_exceptions=True,
        )
        for emoji, result in zip(disputed, pages):
            # Couldn't page this one — keep the tally we already had rather
            # than silently zeroing real votes.
            votes[emoji] = (
                set(slot["votes"].get(emoji, ()))
                if isinstance(result, BaseException)
                else result
            )

    slot["votes"] = votes


async def repair_slot(channel, slot: dict, keep: bool = False) -> None:
    """Write path: drop spent placeholders and restore the option order.

    Kept separate from the read above because these are reaction writes, and
    Discord rate limits those hard — roughly a quarter second apiece. Running
    them before /summary could send its image added seconds of delay for
    something the reader never sees.
    """
    message = (
        channel.get_partial_message(slot["message_id"])
        if hasattr(channel, "get_partial_message")
        else None
    )

    if message is not None and not keep:
        for emoji in STATUS_ORDER:
            key = (slot["message_id"], emoji)
            if slot["votes"].get(emoji) and key in bot_seeded:
                # Real votes landed here, so the bot's placeholder is no longer
                # needed and would otherwise inflate the count.
                bot_seeded.discard(key)
                try:
                    await message.remove_reaction(emoji, bot.user)
                except discord.HTTPException:
                    pass

    await ensure_option_order(channel, slot, keep)


async def reconcile_event(event: dict, channel, repair: bool = True) -> None:
    """Re-read the real reaction state from Discord into the event.

    Reaction events can be missed — while the bot is busy seeding a new RSVP,
    or if the gateway drops events across a reconnect. This resyncs the tally
    to what Discord actually holds, so a missed click is recovered rather than
    lost until someone re-reacts.

    Slots are handled concurrently. Done one at a time, a seven-slot RSVP meant
    roughly thirty sequential round trips before /summary could start rendering.

    Pass repair=False on a read path to skip the rate-limited reaction writes;
    callers that want them can schedule a repair pass afterwards.

    Does nothing for a button RSVP. Its votes live only in memory — there are
    no reactions to read back, so "reconciling" one would find every option at
    zero and wipe the tally.
    """
    if channel is None or event["mode"] == BUTTON_MODE:
        return

    await asyncio.gather(
        *(reconcile_slot(channel, slot) for slot in event["slots"].values()),
        return_exceptions=True,
    )

    if repair:
        await repair_event(event, channel)


async def repair_event(event: dict, channel) -> None:
    """Run the reaction-write half of reconcile across every slot."""
    if channel is None:
        return

    if event["mode"] == BUTTON_MODE:
        return  # nothing to repair: a button RSVP carries no reactions

    keep = event["mode"] == KEEP_PLACEHOLDERS
    await asyncio.gather(
        *(repair_slot(channel, slot, keep) for slot in event["slots"].values()),
        return_exceptions=True,
    )


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if bot.user is None or payload.user_id == bot.user.id:
        return
    emoji = str(payload.emoji)
    if emoji not in STATUS_EMOJIS:
        return

    found = locate_slot(payload.message_id)
    if found is None:
        return
    event, slot = found
    if event["mode"] == BUTTON_MODE:
        return  # that RSVP answers through its buttons; reactions aren't votes
    slot["votes"][emoji].add(payload.user_id)
    schedule_live_refresh(event["id"])
    save_state_soon()

    # Warm this person's avatar now, so /summary isn't paying for the download
    # later while someone waits on the image.
    if payload.user_id not in _avatar_cache and payload.guild_id is not None:
        guild = bot.get_guild(payload.guild_id)
        if guild is not None:
            run_in_background(fetch_avatars(guild, [payload.user_id]))

    # A real person reacted, so drop the bot's seed to stop it inflating the
    # count. Claim the key before awaiting: several people reacting at once
    # would otherwise each fire their own removal request.
    key = (payload.message_id, emoji)
    if event["mode"] == KEEP_PLACEHOLDERS or key not in bot_seeded:
        return
    bot_seeded.discard(key)

    message = partial_message(payload.channel_id, payload.message_id)
    if message is None:
        return
    try:
        await message.remove_reaction(emoji, bot.user)
    except discord.HTTPException:
        pass


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    emoji = str(payload.emoji)
    if emoji not in STATUS_EMOJIS:
        return

    found = locate_slot(payload.message_id)
    if found is None:
        return
    event, slot = found
    if event["mode"] == BUTTON_MODE:
        return
    slot["votes"][emoji].discard(payload.user_id)
    schedule_live_refresh(event["id"])
    save_state_soon()

    # If that was the last real reaction the option would disappear from the
    # message, so re-seed it. Claim first, then roll back if the call fails —
    # marking it seeded regardless used to strand the option permanently.
    key = (payload.message_id, emoji)
    if slot["votes"][emoji] or key in bot_seeded:
        return
    # That was the last real vote, so the option has just vanished from the
    # message. Re-lay the placeholders rather than adding the one back: a
    # re-added emoji lands at the end, which is what knocked ✅ ❌ ❓ out of
    # order. ensure_option_order owns bot_seeded for the options it touches.
    await ensure_option_order(
        bot.get_channel(payload.channel_id),
        slot,
        event["mode"] == KEEP_PLACEHOLDERS,
    )


@bot.tree.command(name="reactping", description="Ping roster members missing a reaction on any time slot")
@app_commands.describe(title="Which RSVP — leave blank if only one is running")
@app_commands.autocomplete(title=rsvp_title_autocomplete)
async def reactping(interaction: discord.Interaction, title: str = None):
    guild_id = interaction.guild_id

    found, problem = resolve_event(guild_id, title)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    _event_id, event = found

    role_id = rosters.get(guild_id)
    if role_id is None:
        await interaction.response.send_message(
            "No roster set. Use `/setroster @role` first.", ephemeral=True
        )
        return

    role = interaction.guild.get_role(role_id)
    if role is None:
        await interaction.response.send_message(
            "Roster role not found (was it deleted?).", ephemeral=True
        )
        return

    # Resync before naming names — pinging someone who did respond, because
    # their reaction was missed, is worse than the command being a bit slow.
    # Reaction repairs can wait until after the ping has gone out.
    await interaction.response.defer()
    channel = bot.get_channel(event["channel_id"])
    await reconcile_event(event, channel, repair=False)

    # A member "reacted" to a time slot if they used ANY of the 3 status
    # emojis on that slot's message. They need to have reacted to EVERY slot.
    missing_people = []
    for member in role.members:
        if member.bot:
            continue
        reacted_all = True
        for time_label, slot in event["slots"].items():
            reacted_this_slot = any(
                member.id in slot["votes"][emoji] for emoji in STATUS_EMOJIS
            )
            if not reacted_this_slot:
                reacted_all = False
                break
        if not reacted_all:
            missing_people.append(member)

    if not missing_people:
        await interaction.followup.send(
            f"Everyone in {role.mention} has responded to every time slot. ✅"
        )
        return

    mentions = " ".join(m.mention for m in missing_people)
    await interaction.followup.send(
        f"⏰ Reminder for **{event['title']}** — you're missing a response on at least one time slot: {mentions}"
    )


@bot.tree.command(name="summary", description="Generate a shareable image of all RSVP responses")
@app_commands.describe(title="Which RSVP — leave blank if only one is running")
@app_commands.autocomplete(title=rsvp_title_autocomplete)
async def summary(interaction: discord.Interaction, title: str = None):
    found, problem = resolve_event(interaction.guild_id, title)
    if problem:
        await interaction.response.send_message(problem, ephemeral=True)
        return
    _event_id, event = found

    await interaction.response.defer()

    channel = bot.get_channel(event["channel_id"])
    started = time.perf_counter()

    def since_start():
        return (time.perf_counter() - started) * 1000

    def current_voters():
        return {
            uid
            for slot in event["slots"].values()
            for voters in slot["votes"].values()
            for uid in voters
        }

    # Read the true state so the image reflects Discord, but skip the reaction
    # writes: they're rate limited and would sit in front of the image for no
    # benefit the reader can see. They run in the background once it's sent.
    #
    # Warm avatars for everyone already known while that read is in flight —
    # both are network-bound and neither depends on the other.
    await asyncio.gather(
        reconcile_event(event, channel, repair=False),
        fetch_avatars(interaction.guild, current_voters()),
    )
    read_ms = since_start()

    # Anyone the read turned up who wasn't known before; the rest are cached.
    avatars = await fetch_avatars(interaction.guild, current_voters())
    avatar_ms = since_start() - read_ms

    # Compositing a few dozen avatars is CPU-bound; keep it off the event loop
    # so the bot stays responsive while the image is built.
    buffer = await asyncio.to_thread(
        build_summary_image, event, interaction.guild, avatars
    )
    render_ms = since_start() - read_ms - avatar_ms

    file = discord.File(buffer, filename="rsvp_summary.png")
    await interaction.followup.send(file=file)

    # Logged per stage so a slow /summary can be diagnosed from the container
    # logs rather than guessed at.
    print(
        f"/summary {event['title']!r}: read {read_ms:.0f}ms, "
        f"avatars {avatar_ms:.0f}ms, render {render_ms:.0f}ms, "
        f"upload {since_start() - read_ms - avatar_ms - render_ms:.0f}ms, "
        f"total {since_start():.0f}ms",
        flush=True,
    )

    # Tidy the reactions after the image has landed rather than before it.
    run_in_background(repair_event(event, channel))


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
    bot.run(token)
