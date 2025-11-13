import os
import asyncio
import random
import time
from pathlib import Path

import discord
from discord import FFmpegPCMAudio
from discord.ext import tasks
from dotenv import load_dotenv

# ----------------- CONFIG -----------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ROLE_NAME = os.getenv("ROLE_NAME", "Виктор Завальный")

JOIN_SOUND = os.getenv("JOIN_SOUND", "./sounds/join.mp3")
LEAVE_SOUND = os.getenv("LEAVE_SOUND", "./sounds/leave.mp3")
RANDOM_DIR = os.getenv("RANDOM_SOUNDS_DIR", "./sounds/random")

BASE_INTERVAL_MIN = int(os.getenv("BASE_INTERVAL_MIN", "5"))   # базовый интервал (мин)
JITTER_SEC = int(os.getenv("JITTER_SEC", "90"))                # разброс (сек)
TEXT_CHANNEL_ID = int(os.getenv("TEXT_CHANNEL_ID", "0"))

# путь к ffmpeg; на Railway это просто "ffmpeg"
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")

# ----------------- DISCORD SETUP -----------------

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.voice_states = True
intents.presences = True

bot = discord.Client(intents=intents)

voice_lock = asyncio.Lock()

# timers: "when to play the next random sound" per guild
next_play_at: dict[int, float] = {}       # guild_id -> unix timestamp
last_viktor_channel: dict[int, int] = {}  # guild_id -> last voice_channel_id for Viktor


# ----------------- HELPERS -----------------

def has_target_role(m: discord.Member) -> bool:
    """Check if member has the target role."""
    target = ROLE_NAME.strip().lower()
    return any(r.name.strip().lower() == target for r in m.roles)


def list_random_files() -> list[Path]:
    """Return list of .mp3/.wav files from RANDOM_DIR."""
    p = Path(RANDOM_DIR)
    if not p.exists() or not p.is_dir():
        print(f"[WARN] Random sounds directory does not exist: {RANDOM_DIR}")
        return []
    files = [f for f in p.iterdir() if f.suffix.lower() in (".mp3", ".wav")]
    if not files:
        print(f"[WARN] No random sound files found in: {RANDOM_DIR}")
    return files


async def ensure_voice_client(channel: discord.VoiceChannel) -> discord.VoiceClient | None:
    """
    Connect to the given voice channel (or move there if already connected).
    Does NOT disconnect automatically.
    """
    try:
        vc = discord.utils.get(bot.voice_clients, guild=channel.guild)
        if vc and vc.is_connected():
            if vc.channel.id != channel.id:
                print(f"[INFO] Moving bot to voice channel: {channel.name}")
                await vc.move_to(channel)
        else:
            print(f"[INFO] Connecting bot to voice channel: {channel.name}")
            vc = await channel.connect()
        return vc
    except Exception as e:
        print(f"[ERROR] ensure_voice_client: {e}")
        return None


async def play_file(channel: discord.VoiceChannel, path: str):
    """Play an audio file in the given voice channel and stay connected."""
    if not os.path.exists(path):
        print(f"[WARN] Sound file not found: {path}")
        return

    try:
        vc = await ensure_voice_client(channel)
        if not vc or not vc.is_connected():
            print("[WARN] No active voice client to play on.")
            return

        if vc.is_playing():
            vc.stop()
            await asyncio.sleep(0.1)

        print(f"[INFO] Playing sound: {path}")

        # Настройки ffmpeg – более дружелюбные к хостингу
        source = FFmpegPCMAudio(
            path,
            executable=FFMPEG_PATH,
            before_options="-nostdin",
            options="-vn -loglevel panic",
        )

        try:
            vc.play(source)
        except Exception as e:
            print(f"[ERROR] vc.play failed: {repr(e)}")
            source.cleanup()
            return

        # ждём окончания воспроизведения, пока соединение живо
        while vc.is_connected() and vc.is_playing():
            await asyncio.sleep(0.3)

        source.cleanup()
        await asyncio.sleep(0.2)

    except Exception as e:
        import traceback
        print("[ERROR] play_file exception:")
        traceback.print_exc()


def schedule_next(guild_id: int):
    """Schedule next random sound time for a guild."""
    base = BASE_INTERVAL_MIN * 60
    jitter = random.randint(0, max(5, JITTER_SEC))
    delay = base + jitter
    ts = time.time() + delay
    next_play_at[guild_id] = ts
    print(f"[DEBUG] Scheduled next random sound in guild {guild_id} in {delay:.1f} seconds.")


