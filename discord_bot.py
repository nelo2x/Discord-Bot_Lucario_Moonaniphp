import asyncio
import io
import json
import os
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands
import requests

from moonani_client import MoonaniClient, PokemonSpawn
from raidtest import get_raid_data

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


def _country_flag(country_code):
    if not country_code:
        return ""
    code = country_code.strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(char) - ord("A")) for char in code)


def _format_country(country_code):
    if not country_code or str(country_code).strip().lower() in {"n/d", "n/a", "unknown", "??"}:
        return "Unknown"
    code = str(country_code).strip()
    flag = _country_flag(code)
    return (flag + " " + code.upper()).strip() if flag else code.upper()


GLOBAL_IV100_KIND = "global_iv100"
GLOBAL_IV0_KIND = "global_iv0"
WATCH_SPAWN_COOLDOWN_SECONDS = 90 * 60
WATCH_ERROR_COOLDOWN_SECONDS = 30 * 60
POKEMON_IMAGE_TIMEOUT_SECONDS = 10
POKEMON_IMAGE_MAX_BYTES = 2 * 1024 * 1024

def _read_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default

    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"The {name} variable must be an integer.") from exc


def _read_bool_env(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _format_moonani_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        if exc.response.status_code == 403:
            return (
                "Moonani returned `403 Forbidden` from this environment. "
                "This usually indicates a Cloudflare block or a restriction on the host running the bot."
            )
        return f"Moonani returned HTTP {exc.response.status_code}."
    return f"{type(exc).__name__}: {exc}"


def _build_detail_embed(
    spawn: PokemonSpawn,
    source_label: str,
    thumbnail_attachment_name: Optional[str] = None,
) -> discord.Embed:
    if spawn.iv_percent == 100:
        color = discord.Color.green()
    elif spawn.iv_percent == 0:
        color = discord.Color.red()
    else:
        color = discord.Color.blurple()

    embed = discord.Embed(
        title=f"{spawn.name} (#{spawn.number})",
        description=f"Coords: `{spawn.coords}`",
        color=color,
    )
    embed.add_field(name="Map", value=f"[Open in Google Maps]({spawn.maps_url})", inline=False)
    embed.add_field(name="IV", value=f"{spawn.iv_percent}%", inline=True)
    embed.add_field(name="CP", value=str(spawn.cp), inline=True)
    embed.add_field(name="Level", value=str(spawn.level), inline=True)
    embed.add_field(
        name="Stats",
        value=f"ATK {spawn.attack} | DEF {spawn.defense} | HP {spawn.hp}",
        inline=False,
    )
    embed.add_field(name="Start", value=spawn.start_time or "N/D", inline=True)
    embed.add_field(name="End", value=spawn.end_time or "N/D", inline=True)
    embed.add_field(name="Country", value=_format_country(spawn.country), inline=True)
    embed.set_footer(text=f"Data obtained by Arceus from {source_label}")

    if thumbnail_attachment_name:
        embed.set_thumbnail(url=f"attachment://{thumbnail_attachment_name}")
    elif spawn.image_url:
        embed.set_thumbnail(url=spawn.image_url)

    return embed


def _build_raid_digest_embed(raids: List[Dict[str, str]]) -> discord.Embed:
    embed = discord.Embed(
        title=f"🔥 Active Raids — Top {len(raids)}",
        color=discord.Color.orange(),
    )
    lines = []
    for index, raid_item in enumerate(raids, start=1):
        lines.append(
            f"**{index}. {raid_item.get('raid_name', 'Raid')}**\n"
            f"Level: {raid_item.get('level', 'N/D')} | Country: {_format_country(raid_item.get('country'))}\n"
            f"Coords: `{raid_item.get('coords', '')}` | [Maps]({raid_item.get('maps_url', '')})"
        )
    embed.description = "\n\n".join(lines)
    embed.set_footer(text="Data obtained by Arceus from Moonani • Updates every 30 minutes")
    return embed


async def _run_blocking(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args))


def _pokemon_image_filename(spawn: PokemonSpawn) -> str:
    parsed_path = urlparse(spawn.image_url or "").path
    extension = Path(parsed_path).suffix.lower()
    if extension not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        extension = ".png"
    return f"pokemon_{spawn.number or 'spawn'}{extension}"


