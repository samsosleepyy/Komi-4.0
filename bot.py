from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional, Set

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands

from addon_processor import AddonError, convert_addon, inspect_addon

ROOT = Path(__file__).parent
TEMP_ROOT = ROOT / "temp"
PANELS_FILE = ROOT / "panels.json"
TEMP_ROOT.mkdir(exist_ok=True)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
ALLOWED_GUILDS: Set[int] = {
    int(x.strip()) for x in os.getenv("ALLOWED_GUILDS", "1420339720277463112,1441795602550882334").split(",") if x.strip().isdigit()
}
MAX_PARALLEL_JOBS = int(os.getenv("MAX_PARALLEL_JOBS", "1"))

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
convert_semaphore = asyncio.Semaphore(MAX_PARALLEL_JOBS)

# channel_id -> ticket state
TICKETS: Dict[int, dict] = {}


def load_panels() -> Dict[str, dict]:
    if not PANELS_FILE.exists():
        return {}
    try:
        return json.loads(PANELS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_panels(data: Dict[str, dict]) -> None:
    PANELS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def send_webhook_log(
    *,
    title: str,
    description: str = "",
    color: int = 0x2B2D31,
    fields: Optional[list[tuple[str, str, bool]]] = None,
    files: Optional[list[Path]] = None,
) -> None:
    if not WEBHOOK_URL:
        return

    embed = {
        "title": title,
        "description": description[:4000],
        "color": color,
        "fields": [
            {"name": name[:256], "value": value[:1024], "inline": inline}
            for name, value, inline in (fields or [])
        ],
    }
    payload = {"embeds": [embed]}

    try:
        async with aiohttp.ClientSession() as session:
            if files:
                form = aiohttp.FormData()
                form.add_field("payload_json", json.dumps(payload, ensure_ascii=False), content_type="application/json")
                handles = []
                try:
                    for i, path in enumerate(files[:10]):
                        if not path.exists():
                            continue
                        handle = open(path, "rb")
                        handles.append(handle)
                        form.add_field(
                            f"files[{i}]",
                            handle,
                            filename=path.name,
                            content_type="application/octet-stream",
                        )
                    async with session.post(WEBHOOK_URL, data=form) as resp:
                        await resp.text()
                finally:
                    for handle in handles:
                        handle.close()
            else:
                async with session.post(WEBHOOK_URL, json=payload) as resp:
                    await resp.text()
    except Exception as exc:
        print(f"Webhook log failed: {exc}")


async def report_unauthorized_guild(guild: discord.Guild) -> None:
    owner_text = f"Unknown owner\n`{guild.owner_id}`"
    try:
        owner = guild.owner or await bot.fetch_user(guild.owner_id)
        owner_text = f"{owner}\n`{guild.owner_id}`"
    except Exception:
        pass

    await send_webhook_log(
        title="Unauthorized Guild Detected",
        description="บอทถูกเพิ่มเข้า server นอก allowlist และกำลังออกจาก server นี้ทันที\n\nเพื่อความปลอดภัย ระบบนี้ไม่สร้างหรือส่ง invite link ของ server ที่ไม่ได้รับอนุญาต",
        color=0xED4245,
        fields=[
            ("Server", f"{guild.name}\n`{guild.id}`", False),
            ("Owner", owner_text, False),
            ("Members", str(guild.member_count or "Unknown"), True),
        ],
    )


async def enforce_guild_allowlist() -> None:
    for guild in list(bot.guilds):
        if guild.id not in ALLOWED_GUILDS:
            await report_unauthorized_guild(guild)
            try:
                await guild.leave()
            except Exception as exc:
                print(f"Failed to leave guild {guild.id}: {exc}")


async def schedule_delete(channel: discord.TextChannel, delay: int) -> None:
    try:
        await asyncio.sleep(delay)
        await channel.delete(reason="Temporary addon UI ticket expired")
    except asyncio.CancelledError:
        return
    except discord.NotFound:
        return
    except Exception as exc:
        print(f"Failed deleting channel: {exc}")


class StartPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="เริ่มรวมแอดออน", style=discord.ButtonStyle.primary, custom_id="addon_ui:start")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("ใช้ได้เฉพาะใน server เท่านั้น", ephemeral=True)
            return
        if interaction.guild.id not in ALLOWED_GUILDS:
            await interaction.response.send_message("server นี้ไม่ได้รับอนุญาตให้ใช้บอท", ephemeral=True)
            return

        panels = load_panels()
        conf = panels.get(str(interaction.guild.id), {})
        category_id = conf.get("category_id")
        category = interaction.guild.get_channel(int(category_id)) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            category = interaction.channel.category if isinstance(interaction.channel, discord.TextChannel) else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("ไม่พบ category สำหรับสร้าง ticket ให้ใช้ /setup ใหม่", ephemeral=True)
            return

        safe_name = f"addon-{interaction.user.name}".lower().replace(" ", "-")[:80]
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True, manage_channels=True),
        }

        try:
            channel = await interaction.guild.create_text_channel(
                name=safe_name,
                category=category,
                overwrites=overwrites,
                reason="Addon UI temporary ticket",
            )
        except Exception as exc:
            await interaction.response.send_message(f"สร้างช่องไม่สำเร็จ: {exc}", ephemeral=True)
            return

        task = asyncio.create_task(schedule_delete(channel, 180))
        TICKETS[channel.id] = {
            "user_id": interaction.user.id,
            "delete_task": task,
            "work_dir": None,
            "source_file": None,
            "inspection": None,
        }

        await interaction.response.send_message(f"สร้างช่องแล้ว: {channel.mention}", ephemeral=True)
        await channel.send(
            f"{interaction.user.mention} สามารถอัปโหลดแอดออน `.mcaddon` หรือ `.zip` ลงในช่องได้เลย\n"
            "ช่องนี้มีเวลา 3 นาทีก่อนจะโดนลบ หากส่งไฟล์แล้วเวลาลบอัตโนมัติจะหยุดจนกว่าบอทจะแปลงเสร็จ"
        )


