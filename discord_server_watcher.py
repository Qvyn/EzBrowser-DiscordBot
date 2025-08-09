# discord_server_watcher.py
# EZ setup: asks for token once, persists config; /usehere picks the post channel.
# Commands: /status, /setinterval, /watch add/remove/list, /usehere, /clearchannel, /where

from __future__ import annotations
import os, json, asyncio, logging
from typing import Dict, List, Optional, Set, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

# ---------------- Persistence ----------------
def cfg_dir() -> str:
    # Windows: %APPDATA%\JacintoWatcher ; others: ~/.config/JacintoWatcher
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, "JacintoWatcher")
    os.makedirs(path, exist_ok=True)
    return path

CFG_PATH = os.path.join(cfg_dir(), "config.json")
STATE_FILE = os.path.join(cfg_dir(), "watcher_state.json")

def load_cfg() -> dict:
    if os.path.exists(CFG_PATH):
        try:
            return json.load(open(CFG_PATH, "r"))
        except Exception:
            pass
    return {"token": "", "channel_id": 0, "backend_url": "https://jacinto-server.fly.dev", "poll_seconds": 15, "watch_names": []}

def save_cfg(cfg: dict) -> None:
    json.dump(cfg, open(CFG_PATH, "w"), indent=2)

def load_state() -> Set[tuple]:
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        return {tuple(x) for x in json.load(open(STATE_FILE, "r"))}
    except Exception:
        return set()

def save_state(keys: Set[tuple]) -> None:
    try:
        json.dump([list(k) for k in sorted(keys)], open(STATE_FILE, "w"), indent=2)
    except Exception:
        pass

# ---------------- HTTP ----------------
import ssl
import certifi
import aiohttp

async def fetch_servers(session: aiohttp.ClientSession, backend_url: str) -> List[dict]:
    async with session.get(f"{backend_url}/servers", timeout=5) as resp:
        resp.raise_for_status()
        return await resp.json()

# ---------------- Logging --------------
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
log = logging.getLogger("watcher")

ServerKey = Tuple[str, str, int]  # (name, public_ip, port)

def _key(s: dict) -> ServerKey:
    return (s.get("name", ""), s.get("public_ip", ""), int(s.get("port", 0)))

