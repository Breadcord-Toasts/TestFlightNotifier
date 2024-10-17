import sqlite3
from asyncio import tasks
from dataclasses import dataclass
from typing import cast

import discord
from bs4 import BeautifulSoup
from discord.ext import tasks, commands

import breadcord
from breadcord.helpers import HTTPModuleCog

HEADERS = {
    "Accept": "text/html",
    "Accept-Language": "en-US,en",
}


@dataclass
class TestFlightApp:
    is_full: bool
    id: str
    name: str
    icon_url: str


class TestFlightNotifier(HTTPModuleCog):
    def __init__(self, module_id: str) -> None:
        super().__init__(module_id)
        self.db_connection = sqlite3.connect(self.module.storage_path / "state.db")
        self.fresh_start: bool = self.create_cache(self.db_connection)
        self.loop: tasks.Loop | None = None

        @self.settings.check_interval.observe
        def on_check_interval_changed(_, new: float) -> None:
            if self.loop is not None:
                self.loop.stop()
            self.logger.debug(f'Check interval set to {new} hours')
            self.loop = tasks.loop(hours=new)(self.loop_callback)
            self.loop.start()

        on_check_interval_changed(0, self.settings.check_interval.value)

    async def cog_unload(self) -> None:
        self.db_connection.close()

    @staticmethod
    def create_cache(connection: sqlite3.Connection) -> bool:
        """Returns True if the table was created, False if it already existed."""
        existed_before = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='state'",
        ).fetchone()

        connection.execute(
            "CREATE TABLE IF NOT EXISTS state ("
            "   app_id TEXT PRIMARY KEY,"
            "   is_full BOOLEAN"
            ")",
        )
        connection.commit()
        return existed_before is None

    async def loop_callback(self) -> None:
        self.logger.debug("Checking TestFlight app statuses")
        watched_apps: list[str] = cast(list[str], self.settings.watched_apps.value)
        for app_id in watched_apps:
            app_info = await self.fetch_app_info(app_id)
            if app_info is None:
                if self.settings.send_errors.value:
                    await self.send_error(app_id)
                continue
            if not self.fresh_start and (app_info.is_full != self.was_full(app_id)):
                await self.send_info(app_info)
            self.store_app_info(app_info)

    async def send_info(self, app_info: TestFlightApp) -> None:
        channel = (
            self.bot.get_channel(self.settings.notification_channel_id.value)
            or await self.bot.fetch_channel(self.settings.notification_channel_id.value)
        )
        if channel is None:
            self.logger.error("Notification channel not found")
            return

        embed = (
            discord.Embed(
                title=app_info.name,
                url=f"https://testflight.apple.com/join/{app_info.id}",
                description=f"TestFlight app is now **{'full' if app_info.is_full else 'not full'}**",
                color=discord.Color.red() if app_info.is_full else discord.Color.green(),
            )
            .set_thumbnail(url=app_info.icon_url)
        )
        await channel.send(
            embed=embed,
            content=cast(
                str,
                self.settings.filled_message.value if app_info.is_full else self.settings.unfilled_message.value
            ) or None
        )

    async def send_error(self, app_id: str) -> None:
        channel = (
            self.bot.get_channel(self.settings.notification_channel_id.value)
            or await self.bot.fetch_channel(self.settings.notification_channel_id.value)
        )
        if channel is None:
            self.logger.error("Notification channel not found")
            return

        await channel.send(f"Failed to fetch app info for app `{app_id}`")

    async def fetch_app_info(self, app_id: str) -> TestFlightApp | None:
        assert self.session is not None, "HTTP session not initialized"
        async with self.session.get(
            f"https://testflight.apple.com/join/{app_id}",
            headers=HEADERS,
        ) as resp:
            if resp.status != 200:
                return None
            soup = BeautifulSoup(await resp.text(), "html.parser")

        status_text = soup.find("div", id="status").find("span")
        full = "is full" in status_text.text.lower()

        icon_element = soup.find("div", id="status").find("div", class_=["app-icon", "ios"])
        icon_url = icon_element["style"].split("url(", 1)[1].split(")")[0]

        page_title = soup.find("head").find("title").text
        name = page_title.removeprefix("Join the ").removesuffix(" - TestFlight - Apple")

        return TestFlightApp(
            is_full=full,
            id=app_id,
            name=name,
            icon_url=icon_url,
        )

    def store_app_info(self, info: TestFlightApp) -> None:
        with self.db_connection:
            cursor = self.db_connection.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO state (app_id, is_full) VALUES (?, ?)",
                (info.id, info.is_full),
            )
            self.db_connection.commit()

    def was_full(self, app_id: str) -> bool | None:
        cursor = self.db_connection.cursor()
        full = cursor.execute(
            "SELECT is_full FROM state WHERE app_id = ?",
            (app_id,),
        ).fetchone()
        return bool(full[0]) if full is not None else None

    @commands.command()
    @commands.is_owner()
    async def add_testflight(self, ctx: commands.Context, app_id: str):
        if app_id.startswith(("https://", "http://")):
            # https://testflight.apple.com/join/xxxxxxx
            parts = app_id.split("/", 4)
            if len(parts) < 4 or parts[2] != "testflight.apple.com" or parts[3] != "join":
                return await ctx.send("Invalid URL")
            app_id = parts[4]

        setting = self.settings.watched_apps
        if app_id in setting.value:
            return await ctx.send("App already being watched")
        info = await self.fetch_app_info(app_id)
        if info is None:
            return await ctx.send("App not found")
        self.store_app_info(info)

        setting.value.append(app_id)
        await ctx.send(f"Watching {info.name}")

    @commands.command()
    @commands.is_owner()
    async def remove_testflight(self, ctx: commands.Context, app_id: str):
        if app_id.startswith(("https://", "http://")):
            # https://testflight.apple.com/join/xxxxxxx
            parts = app_id.split("/", 4)
            if len(parts) < 4 or parts[2] != "testflight.apple.com" or parts[3] != "join":
                return await ctx.send("Invalid URL")
            app_id = parts[4]

        setting = self.settings.watched_apps
        if app_id not in setting.value:
            return await ctx.send("App not being watched")
        setting.value.remove(app_id)
        await ctx.send("No longer watching app")

    @commands.command()
    @commands.is_owner()
    async def list_testflight(self, ctx: commands.Context):
        setting = self.settings.watched_apps
        if not setting.value:
            return await ctx.send("No apps being watched")

        infos: list[TestFlightApp] = []
        for app_id in setting.value:
            info = await self.fetch_app_info(app_id)
            if info is None:
                continue
            self.store_app_info(info)
            infos.append(info)

        await ctx.send(
            "Watching apps:\n"
            + "\n".join([f"- {info.name} (`{info.id}`)" for info in infos]),
        )

    @commands.command()
    @commands.is_owner()
    async def set_testflight_channel(self, ctx: commands.Context, channel_id: int):
        channel = await self.bot.fetch_channel(channel_id)
        self.settings.notification_channel_id.value = channel.id
        await ctx.send(f"Notification channel set to {channel.mention}")

    @commands.command()
    @commands.is_owner()
    async def set_testflight_check_interval(self, ctx: commands.Context, hours: float):
        self.settings.check_interval.value = hours
        await ctx.send(f"Check interval set to {hours} hours")


async def setup(bot: breadcord.Bot, module: breadcord.module.Module) -> None:
    await bot.add_cog(TestFlightNotifier(module.id))