class ItemReviewSelect(discord.ui.Select):
    def __init__(self, ticket_channel_id: int, candidates: list[dict]):
        self.ticket_channel_id = ticket_channel_id
        options = []
        for c in candidates[:25]:
            label = c["display_name"][:100] or c["identifier"][:100]
            desc = c["file_path"][:100]
            options.append(discord.SelectOption(label=label, value=c["identifier"], description=desc, emoji="📄"))
        super().__init__(
            placeholder="เลือกไอเท็มที่จะรวมเข้า UI",
            min_values=1,
            max_values=max(1, len(options)),
            options=options,
            custom_id=f"addon_ui:select:{ticket_channel_id}",
        )

    async def callback(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state["user_id"]:
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่เลือกได้", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)

        channel = interaction.channel
        selected_ids = list(self.values)
        work_dir = state["work_dir"]
        source_file = state["source_file"]
        if not work_dir or not source_file:
            await interaction.followup.send("ไม่พบไฟล์ต้นฉบับใน ticket นี้")
            return

        try:
            async with convert_semaphore:
                converted = await asyncio.to_thread(convert_addon, source_file, selected_ids, work_dir)
            converted_path = Path(converted)
            await interaction.followup.send(
                f"{interaction.user.mention} แปลงเสร็จแล้ว มีเวลาดาวน์โหลด 1 นาทีก่อนช่องจะถูกลบ",
                file=discord.File(converted_path),
            )
            await send_webhook_log(
                title="Addon Converted",
                description="แปลง addon สำเร็จ",
                color=0x57F287,
                fields=[
                    ("User", f"{interaction.user}\n`{interaction.user.id}`", False),
                    ("Guild", f"{interaction.guild.name if interaction.guild else 'DM'}\n`{interaction.guild.id if interaction.guild else 'DM'}`", False),
                    ("Selected Items", "\n".join(f"`{x}`" for x in selected_ids)[:1024], False),
                ],
                files=[Path(source_file), converted_path],
            )
        except Exception as exc:
            await interaction.followup.send(f"แปลงไม่สำเร็จ: `{exc}`")
            await send_webhook_log(
                title="Addon Convert Failed",
                description=str(exc),
                color=0xED4245,
                fields=[("User", f"{interaction.user}\n`{interaction.user.id}`", False)],
            )
            return

        old_task = state.get("delete_task")
        if old_task and not old_task.done():
            old_task.cancel()
        if isinstance(channel, discord.TextChannel):
            TICKETS[self.ticket_channel_id]["delete_task"] = asyncio.create_task(schedule_delete(channel, 60))


class ItemReviewView(discord.ui.View):
    def __init__(self, ticket_channel_id: int, candidates: list[dict]):
        super().__init__(timeout=600)
        self.add_item(ItemReviewSelect(ticket_channel_id, candidates))


@bot.event
async def on_ready():
    bot.add_view(StartPanelView())
    await enforce_guild_allowlist()
    try:
        # Sync globally; may take time to appear. For fast dev, sync per allowed guild manually if needed.
        await bot.tree.sync()
    except Exception as exc:
        print(f"Command sync failed: {exc}")
    print(f"Logged in as {bot.user} ({bot.user.id if bot.user else 'unknown'})")


@bot.event
async def on_guild_join(guild: discord.Guild):
    if guild.id not in ALLOWED_GUILDS:
        await report_unauthorized_guild(guild)
        await guild.leave()


@bot.tree.command(name="setup", description="สร้าง panel รวมไอเท็ม addon เข้า UI อัตโนมัติ")
@app_commands.describe(category="หมวดหมู่ที่จะสร้าง ticket", channel="ช่องที่จะส่ง panel", image_url="ลิงก์รูปสำหรับ embed")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, category: discord.CategoryChannel, channel: discord.TextChannel, image_url: Optional[str] = None):
    if not interaction.guild or interaction.guild.id not in ALLOWED_GUILDS:
        await interaction.response.send_message("server นี้ไม่ได้รับอนุญาตให้ใช้บอท", ephemeral=True)
        return

    panels = load_panels()
    panels[str(interaction.guild.id)] = {"category_id": category.id, "panel_channel_id": channel.id}
    save_panels(panels)

    embed = discord.Embed(
        title="รวมไอเท่มใส่ ui อัตโนมัติ",
        description=(
            "บอทนี้ใช้สำหรับแปลง Minecraft Bedrock addon ที่มีไอเท็มเกราะหลายชิ้น "
            "ให้รวมเป็นไอเท็มเปิด UI เพียงอันเดียว\n\n"
            "**วิธีทำงาน**\n"
            "1. กดปุ่ม **เริ่มรวมแอดออน**\n"
            "2. บอทจะสร้างช่องส่วนตัวชั่วคราวให้คุณ\n"
            "3. อัปโหลดไฟล์ `.mcaddon` หรือ `.zip`\n"
            "4. บอทจะตรวจหา `items`, `attachables`, `geometry`, `animations`, `textures`\n"
            "5. เลือกไอเท็มที่จะรวมเข้า UI ผ่าน dropdown แบบ Review Mode\n"
            "6. บอทจะ copy ไอเท็มให้ครบช่อง หัว/ตัว/กางเกง/รองเท้า และสร้าง UI opener\n"
            "7. ในเกม ผู้เล่นกดใช้ไอเท็ม UI เพื่อเลือกเกราะและช่องที่จะใส่ด้วย `replaceitem`\n\n"
            "**ระบบป้องกันการทับ**\n"
            "ถ้าช่องปลายทางมีไอเท็มอยู่แล้ว บอทจะสร้าง script ให้ถามก่อนว่าจะทับไหม\n"
            "ก่อนใส่ชิ้นใหม่ script จะลบเกราะจาก addon เดียวกันออกจากช่องอื่นเท่านั้น ไม่แตะ vanilla หรือ addon อื่น\n\n"
            "**ปุ่ม**\n"
            "`เริ่มรวมแอดออน` - เปิด ticket ส่วนตัวสำหรับอัปโหลดและแปลงไฟล์"
        ),
        color=discord.Color.blurple(),
    )
    if image_url:
        embed.set_image(url=image_url)

    await channel.send(embed=embed, view=StartPanelView())
    await interaction.response.send_message(f"สร้าง panel แล้วใน {channel.mention}", ephemeral=True)