# ---------------- Bot ------------------
class WatcherBot(discord.Client):
    def __init__(self, *, intents: discord.Intents, cfg: dict):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.cfg = cfg
        self._channel: Optional[discord.TextChannel] = None
        self._last_seen: Set[ServerKey] = load_state()
        self._session: Optional[aiohttp.ClientSession] = None
        self.poll_seconds = int(cfg.get("poll_seconds", 15))
        self.watch_names: Set[str] = set(cfg.get("watch_names", []))

    async def setup_hook(self) -> None:
        # Slash commands
        @self.tree.command(name="status", description="Show current Jacinto servers")
        async def status_cmd(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True, ephemeral=True)
            try:
                servers = await fetch_servers(self._session, self.cfg["backend_url"])
                if self.watch_names:
                    servers = [s for s in servers if s.get("name") in self.watch_names]
                await interaction.followup.send(self._format_servers(servers) or "No servers found.", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Error: {e}", ephemeral=True)

        @self.tree.command(name="setinterval", description="Set polling interval in seconds (min 5)")
        @app_commands.describe(seconds="Polling interval in seconds")
        async def setinterval_cmd(interaction: discord.Interaction, seconds: int):
            if seconds < 5:
                await interaction.response.send_message("Interval too low. Min: 5s", ephemeral=True)
                return
            self.poll_seconds = seconds
            self.cfg["poll_seconds"] = seconds
            save_cfg(self.cfg)
            self.poller.change_interval(seconds=self.poll_seconds)
            await interaction.response.send_message(f"Poll interval set to {self.poll_seconds}s", ephemeral=True)

        @self.tree.command(name="watch", description="Manage name filters (add/remove/list)")
        @app_commands.describe(action="add/remove/list", name="Server name (optional for list)")
        async def watch_cmd(interaction: discord.Interaction, action: str, name: Optional[str] = None):
            action = action.lower()
            if action == "list":
                msg = ", ".join(sorted(self.watch_names)) or "<none>"
                await interaction.response.send_message(f"Filters: {msg}", ephemeral=True)
                return
            if not name:
                await interaction.response.send_message("Provide a server name.", ephemeral=True)
                return
            if action == "add":
                self.watch_names.add(name)
                self.cfg["watch_names"] = sorted(self.watch_names)
                save_cfg(self.cfg)
                await interaction.response.send_message(f"Added filter: {name}", ephemeral=True)
            elif action == "remove":
                self.watch_names.discard(name)
                self.cfg["watch_names"] = sorted(self.watch_names)
                save_cfg(self.cfg)
                await interaction.response.send_message(f"Removed filter: {name}", ephemeral=True)
            else:
                await interaction.response.send_message("Use add/remove/list", ephemeral=True)

        @self.tree.command(name="usehere", description="Post UP/DOWN messages in this channel")
        async def usehere_cmd(interaction: discord.Interaction):
            # Optional: require Manage Channels to move it
            # perms = interaction.user.guild_permissions
            # if not perms.manage_channels: ...
            ch = interaction.channel
            if not isinstance(ch, discord.TextChannel):
                await interaction.response.send_message("This isn't a text channel.", ephemeral=True)
                return
            self._channel = ch
            self.cfg["channel_id"] = ch.id
            save_cfg(self.cfg)
            await interaction.response.send_message(f"Okay! I’ll post here: #{ch.name}", ephemeral=True)

        @self.tree.command(name="clearchannel", description="Stop posting (log only)")
        async def clearchannel_cmd(interaction: discord.Interaction):
            self._channel = None
            self.cfg["channel_id"] = 0
            save_cfg(self.cfg)
            await interaction.response.send_message("Posting disabled. I’ll log only.", ephemeral=True)

        @self.tree.command(name="where", description="Show where I will post")
        async def where_cmd(interaction: discord.Interaction):
            if self._channel:
                await interaction.response.send_message(f"Posting to: #{self._channel.name} ({self._channel.id})", ephemeral=True)
            else:
                await interaction.response.send_message("No post channel set. Use /usehere in a text channel.", ephemeral=True)

        await self.tree.sync()
        # HTTPS session
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), connector=aiohttp.TCPConnector(ssl=ssl_ctx))
        self.poller.change_interval(seconds=self.poll_seconds)
        self.poller.start()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        # Restore channel if saved
        ch_id = int(self.cfg.get("channel_id", 0) or 0)
        if ch_id:
            ch = self.get_channel(ch_id)
            if isinstance(ch, discord.TextChannel):
                self._channel = ch
                log.info("Posting to #%s (%d)", ch.name, ch.id)
            else:
                log.warning("Saved channel not found or not a text channel; use /usehere again.")

    @tasks.loop(seconds=15)
    async def poller(self):
        if not self._session:
            return
        try:
            servers = await fetch_servers(self._session, self.cfg["backend_url"])
        except Exception as e:
            log.warning("Fetch error: %s", e)
            return
        if self.watch_names:
            servers = [s for s in servers if s.get("name") in self.watch_names]
        current = {_key(s) for s in servers}
        ups = current - self._last_seen
        downs = self._last_seen - current
        if ups or downs:
            save_state(current)
            self._last_seen = current

        if self._channel:
            for name, ip, port in sorted(ups):
                await self._channel.send(f"🟢 **UP**: `{name}` at `{ip}:{port}` is now available.")
            for name, ip, port in sorted(downs):
                await self._channel.send(f"🔴 **DOWN**: `{name}` at `{ip}:{port}` is no longer available.")
        else:
            for name, ip, port in sorted(ups):
                log.info("UP: %s %s:%d", name, ip, port)
            for name, ip, port in sorted(downs):
                log.info("DOWN: %s %s:%d", name, ip, port)

    @poller.before_loop
    async def before_poller(self):
        await self.wait_until_ready()

    def _format_servers(self, servers: List[dict]) -> str:
        if not servers:
            return ""
        lines = ["**Current Servers**"]
        for s in servers:
            lines.append(f"• {s.get('name','?')} — `{s.get('public_ip','?')}:{s.get('port','?')}` (map: {s.get('map','?')})")
        return "\n".join(lines)

    async def close(self):
        try:
            if self._session:
                await self._session.close()
        finally:
            await super().close()

# ---------------- Main ------------------
def main():
    cfg = load_cfg()
    token = cfg.get("token", "").strip()
    if not token:
        # Ask once, then persist
        print("Enter your Discord bot token (saved to config for next runs):")
        token = input("> ").strip()
        if not token:
            raise SystemExit("No token provided.")
        cfg["token"] = token
        save_cfg(cfg)

    intents = discord.Intents.none()
    bot = WatcherBot(intents=intents, cfg=cfg)
    bot.run(token)

if __name__ == "__main__":
    main()