async def disconnect_if_viktor_gone(guild: discord.Guild, delay: int = 10):
    """
    After `delay` seconds, check if Viktor is still in any voice channel.
    If not, disconnect the bot from voice.
    """
    await asyncio.sleep(delay)

    viktor_present = any(
        has_target_role(m) and m.voice and m.voice.channel
        for m in guild.members
    )

    if not viktor_present:
        print(f"[INFO] Viktor is no longer in voice on guild {guild.id}. Disconnecting bot.")
        vc = discord.utils.get(bot.voice_clients, guild=guild)
        if vc and vc.is_connected():
            await vc.disconnect(force=True)
        next_play_at.pop(guild.id, None)  # reset random timer


# ----------------- EVENTS -----------------

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")

    # If the bot started while Viktor is already in a voice channel – join him.
    for guild in bot.guilds:
        viktor_member = None
        viktor_channel = None
        for m in guild.members:
            if has_target_role(m) and m.voice and m.voice.channel:
                viktor_member = m
                viktor_channel = m.voice.channel
                break

        if viktor_member and viktor_channel:
            last_viktor_channel[guild.id] = viktor_channel.id
            print(f"[INFO] Viktor is already in voice ({viktor_channel.name}) on startup. Joining.")
            async with voice_lock:
                await ensure_voice_client(viktor_channel)
                await play_file(viktor_channel, JOIN_SOUND)
            schedule_next(guild.id)

    if not random_loop.is_running():
        random_loop.start()
        print("[INFO] random_loop started.")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    try:
        is_viktor = has_target_role(member)

        # Track the last channel where Viktor was seen
        if after.channel and is_viktor:
            last_viktor_channel[member.guild.id] = after.channel.id

        # Joined a new channel
        if after.channel and (before.channel is None or before.channel.id != after.channel.id):
            if is_viktor:
                print(f"[INFO] Viktor joined voice channel: {after.channel.name}")
                async with voice_lock:
                    await ensure_voice_client(after.channel)
                    await play_file(after.channel, JOIN_SOUND)
                schedule_next(member.guild.id)

        # Left a channel or moved away from it
        if before.channel and (after.channel is None or after.channel.id != before.channel.id):
            if is_viktor:
                print(f"[INFO] Viktor left voice channel: {before.channel.name}")
                # Check if Viktor is completely gone from voice after a delay
                bot.loop.create_task(disconnect_if_viktor_gone(member.guild, delay=10))

                # Optional delayed notification or leave sound
                async def delayed_leave_notice(guild: discord.Guild):
                    await asyncio.sleep(random.randint(20, 60))
                    if TEXT_CHANNEL_ID and TEXT_CHANNEL_ID != 0:
                        ch = guild.get_channel(TEXT_CHANNEL_ID)
                        if ch and isinstance(ch, discord.TextChannel):
                            await ch.send("🔕 Viktor left voice. I'll leave soon if he doesn't come back.")
                    else:
                        vc_id = last_viktor_channel.get(guild.id)
                        if vc_id:
                            vc_chan = guild.get_channel(vc_id)
                            if isinstance(vc_chan, discord.VoiceChannel):
                                async with voice_lock:
                                    await play_file(vc_chan, LEAVE_SOUND)

                bot.loop.create_task(delayed_leave_notice(member.guild))

    except Exception as e:
        print(f"[ERROR] on_voice_state_update: {e}")


# ----------------- RANDOM LOOP -----------------

@tasks.loop(seconds=30)
async def random_loop():
    """Loop that periodically checks if it's time to play a random sound."""
    try:
        now = time.time()
        for guild in bot.guilds:
            # Find Viktor in any voice channel in this guild
            target_member = None
            target_channel = None
            for m in guild.members:
                if has_target_role(m) and m.voice and m.voice.channel:
                    target_member = m
                    target_channel = m.voice.channel
                    last_viktor_channel[guild.id] = target_channel.id
                    break

            if not target_member:
                # Viktor is not in voice – skip this guild
                continue

            # Make sure the bot is in the same channel
            async with voice_lock:
                await ensure_voice_client(target_channel)

            # If timer is missing – schedule first random sound
            if guild.id not in next_play_at:
                print(f"[DEBUG] No timer set for guild {guild.id}. Scheduling first random sound.")
                schedule_next(guild.id)
                continue

            # Check if it's time to play a random sound
            if now >= next_play_at.get(guild.id, now + 999999):
                files = list_random_files()
                if files:
                    pick = str(random.choice(files))
                    print(f"[INFO] Playing random sound in guild {guild.id}: {pick}")
                    async with voice_lock:
                        await play_file(target_channel, pick)
                else:
                    print(f"[WARN] No random sounds available to play in guild {guild.id}.")
                schedule_next(guild.id)
    except Exception as e:
        print(f"[ERROR] random_loop: {e}")


@random_loop.before_loop
async def before_loop():
    await bot.wait_until_ready()


# ----------------- ENTRY POINT -----------------

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ Please set DISCORD_TOKEN in environment")
    bot.run(TOKEN)