def _download_pokemon_image(spawn: PokemonSpawn) -> Optional[Tuple[bytes, str]]:
    if not spawn.image_url:
        return None

    response = requests.get(
        spawn.image_url,
        headers={"User-Agent": "Mozilla/5.0 (Arceus Discord Bot)"},
        timeout=POKEMON_IMAGE_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    content = response.content
    if not content or len(content) > POKEMON_IMAGE_MAX_BYTES:
        return None

    content_type = response.headers.get("Content-Type", "").lower()
    if content_type and "image" not in content_type:
        return None

    return content, _pokemon_image_filename(spawn)


async def _build_pokemon_embed_payload(
    spawn: PokemonSpawn,
    source_label: str,
) -> Tuple[discord.Embed, Optional[discord.File]]:
    image_payload = None
    try:
        image_payload = await _run_blocking(_download_pokemon_image, spawn)
    except Exception:
        image_payload = None

    if image_payload is None:
        return _build_detail_embed(spawn, source_label), None

    image_bytes, filename = image_payload
    embed = _build_detail_embed(spawn, source_label, filename)
    file = discord.File(io.BytesIO(image_bytes), filename=filename)
    return embed, file


async def _send_pokemon_embed_to_channel(
    channel: discord.TextChannel,
    spawn: PokemonSpawn,
    source_label: str,
) -> None:
    embed, file = await _build_pokemon_embed_payload(spawn, source_label)
    if file is not None:
        await channel.send(embed=embed, file=file)
    else:
        await channel.send(embed=embed)


class LucarioDiscordBot(commands.Bot):
    def __init__(
        self,
        moonani: MoonaniClient,
        guild_id: Optional[int],
        page_size: int,
        max_scan_records: int,
        settings_path: Path,
        watch_monitor_interval_seconds: int,
        watch_scan_limit: int,
        zero_iv_scan_limit: int,
        raid_broadcast_interval_seconds: int = 1800,
        raid_broadcast_limit: int = 10,
    ) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.moonani = moonani
        self.guild_id = guild_id
        self.page_size = page_size
        self.max_scan_records = max_scan_records
        self.settings_path = settings_path
        self.watch_monitor_interval_seconds = watch_monitor_interval_seconds
        self.watch_scan_limit = watch_scan_limit
        self.zero_iv_scan_limit = zero_iv_scan_limit
        self.raid_broadcast_interval_seconds = raid_broadcast_interval_seconds
        self.raid_broadcast_limit = raid_broadcast_limit
        self.guild_settings = self._load_settings()
        self.watch_seen_cache = {}  # type: Dict[Tuple[int, str], Set[str]]
        self.watch_cooldown_cache = {}  # type: Dict[Tuple[int, str, str], float]
        self.watch_error_cooldown_cache = {}  # type: Dict[int, float]
        self.monitor_task = None  # type: Optional[asyncio.Task]

    def _load_settings(self) -> Dict[str, Dict[str, object]]:
        if not self.settings_path.exists():
            return {}

        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

        guilds = payload.get("guilds", {})
        if not isinstance(guilds, dict):
            return {}

        normalized = {}  # type: Dict[str, Dict[str, object]]
        for guild_key, settings in guilds.items():
            if not isinstance(settings, dict):
                continue

            normalized[str(guild_key)] = {
                "iv100_channels": self._normalize_channel_list(settings.get("iv100_channels", [])),
                "iv0_channels": self._normalize_channel_list(settings.get("iv0_channels", [])),
                "raid_channels": self._normalize_channel_list(settings.get("raid_channels", [])),
            }
        return normalized

    def _save_settings(self) -> None:
        self.settings_path.write_text(json.dumps({"guilds": self.guild_settings}, indent=2), encoding="utf-8")

    def _normalize_channel_list(self, raw_channels: object) -> List[int]:
        channels = []
        if not isinstance(raw_channels, list):
            return channels

        for channel_id in raw_channels:
            try:
                parsed_channel_id = int(channel_id)
            except (TypeError, ValueError):
                continue
            if parsed_channel_id not in channels:
                channels.append(parsed_channel_id)

        return channels

    def _ensure_guild_settings(self, guild_id: int) -> Dict[str, object]:
        guild_key = str(guild_id)
        if guild_key not in self.guild_settings:
            self.guild_settings[guild_key] = {
                "iv100_channels": [],
                "iv0_channels": [],
                "raid_channels": [],
            }
        self.guild_settings[guild_key].setdefault("iv100_channels", [])
        self.guild_settings[guild_key].setdefault("iv0_channels", [])
        self.guild_settings[guild_key].setdefault("raid_channels", [])
        return self.guild_settings[guild_key]

    def get_global_channels(self, guild_id: int, channel_key: str) -> List[int]:
        settings = self._ensure_guild_settings(guild_id)
        return self._normalize_channel_list(settings.get(channel_key, []))

    def add_global_channel(self, guild_id: int, channel_key: str, channel_id: int) -> bool:
        settings = self._ensure_guild_settings(guild_id)
        channels = self._normalize_channel_list(settings.get(channel_key, []))
        if channel_id in channels:
            return False

        channels.append(channel_id)
        settings[channel_key] = channels
        self.watch_seen_cache.pop((guild_id, channel_key), None)
        self._save_settings()
        return True

    def remove_global_channel(self, guild_id: int, channel_key: str, channel_id: int) -> bool:
        settings = self._ensure_guild_settings(guild_id)
        channels = self._normalize_channel_list(settings.get(channel_key, []))
        if channel_id not in channels:
            return False

        settings[channel_key] = [saved_channel_id for saved_channel_id in channels if saved_channel_id != channel_id]
        self.watch_seen_cache.pop((guild_id, channel_key), None)
        self._save_settings()
        return True

    def _collect_alert_channels(self, channel_key: str) -> List[Tuple[int, int]]:
        channels = []
        for guild_key, settings in self.guild_settings.items():
            try:
                guild_id = int(guild_key)
            except ValueError:
                continue
            for channel_id in self._normalize_channel_list(settings.get(channel_key, [])):
                channels.append((guild_id, channel_id))
        return channels

    def _purge_watch_caches(self) -> None:
        now = asyncio.get_running_loop().time()
        expired_spawn_keys = [
            key for key, timestamp in self.watch_cooldown_cache.items()
            if (now - timestamp) > WATCH_SPAWN_COOLDOWN_SECONDS
        ]
        for key in expired_spawn_keys:
            del self.watch_cooldown_cache[key]

        expired_error_keys = [
            key for key, timestamp in self.watch_error_cooldown_cache.items()
            if (now - timestamp) > WATCH_ERROR_COOLDOWN_SECONDS
        ]
        for key in expired_error_keys:
            del self.watch_error_cooldown_cache[key]

    def _is_watch_on_cooldown(self, guild_id: int, channel_id: int, spawn: PokemonSpawn) -> bool:
        key = (guild_id, channel_id, spawn.number, spawn.coords)
        last_sent = self.watch_cooldown_cache.get(key)
        if last_sent is None:
            return False
        return (asyncio.get_running_loop().time() - last_sent) < WATCH_SPAWN_COOLDOWN_SECONDS

    def _mark_watch_cooldown(self, guild_id: int, channel_id: int, spawn: PokemonSpawn) -> None:
        key = (guild_id, channel_id, spawn.number, spawn.coords)
        self.watch_cooldown_cache[key] = asyncio.get_running_loop().time()

    async def _fetch_watch_source_spawns(self) -> List[PokemonSpawn]:
        return await _run_blocking(
            self.moonani.search_pokemon,
            "",
            self.watch_scan_limit,
            100,
            False,
            0,
            self.watch_scan_limit,
            self.watch_scan_limit,
        )

    async def _fetch_zero_iv_source_spawns(self) -> List[PokemonSpawn]:
        return await _run_blocking(
            self.moonani.list_current_zero_iv_spawns,
            self.zero_iv_scan_limit,
            self.page_size,
            self.max_scan_records,
        )

    async def _fetch_raid_source_data(self) -> List[Dict[str, str]]:
        return await _run_blocking(get_raid_data)

    async def _resolve_text_channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        channel = self.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel

        try:
            fetched_channel = await self.fetch_channel(channel_id)
        except Exception:
            return None

        if isinstance(fetched_channel, discord.TextChannel):
            return fetched_channel
        return None

    async def _log_watch_error(self, channel_id: int, exc: Exception, label: str = "watches") -> None:
        now = asyncio.get_running_loop().time()
        if (now - self.watch_error_cooldown_cache.get(channel_id, 0.0)) < WATCH_ERROR_COOLDOWN_SECONDS:
            return

        print(f"Could not check {label} right now. Reason: {_format_moonani_error(exc)}")
        self.watch_error_cooldown_cache[channel_id] = now

    async def _send_spawn_alerts(
        self,
        guild_id: int,
        channel_id: int,
        cache_kind: str,
        spawns: List[PokemonSpawn],
    ) -> None:
        channel = await self._resolve_text_channel(channel_id)
        if channel is None:
            return

        seen_key = (guild_id, cache_kind)
        seen = self.watch_seen_cache.setdefault(seen_key, set())

        for spawn in spawns:
            if spawn.unique_key in seen:
                continue
            if self._is_watch_on_cooldown(guild_id, channel_id, spawn):
                seen.add(spawn.unique_key)
                continue

            try:
                await _send_pokemon_embed_to_channel(channel, spawn, "Moonani")
            except Exception as exc:
                print(f"Could not send alert '{cache_kind}' to channel {channel_id}: {exc}")
                break

            seen.add(spawn.unique_key)
            self._mark_watch_cooldown(guild_id, channel_id, spawn)

        self.watch_seen_cache[seen_key] = {spawn.unique_key for spawn in spawns}

    async def _broadcast_raid_digest(self) -> None:
        """Fetches and sends the raid digest. Runs sequentially inside the main
        monitor loop so it never competes for memory with the IV100/IV0 fetches."""
        raid_channels = self._collect_alert_channels("raid_channels")
        if not raid_channels:
            return

        try:
            raids = await self._fetch_raid_source_data()
        except Exception as exc:
            notified_channels = set()
            for _, channel_id in raid_channels:
                if channel_id not in notified_channels:
                    await self._log_watch_error(channel_id, exc, "global raid channels")
                    notified_channels.add(channel_id)
            return

        top_raids = raids[: self.raid_broadcast_limit]
        if not top_raids:
            return

        embed = _build_raid_digest_embed(top_raids)
        for _, channel_id in raid_channels:
            channel = await self._resolve_text_channel(channel_id)
            if channel is None:
                continue
            try:
                await channel.send(embed=embed)
            except Exception as exc:
                print(f"Could not send raid digest to channel {channel_id}: {exc}")

    async def _monitor_watch_loop(self) -> None:
        await self.wait_until_ready()
        await asyncio.sleep(self.watch_monitor_interval_seconds)

        loop = asyncio.get_running_loop()
        last_raid_broadcast = 0.0

        while not self.is_closed():
            iv100_channels = self._collect_alert_channels("iv100_channels")
            iv0_channels = self._collect_alert_channels("iv0_channels")

            if iv100_channels or iv0_channels:
                self._purge_watch_caches()

                current_spawns = []  # type: List[PokemonSpawn]
                skip_iv100 = False
                if iv100_channels:
                    try:
                        current_spawns = await self._fetch_watch_source_spawns()
                    except Exception as exc:
                        notified_channels = set()
                        for _, channel_id in iv100_channels:
                            if channel_id not in notified_channels:
                                await self._log_watch_error(channel_id, exc, "global IV100 channels")
                                notified_channels.add(channel_id)
                        skip_iv100 = True

                current_zero_iv_spawns = []  # type: List[PokemonSpawn]
                if iv0_channels:
                    try:
                        current_zero_iv_spawns = await self._fetch_zero_iv_source_spawns()
                    except Exception as exc:
                        notified_channels = set()
                        for _, channel_id in iv0_channels:
                            if channel_id not in notified_channels:
                                await self._log_watch_error(channel_id, exc, "global IV0 channels")
                                notified_channels.add(channel_id)

                if not skip_iv100:
                    for guild_id, channel_id in iv100_channels:
                        await self._send_spawn_alerts(
                            guild_id,
                            channel_id,
                            f"{GLOBAL_IV100_KIND}:{channel_id}",
                            current_spawns,
                        )

                for guild_id, channel_id in iv0_channels:
                    await self._send_spawn_alerts(
                        guild_id,
                        channel_id,
                        f"{GLOBAL_IV0_KIND}:{channel_id}",
                        current_zero_iv_spawns,
                    )

            # Raids run sequentially, after the IV work above, so the two never
            # fetch heavy data from Moonani at the same time.
            now = loop.time()
            if now - last_raid_broadcast >= self.raid_broadcast_interval_seconds:
                await self._broadcast_raid_digest()
                last_raid_broadcast = now

            await asyncio.sleep(self.watch_monitor_interval_seconds)

    async def setup_hook(self) -> None:
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Slash commands synced in server {self.guild_id}: {len(synced)}")
            self.tree.clear_commands(guild=None)
            cleared = await self.tree.sync()
            print(f"Global slash commands cleared to avoid duplicates: {len(cleared)}")
        else:
            synced = await self.tree.sync()
            print(f"Global slash commands synced: {len(synced)}")

        self.monitor_task = asyncio.create_task(self._monitor_watch_loop())


def register_commands(bot: LucarioDiscordBot) -> None:
    @bot.tree.command(name="ping", description="Checks if the bot is online.")
    async def ping(interaction: discord.Interaction) -> None:
        latency_ms = round(bot.latency * 1000, 2)
        await interaction.response.send_message(f"Pong. Approximate latency: {latency_ms} ms")

    @bot.tree.command(name="view_iv_channels", description="Shows the saved global IV100 and IV0 channels.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def view_iv_channels(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        iv100_channels = bot.get_global_channels(interaction.guild_id, "iv100_channels")
        iv0_channels = bot.get_global_channels(interaction.guild_id, "iv0_channels")
        embed = discord.Embed(title="Global IV channels", color=discord.Color.green())
        embed.add_field(
            name="IV100",
            value="\n".join(f"<#{channel_id}>" for channel_id in iv100_channels) if iv100_channels else "None",
            inline=False,
        )
        embed.add_field(
            name="IV0",
            value="\n".join(f"<#{channel_id}>" for channel_id in iv0_channels) if iv0_channels else "None",
            inline=False,
        )
        embed.set_footer(text="Global wild alert channels")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="add_iv100_channel", description="Enables alerts for all wild IV100 Pokemon in a channel.")
    @app_commands.describe(channel="Channel where all IV100 will be sent")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def add_iv100_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        added = bot.add_global_channel(interaction.guild_id, "iv100_channels", channel.id)
        if added:
            await interaction.response.send_message(
                f"IV100 channel added: {channel.mention}. I will send all new wild IV100 spawns there.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{channel.mention} was already set as an IV100 channel.",
                ephemeral=True,
            )

    @bot.tree.command(name="remove_iv100_channel", description="Disables global IV100 alerts in a channel.")
    @app_commands.describe(channel="Channel that will stop receiving all IV100")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_iv100_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        removed = bot.remove_global_channel(interaction.guild_id, "iv100_channels", channel.id)
        if removed:
            await interaction.response.send_message(f"IV100 channel removed: {channel.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"{channel.mention} was not set as an IV100 channel.", ephemeral=True)

    @bot.tree.command(name="add_iv0_channel", description="Enables alerts for all wild IV0 Pokemon in a channel.")
    @app_commands.describe(channel="Channel where all IV0 will be sent")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def add_iv0_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        added = bot.add_global_channel(interaction.guild_id, "iv0_channels", channel.id)
        if added:
            await interaction.response.send_message(
                f"IV0 channel added: {channel.mention}. I will send all new wild IV0 spawns there.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{channel.mention} was already set as an IV0 channel.",
                ephemeral=True,
            )

    @bot.tree.command(name="remove_iv0_channel", description="Disables global IV0 alerts in a channel.")
    @app_commands.describe(channel="Channel that will stop receiving all IV0")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_iv0_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        removed = bot.remove_global_channel(interaction.guild_id, "iv0_channels", channel.id)
        if removed:
            await interaction.response.send_message(f"IV0 channel removed: {channel.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"{channel.mention} was not set as an IV0 channel.", ephemeral=True)

    @bot.tree.command(
        name="add_raid_channel",
        description="Configures a channel to receive the top 10 raids automatically every 30 minutes.",
    )
    @app_commands.describe(channel="Channel where the raid digest will be sent")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def add_raid_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        added = bot.add_global_channel(interaction.guild_id, "raid_channels", channel.id)
        if not added:
            await interaction.response.send_message(
                f"{channel.mention} was already set as a raid channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            raids = await bot._fetch_raid_source_data()
            top_raids = raids[: bot.raid_broadcast_limit]
            if top_raids:
                await channel.send(embed=_build_raid_digest_embed(top_raids))
        except Exception as exc:
            print(f"Could not send the initial raid digest to channel {channel.id}: {exc}")

        await interaction.followup.send(
            f"Raid channel added: {channel.mention}. I will post the top "
            f"{bot.raid_broadcast_limit} raids there right away, then every 30 minutes.",
            ephemeral=True,
        )

    @bot.tree.command(name="remove_raid_channel", description="Disables the automatic raid digest in a channel.")
    @app_commands.describe(channel="Channel that will stop receiving the raid digest")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_raid_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        removed = bot.remove_global_channel(interaction.guild_id, "raid_channels", channel.id)
        if removed:
            await interaction.response.send_message(f"Raid channel removed: {channel.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"{channel.mention} was not set as a raid channel.", ephemeral=True
            )

    @bot.tree.command(name="view_raid_channels", description="Shows the saved automatic raid channels.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def view_raid_channels(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        raid_channels = bot.get_global_channels(interaction.guild_id, "raid_channels")
        embed = discord.Embed(title="Automatic raid channels", color=discord.Color.orange())
        embed.add_field(
            name="Raids",
            value="\n".join(f"<#{channel_id}>" for channel_id in raid_channels) if raid_channels else "None",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        message = f"An error occurred while running the command: `{type(error).__name__}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.NotFound:
            print(f"Could not respond to the interaction because it no longer exists: {error}")


def main() -> None:
    if load_dotenv is not None:
        load_dotenv()

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing environment variable DISCORD_BOT_TOKEN.")

    guild_id_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
    guild_id = int(guild_id_raw) if guild_id_raw else None

    timeout = _read_int_env("MOONANI_TIMEOUT", 20)
    page_size = _read_int_env("MOONANI_PAGE_SIZE", 100)
    max_scan_records = _read_int_env("MOONANI_MAX_SCAN_RECORDS", 10000)
    resolve_countries = _read_bool_env("MOONANI_RESOLVE_COUNTRIES", False)
    geocoder_endpoint = os.getenv("MOONANI_GEOCODER_ENDPOINT", "").strip()
    geocoder_user_agent = os.getenv("MOONANI_GEOCODER_USER_AGENT", "").strip() or "Arceus Discord Bot/1.0"
    settings_path = Path(os.getenv("LUCARIO_SETTINGS_PATH", "lucario_guild_settings.json")).resolve()
    watch_monitor_interval_seconds = _read_int_env("LUCARIO_MONITOR_INTERVAL_SECONDS", 180)
    watch_scan_limit = _read_int_env("LUCARIO_ALERT_LIMIT_100IV", 250)
    zero_iv_scan_limit = _read_int_env("LUCARIO_ALERT_LIMIT_0IV", 250)
    raid_broadcast_interval_seconds = _read_int_env("LUCARIO_RAID_BROADCAST_INTERVAL_SECONDS", 1800)
    raid_broadcast_limit = _read_int_env("LUCARIO_RAID_BROADCAST_LIMIT", 10)

    moonani = MoonaniClient(
        timeout=timeout,
        resolve_missing_countries=resolve_countries,
        geocoder_endpoint=geocoder_endpoint or "https://nominatim.openstreetmap.org/reverse",
        geocoder_user_agent=geocoder_user_agent,
    )
    bot = LucarioDiscordBot(
        moonani=moonani,
        guild_id=guild_id,
        page_size=page_size,
        max_scan_records=max_scan_records,
        settings_path=settings_path,
        watch_monitor_interval_seconds=watch_monitor_interval_seconds,
        watch_scan_limit=watch_scan_limit,
        zero_iv_scan_limit=zero_iv_scan_limit,
        raid_broadcast_interval_seconds=raid_broadcast_interval_seconds,
        raid_broadcast_limit=raid_broadcast_limit,
    )
    register_commands(bot)
    bot.run(token)


if __name__ == "__main__":
    main()
