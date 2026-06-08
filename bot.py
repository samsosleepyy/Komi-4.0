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

from addon_processor import AddonError, convert_addon, inspect_addon, merge_addons

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
MIN_MERGE_ADDONS = 2
MAX_MERGE_ADDONS = 5

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
                        if not path or not path.exists():
                            continue
                        handle = open(path, "rb")
                        handles.append(handle)
                        form.add_field(f"files[{i}]", handle, filename=path.name, content_type="application/octet-stream")
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
        await channel.delete(reason="Temporary addon ticket expired")
    except asyncio.CancelledError:
        return
    except discord.NotFound:
        return
    except Exception as exc:
        print(f"Failed deleting channel: {exc}")


def mode_label(mode: str) -> str:
    return f"รวมแอดออน {MIN_MERGE_ADDONS}-{MAX_MERGE_ADDONS} ไฟล์" if mode == "merge_addons" else "รวมไอเท็มเป็น UI"


async def create_ticket(interaction: discord.Interaction, mode: str) -> None:
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

    safe_user = interaction.user.name.lower().replace(" ", "-")[:50]
    safe_name = ("merge-" if mode == "merge_addons" else "ui-") + safe_user
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True, manage_channels=True),
    }
    try:
        channel = await interaction.guild.create_text_channel(name=safe_name[:80], category=category, overwrites=overwrites, reason=f"Addon ticket: {mode}")
    except Exception as exc:
        await interaction.response.send_message(f"สร้างช่องไม่สำเร็จ: {exc}", ephemeral=True)
        return

    task = asyncio.create_task(schedule_delete(channel, 180))
    TICKETS[channel.id] = {
        "mode": mode,
        "user_id": interaction.user.id,
        "delete_task": task,
        "work_dir": None,
        "source_file": None,
        "source_files": [],
        "inspection": None,
    }
    await interaction.response.send_message(f"สร้างช่องแล้ว: {channel.mention}", ephemeral=True)
    if mode == "merge_addons":
        await channel.send(
            f"{interaction.user.mention} โหมด **รวมแอดออน 2-5 ไฟล์**\n"
            "อัปโหลด `.mcaddon` หรือ `.zip` ได้ 2-5 ไฟล์ในช่องนี้ได้เลย\n"
            "ช่องนี้มีเวลา 3 นาทีก่อนจะโดนลบ ถ้าเริ่มส่งไฟล์แล้วเวลาลบอัตโนมัติจะหยุดจนกว่าบอททำงานเสร็จ"
        )
    else:
        await channel.send(
            f"{interaction.user.mention} โหมด **รวมไอเท็มเป็น UI**\n"
            "อัปโหลดแอดออน `.mcaddon` หรือ `.zip` ลงในช่องได้เลย\n"
            "ช่องนี้มีเวลา 3 นาทีก่อนจะโดนลบ หากส่งไฟล์แล้วเวลาลบอัตโนมัติจะหยุดจนกว่าบอทจะแปลงเสร็จ"
        )


class PanelModeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="รวมไอเท็มเป็น UI", value="combine_ui", description="แปลง addon ที่มีหลายไอเท็มให้เป็นไอเท็ม UI อัตโนมัติ", emoji="🎨"),
            discord.SelectOption(label="รวมแอดออน", value="merge_addons", description="รวม addon ได้สูงสุด 5 ไฟล์เป็น addon เดียว พร้อมกันชื่อ/ไฟล์ชน", emoji="📦"),
        ]
        super().__init__(placeholder="เลือกโหมดที่ต้องการใช้งาน", min_values=1, max_values=1, options=options, custom_id="addon_tools:mode_select")

    async def callback(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.values[0])


class StartPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PanelModeSelect())