@setup.error
async def setup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("คำสั่งนี้ใช้ได้เฉพาะผู้ดูแลระบบเท่านั้น", ephemeral=True)
    else:
        await interaction.response.send_message(f"เกิดข้อผิดพลาด: {error}", ephemeral=True)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    state = TICKETS.get(message.channel.id)
    if not state:
        return
    if message.author.id != state["user_id"]:
        return
    if not message.attachments:
        return

    attachment = next((a for a in message.attachments if a.filename.lower().endswith((".mcaddon", ".zip"))), None)
    if not attachment:
        await message.channel.send("กรุณาอัปโหลดไฟล์ `.mcaddon` หรือ `.zip` เท่านั้น")
        return

    old_task = state.get("delete_task")
    if old_task and not old_task.done():
        old_task.cancel()

    job_dir = Path(tempfile.mkdtemp(prefix=f"addon_job_{message.author.id}_", dir=TEMP_ROOT))
    source_file = job_dir / attachment.filename
    try:
        await attachment.save(source_file)
        state["work_dir"] = str(job_dir)
        state["source_file"] = str(source_file)
        await message.channel.send("ได้รับไฟล์แล้ว กำลังตรวจสอบโครงสร้าง addon...")

        inspection = await asyncio.to_thread(inspect_addon, str(source_file), str(job_dir))
        candidates = [asdict(c) for c in inspection.candidates]
        state["inspection"] = candidates

        await send_webhook_log(
            title="Addon Uploaded",
            description="มีผู้ใช้อัปโหลด addon สำหรับแปลง",
            color=0x5865F2,
            fields=[
                ("User", f"{message.author}\n`{message.author.id}`", False),
                ("Guild", f"{message.guild.name if message.guild else 'DM'}\n`{message.guild.id if message.guild else 'DM'}`", False),
                ("File", attachment.filename, True),
                ("Detected Items", str(len(candidates)), True),
            ],
            files=[source_file],
        )

        desc = "\n".join(f"`{i+1}.` **{c['display_name']}** - `{c['file_path']}`" for i, c in enumerate(candidates[:25]))
        if len(candidates) > 25:
            desc += f"\n\nพบ {len(candidates)} ไอเท็ม แต่ Discord dropdown แสดงได้ครั้งละ 25 ตัวเลือก ตอนนี้แสดง 25 รายการแรก"
        embed = discord.Embed(
            title="📄 Review Mode: เลือกไอเท็มที่จะรวมเข้า UI",
            description=desc or "ไม่พบรายการ",
            color=discord.Color.green(),
        )
        await message.channel.send(embed=embed, view=ItemReviewView(message.channel.id, candidates))
    except Exception as exc:
        await message.channel.send(f"ตรวจสอบ/อ่าน addon ไม่สำเร็จ: `{exc}`")
        await send_webhook_log(
            title="Addon Inspect Failed",
            description=str(exc),
            color=0xED4245,
            fields=[("User", f"{message.author}\n`{message.author.id}`", False)],
            files=[source_file] if source_file.exists() else None,
        )
        try:
            shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass


async def start_health_server() -> None:
    """Small HTTP server for Render Web Service deployments.

    Discord bots are long-running gateway clients and normally should be
    deployed as a Render Background Worker. If the project is deployed as a
    Web Service instead, Render requires a port to be bound. This health
    server satisfies that requirement without affecting the Discord bot.
    """
    port = int(os.getenv("PORT", "10000"))

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Health server listening on 0.0.0.0:{port}")


async def main() -> None:
    await start_health_server()
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