class ItemReviewSelect(discord.ui.Select):
    def __init__(self, ticket_channel_id: int, candidates: list[dict]):
        self.ticket_channel_id = ticket_channel_id
        options = []
        for c in candidates[:25]:
            label = c["display_name"][:100] or c["identifier"][:100]
            desc = c["file_path"][:100]
            options.append(discord.SelectOption(label=label, value=c["identifier"], description=desc, emoji="📄"))
        super().__init__(placeholder="เลือกไอเท็มที่จะรวมเข้า UI", min_values=1, max_values=max(1, len(options)), options=options, custom_id=f"addon_ui:select:{ticket_channel_id}")

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
            await interaction.followup.send(f"{interaction.user.mention} แปลงเสร็จแล้ว มีเวลาดาวน์โหลด 1 นาทีก่อนช่องจะถูกลบ", file=discord.File(converted_path))
            await send_webhook_log(
                title="Addon UI Converted",
                description="แปลง addon เป็น UI สำเร็จ",
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
            await send_webhook_log(title="Addon UI Convert Failed", description=str(exc), color=0xED4245, fields=[("User", f"{interaction.user}\n`{interaction.user.id}`", False)])
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
        await bot.tree.sync()
    except Exception as exc:
        print(f"Command sync failed: {exc}")
    print(f"Logged in as {bot.user} ({bot.user.id if bot.user else 'unknown'})")


@bot.event
async def on_guild_join(guild: discord.Guild):
    if guild.id not in ALLOWED_GUILDS:
        await report_unauthorized_guild(guild)
        await guild.leave()


@bot.tree.command(name="setup", description="สร้าง panel แปลง/รวม Minecraft Bedrock addon")
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
        title="Minecraft Bedrock Addon Tools",
        description=(
            "เลือกโหมดจาก dropdown ด้านล่าง\n\n"
            "🎨 **รวมไอเท็มเป็น UI**\n"
            "แปลง addon ที่มีไอเท็มเกราะหลายอันให้เหลือไอเท็ม UI อันเดียว ใช้ `replaceitem` ใส่ช่องหัว/ตัว/กางเกง/รองเท้า และลบเฉพาะเกราะจาก addon เดียวกัน\n\n"
            "📦 **รวมแอดออน**\n"
            "รวม addon ได้ 2-5 ไฟล์เป็น addon เดียว สุ่ม UUID ใหม่ แยก scripts เป็น `scripts/addon_*` กันชื่อไฟล์/identifier/geometry/animation/texture ชน และสร้าง `MERGE_REPORT.txt` ให้ตรวจสอบ\n\n"
            "หลังเลือกโหมด บอทจะสร้าง ticket ส่วนตัวให้อัปโหลดไฟล์"
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


def _valid_addon_attachments(message: discord.Message) -> list[discord.Attachment]:
    return [a for a in message.attachments if a.filename.lower().endswith((".mcaddon", ".zip"))]


async def handle_combine_ui_message(message: discord.Message, state: dict, attachments: list[discord.Attachment]) -> None:
    attachment = attachments[0]
    old_task = state.get("delete_task")
    if old_task and not old_task.done():
        old_task.cancel()
    job_dir = Path(tempfile.mkdtemp(prefix=f"addon_ui_{message.author.id}_", dir=TEMP_ROOT))
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
            title="Addon Uploaded for UI",
            description="มีผู้ใช้อัปโหลด addon สำหรับแปลงเป็น UI",
            color=0x5865F2,
            fields=[("User", f"{message.author}\n`{message.author.id}`", False), ("Guild", f"{message.guild.name if message.guild else 'DM'}\n`{message.guild.id if message.guild else 'DM'}`", False), ("File", attachment.filename, True), ("Detected Items", str(len(candidates)), True)],
            files=[source_file],
        )
        desc = "\n".join(f"`{i+1}.` **{c['display_name']}** - `{c['file_path']}`" for i, c in enumerate(candidates[:25]))
        if len(candidates) > 25:
            desc += f"\n\nพบ {len(candidates)} ไอเท็ม แต่ Discord dropdown แสดงได้ครั้งละ 25 ตัวเลือก ตอนนี้แสดง 25 รายการแรก"
        embed = discord.Embed(title="📄 Review Mode: เลือกไอเท็มที่จะรวมเข้า UI", description=desc or "ไม่พบรายการ", color=discord.Color.green())
        await message.channel.send(embed=embed, view=ItemReviewView(message.channel.id, candidates))
    except Exception as exc:
        await message.channel.send(f"ตรวจสอบ/อ่าน addon ไม่สำเร็จ: `{exc}`")
        await send_webhook_log(title="Addon UI Inspect Failed", description=str(exc), color=0xED4245, fields=[("User", f"{message.author}\n`{message.author.id}`", False)], files=[source_file] if source_file.exists() else None)
        shutil.rmtree(job_dir, ignore_errors=True)


async def run_merge_job(channel: discord.TextChannel, user: discord.abc.User, state: dict) -> None:
    if state.get("merge_running"):
        await channel.send("กำลังรวมแอดออนอยู่แล้ว กรุณารอให้เสร็จก่อน")
        return
    source_files: list[str] = state.get("source_files", [])
    if len(source_files) < MIN_MERGE_ADDONS:
        await channel.send(f"ต้องมีไฟล์อย่างน้อย {MIN_MERGE_ADDONS} ไฟล์ก่อนเริ่มรวม")
        return
    if len(source_files) > MAX_MERGE_ADDONS:
        source_files = source_files[:MAX_MERGE_ADDONS]
        state["source_files"] = source_files

    state["merge_running"] = True
    await channel.send(f"ได้รับ {len(source_files)} ไฟล์แล้ว กำลังรวมแอดออน อาจใช้เวลาสักครู่...")
    try:
        async with convert_semaphore:
            merged = await asyncio.to_thread(merge_addons, source_files, str(state["work_dir"]))
        merged_path = Path(merged)
        await channel.send(f"{user.mention} รวมแอดออนเสร็จแล้ว มีเวลาดาวน์โหลด 1 นาทีก่อนช่องจะถูกลบ", file=discord.File(merged_path))
        guild = channel.guild if isinstance(channel, discord.TextChannel) else None
        await send_webhook_log(
            title="Addons Merged",
            description=f"รวม addon {len(source_files)} ไฟล์สำเร็จ",
            color=0x57F287,
            fields=[
                ("User", f"{user}\n`{user.id}`", False),
                ("Guild", f"{guild.name if guild else 'DM'}\n`{guild.id if guild else 'DM'}`", False),
                ("Mode", f"รวมแอดออน {len(source_files)} ไฟล์", True),
            ],
            files=[Path(p) for p in source_files if Path(p).exists()] + [merged_path],
        )
    except Exception as exc:
        state["merge_running"] = False
        await channel.send(f"รวมแอดออนไม่สำเร็จ: `{exc}`")
        await send_webhook_log(
            title="Addon Merge Failed",
            description=str(exc),
            color=0xED4245,
            fields=[("User", f"{user}\n`{user.id}`", False)],
            files=[Path(p) for p in source_files if Path(p).exists()],
        )
        return
    if isinstance(channel, discord.TextChannel):
        old_task = state.get("delete_task")
        if old_task and not old_task.done():
            old_task.cancel()
        state["delete_task"] = asyncio.create_task(schedule_delete(channel, 60))


class MergeNowView(discord.ui.View):
    def __init__(self, ticket_channel_id: int):
        super().__init__(timeout=600)
        self.ticket_channel_id = ticket_channel_id

    @discord.ui.button(label="เริ่มรวมตอนนี้", style=discord.ButtonStyle.success, emoji="📦")
    async def start_merge(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state.get("user_id"):
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่เริ่มรวมได้", ephemeral=True)
            return
        if state.get("merge_running"):
            await interaction.response.send_message("กำลังรวมแอดออนอยู่แล้ว", ephemeral=True)
            return
        source_files: list[str] = state.get("source_files", [])
        if len(source_files) < MIN_MERGE_ADDONS:
            await interaction.response.send_message(f"ต้องอัปโหลดอย่างน้อย {MIN_MERGE_ADDONS} ไฟล์ก่อน", ephemeral=True)
            return
        await interaction.response.defer(thinking=False)
        if isinstance(interaction.channel, discord.TextChannel):
            await run_merge_job(interaction.channel, interaction.user, state)


async def handle_merge_addons_message(message: discord.Message, state: dict, attachments: list[discord.Attachment]) -> None:
    if state.get("merge_running"):
        await message.channel.send("กำลังรวมแอดออนอยู่แล้ว กรุณารอให้เสร็จก่อน")
        return
    old_task = state.get("delete_task")
    if old_task and not old_task.done():
        old_task.cancel()
    if not state.get("work_dir"):
        state["work_dir"] = tempfile.mkdtemp(prefix=f"addon_merge_{message.author.id}_", dir=TEMP_ROOT)
    job_dir = Path(state["work_dir"])
    source_files: list[str] = state.setdefault("source_files", [])
    saved_count = 0
    for attachment in attachments:
        if len(source_files) >= MAX_MERGE_ADDONS:
            break
        target = job_dir / attachment.filename
        if target.exists():
            target = job_dir / f"{len(source_files)+1}_{attachment.filename}"
        await attachment.save(target)
        source_files.append(str(target))
        saved_count += 1

    if saved_count == 0 and len(source_files) >= MAX_MERGE_ADDONS:
        await message.channel.send(f"รับไฟล์ครบสูงสุด {MAX_MERGE_ADDONS} ไฟล์แล้ว กำลังเริ่มรวมให้อัตโนมัติ")
        if isinstance(message.channel, discord.TextChannel):
            await run_merge_job(message.channel, message.author, state)
        return

    if len(source_files) < MIN_MERGE_ADDONS:
        await message.channel.send(
            f"ได้รับ {len(source_files)}/{MAX_MERGE_ADDONS} ไฟล์แล้ว กรุณาอัปโหลดอย่างน้อยอีก {MIN_MERGE_ADDONS-len(source_files)} ไฟล์"
        )
        return

    if len(source_files) >= MAX_MERGE_ADDONS:
        await message.channel.send(f"ได้รับครบ {MAX_MERGE_ADDONS} ไฟล์แล้ว กำลังเริ่มรวมให้อัตโนมัติ")
        if isinstance(message.channel, discord.TextChannel):
            await run_merge_job(message.channel, message.author, state)
        return

    await message.channel.send(
        f"ได้รับ {len(source_files)}/{MAX_MERGE_ADDONS} ไฟล์แล้ว\n"
        f"อัปโหลดเพิ่มได้อีก {MAX_MERGE_ADDONS-len(source_files)} ไฟล์ หรือกดปุ่มด้านล่างเพื่อเริ่มรวมตอนนี้",
        view=MergeNowView(message.channel.id),
    )

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    state = TICKETS.get(message.channel.id)
    if not state or message.author.id != state["user_id"]:
        return
    attachments = _valid_addon_attachments(message)
    if not attachments:
        if message.attachments:
            await message.channel.send("กรุณาอัปโหลดไฟล์ `.mcaddon` หรือ `.zip` เท่านั้น")
        return
    if state.get("mode") == "merge_addons":
        await handle_merge_addons_message(message, state, attachments)
    else:
        await handle_combine_ui_message(message, state, attachments)


async def start_health_server() -> None:
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
