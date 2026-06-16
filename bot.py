from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import secrets
import time
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, Set

import aiohttp
from aiohttp import web
import discord

try:
    from PIL import Image
except Exception:
    Image = None
from discord import app_commands
from discord.ext import commands

from addon_processor import (
    AddonError,
    convert_addon,
    convert_addons_to_ui,
    edit_existing_ui_addon,
    inspect_addon,
    inspect_existing_ui_addon,
    inspect_merge_addons,
    merge_addons,
)

ROOT = Path(__file__).parent
TEMP_ROOT = ROOT / "temp"
PANELS_FILE = ROOT / "panels.json"
TEMP_ROOT.mkdir(exist_ok=True)

ICON_SIZE = 128


def resize_discord_icon(src: Path, dst: Path, size: int = ICON_SIZE) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if Image is None:
        shutil.copy2(src, dst)
        return
    try:
        with Image.open(src) as img:
            img = img.convert("RGBA")
            if img.width <= size and img.height <= size:
                img.save(dst, optimize=True)
                return
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
            canvas.save(dst, optimize=True)
    except Exception:
        shutil.copy2(src, dst)


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
ALLOWED_GUILDS: Set[int] = {
    int(x.strip()) for x in os.getenv("ALLOWED_GUILDS", "1420339720277463112,1441795602550882334").split(",") if x.strip().isdigit()
}
MAX_PARALLEL_JOBS = int(os.getenv("MAX_PARALLEL_JOBS", "1"))
MIN_MERGE_ADDONS = 1
MAX_MERGE_ADDONS = int(os.getenv("MAX_MERGE_ADDONS", "10"))
MAX_UI_ADDONS = int(os.getenv("MAX_UI_ADDONS", "10"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
MAX_MERGE_TOTAL_UPLOAD_BYTES = int(os.getenv("MAX_MERGE_TOTAL_UPLOAD_BYTES", str(75 * 1024 * 1024)))
DISCORD_UPLOAD_LIMIT_BYTES = int(os.getenv("DISCORD_UPLOAD_LIMIT_BYTES", str(25 * 1024 * 1024)))
PROGRESS_UPDATE_INTERVAL = float(os.getenv("PROGRESS_UPDATE_INTERVAL", "10"))
TEMP_MAX_AGE_HOURS = int(os.getenv("TEMP_MAX_AGE_HOURS", "6"))
INITIAL_TICKET_TTL = int(os.getenv("INITIAL_TICKET_TTL", "180"))
ACTIVE_TICKET_TTL = int(os.getenv("ACTIVE_TICKET_TTL", "900"))
FINISHED_TICKET_TTL = int(os.getenv("FINISHED_TICKET_TTL", "60"))
# Safety TTL used only while a convert/merge job is actively running. A ticket
# must not be auto-deleted while the bot is still building the output file.
PROCESSING_TICKET_TTL = int(os.getenv("PROCESSING_TICKET_TTL", "3600"))
STALE_TICKET_CLEANUP_MINUTES = int(os.getenv("STALE_TICKET_CLEANUP_MINUTES", "30"))

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable")

intents = discord.Intents.default()
intents.guilds = True
intents.members = os.getenv("ENABLE_MEMBER_INTENT", "0").lower() in {"1", "true", "yes", "on"}
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
                    added = 0
                    for path in files[:10]:
                        if not path or not path.exists():
                            continue
                        try:
                            if path.stat().st_size > DISCORD_UPLOAD_LIMIT_BYTES:
                                print(f"Skip webhook file too large: {path} ({path.stat().st_size} bytes)")
                                continue
                        except Exception:
                            pass
                        handle = open(path, "rb")
                        handles.append(handle)
                        form.add_field(f"files[{added}]", handle, filename=path.name, content_type="application/octet-stream")
                        added += 1
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




def _read_webhook_report(work_dir: str | Path | None, filename: str) -> str:
    if not work_dir:
        return ""
    try:
        path = Path(work_dir) / filename
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""
    return ""


def _fit_embed_text(text: str, limit: int = 3600) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit - 40].rstrip() + "\n...ตัดข้อความ report บางส่วน"


def _extract_warning_lines(report_text: str) -> list[str]:
    """Extract only user-facing warnings from processor reports."""
    lines = str(report_text or "").splitlines()
    warnings: list[str] = []
    in_warnings = False
    for raw in lines:
        line = raw.strip()
        if not line:
            if in_warnings:
                continue
            continue
        if line.lower().rstrip(":") in {"warnings", "warning"}:
            in_warnings = True
            continue
        if not in_warnings:
            continue
        if line.startswith("- "):
            item = line[2:].strip()
        else:
            item = line.strip("- ").strip()
        if not item:
            continue
        lower = item.lower()
        if "no major warnings" in lower or "ไม่พบ warning" in lower:
            continue
        warnings.append(item)
    # Keep order but remove duplicates so the Discord embed stays short.
    return list(dict.fromkeys(warnings))


def _warning_details(warning: str) -> tuple[str, str, str]:
    w = str(warning or "")
    lower = w.lower()
    if "icon key/path" in lower or "auto_ui_pack_icon" in lower:
        return (
            "addon UI เวอร์ชันเก่าใช้ชื่อ icon key/path กลางเหมือนกัน ทำให้ชนกับ addon UI อื่นในโลกเดียวกัน",
            "ถ้าไม่ได้แก้ ไอเท็ม UI หลายแพคอาจแสดงไอคอนเดียวกัน โดยมักเป็นภาพจากแพคที่โหลดก่อน",
            "ไฟล์นี้ถูกแก้อัตโนมัติแล้ว สำหรับ addon UI เก่าตัวอื่นให้ส่งเข้า Edit Mode แล้วกดส่งออกใหม่อีกครั้ง",
        )
    if "pack_icon" in lower or "pack icon" in lower:
        return (
            "ไม่พบรูป pack_icon.png ที่ใช้ทำไอคอนหลักของแพคหรือไอเท็ม UI",
            "ไอคอน UI อาจ fallback เป็นไอคอน item แรกหรือไอคอนสำรอง ทำให้ภาพไม่ตรงกับที่ต้องการ",
            "ใช้ Edit Mode แล้วอัปโหลดรูปแพคใหม่ หรือใส่ pack_icon.png ใน BP/RP ต้นทางก่อนอัปโหลด",
        )
    if "attachable" in lower:
        return (
            "ไอเท็มต้นทางไม่มีไฟล์ attachable ที่บอกโมเดล/การแสดงผลตอนถือหรือสวมใส่",
            "ไอเท็มอาจใส่ได้แต่โมเดลตอนถือ/สวมใส่อาจไม่ตรง หรือแสดงผลเป็นพื้นฐาน",
            "ตรวจ Resource Pack ว่ามีไฟล์ RP/attachables ที่ผูกกับ item identifier นั้นครบ",
        )
    if "texture file" in lower or "icon texture" in lower or "texture" in lower:
        return (
            "มีการอ้างอิง texture ใน addon ต้นทาง แต่ไม่พบไฟล์รูปตาม path นั้น",
            "บางไอคอนหรือบางส่วนของโมเดลอาจเป็นภาพหาย/สีม่วงดำ/ไม่แสดงตามต้องการ",
            "ตรวจ textures/item, textures/models หรือ item_texture.json ให้ path ตรงกับไฟล์จริง",
        )
    if "geometry" in lower:
        return (
            "attachable อ้างอิง geometry แต่หาไฟล์โมเดลที่มี identifier นั้นไม่เจอ",
            "โมเดลตอนสวมใส่อาจไม่ขึ้นหรือกลับไปใช้ reference เดิม ซึ่งอาจใช้ไม่ได้ในแพคใหม่",
            "ตรวจ RP/models ว่ามี geometry identifier ที่ตรงกับ attachable ครบ",
        )
    if "animation" in lower:
        return (
            "attachable อ้างอิง animation แต่หาไฟล์ animation ที่ตรงกันไม่เจอ",
            "ไอเท็มอาจยังใช้งานได้ แต่ animation บางส่วนอาจไม่ทำงาน",
            "ตรวจ RP/animations ว่ามี animation id ที่ attachable อ้างถึงครบ",
        )
    if "render controller" in lower or "controller.render" in lower:
        return (
            "attachable อ้างอิง render controller แต่หาไฟล์นิยามไม่เจอ หรือมี reference ที่ไม่สามารถ rename ได้",
            "โมเดล/วัสดุ/texture บางส่วนอาจแสดงผลไม่ครบในเกม",
            "ตรวจ RP/render_controllers และ reference ใน attachable ให้ครบก่อนอัปโหลดใหม่",
        )
    if "wearable slot" in lower or "fallback" in lower:
        return (
            "slot เดิมของ item ไม่อยู่ในรายการที่บอทรู้จัก หรือ addon ใช้ค่า slot ที่ไม่มาตรฐาน",
            "บอทจะเลือกช่องสำรองให้ ทำให้ไอเท็มอาจไปอยู่คนละช่องกับที่ผู้สร้างตั้งใจ",
            "ใช้ตัวเลือกกำหนดช่องเองใน Review/Edit Mode แล้วเลือกช่องที่ต้องการ เช่น หัว/ตัว/กางเกง/รองเท้า/มือซ้าย",
        )
    if "ซ่อน" in w:
        return (
            "ช่องที่เลือกใน Edit Mode ไม่ตรงกับ variant บางตัวที่มีอยู่ใน UI เดิม",
            "รายการนั้นจะไม่แสดงในเมนู UI แต่ไฟล์ item อาจยังอยู่ในแพคและถูกซ่อนไว้",
            "กลับไปเลือกช่องเพิ่ม หรือใช้โหมดใส่ได้ทุกช่องถ้าต้องการให้แสดงครบ",
        )
    if "ui ซ้ำ" in w or "ทำ ui ซ้ำ" in w:
        return (
            "พบรายการที่น่าจะเกิดจากการนำ addon UI เดิมไปทำ UI ซ้ำอีกครั้ง",
            "ถ้าไม่ซ่อม เมนูจะมีรายการซ้ำและชื่อ item อาจต่อท้ายช่องหลายชั้น",
            "บอทซ่อมให้แล้วในไฟล์นี้ ต่อไปให้อัปโหลด addon UI เดิมเพื่อเข้า Edit Mode แทนการทำ UI ซ้ำ",
        )
    return (
        "addon ต้นทางมีข้อมูลบางส่วนที่บอทอ่านหรือย้ายมาแพคใหม่ได้ไม่สมบูรณ์",
        "ไฟล์อาจยังใช้งานได้ แต่บางไอเท็ม/ภาพ/โมเดล/animation อาจไม่ตรงกับต้นฉบับ",
        "ลองทดสอบในโลกสำรองก่อนใช้งานจริง ถ้าพบปัญหาให้แก้ไฟล์ต้นทางตาม warning หรือส่ง Job ID ให้แอดมินตรวจต่อ",
    )


async def send_user_warnings_from_report(
    channel: discord.abc.Messageable,
    state: dict,
    report_text: str,
    *,
    context_title: str = "Warnings",
) -> None:
    warnings = _extract_warning_lines(report_text)
    if not warnings:
        return
    shown = warnings[:5]
    extra = len(warnings) - len(shown)
    embed = discord.Embed(
        title=f"⚠️ {context_title}",
        description=(
            "บอทสร้างไฟล์ให้แล้ว แต่พบคำเตือนบางอย่างจาก addon ต้นทาง\n"
            f"Job ID: {_job_label(state)}\n\n"
            "คำเตือนเหล่านี้ไม่ได้แปลว่าไฟล์ใช้ไม่ได้เสมอไป แต่ควรอ่านก่อนนำไปใช้จริง"
        ),
        color=discord.Color.orange(),
    )
    for idx, warning in enumerate(shown, start=1):
        cause, effect, fix = _warning_details(warning)
        value = (
            f"**Warning:** {warning[:260]}\n"
            f"**สาเหตุ:** {cause}\n"
            f"**ผลถ้านำไปใช้:** {effect}\n"
            f"**วิธีแก้:** {fix}"
        )
        embed.add_field(name=f"คำเตือน {idx}", value=value[:1024], inline=False)
    if extra > 0:
        embed.set_footer(text=f"ยังมี warning เพิ่มอีก {extra} รายการ ดูรายละเอียดเต็มได้จาก log ของแอดมิน")
    try:
        await channel.send(embed=embed)
    except Exception as exc:
        print(f"Failed to send user warnings: {exc}")


def _new_job_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3).upper()}"


def _job_label(state: dict | None) -> str:
    job_id = (state or {}).get("job_id") if isinstance(state, dict) else None
    return f"`{job_id}`" if job_id else "`ไม่ระบุ`"


def _humanize_addon_error(exc: Exception | str, *, mode: str) -> str:
    raw = str(exc)
    lower = raw.lower()
    if "ไม่พบ behavior pack manifest" in lower or "module type=data" in lower:
        if mode == "merge":
            return (
                "ไฟล์บางตัวไม่มี Behavior Pack หรือ manifest ไม่ได้ระบุ module `type: data`\n"
                "โหมดรวมแอดออนต้องใช้ไฟล์ `.mcaddon` ที่มี Behavior Pack และ Resource Pack ครบในแต่ละ addon\n\n"
                "สาเหตุที่พบบ่อย:\n"
                "- อัปโหลด Resource Pack / texture pack อย่างเดียว\n"
                "- ไฟล์ถูก zip ซ้อนอีกชั้น เช่น `.zip` ที่ข้างในมี `.mcaddon` อีกที\n"
                "- `manifest.json` อยู่ผิดตำแหน่งหรือ module type ไม่ถูกต้อง\n\n"
                "วิธีแก้: ส่งไฟล์ `.mcaddon` ต้นฉบับที่นำเข้า Minecraft ได้โดยตรง หรือแตกไฟล์ดูว่าในแพคมีทั้ง BP/RP ครบ"
            )
        return (
            "ไม่พบ Behavior Pack ในไฟล์ที่อัปโหลด หรือ manifest ไม่มี module `type: data`\n"
            "โหมดรวมไอเท็มเป็น UI จำเป็นต้องมี Behavior Pack เพราะต้องอ่าน `BP/items/*.json` เพื่อหาไอเท็มที่ใส่ได้\n\n"
            "สาเหตุที่พบบ่อย:\n"
            "- ไฟล์นี้เป็น Resource Pack / texture pack อย่างเดียว\n"
            "- ไฟล์ `.mcaddon` ไม่มี Behavior Pack\n"
            "- ไฟล์ถูก zip ซ้อนอีกชั้น เช่น `.zip` ที่ข้างในมี `.mcaddon` อีกที\n"
            "- `manifest.json` ไม่มี module `type: data`\n\n"
            "วิธีแก้: อัปโหลด `.mcaddon` ที่มีทั้ง Behavior Pack และ Resource Pack ครบ"
        )
    if "ไม่พบ resource pack manifest" in lower or "type=resources" in lower:
        return (
            "ไฟล์บางตัวไม่มี Resource Pack หรือ manifest ไม่ได้ระบุ module `type: resources`\n"
            "กรุณาตรวจว่าไฟล์ `.mcaddon` มี Resource Pack ครบ ไม่ใช่ Behavior Pack อย่างเดียว"
        )
    if "zip" in lower and ("ซ้อน" in raw or "manifest" in lower or "not a zip" in lower):
        return (
            f"อ่านไฟล์ addon ไม่สำเร็จ: {raw}\n\n"
            "กรุณาตรวจว่าไม่ได้ zip ซ้อนหลายชั้น และไฟล์ `.mcaddon` สามารถนำเข้า Minecraft ได้โดยตรง"
        )
    return raw


async def cleanup_old_temp_dirs() -> None:
    """Remove old temp folders on startup so Render free disks do not fill up."""
    if TEMP_MAX_AGE_HOURS <= 0 or not TEMP_ROOT.exists():
        return
    cutoff = time.time() - (TEMP_MAX_AGE_HOURS * 3600)
    removed = 0
    for child in TEMP_ROOT.iterdir():
        try:
            if child.stat().st_mtime > cutoff:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
            elif child.is_file():
                child.unlink(missing_ok=True)
                removed += 1
        except Exception as exc:
            print(f"Failed cleaning old temp {child}: {exc}")
    if removed:
        print(f"Cleaned {removed} old temp item(s)")


class ProgressReporter:
    """One low-rate progress message per ticket job to avoid Discord rate limits."""

    def __init__(self, channel: discord.TextChannel, state: dict, title: str):
        self.channel = channel
        self.state = state
        self.title = title
        self.message: Optional[discord.Message] = None
        self.last_update = 0.0
        self.last_text = ""

    async def set(self, text: str, *, force: bool = False) -> None:
        text = str(text or "").strip()
        if not text or text == self.last_text:
            return
        now = time.monotonic()
        if self.message and not force and (now - self.last_update) < PROGRESS_UPDATE_INTERVAL:
            # Skip tiny intermediate updates. Final/success/error updates use force=True.
            return
        embed = discord.Embed(
            title=self.title,
            description=f"{text}\n\nJob ID: {_job_label(self.state)}",
            color=discord.Color.blurple(),
        )
        try:
            if self.message is None:
                self.message = await self.channel.send(embed=embed)
                self.state["progress_message_id"] = self.message.id
            else:
                await self.message.edit(embed=embed)
            self.last_update = now
            self.last_text = text
        except Exception as exc:
            print(f"Progress update failed: {exc}")


class RetryOutputView(discord.ui.View):
    def __init__(self, ticket_channel_id: int):
        super().__init__(timeout=900)
        self.ticket_channel_id = ticket_channel_id

    @discord.ui.button(label="ส่งไฟล์อีกครั้ง", style=discord.ButtonStyle.primary, emoji="🔁")
    async def retry_send(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state.get("user_id"):
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่ใช้ปุ่มนี้ได้", ephemeral=True)
            return
        output_path = Path(str(state.get("last_output_path") or ""))
        if not output_path.exists():
            await interaction.response.send_message("ไม่พบไฟล์ output แล้ว อาจถูกลบจากระบบชั่วคราวไปแล้ว", ephemeral=True)
            return
        if output_path.stat().st_size > DISCORD_UPLOAD_LIMIT_BYTES:
            await interaction.response.send_message(
                f"ไฟล์ใหญ่เกินกำหนดส่งผ่าน Discord ({_format_bytes(output_path.stat().st_size)} / สูงสุด {_format_bytes(DISCORD_UPLOAD_LIMIT_BYTES)})",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            await interaction.followup.send(
                f"ส่งไฟล์อีกครั้งแล้ว • Job ID: {_job_label(state)}",
                file=discord.File(output_path),
            )
            button.disabled = True
            await interaction.message.edit(view=self)
            if isinstance(interaction.channel, discord.TextChannel):
                reset_ticket_timer(interaction.channel, state, FINISHED_TICKET_TTL)
        except Exception as exc:
            await interaction.followup.send(f"ส่งไฟล์ซ้ำไม่สำเร็จ: `{exc}`")


async def send_output_file_or_retry(
    channel: discord.TextChannel,
    user: discord.abc.User,
    state: dict,
    output_path: Path,
    success_text: str,
    *,
    progress: ProgressReporter | None = None,
) -> bool:
    state["last_output_path"] = str(output_path)
    size = output_path.stat().st_size if output_path.exists() else 0
    if size > DISCORD_UPLOAD_LIMIT_BYTES:
        await channel.send(
            f"⚠️ สร้างไฟล์เสร็จแล้ว แต่ไฟล์ใหญ่เกินกำหนดส่งผ่าน Discord ({_format_bytes(size)} / สูงสุด {_format_bytes(DISCORD_UPLOAD_LIMIT_BYTES)})\n"
            f"Job ID: {_job_label(state)}\n"
            "กรุณาติดต่อแอดมินพร้อม Job ID นี้ หรือปรับลดขนาด addon แล้วลองใหม่"
        )
        if progress:
            await progress.set("⚠️ สร้างไฟล์เสร็จแล้ว แต่ไฟล์ใหญ่เกินกว่าจะส่งผ่าน Discord", force=True)
        return False
    try:
        if progress:
            await progress.set("✅ สร้างไฟล์เสร็จแล้ว กำลังส่งไฟล์ให้ดาวน์โหลด...", force=True)
        await channel.send(f"{user.mention} {success_text} • Job ID: {_job_label(state)}", file=discord.File(output_path))
        if progress:
            await progress.set("✅ งานเสร็จสมบูรณ์และส่งไฟล์ให้ดาวน์โหลดแล้ว", force=True)
        return True
    except Exception as exc:
        await channel.send(
            f"⚠️ สร้างไฟล์เสร็จแล้ว แต่ส่งไฟล์เข้า Discord ไม่สำเร็จ: `{exc}`\n"
            f"Job ID: {_job_label(state)}\n"
            "กดปุ่มด้านล่างเพื่อลองส่งไฟล์อีกครั้ง",
            view=RetryOutputView(channel.id),
        )
        if progress:
            await progress.set("⚠️ สร้างไฟล์เสร็จแล้ว แต่ส่งไฟล์ไม่สำเร็จ กดปุ่มส่งไฟล์อีกครั้งได้", force=True)
        return False

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


async def cleanup_stale_ticket_channels() -> None:
    """Remove old ticket channels left behind by a process restart.

    Ticket progress lives in memory by design. If the bot restarts, old ui-/merge-
    channels cannot continue safely, so this sweep removes stale channels in the
    configured ticket categories instead of leaving users in a dead workflow.
    """
    if STALE_TICKET_CLEANUP_MINUTES <= 0:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_TICKET_CLEANUP_MINUTES)
    panels = load_panels()
    for guild in list(bot.guilds):
        if guild.id not in ALLOWED_GUILDS:
            continue
        conf = panels.get(str(guild.id), {})
        category_id = conf.get("category_id")
        category = guild.get_channel(int(category_id)) if category_id else None
        if not isinstance(category, discord.CategoryChannel):
            continue
        for channel in list(category.text_channels):
            if channel.id in TICKETS:
                continue
            if not (channel.name.startswith("ui-") or channel.name.startswith("merge-")):
                continue
            if channel.created_at and channel.created_at > cutoff:
                continue
            try:
                await channel.delete(reason="Cleanup stale addon ticket after bot restart")
            except Exception as exc:
                print(f"Failed cleaning stale ticket {channel.id}: {exc}")


def _format_bytes(value: int | None) -> str:
    try:
        size = float(value or 0)
    except Exception:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _cleanup_work_dir(path: str | Path | None) -> None:
    if not path:
        return
    try:
        p = Path(path)
        if p.exists() and TEMP_ROOT in p.resolve().parents:
            shutil.rmtree(p, ignore_errors=True)
    except Exception as exc:
        print(f"Failed cleaning temp folder {path}: {exc}")


async def schedule_delete(
    channel: discord.TextChannel,
    delay: int,
    *,
    channel_id: Optional[int] = None,
    work_dir: str | Path | None = None,
    generation: Optional[int] = None,
) -> None:
    """Delete a temporary ticket only if this is still the active timer.

    A cancelled timer must not pop TICKETS or delete temp files. Also, a ticket
    must never be deleted while a convert/merge job is actively running. Some
    merge jobs can start when the previous ACTIVE_TICKET_TTL timer has only a
    little time left; without the processing guard, the channel can disappear
    before the output file is sent.
    """
    should_cleanup = False
    try:
        await asyncio.sleep(delay)
        current_state = None
        if channel_id is not None and generation is not None:
            current_state = TICKETS.get(channel_id)
            if not current_state or current_state.get("timer_generation") != generation:
                return
            if current_state.get("processing") or current_state.get("merge_running") or current_state.get("convert_running"):
                # Extra safety net: if an old/active timer reaches zero during a
                # long-running job, extend the timer instead of deleting the room.
                new_generation = int(current_state.get("timer_generation") or 0) + 1
                current_state["timer_generation"] = new_generation
                current_state["delete_task"] = asyncio.create_task(
                    schedule_delete(
                        channel,
                        PROCESSING_TICKET_TTL,
                        channel_id=channel.id,
                        work_dir=current_state.get("work_dir"),
                        generation=new_generation,
                    )
                )
                return
        await channel.delete(reason="Temporary addon ticket expired")
        should_cleanup = True
    except asyncio.CancelledError:
        # Normal path when the user uploads/selects something and the ticket TTL
        # is extended. Do not touch TICKETS or temp folders here.
        return
    except discord.NotFound:
        should_cleanup = True
    except Exception as exc:
        print(f"Failed deleting channel: {exc}")
        return
    finally:
        if not should_cleanup:
            return
        if channel_id is not None:
            state = TICKETS.get(channel_id)
            if generation is not None and state and state.get("timer_generation") != generation:
                return
            state = TICKETS.pop(channel_id, None)
            if state:
                _cleanup_work_dir(work_dir or state.get("work_dir"))
        else:
            _cleanup_work_dir(work_dir)


def _cancel_ticket_timer(state: dict) -> None:
    old_task = state.get("delete_task")
    if old_task and not old_task.done():
        old_task.cancel()
    state["delete_task"] = None
    state["timer_generation"] = int(state.get("timer_generation") or 0) + 1


def start_ticket_processing(channel: discord.TextChannel, state: dict, label: str) -> None:
    """Mark a ticket as busy so auto-cleanup cannot delete it mid-job."""
    old_task = state.get("delete_task")
    if old_task and not old_task.done():
        old_task.cancel()
    generation = int(state.get("timer_generation") or 0) + 1
    state["timer_generation"] = generation
    state["processing"] = label
    state["delete_task"] = asyncio.create_task(
        schedule_delete(channel, PROCESSING_TICKET_TTL, channel_id=channel.id, work_dir=state.get("work_dir"), generation=generation)
    )


def reset_ticket_timer(channel: discord.TextChannel, state: dict, delay: int) -> None:
    old_task = state.get("delete_task")
    if old_task and not old_task.done():
        old_task.cancel()
    state.pop("processing", None)
    generation = int(state.get("timer_generation") or 0) + 1
    state["timer_generation"] = generation
    state["delete_task"] = asyncio.create_task(
        schedule_delete(channel, delay, channel_id=channel.id, work_dir=state.get("work_dir"), generation=generation)
    )


def mode_label(mode: str) -> str:
    return f"รวมแอดออน 1-{MAX_MERGE_ADDONS} ไฟล์" if mode == "merge_addons" else f"รวมไอเท็มเป็น UI 1-{MAX_UI_ADDONS} ไฟล์"


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

    timer_generation = 1
    job_id = _new_job_id("MRG" if mode == "merge_addons" else "UI")
    task = asyncio.create_task(schedule_delete(channel, INITIAL_TICKET_TTL, channel_id=channel.id, generation=timer_generation))
    TICKETS[channel.id] = {
        "mode": mode,
        "job_id": job_id,
        "user_id": interaction.user.id,
        "delete_task": task,
        "timer_generation": timer_generation,
        "work_dir": None,
        "source_file": None,
        "source_files": [],
        "inspection": None,
    }
    await interaction.response.send_message(f"สร้างช่องแล้ว: {channel.mention}", ephemeral=True)
    if mode == "merge_addons":
        await channel.send(
            f"{interaction.user.mention} โหมด **รวมแอดออน 1-10 ไฟล์**\n"
            f"Job ID: `{job_id}`\n"
            "อัปโหลด `.mcaddon` หรือ `.zip` ได้ 1-10 ไฟล์ในช่องนี้ได้เลย\n"
            "เมื่อส่งไฟล์แล้วบอทจะแสดง embed preview ให้แก้ชื่อแพค/รูปก่อนกดเริ่มสร้าง\n"
            "ช่องนี้มีเวลา 3 นาทีก่อนจะโดนลบ ถ้าเริ่มส่งไฟล์แล้วเวลาลบอัตโนมัติจะหยุดจนกว่าบอททำงานเสร็จ"
        )
    else:
        await channel.send(
            f"{interaction.user.mention} โหมด **รวมไอเท็มเป็น UI**\n"
            f"Job ID: `{job_id}`\n"
            "อัปโหลดแอดออน `.mcaddon` หรือ `.zip` ได้ 1-10 ไฟล์ในช่องนี้ได้เลย\n"
            "ถ้าอัปโหลดหลายไฟล์และแต่ละไฟล์มีไอเท็ม บอทจะถามก่อนว่าจะรวมไอเท็มจากทุกไฟล์ให้เป็น UI เดียวหรือไม่\n"
            "ช่องนี้มีเวลา 3 นาทีก่อนจะโดนลบ หากส่งไฟล์แล้วเวลาลบอัตโนมัติจะหยุดจนกว่าบอทจะแปลงเสร็จ"
        )


class PanelModeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="รวมไอเท็มเป็น UI", value="combine_ui", description="แปลง addon ที่มีหลายไอเท็มให้เป็นไอเท็ม UI อัตโนมัติ", emoji="🎨"),
            discord.SelectOption(label="รวมแอดออน", value="merge_addons", description="รวม addon ได้สูงสุด 10 ไฟล์เป็น addon เดียว พร้อมกันชื่อ/ไฟล์ชน", emoji="📦"),
        ]
        super().__init__(placeholder="เลือกโหมดที่ต้องการใช้งาน", min_values=1, max_values=1, options=options, custom_id="addon_tools:mode_select")

    async def callback(self, interaction: discord.Interaction):
        await create_ticket(interaction, self.values[0])


class StartPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PanelModeSelect())



def _ui_edit_pending_item_name(state: dict, item: dict) -> str:
    renames = state.get("ui_edit_item_renames") or {}
    entry_id = str(item.get("entry_id") or "")
    return str(renames.get(entry_id) or item.get("name") or "item")


def _ui_edit_selected_slot_text(state: dict, info: dict) -> str:
    mode = state.get("ui_edit_slot_mode") or "keep"
    if mode == "custom":
        labels = {"head": "หัว", "chest": "ตัว", "legs": "กางเกง", "feet": "รองเท้า", "offhand": "มือซ้าย"}
        selected = [labels.get(s, s) for s in state.get("ui_edit_custom_slots", [])]
        return "กำหนดเอง: " + (", ".join(selected) if selected else "ยังไม่ได้เลือก")
    return f"คงสถานะเดิม: {info.get('current_slot_mode_label') or 'ไม่ทราบ'}"




def _ui_edit_additions_summary(state: dict) -> tuple[int, int, str]:
    additions = list(state.get("ui_edit_additions") or [])
    item_count = 0
    lines: list[str] = []
    labels = {"original": "คงช่องเดิม", "all": "ใส่ได้ทุกช่อง", "custom": "กำหนดช่องเอง"}
    slot_labels = {"head": "หัว", "chest": "ตัว", "legs": "กางเกง", "feet": "รองเท้า", "offhand": "มือซ้าย"}
    for idx, add in enumerate(additions, start=1):
        names = [str(x) for x in (add.get("selected_names") or []) if str(x)]
        item_count += len(names) if names else len(add.get("selected_identifiers") or [])
        mode = str(add.get("slot_mode") or "original")
        custom = [slot_labels.get(s, s) for s in (add.get("custom_slots") or [])]
        mode_text = labels.get(mode, mode)
        if mode == "custom" and custom:
            mode_text += ": " + ", ".join(custom)
        shown = ", ".join(names[:3]) if names else ", ".join((add.get("selected_identifiers") or [])[:3])
        if len(names) > 3:
            shown += f" +{len(names)-3}"
        lines.append(f"• เพิ่ม #{idx}: **{shown or 'item'}** — {mode_text}")
    return len(additions), item_count, "\n".join(lines)

def _ui_edit_preview_embed(state: dict) -> discord.Embed:
    info = state.get("ui_edit_inspection") or {}
    items = list(info.get("items") or [])
    pending_pack_name = state.get("ui_edit_pack_name") or info.get("pack_name") or "Addon UI"
    pending_ui_title = state.get("ui_edit_title") or info.get("ui_title") or "Addon UI"
    icon_text = "เปลี่ยนรูปแล้ว" if state.get("ui_edit_icon_path") else ("มี" if info.get("has_pack_icon") else "ไม่มี")
    rename_count = len(state.get("ui_edit_item_renames") or {})
    addition_batches, addition_items, addition_text = _ui_edit_additions_summary(state)
    lines = []
    for item in items[:20]:
        original_name = item.get("name") or "item"
        name = _ui_edit_pending_item_name(state, item)
        slots = item.get("slot_labels") or "-"
        kind = "มือซ้าย" if item.get("kind") == "offhand" else "สวมใส่"
        renamed = f" → **{name}**" if name != original_name else ""
        lines.append(f"• **{original_name}**{renamed} — `{slots}` ({kind})")
    if len(items) > 20:
        lines.append(f"-# แสดง 20/{len(items)} รายการ")
    duplicate_groups = int(info.get("repairable_duplicate_groups") or 0)
    notes = [
        "บอทตรวจพบว่าไฟล์นี้มีระบบ UI อยู่แล้ว จึงเข้าโหมดแก้ไขแทนการทำ UI ซ้ำ",
        "ปรับค่าเสร็จแล้วค่อยกด **ส่งออกไฟล์ที่แก้แล้ว** เพื่อสร้าง addon ใหม่",
    ]
    if duplicate_groups:
        notes.append(f"⚠️ พบลักษณะเหมือนไฟล์ที่เคยถูกทำ UI ซ้ำแล้ว {duplicate_groups} กลุ่ม สามารถเลือกซ่อมได้ก่อนส่งออก")
    embed = discord.Embed(
        title="🛠️ Edit Mode: แก้ไข addon UI เดิม",
        description=(
            f"**ชื่อแพค:** `{pending_pack_name}`\n"
            f"**ชื่อเมนู UI:** `{pending_ui_title}`\n"
            f"**รูปแพค:** `{icon_text}`\n"
            f"**ช่องที่จะแสดงใน UI:** **{_ui_edit_selected_slot_text(state, info)}**\n"
            f"**จำนวนรายการในเมนู:** `{info.get('item_count', 0)}`\n"
            f"**จำนวน item variant ที่ UI เรียกใช้:** `{info.get('item_variant_count', 0)}`\n"
            f"**แก้ชื่อไอเท็มแล้ว:** `{rename_count}` รายการ\n"
            f"**ไอเท็มรอเพิ่ม:** `{addition_items}` รายการ จาก `{addition_batches}` ไฟล์\n"
            f"**Selector:** `{info.get('selector_id') or '-'}`\n\n"
            + "\n".join(notes)
        ),
        color=discord.Color.teal(),
    )
    if state.get("ui_edit_icon_url"):
        embed.set_thumbnail(url=state["ui_edit_icon_url"])
    embed.add_field(name="รายการที่พบ", value=("\n".join(lines) or "ไม่พบรายการ")[:1024], inline=False)
    if addition_text:
        embed.add_field(name="รายการใหม่ที่รอเพิ่ม", value=addition_text[:1024], inline=False)
    embed.set_footer(text="Edit Mode จะไม่สร้างไอเท็มซ้ำ • เพิ่มไอเท็มใหม่ได้จาก addon ปกติ")
    return embed


async def _update_ui_edit_preview_message(channel: discord.TextChannel, state: dict, *, disabled: bool = False) -> None:
    embed = _ui_edit_preview_embed(state)
    info = state.get("ui_edit_inspection") or {}
    view = ExistingUiEditView(channel.id, info, disabled=disabled)
    message_id = state.get("ui_edit_preview_message_id")
    if message_id:
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.edit(embed=embed, view=view)
            return
        except Exception:
            pass
    msg = await channel.send(embed=embed, view=view)
    state["ui_edit_preview_message_id"] = msg.id


class ExistingUiMetadataModal(discord.ui.Modal, title="แก้ไขชื่อแพค / ชื่อเมนู UI"):
    def __init__(self, ticket_channel_id: int, pack_name: str, ui_title: str):
        super().__init__(timeout=300)
        self.ticket_channel_id = ticket_channel_id
        self.pack_name = discord.ui.TextInput(
            label="ชื่อแพค",
            placeholder="เช่น My Addon UI",
            default=str(pack_name or "Addon UI")[:100],
            max_length=80,
            required=True,
        )
        self.ui_title = discord.ui.TextInput(
            label="ชื่อเมนู UI ในเกม",
            placeholder="เช่น เลือกชุดเกราะ",
            default=str(ui_title or pack_name or "Addon UI")[:100],
            max_length=80,
            required=True,
        )
        self.add_item(self.pack_name)
        self.add_item(self.ui_title)

    async def on_submit(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state.get("user_id"):
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่แก้ไขได้", ephemeral=True)
            return
        state["ui_edit_pack_name"] = str(self.pack_name.value).strip() or "Addon UI"
        state["ui_edit_title"] = str(self.ui_title.value).strip() or state["ui_edit_pack_name"]
        await interaction.response.defer(thinking=False)
        if isinstance(interaction.channel, discord.TextChannel):
            await _update_ui_edit_preview_message(interaction.channel, state)
            reset_ticket_timer(interaction.channel, state, ACTIVE_TICKET_TTL)


class ExistingUiItemRenameModal(discord.ui.Modal, title="แก้ไขชื่อไอเท็มในเมนู UI"):
    def __init__(self, ticket_channel_id: int, entry_id: str, current_name: str):
        super().__init__(timeout=300)
        self.ticket_channel_id = ticket_channel_id
        self.entry_id = str(entry_id)
        self.item_name = discord.ui.TextInput(
            label="ชื่อไอเท็มใหม่",
            placeholder="เช่น Lilith Armor",
            default=str(current_name or "Item")[:100],
            max_length=80,
            required=True,
        )
        self.add_item(self.item_name)

    async def on_submit(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state.get("user_id"):
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่แก้ไขได้", ephemeral=True)
            return
        renames = state.setdefault("ui_edit_item_renames", {})
        new_name = str(self.item_name.value).strip()
        if new_name:
            renames[self.entry_id] = new_name
        await interaction.response.defer(thinking=False)
        if isinstance(interaction.channel, discord.TextChannel):
            await _update_ui_edit_preview_message(interaction.channel, state)
            reset_ticket_timer(interaction.channel, state, ACTIVE_TICKET_TTL)


class ExistingUiEditActionSelect(discord.ui.Select):
    def __init__(self, ticket_channel_id: int, info: dict):
        self.ticket_channel_id = ticket_channel_id
        duplicate_groups = int(info.get("repairable_duplicate_groups") or 0)
        options = [
            discord.SelectOption(
                label="คงช่องเดิม",
                value="keep",
                description="ใช้ slot mode ปัจจุบันของ addon UI นี้",
                emoji="🛠️",
            ),
            discord.SelectOption(
                label="กำหนดช่องที่จะแสดงใน UI",
                value="custom",
                description="เลือก หัว/ตัว/กางเกง/รองเท้า/มือซ้าย ได้มากกว่า 1 ช่อง",
                emoji="🧩",
            ),
        ]
        if duplicate_groups:
            options.append(discord.SelectOption(
                label="ซ่อมไฟล์ที่ถูกทำ UI ซ้ำ",
                value="repair_duplicates",
                description="รวมรายการซ้ำ เช่น ชื่อ (หัว)/(ตัว)/(กางเกง)/(รองเท้า) กลับเป็นรายการเดียวตอนส่งออก",
                emoji="🧹",
            ))
        super().__init__(placeholder="ตั้งค่า slot / ซ่อม UI ซ้ำ", min_values=1, max_values=1, options=options, custom_id=f"addon_ui:edit_action:{ticket_channel_id}")

    async def callback(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state["user_id"]:
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่เลือกได้", ephemeral=True)
            return
        value = self.values[0]
        if value == "custom":
            embed = discord.Embed(
                title="🧩 เลือกช่องที่จะแสดงในเมนู UI",
                description=(
                    "เลือกได้มากกว่า 1 ช่อง เช่น หัว + ตัว, ตัว + รองเท้า หรือ มือซ้าย\n"
                    "ถ้า item บางตัวไม่มีช่องที่เลือก บอทจะซ่อน item นั้นออกจากเมนู UI โดยไม่สร้าง item ใหม่ซ้ำ"
                ),
                color=discord.Color.teal(),
            )
            await interaction.response.send_message(embed=embed, view=ExistingUiCustomSlotView(self.ticket_channel_id), ephemeral=False)
            if isinstance(interaction.channel, discord.TextChannel):
                reset_ticket_timer(interaction.channel, state, ACTIVE_TICKET_TTL)
            return
        if value == "repair_duplicates":
            state["ui_edit_repair_duplicates"] = True
            content = "ตั้งค่าให้ซ่อมไฟล์ที่ถูกทำ UI ซ้ำแล้ว กด **ส่งออกไฟล์ที่แก้แล้ว** เมื่อต้องการสร้างไฟล์"
        else:
            state["ui_edit_slot_mode"] = "keep"
            state["ui_edit_custom_slots"] = []
            content = "ตั้งค่าให้คงช่องเดิมแล้ว กด **ส่งออกไฟล์ที่แก้แล้ว** เมื่อต้องการสร้างไฟล์"
        await interaction.response.send_message(content, ephemeral=True)
        if isinstance(interaction.channel, discord.TextChannel):
            await _update_ui_edit_preview_message(interaction.channel, state)
            reset_ticket_timer(interaction.channel, state, ACTIVE_TICKET_TTL)


class ExistingUiItemNameSelect(discord.ui.Select):
    def __init__(self, ticket_channel_id: int, items: list[dict], state: dict):
        self.ticket_channel_id = ticket_channel_id
        options = []
        for item in items[:25]:
            entry_id = str(item.get("entry_id") or "")
            name = _ui_edit_pending_item_name(state, item)
            desc = str(item.get("slot_labels") or "-")[:100]
            options.append(discord.SelectOption(label=name[:100] or f"Item {entry_id}", value=entry_id, description=desc, emoji="🏷️"))
        if not options:
            options.append(discord.SelectOption(label="ไม่พบรายการ", value="none", description="ไม่มีไอเท็มให้แก้ชื่อ"))
        super().__init__(placeholder="เลือกไอเท็มที่ต้องการแก้ชื่อ", min_values=1, max_values=1, options=options, custom_id=f"addon_ui:item_rename:{ticket_channel_id}")

    async def callback(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state.get("user_id"):
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่แก้ไขได้", ephemeral=True)
            return
        entry_id = self.values[0]
        if entry_id == "none":
            await interaction.response.send_message("ไม่พบรายการให้แก้ไข", ephemeral=True)
            return
        info = state.get("ui_edit_inspection") or {}
        item = next((x for x in info.get("items", []) if str(x.get("entry_id")) == str(entry_id)), None)
        if not item:
            await interaction.response.send_message("ไม่พบไอเท็มนี้ใน ticket แล้ว", ephemeral=True)
            return
        await interaction.response.send_modal(ExistingUiItemRenameModal(self.ticket_channel_id, entry_id, _ui_edit_pending_item_name(state, item)))


class ExistingUiItemNameView(discord.ui.View):
    def __init__(self, ticket_channel_id: int, items: list[dict], state: dict):
        super().__init__(timeout=600)
        self.add_item(ExistingUiItemNameSelect(ticket_channel_id, items, state))


class ExistingUiAddItemSelect(discord.ui.Select):
    def __init__(self, ticket_channel_id: int, candidates: list[dict]):
        self.ticket_channel_id = ticket_channel_id
        options = []
        for c in candidates[:25]:
            label = str(c.get("display_name") or c.get("identifier") or "item")[:100]
            wearable = c.get("wearable_slot") or ("มือซ้าย" if c.get("item_kind") == "offhand" else "ไม่ระบุ")
            options.append(discord.SelectOption(label=label, value=str(c.get("identifier") or ""), description=str(wearable)[:100], emoji="➕"))
        if not options:
            options.append(discord.SelectOption(label="ไม่พบรายการ", value="none", description="ไม่มีไอเท็มให้เพิ่ม"))
        super().__init__(placeholder="เลือกไอเท็มที่จะเพิ่มเข้า UI", min_values=1, max_values=max(1, len(options)), options=options, custom_id=f"addon_ui:add_item_select:{ticket_channel_id}")

    async def callback(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state.get("user_id"):
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่เลือกได้", ephemeral=True)
            return
        selected = [v for v in self.values if v != "none"]
        if not selected:
            await interaction.response.send_message("ไม่พบไอเท็มให้เพิ่ม", ephemeral=True)
            return
        state["ui_edit_add_selected_ids"] = selected
        candidates = list(state.get("ui_edit_add_candidates") or [])
        selected_candidates = [c for c in candidates if c.get("identifier") in selected]
        selected_text = "\n".join(f"• **{c.get('display_name') or c.get('identifier')}** — `{c.get('wearable_slot') or ('มือซ้าย' if c.get('item_kind') == 'offhand' else 'ไม่ระบุ')}`" for c in selected_candidates[:15])
        if len(selected_candidates) > 15:
            selected_text += f"\n-# และอีก {len(selected_candidates)-15} รายการ"
        embed = discord.Embed(
            title="⚙️ เลือกวิธีเพิ่มไอเท็มเข้า UI เดิม",
            description=(
                "เลือกว่าจะเพิ่มไอเท็มที่เลือกไว้แบบไหน\n\n"
                "**คงช่องเดิม** — ใช้ช่องเดิมจาก addon ที่อัปโหลด\n"
                "**ใส่ได้ทุกช่อง** — สร้างหัว/ตัว/กางเกง/รองเท้าให้รายการที่เป็น wearable\n"
                "**กำหนดช่องเอง** — เลือกช่องที่จะเพิ่มได้มากกว่า 1 ช่อง รวมถึงมือซ้าย\n\n"
                "ไอเท็มที่เป็นมือซ้ายอย่างเดียวจะยังถูกเพิ่มเป็นมือซ้ายเพื่อกันพัง\n\n"
                f"**ไอเท็มที่จะเพิ่ม:**\n{selected_text or '-'}"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=ExistingUiAddSlotModeView(self.ticket_channel_id), ephemeral=False)
        if isinstance(interaction.channel, discord.TextChannel):
            reset_ticket_timer(interaction.channel, state, ACTIVE_TICKET_TTL)


class ExistingUiAddItemView(discord.ui.View):
    def __init__(self, ticket_channel_id: int, candidates: list[dict]):
        super().__init__(timeout=600)
        self.add_item(ExistingUiAddItemSelect(ticket_channel_id, candidates))


class ExistingUiAddSlotModeSelect(discord.ui.Select):
    def __init__(self, ticket_channel_id: int):
        self.ticket_channel_id = ticket_channel_id
        options = [
            discord.SelectOption(label="คงช่องเดิมแล้วเพิ่มเข้า UI", value="original", description="ใช้ slot เดิมของ item จาก addon ที่อัปโหลด", emoji="1️⃣"),
            discord.SelectOption(label="ทำให้ใส่ได้ทุกช่องแล้วเพิ่มเข้า UI", value="all", description="สร้างหัว/ตัว/กางเกง/รองเท้าให้ wearable item", emoji="2️⃣"),
            discord.SelectOption(label="เลือกช่องใหม่แล้วเพิ่มเข้า UI", value="custom", description="เลือกได้มากกว่า 1 ช่อง รวมถึงมือซ้าย", emoji="3️⃣"),
        ]
        super().__init__(placeholder="เลือกวิธีเพิ่มไอเท็ม", min_values=1, max_values=1, options=options, custom_id=f"addon_ui:add_slot_mode:{ticket_channel_id}")

    async def callback(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state.get("user_id"):
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่เลือกได้", ephemeral=True)
            return
        mode = self.values[0]
        if mode == "custom":
            embed = discord.Embed(
                title="🧩 เลือกช่องใหม่สำหรับไอเท็มที่จะเพิ่ม",
                description="เลือกได้มากกว่า 1 ช่อง แล้วบอทจะบันทึกรายการนี้ไว้ใน Edit Mode preview ก่อนส่งออกไฟล์",
                color=discord.Color.blurple(),
            )
            await interaction.response.send_message(embed=embed, view=ExistingUiAddCustomSlotView(self.ticket_channel_id), ephemeral=False)
            if isinstance(interaction.channel, discord.TextChannel):
                reset_ticket_timer(interaction.channel, state, ACTIVE_TICKET_TTL)
            return
        await _commit_ui_edit_addition(interaction, state, mode, [])


class ExistingUiAddSlotModeView(discord.ui.View):
    def __init__(self, ticket_channel_id: int):
        super().__init__(timeout=600)
        self.add_item(ExistingUiAddSlotModeSelect(ticket_channel_id))


class ExistingUiAddCustomSlotSelect(discord.ui.Select):
    def __init__(self, ticket_channel_id: int):
        self.ticket_channel_id = ticket_channel_id
        options = [
            discord.SelectOption(label="หัว", value="head", description="slot.armor.head", emoji="🪖"),
            discord.SelectOption(label="ตัว", value="chest", description="slot.armor.chest", emoji="👕"),
            discord.SelectOption(label="กางเกง", value="legs", description="slot.armor.legs", emoji="👖"),
            discord.SelectOption(label="รองเท้า", value="feet", description="slot.armor.feet", emoji="🥾"),
            discord.SelectOption(label="มือซ้าย", value="offhand", description="slot.weapon.offhand", emoji="🤚"),
        ]
        super().__init__(placeholder="เลือกช่องที่จะเพิ่มเข้า UI", min_values=1, max_values=5, options=options, custom_id=f"addon_ui:add_custom_slots:{ticket_channel_id}")

    async def callback(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state.get("user_id"):
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่เลือกได้", ephemeral=True)
            return
        await _commit_ui_edit_addition(interaction, state, "custom", list(self.values))


class ExistingUiAddCustomSlotView(discord.ui.View):
    def __init__(self, ticket_channel_id: int):
        super().__init__(timeout=600)
        self.add_item(ExistingUiAddCustomSlotSelect(ticket_channel_id))


async def _commit_ui_edit_addition(interaction: discord.Interaction, state: dict, slot_mode: str, custom_slots: list[str]) -> None:
    source_file = state.get("ui_edit_add_source_file")
    selected_ids = list(state.get("ui_edit_add_selected_ids") or [])
    candidates = list(state.get("ui_edit_add_candidates") or [])
    if not source_file or not selected_ids:
        await interaction.response.send_message("ยังไม่มี addon หรือไอเท็มที่เลือกไว้สำหรับเพิ่ม", ephemeral=True)
        return
    by_id = {str(c.get("identifier")): c for c in candidates}
    selected_names = [str(by_id.get(x, {}).get("display_name") or x) for x in selected_ids]
    addition = {
        "addon_path": source_file,
        "filename": Path(source_file).name,
        "selected_identifiers": selected_ids,
        "selected_names": selected_names,
        "slot_mode": slot_mode,
        "custom_slots": list(custom_slots or []),
        "token": secrets.token_hex(3),
    }
    state.setdefault("ui_edit_additions", []).append(addition)
    state["awaiting_ui_edit_addon"] = False
    for key in ("ui_edit_add_source_file", "ui_edit_add_candidates", "ui_edit_add_selected_ids"):
        state.pop(key, None)
    await interaction.response.send_message("บันทึกไอเท็มที่จะเพิ่มเข้า UI แล้ว ตรวจ preview แล้วกด **ส่งออกไฟล์ที่แก้แล้ว** เมื่อต้องการสร้างไฟล์", ephemeral=True)
    if isinstance(interaction.channel, discord.TextChannel):
        await _update_ui_edit_preview_message(interaction.channel, state)
        reset_ticket_timer(interaction.channel, state, ACTIVE_TICKET_TTL)


class ExistingUiEditView(discord.ui.View):
    def __init__(self, ticket_channel_id: int, info: dict, *, disabled: bool = False):
        super().__init__(timeout=900)
        self.ticket_channel_id = ticket_channel_id
        self.add_item(ExistingUiEditActionSelect(ticket_channel_id, info))
        for child in self.children:
            if isinstance(child, (discord.ui.Select, discord.ui.Button)):
                child.disabled = disabled

    async def _get_state(self, interaction: discord.Interaction) -> Optional[dict]:
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return None
        if interaction.user.id != state.get("user_id"):
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่ใช้ปุ่มนี้ได้", ephemeral=True)
            return None
        return state

    @discord.ui.button(label="แก้ชื่อ", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit_metadata(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._get_state(interaction)
        if not state:
            return
        info = state.get("ui_edit_inspection") or {}
        await interaction.response.send_modal(ExistingUiMetadataModal(
            self.ticket_channel_id,
            state.get("ui_edit_pack_name") or info.get("pack_name") or "Addon UI",
            state.get("ui_edit_title") or info.get("ui_title") or "Addon UI",
        ))

    @discord.ui.button(label="แก้รูปแพค", style=discord.ButtonStyle.secondary, emoji="🖼️")
    async def edit_icon(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._get_state(interaction)
        if not state:
            return
        state["awaiting_ui_edit_icon"] = True
        if isinstance(interaction.channel, discord.TextChannel):
            await _update_ui_edit_preview_message(interaction.channel, state, disabled=True)
        await interaction.response.send_message("อัปโหลดรูป pack icon ในช่องนี้ได้เลย รองรับ `.png`, `.jpg`, `.jpeg`, `.webp` และระบบจะย่อรูปที่ใหญ่กว่า 128x128 อัตโนมัติ", ephemeral=False)

    @discord.ui.button(label="แก้ชื่อไอเท็ม", style=discord.ButtonStyle.secondary, emoji="🏷️")
    async def edit_item_names(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._get_state(interaction)
        if not state:
            return
        info = state.get("ui_edit_inspection") or {}
        items = list(info.get("items") or [])
        note = "เลือกไอเท็มที่ต้องการแก้ชื่อในเมนู UI"
        if len(items) > 25:
            note += f"\n-# Discord จำกัด dropdown ไว้ 25 รายการ จึงแสดง 25 รายการแรกจากทั้งหมด {len(items)} รายการ"
        await interaction.response.send_message(note, view=ExistingUiItemNameView(self.ticket_channel_id, items, state), ephemeral=False)

    @discord.ui.button(label="เพิ่มไอเท็ม", style=discord.ButtonStyle.secondary, emoji="➕")
    async def add_items(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._get_state(interaction)
        if not state:
            return
        state["awaiting_ui_edit_addon"] = True
        for key in ("ui_edit_add_source_file", "ui_edit_add_candidates", "ui_edit_add_selected_ids"):
            state.pop(key, None)
        if isinstance(interaction.channel, discord.TextChannel):
            await _update_ui_edit_preview_message(interaction.channel, state, disabled=True)
        await interaction.response.send_message(
            "อัปโหลด addon ปกติที่ต้องการดึงไอเท็มมาเพิ่มใน UI นี้ได้เลย รองรับ `.mcaddon` หรือ `.zip`\n"
            "หลังอัปโหลด บอทจะแสดง dropdown ให้เลือกไอเท็ม แล้วให้เลือกว่าจะคงช่องเดิม / ใส่ได้ทุกช่อง / เลือกช่องเอง",
            ephemeral=False,
        )

    @discord.ui.button(label="ส่งออกไฟล์ที่แก้แล้ว", style=discord.ButtonStyle.success, emoji="✅")
    async def export_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._get_state(interaction)
        if not state:
            return
        if state.get("convert_running"):
            await interaction.response.send_message("กำลังสร้างไฟล์อยู่แล้ว", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        if isinstance(interaction.channel, discord.TextChannel):
            await _update_ui_edit_preview_message(interaction.channel, state, disabled=True)
        await run_existing_ui_edit_job(interaction, state)


class ExistingUiCustomSlotSelect(discord.ui.Select):
    def __init__(self, ticket_channel_id: int):
        self.ticket_channel_id = ticket_channel_id
        options = [
            discord.SelectOption(label="หัว", value="head", description="แสดง slot หัวในเมนู UI", emoji="🪖"),
            discord.SelectOption(label="ตัว", value="chest", description="แสดง slot ตัวในเมนู UI", emoji="👕"),
            discord.SelectOption(label="กางเกง", value="legs", description="แสดง slot กางเกงในเมนู UI", emoji="👖"),
            discord.SelectOption(label="รองเท้า", value="feet", description="แสดง slot รองเท้าในเมนู UI", emoji="🥾"),
            discord.SelectOption(label="มือซ้าย", value="offhand", description="แสดง slot มือซ้ายในเมนู UI ถ้า addon มี item มือซ้ายอยู่แล้ว", emoji="🤚"),
        ]
        super().__init__(placeholder="เลือกช่องที่จะแสดงใน UI", min_values=1, max_values=5, options=options, custom_id=f"addon_ui:edit_custom_slots:{ticket_channel_id}")

    async def callback(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state["user_id"]:
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่เลือกได้", ephemeral=True)
            return
        state["ui_edit_slot_mode"] = "custom"
        state["ui_edit_custom_slots"] = list(self.values)
        # If the file is already double-converted, repair first and then apply the slot filter.
        info = state.get("ui_edit_inspection") or {}
        state["ui_edit_repair_duplicates"] = bool(info.get("repairable_duplicate_groups")) or bool(state.get("ui_edit_repair_duplicates"))
        await interaction.response.send_message("บันทึกช่องที่จะแสดงใน UI แล้ว กด **ส่งออกไฟล์ที่แก้แล้ว** เมื่อต้องการสร้างไฟล์", ephemeral=True)
        if isinstance(interaction.channel, discord.TextChannel):
            await _update_ui_edit_preview_message(interaction.channel, state)
            reset_ticket_timer(interaction.channel, state, ACTIVE_TICKET_TTL)


class ExistingUiCustomSlotView(discord.ui.View):
    def __init__(self, ticket_channel_id: int):
        super().__init__(timeout=600)
        self.add_item(ExistingUiCustomSlotSelect(ticket_channel_id))


async def run_existing_ui_edit_job(interaction: discord.Interaction, state: dict) -> None:
    channel = interaction.channel
    work_dir = state.get("work_dir")
    source_file = state.get("source_file")
    slot_mode = state.get("ui_edit_slot_mode") or "keep"
    custom_slots = list(state.get("ui_edit_custom_slots") or [])
    repair_duplicates = bool(state.get("ui_edit_repair_duplicates"))
    if not work_dir or not source_file:
        await interaction.followup.send("ไม่พบไฟล์ต้นฉบับใน ticket นี้")
        return
    progress = ProgressReporter(channel, state, "🛠️ สถานะงานแก้ไข addon UI เดิม") if isinstance(channel, discord.TextChannel) else None
    if isinstance(channel, discord.TextChannel):
        state["convert_running"] = True
        start_ticket_processing(channel, state, "ui_edit")
    try:
        if progress:
            await progress.set("⏳ งานของคุณกำลังรอคิวประมวลผล..." if convert_semaphore.locked() else "🔍 กำลังเตรียมแก้ไข addon UI เดิม...", force=True)
        async with convert_semaphore:
            if progress:
                await progress.set("🛠️ กำลังแก้ไขข้อมูล UI/ชื่อ/รูปแพค โดยไม่สร้างไอเท็มซ้ำ...", force=True)
            edited = await asyncio.to_thread(
                edit_existing_ui_addon,
                source_file,
                work_dir,
                slot_mode,
                custom_slots,
                repair_duplicates,
                state.get("ui_edit_title"),
                state.get("ui_edit_pack_name"),
                state.get("ui_edit_icon_path"),
                state.get("ui_edit_item_renames") or {},
                state.get("ui_edit_additions") or [],
            )
        edited_path = Path(edited)
        mode_label = "กำหนดช่องเอง" if slot_mode == "custom" else ("ซ่อมไฟล์ที่ทำ UI ซ้ำ" if repair_duplicates else "คง UI เดิม")
        sent_ok = False
        if isinstance(channel, discord.TextChannel):
            sent_ok = await send_output_file_or_retry(
                channel,
                interaction.user,
                state,
                edited_path,
                f"แก้ไข addon UI เดิมเสร็จแล้ว โหมด: **{mode_label}** มีเวลาดาวน์โหลด 1 นาทีก่อนช่องจะถูกลบ",
                progress=progress,
            )
        else:
            await interaction.followup.send(f"แก้ไข addon UI เดิมเสร็จแล้ว โหมด: **{mode_label}**", file=discord.File(edited_path))
            sent_ok = True
        report_text = _read_webhook_report(work_dir, "UI_EDIT_REPORT_WEBHOOK.txt")
        if isinstance(channel, discord.TextChannel):
            await send_user_warnings_from_report(channel, state, report_text, context_title="Warnings จากการแก้ไข addon UI")
        webhook_description = "แก้ไข addon UI เดิมสำเร็จ"
        if report_text:
            webhook_description += "\n\nReport:\n```text\n" + _fit_embed_text(report_text, 3200) + "\n```"
        await send_webhook_log(
            title="Existing Addon UI Edited",
            description=webhook_description,
            color=0x57F287,
            fields=[
                ("User", f"{interaction.user}\n`{interaction.user.id}`", False),
                ("Job ID", str(state.get("job_id") or "-"), True),
                ("Edit Mode", mode_label, True),
                ("Custom Slots", ", ".join(custom_slots) if custom_slots else "-", True),
                ("Renamed Items", str(len(state.get("ui_edit_item_renames") or {})), True),
                ("Added Items", str(_ui_edit_additions_summary(state)[1]), True),
            ],
            files=[Path(source_file), edited_path],
        )
    except Exception as exc:
        state["convert_running"] = False
        user_message = _humanize_addon_error(exc, mode="ui")
        await interaction.followup.send(f"แก้ไข addon UI เดิมไม่สำเร็จ:\n```text\n{user_message[:1800]}\n```\nJob ID: {_job_label(state)}")
        if progress:
            await progress.set("❌ งานแก้ไข UI ล้มเหลว ตรวจข้อความด้านล่างเพื่อดูรายละเอียด", force=True)
        await send_webhook_log(title="Existing Addon UI Edit Failed", description=str(exc), color=0xED4245, fields=[("User", f"{interaction.user}\n`{interaction.user.id}`", False), ("Job ID", str(state.get("job_id") or "-"), True)])
        if isinstance(channel, discord.TextChannel):
            await _update_ui_edit_preview_message(channel, state)
            reset_ticket_timer(channel, state, ACTIVE_TICKET_TTL)
        return
    state["convert_running"] = False
    if isinstance(channel, discord.TextChannel):
        reset_ticket_timer(channel, state, FINISHED_TICKET_TTL if sent_ok else ACTIVE_TICKET_TTL)

class ItemReviewSelect(discord.ui.Select):
    def __init__(self, ticket_channel_id: int, candidates: list[dict]):
        self.ticket_channel_id = ticket_channel_id
        options = []
        for c in candidates[:25]:
            label = (c.get("display_name") or c.get("identifier") or "item")[:100]
            source_name = str(c.get("source_file_name") or "")
            desc_base = source_name or str(c.get("file_path") or "")
            if source_name and c.get("wearable_slot"):
                desc_base = f"{source_name} • {c.get('wearable_slot')}"
            value = str(c.get("option_value") or c.get("identifier") or "")[:100]
            options.append(discord.SelectOption(label=label, value=value, description=desc_base[:100], emoji="📄"))
        super().__init__(placeholder="เลือกไอเท็มที่จะรวมเข้า UI", min_values=1, max_values=max(1, len(options)), options=options, custom_id=f"addon_ui:select:{ticket_channel_id}")

    async def callback(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state["user_id"]:
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่เลือกได้", ephemeral=True)
            return
        selected_ids = list(self.values)
        state["selected_ids"] = selected_ids
        state.pop("slot_mode", None)
        state.pop("custom_slots", None)

        candidates = list(state.get("inspection") or [])
        selected_candidates = [c for c in candidates if str(c.get("option_value") or c.get("identifier")) in selected_ids]
        has_wearable = any((c.get("item_kind") or "wearable") != "offhand" for c in selected_candidates)
        if selected_candidates and not has_wearable:
            state["slot_mode"] = "original"
            state["custom_slots"] = []
            await interaction.response.defer(thinking=True)
            await run_ui_convert_job(interaction, state)
            return

        def _selected_label(c: dict) -> str:
            name = c.get("display_name") or c.get("identifier") or "item"
            src = c.get("source_file_name")
            return f"• **{name}**" + (f" จาก `{src}`" if src else "")
        selected_text = "\n".join(_selected_label(c) for c in selected_candidates[:12])
        if len(selected_candidates) > 12:
            selected_text += f"\n-# และอีก {len(selected_candidates)-12} รายการ"
        embed = discord.Embed(
            title="⚙️ เลือกวิธีตั้งช่องสวมใส่",
            description=(
                "เลือกว่าจะให้ไอเท็มที่เลือกไว้ใส่ได้กี่ช่องก่อนเริ่มสร้าง addon\n\n"
                "**1. คงช่องเดิม** — รวมเข้า UI อย่างเดียว ตอนกดเลือกไอเท็มจะใส่ช่องเดิมทันที ไม่ถามช่อง\n"
                "**2. ใส่ได้ทุกช่อง** — สร้างไอเท็มครบ หัว/ตัว/กางเกง/รองเท้า แล้วถามช่องตอนใช้ UI\n"
                "**3. กำหนดช่องเอง** — เลือกช่องที่จะให้ใส่ได้ สามารถเลือกได้มากกว่า 1 ช่อง\n\n"
                "ถ้าในรายการมีไอเท็มที่เปิด `minecraft:allow_off_hand` ระบบจะใส่เป็นมือซ้ายโดยตรง ไม่ถามช่องในเกม\n\n"
                f"**ไอเท็มที่เลือก:**\n{selected_text}"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=SlotModeReviewView(self.ticket_channel_id))
        if isinstance(interaction.channel, discord.TextChannel):
            reset_ticket_timer(interaction.channel, state, ACTIVE_TICKET_TTL)


class ItemReviewView(discord.ui.View):
    def __init__(self, ticket_channel_id: int, candidates: list[dict]):
        super().__init__(timeout=600)
        self.add_item(ItemReviewSelect(ticket_channel_id, candidates))


async def run_ui_convert_job(interaction: discord.Interaction, state: dict) -> None:
    channel = interaction.channel
    selected_ids = list(state.get("selected_ids") or [])
    slot_mode = state.get("slot_mode") or "all"
    custom_slots = list(state.get("custom_slots") or [])
    work_dir = state.get("work_dir")
    source_file = state.get("source_file")
    source_files = list(state.get("source_files") or ([] if not source_file else [source_file]))
    if not selected_ids:
        await interaction.followup.send("ยังไม่ได้เลือกไอเท็มที่จะรวมเข้า UI")
        return
    if not work_dir or not source_files:
        await interaction.followup.send("ไม่พบไฟล์ต้นฉบับใน ticket นี้")
        return
    progress = ProgressReporter(channel, state, "🎨 สถานะงานรวมไอเท็มเป็น UI") if isinstance(channel, discord.TextChannel) else None
    if isinstance(channel, discord.TextChannel):
        state["convert_running"] = True
        start_ticket_processing(channel, state, "ui_convert")
    try:
        if progress:
            await progress.set("⏳ งานของคุณกำลังรอคิวประมวลผล..." if convert_semaphore.locked() else "🔍 กำลังเตรียมประมวลผล addon...", force=True)
        async with convert_semaphore:
            if progress:
                await progress.set("🎨 กำลังสร้าง addon UI ใหม่ กรุณารอสักครู่...", force=True)
            ref_map = state.get("candidate_ref_map") or {}
            selected_refs = [str(ref_map.get(x, {}).get("ref") or x) for x in selected_ids]
            converted = await asyncio.to_thread(convert_addons_to_ui, source_files, selected_refs, work_dir, slot_mode, custom_slots)
        converted_path = Path(converted)
        mode_label = {
            "original": "คงช่องเดิม",
            "all": "ใส่ได้ทุกช่อง",
            "custom": "กำหนดช่องเอง",
        }.get(slot_mode, slot_mode)
        sent_ok = False
        if isinstance(channel, discord.TextChannel):
            sent_ok = await send_output_file_or_retry(
                channel,
                interaction.user,
                state,
                converted_path,
                f"แปลงเสร็จแล้ว โหมดช่อง: **{mode_label}** มีเวลาดาวน์โหลด 1 นาทีก่อนช่องจะถูกลบ",
                progress=progress,
            )
        else:
            await interaction.followup.send(
                f"{interaction.user.mention} แปลงเสร็จแล้ว โหมดช่อง: **{mode_label}** มีเวลาดาวน์โหลด 1 นาทีก่อนช่องจะถูกลบ",
                file=discord.File(converted_path),
            )
            sent_ok = True
        report_text = _read_webhook_report(work_dir, "NORMALIZE_REPORT_WEBHOOK.txt")
        if isinstance(channel, discord.TextChannel):
            await send_user_warnings_from_report(channel, state, report_text, context_title="Warnings จากการรวมไอเท็มเป็น UI")
        webhook_description = "แปลง addon เป็น UI สำเร็จ"
        if report_text:
            webhook_description += "\n\nReport:\n```text\n" + _fit_embed_text(report_text, 3200) + "\n```"
        await send_webhook_log(
            title="Addon UI Converted",
            description=webhook_description,
            color=0x57F287,
            fields=[
                ("User", f"{interaction.user}\n`{interaction.user.id}`", False),
                ("Job ID", str(state.get("job_id") or "-"), True),
                ("Guild", f"{interaction.guild.name if interaction.guild else 'DM'}\n`{interaction.guild.id if interaction.guild else 'DM'}`", False),
                ("Selected Items", "\n".join(
                    f"`{(state.get('candidate_ref_map') or {}).get(x, {}).get('identifier') or x}`"
                    + (f" จาก {(state.get('candidate_ref_map') or {}).get(x, {}).get('source_file_name')}" if (state.get('candidate_ref_map') or {}).get(x, {}).get('source_file_name') else "")
                    for x in selected_ids
                )[:1024], False),
                ("Slot Mode", mode_label, True),
                ("Custom Slots", ", ".join(custom_slots) if custom_slots else "-", True),
            ],
            files=[Path(p) for p in source_files if Path(p).exists()] + [converted_path],
        )
    except Exception as exc:
        state["convert_running"] = False
        user_message = _humanize_addon_error(exc, mode="ui")
        await interaction.followup.send(f"แปลงไม่สำเร็จ:\n```text\n{user_message[:1800]}\n```\nJob ID: {_job_label(state)}")
        if progress:
            await progress.set("❌ งานล้มเหลว ตรวจข้อความด้านล่างเพื่อดูวิธีแก้", force=True)
        await send_webhook_log(title="Addon UI Convert Failed", description=str(exc), color=0xED4245, fields=[("User", f"{interaction.user}\n`{interaction.user.id}`", False), ("Job ID", str(state.get("job_id") or "-"), True)])
        if isinstance(channel, discord.TextChannel):
            reset_ticket_timer(channel, state, ACTIVE_TICKET_TTL)
        return
    state["convert_running"] = False
    if isinstance(channel, discord.TextChannel):
        reset_ticket_timer(channel, state, FINISHED_TICKET_TTL if sent_ok else ACTIVE_TICKET_TTL)


class SlotModeSelect(discord.ui.Select):
    def __init__(self, ticket_channel_id: int):
        self.ticket_channel_id = ticket_channel_id
        options = [
            discord.SelectOption(
                label="คงไอเท็มใส่ช่องเดิมและช่องเดียว",
                value="original",
                description="รวมเข้า UI เท่านั้น กดเลือกแล้วใส่ช่องเดิมทันที",
                emoji="1️⃣",
            ),
            discord.SelectOption(
                label="ทำให้ไอเท็มที่เลือกใส่ได้ทุกช่อง",
                value="all",
                description="สร้างหัว/ตัว/กางเกง/รองเท้า และถามช่องตอนใช้ UI",
                emoji="2️⃣",
            ),
            discord.SelectOption(
                label="เปลี่ยนช่องที่จะใส่และรวมเป็น UI",
                value="custom",
                description="เลือกช่องเองได้มากกว่า 1 ช่อง รวมถึงมือซ้าย",
                emoji="3️⃣",
            ),
        ]
        super().__init__(placeholder="เลือกวิธีตั้งช่องสวมใส่", min_values=1, max_values=1, options=options, custom_id=f"addon_ui:slot_mode:{ticket_channel_id}")

    async def callback(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state["user_id"]:
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่เลือกได้", ephemeral=True)
            return
        mode = self.values[0]
        state["slot_mode"] = mode
        state["custom_slots"] = []
        if mode == "custom":
            embed = discord.Embed(
                title="🧩 เลือกช่องที่จะให้ไอเท็มใส่ได้",
                description="เลือกได้มากกว่า 1 ช่อง รวมถึงมือซ้าย แล้วบอทจะเริ่มสร้าง addon หลังจากยืนยันตัวเลือกนี้",
                color=discord.Color.blurple(),
            )
            await interaction.response.send_message(embed=embed, view=CustomSlotReviewView(self.ticket_channel_id))
            if isinstance(interaction.channel, discord.TextChannel):
                reset_ticket_timer(interaction.channel, state, ACTIVE_TICKET_TTL)
            return
        await interaction.response.defer(thinking=True)
        await run_ui_convert_job(interaction, state)


class SlotModeReviewView(discord.ui.View):
    def __init__(self, ticket_channel_id: int):
        super().__init__(timeout=600)
        self.add_item(SlotModeSelect(ticket_channel_id))


class CustomSlotSelect(discord.ui.Select):
    def __init__(self, ticket_channel_id: int):
        self.ticket_channel_id = ticket_channel_id
        options = [
            discord.SelectOption(label="หัว", value="head", description="slot.armor.head", emoji="🪖"),
            discord.SelectOption(label="ตัว", value="chest", description="slot.armor.chest", emoji="👕"),
            discord.SelectOption(label="กางเกง", value="legs", description="slot.armor.legs", emoji="👖"),
            discord.SelectOption(label="รองเท้า", value="feet", description="slot.armor.feet", emoji="🥾"),
            discord.SelectOption(label="มือซ้าย", value="offhand", description="slot.weapon.offhand", emoji="🤚"),
        ]
        super().__init__(placeholder="เลือกช่องที่จะให้ใส่ได้", min_values=1, max_values=5, options=options, custom_id=f"addon_ui:custom_slots:{ticket_channel_id}")

    async def callback(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state["user_id"]:
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่เลือกได้", ephemeral=True)
            return
        state["slot_mode"] = "custom"
        state["custom_slots"] = list(self.values)
        await interaction.response.defer(thinking=True)
        await run_ui_convert_job(interaction, state)


class CustomSlotReviewView(discord.ui.View):
    def __init__(self, ticket_channel_id: int):
        super().__init__(timeout=600)
        self.add_item(CustomSlotSelect(ticket_channel_id))


def _ui_review_embed(state: dict, *, multi_intro: bool = False) -> discord.Embed:
    candidates = list(state.get("inspection") or [])
    source_files = list(state.get("source_files") or [])
    file_count = len(source_files) or (1 if state.get("source_file") else 0)
    lines: list[str] = []
    for i, c in enumerate(candidates[:25], start=1):
        src = c.get("source_file_name")
        slot = c.get("wearable_slot") or ("มือซ้าย" if c.get("item_kind") == "offhand" else "ไม่ระบุ")
        source_text = f" จาก `{src}`" if src and file_count > 1 else ""
        lines.append(f"`{i}.` **{c.get('display_name') or c.get('identifier') or 'item'}**{source_text} - `{slot}`\n-# {c.get('file_path') or '-'}")
    if len(candidates) > 25:
        lines.append(f"\nพบ {len(candidates)} ไอเท็ม แต่ Discord dropdown แสดงได้ครั้งละ 25 ตัวเลือก ตอนนี้แสดง 25 รายการแรก")
    title = "📄 Review Mode: เลือกไอเท็มที่จะรวมเข้า UI"
    description = "\n".join(lines) or "ไม่พบรายการ"
    if multi_intro:
        description = (
            f"พบ addon `{file_count}` ไฟล์ และพบไอเท็มที่รวมเข้า UI ได้ `{len(candidates)}` รายการ\n"
            "กดปุ่มด้านล่างเพื่อยืนยันว่าจะรวมไอเท็มจากไฟล์เหล่านี้เป็น UI เดียว แล้วเลือกไอเท็มในขั้นต่อไป\n\n"
            + description
        )
    embed = discord.Embed(title=title, description=description[:4096], color=discord.Color.green())
    embed.set_footer(text=f"รองรับ UI หลายไฟล์สูงสุด {MAX_UI_ADDONS} ไฟล์ • เลือกได้สูงสุด 25 รายการต่อรอบ")
    return embed


async def _send_ui_item_review(channel: discord.TextChannel, state: dict) -> None:
    await channel.send(embed=_ui_review_embed(state), view=ItemReviewView(channel.id, list(state.get("inspection") or [])))


class MultiUiConfirmView(discord.ui.View):
    def __init__(self, ticket_channel_id: int):
        super().__init__(timeout=600)
        self.ticket_channel_id = ticket_channel_id

    async def _get_state(self, interaction: discord.Interaction) -> Optional[dict]:
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return None
        if interaction.user.id != state.get("user_id"):
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่ใช้ปุ่มนี้ได้", ephemeral=True)
            return None
        return state

    @discord.ui.button(label="รวมไฟล์เหล่านี้เป็น UI เดียว", style=discord.ButtonStyle.success, emoji="🎨")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._get_state(interaction)
        if not state:
            return
        state["ui_multi_confirmed"] = True
        await interaction.response.defer(thinking=False)
        if isinstance(interaction.channel, discord.TextChannel):
            await _send_ui_item_review(interaction.channel, state)
            reset_ticket_timer(interaction.channel, state, ACTIVE_TICKET_TTL)

    @discord.ui.button(label="ยกเลิก", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._get_state(interaction)
        if not state:
            return
        _cleanup_work_dir(state.get("work_dir"))
        state["work_dir"] = None
        state["source_file"] = None
        state["source_files"] = []
        state["inspection"] = None
        await interaction.response.send_message("ยกเลิกการรวมหลายไฟล์แล้ว อัปโหลดไฟล์ใหม่ได้เลย", ephemeral=False)
        if isinstance(interaction.channel, discord.TextChannel):
            reset_ticket_timer(interaction.channel, state, INITIAL_TICKET_TTL)


_COMMANDS_SYNCED = False
_STALE_TICKETS_CLEANED = False

@bot.event
async def on_ready():
    global _COMMANDS_SYNCED, _STALE_TICKETS_CLEANED
    await enforce_guild_allowlist()
    if not _STALE_TICKETS_CLEANED:
        await cleanup_stale_ticket_channels()
        _STALE_TICKETS_CLEANED = True
    if not _COMMANDS_SYNCED:
        try:
            await bot.tree.sync()
            _COMMANDS_SYNCED = True
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
            "เลือกโหมดจากเมนูด้านล่าง แล้วบอทจะเปิด ticket ส่วนตัวให้คุณอัปโหลดไฟล์ addon\n\n"
            "🎨 **รวมไอเท็มเป็น UI**\n"
            "สำหรับ addon ที่มีไอเท็มสวมใส่หรือไอเท็มถือมือรองหลายชิ้น บอทจะตรวจไฟล์ที่อัปโหลด แสดงรายการไอเท็มให้เลือก แล้วสร้าง addon ใหม่ที่มีไอเท็มเมนูเพียงชิ้นเดียว ใช้กดเปิดหน้าต่างเลือกไอเท็มในเกมได้สะดวกขึ้น รองรับการอัปโหลด 1-10 ไฟล์ในครั้งเดียว และถ้ามีไอเท็มจากหลายไฟล์ บอทจะถามก่อนว่าจะรวมเป็น UI เดียวหรือไม่\n"
            "ถ้าอัปโหลด addon ที่เคยทำ UI แล้ว บอทจะเข้าโหมดแก้ไขให้แทน สามารถเปลี่ยนช่องที่แสดงใน UI แก้ชื่อแพค แก้รูปแพค แก้ชื่อไอเท็ม และเพิ่มไอเท็มจาก addon ปกติอื่นเข้าไปได้ โดยไม่สร้างไอเท็มซ้ำ\n\n"
            "📦 **รวมแอดออน**\n"
            "สำหรับคนที่มี addon หลายไฟล์ บอทจะรวมได้ 1-10 ไฟล์เป็นไฟล์เดียว พร้อมจัดชื่อและไฟล์ภายในใหม่เพื่อลดปัญหา addon ชนกัน จากนั้นส่งไฟล์ที่รวมเสร็จกลับมาให้ดาวน์โหลด\n\n"
            "🔒 **ความเป็นส่วนตัว**\n"
            "บอทไม่ได้บันทึกข้อมูลส่วนตัวของผู้ใช้ และไม่เก็บไฟล์ addon ไว้ถาวร ไฟล์ที่อัปโหลดจะใช้เพื่อประมวลผลใน ticket นี้เท่านั้น แล้วระบบจะลบไฟล์ชั่วคราวหลังงานเสร็จหรือเมื่อ ticket หมดเวลา"
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
    if len(attachments) > MAX_UI_ADDONS:
        await message.channel.send(f"โหมดรวมไอเท็มเป็น UI รองรับสูงสุด {MAX_UI_ADDONS} ไฟล์ต่อครั้ง กรุณาลดจำนวนไฟล์แล้วอัปโหลดใหม่")
        if isinstance(message.channel, discord.TextChannel):
            reset_ticket_timer(message.channel, state, INITIAL_TICKET_TTL)
        return
    too_large = [a for a in attachments if a.size and a.size > MAX_UPLOAD_BYTES]
    if too_large:
        names = ", ".join(f"{a.filename} ({_format_bytes(a.size)})" for a in too_large[:3])
        await message.channel.send(f"มีไฟล์ใหญ่เกินกำหนดสูงสุด {_format_bytes(MAX_UPLOAD_BYTES)} ต่อไฟล์: {names}")
        if isinstance(message.channel, discord.TextChannel):
            reset_ticket_timer(message.channel, state, INITIAL_TICKET_TTL)
        return
    incoming_total = sum(int(a.size or 0) for a in attachments)
    if incoming_total > MAX_MERGE_TOTAL_UPLOAD_BYTES:
        await message.channel.send(f"ขนาดไฟล์รวมเกินกำหนด ({_format_bytes(incoming_total)} / สูงสุด {_format_bytes(MAX_MERGE_TOTAL_UPLOAD_BYTES)}) กรุณาลดจำนวนหรือขนาดไฟล์")
        if isinstance(message.channel, discord.TextChannel):
            reset_ticket_timer(message.channel, state, INITIAL_TICKET_TTL)
        return
    if isinstance(message.channel, discord.TextChannel):
        reset_ticket_timer(message.channel, state, ACTIVE_TICKET_TTL)

    _cleanup_work_dir(state.get("work_dir"))
    state["work_dir"] = None
    state["source_file"] = None
    state["source_files"] = []
    state["inspection"] = None
    state["candidate_ref_map"] = {}
    state["ui_multi_confirmed"] = False
    state["ui_edit_inspection"] = None
    state["awaiting_ui_edit_icon"] = False
    state.pop("ui_edit_preview_message_id", None)
    state.pop("ui_edit_slot_mode", None)
    state.pop("ui_edit_custom_slots", None)
    state.pop("ui_edit_repair_duplicates", None)
    state.pop("ui_edit_pack_name", None)
    state.pop("ui_edit_title", None)
    state.pop("ui_edit_icon_path", None)
    state.pop("ui_edit_icon_url", None)
    state.pop("ui_edit_item_renames", None)
    state.pop("ui_edit_additions", None)
    state.pop("awaiting_ui_edit_addon", None)
    state.pop("ui_edit_add_source_file", None)
    state.pop("ui_edit_add_candidates", None)
    state.pop("ui_edit_add_selected_ids", None)

    job_dir = Path(tempfile.mkdtemp(prefix=f"addon_ui_{message.author.id}_", dir=TEMP_ROOT))
    saved_files: list[Path] = []
    try:
        for idx, attachment in enumerate(attachments, start=1):
            target = job_dir / attachment.filename
            if target.exists():
                target = job_dir / f"{idx}_{attachment.filename}"
            await attachment.save(target)
            saved_files.append(target)
        state["work_dir"] = str(job_dir)
        state["source_files"] = [str(p) for p in saved_files]
        state["source_file"] = str(saved_files[0]) if saved_files else None
        await message.channel.send(f"ได้รับ {len(saved_files)} ไฟล์แล้ว กำลังตรวจสอบโครงสร้าง addon...")

        # Existing UI addons should be edited one file at a time. When users want
        # to add items into an existing UI addon, Edit Mode already has a dedicated
        # ➕ เพิ่มไอเท็ม flow, which avoids reconverting hidden UI items by mistake.
        existing_infos = []
        for source_file in saved_files:
            existing_ui = await asyncio.to_thread(inspect_existing_ui_addon, str(source_file), str(job_dir / f"existing_ui_check_{source_file.stem}"))
            if existing_ui is not None:
                existing_infos.append((source_file, existing_ui))
        if existing_infos and len(saved_files) > 1:
            await message.channel.send(
                "ตรวจพบว่ามีไฟล์ที่เป็น addon UI อยู่แล้วในชุดที่อัปโหลด\n"
                "ถ้าต้องการแก้ UI เดิมหรือเพิ่มไอเท็มเข้า UI เดิม ให้ส่ง addon UI เพียงไฟล์เดียวก่อนเพื่อเข้า **Edit Mode** แล้วกดปุ่ม **➕ เพิ่มไอเท็ม**"
            )
            _cleanup_work_dir(job_dir)
            state["work_dir"] = None
            state["source_file"] = None
            state["source_files"] = []
            if isinstance(message.channel, discord.TextChannel):
                reset_ticket_timer(message.channel, state, INITIAL_TICKET_TTL)
            return

        if existing_infos and len(saved_files) == 1:
            source_file, existing_ui = existing_infos[0]
            info = asdict(existing_ui)
            state["ui_edit_inspection"] = info
            state["inspection"] = None
            state["ui_edit_slot_mode"] = "keep"
            state["ui_edit_custom_slots"] = []
            state["ui_edit_repair_duplicates"] = False
            state["ui_edit_pack_name"] = info.get("pack_name") or "Addon UI"
            state["ui_edit_title"] = info.get("ui_title") or state["ui_edit_pack_name"]
            state["ui_edit_item_renames"] = {}
            state["ui_edit_additions"] = []
            state["awaiting_ui_edit_icon"] = False
            state["awaiting_ui_edit_addon"] = False
            await send_webhook_log(
                title="Existing Addon UI Uploaded",
                description="ผู้ใช้อัปโหลด addon ที่มีระบบ UI อยู่แล้ว บอทเข้าสู่ Edit Mode แทนการทำ UI ซ้ำ",
                color=0x2ECC71,
                fields=[
                    ("User", f"{message.author}\n`{message.author.id}`", False),
                    ("Job ID", str(state.get("job_id") or "-"), True),
                    ("Guild", f"{message.guild.name if message.guild else 'DM'}\n`{message.guild.id if message.guild else 'DM'}`", False),
                    ("File", source_file.name, True),
                    ("UI Items", str(info.get("item_count", 0)), True),
                    ("Current Slot Mode", str(info.get("current_slot_mode_label") or "-"), True),
                ],
                files=[source_file],
            )
            if isinstance(message.channel, discord.TextChannel):
                await _update_ui_edit_preview_message(message.channel, state)
                reset_ticket_timer(message.channel, state, ACTIVE_TICKET_TTL)
            else:
                await message.channel.send(embed=_ui_edit_preview_embed(state), view=ExistingUiEditView(message.channel.id, info))
            return

        all_candidates: list[dict] = []
        ref_map: dict[str, dict] = {}
        token_counter = 0
        files_with_items = 0
        for source_index, source_file in enumerate(saved_files, start=1):
            inspection = await asyncio.to_thread(inspect_addon, str(source_file), str(job_dir / f"inspect_{source_index}"))
            candidates = [asdict(c) for c in inspection.candidates]
            if candidates:
                files_with_items += 1
            for c in candidates:
                token = f"c{token_counter}"
                token_counter += 1
                c["source_index"] = source_index
                c["source_file_name"] = source_file.name
                c["option_value"] = token
                ref = f"{source_index}:{c.get('identifier')}"
                c["convert_ref"] = ref
                ref_map[token] = {"ref": ref, "identifier": c.get("identifier"), "source_index": source_index, "display_name": c.get("display_name"), "source_file_name": source_file.name}
                all_candidates.append(c)

        if not all_candidates:
            raise AddonError("ไม่พบไอเท็มที่แปลงได้ (ต้องเป็นเกราะ minecraft:wearable หรือไอเท็มที่เปิด minecraft:allow_off_hand)")
        state["inspection"] = all_candidates
        state["candidate_ref_map"] = ref_map
        state["ui_edit_inspection"] = None
        await send_webhook_log(
            title="Addon Uploaded for UI",
            description="มีผู้ใช้อัปโหลด addon สำหรับแปลงเป็น UI",
            color=0x5865F2,
            fields=[
                ("User", f"{message.author}\n`{message.author.id}`", False),
                ("Job ID", str(state.get("job_id") or "-"), True),
                ("Guild", f"{message.guild.name if message.guild else 'DM'}\n`{message.guild.id if message.guild else 'DM'}`", False),
                ("Files", str(len(saved_files)), True),
                ("Detected Items", str(len(all_candidates)), True),
            ],
            files=saved_files,
        )
        if len(saved_files) > 1 and files_with_items >= 2:
            embed = _ui_review_embed(state, multi_intro=True)
            await message.channel.send(embed=embed, view=MultiUiConfirmView(message.channel.id))
        else:
            await _send_ui_item_review(message.channel, state)  # type: ignore[arg-type]
        if isinstance(message.channel, discord.TextChannel):
            reset_ticket_timer(message.channel, state, ACTIVE_TICKET_TTL)
    except Exception as exc:
        user_message = _humanize_addon_error(exc, mode="ui")
        await message.channel.send(f"ตรวจสอบ/อ่าน addon ไม่สำเร็จ:\n```text\n{user_message[:1800]}\n```\nJob ID: {_job_label(state)}")
        await send_webhook_log(title="Addon UI Inspect Failed", description=str(exc), color=0xED4245, fields=[("User", f"{message.author}\n`{message.author.id}`", False), ("Job ID", str(state.get("job_id") or "-"), True)], files=saved_files if saved_files else None)
        _cleanup_work_dir(job_dir)
        state["work_dir"] = None
        state["source_file"] = None
        state["source_files"] = []
        if isinstance(message.channel, discord.TextChannel):
            reset_ticket_timer(message.channel, state, INITIAL_TICKET_TTL)



def _valid_image_attachments(message: discord.Message) -> list[discord.Attachment]:
    return [a for a in message.attachments if a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]


async def handle_ui_edit_addon_message(message: discord.Message, state: dict) -> bool:
    if not state.get("awaiting_ui_edit_addon"):
        return False
    attachments = _valid_addon_attachments(message)
    if not attachments:
        if message.attachments:
            await message.channel.send("กรุณาอัปโหลด addon `.mcaddon` หรือ `.zip` สำหรับเพิ่มไอเท็มเข้า UI")
        return True
    attachment = attachments[0]
    if attachment.size and attachment.size > MAX_UPLOAD_BYTES:
        await message.channel.send(f"ไฟล์ใหญ่เกินกำหนด ({_format_bytes(attachment.size)} / สูงสุด {_format_bytes(MAX_UPLOAD_BYTES)}) กรุณาลดขนาด addon แล้วอัปโหลดใหม่")
        return True
    work_dir = state.get("work_dir")
    if not work_dir:
        await message.channel.send("ไม่พบไฟล์ UI เดิมใน ticket นี้ กรุณาเปิด ticket ใหม่")
        return True
    job_dir = Path(work_dir)
    add_dir = job_dir / "ui_edit_added_sources"
    add_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(3)
    safe_name = Path(attachment.filename).name
    source_file = add_dir / f"add_{token}_{safe_name}"
    try:
        await attachment.save(source_file)
        await message.channel.send("ได้รับ addon สำหรับเพิ่มไอเท็มแล้ว กำลังตรวจรายการ...")
        inspect_work = job_dir / f"ui_edit_add_inspect_{token}"
        existing_ui = await asyncio.to_thread(inspect_existing_ui_addon, str(source_file), str(inspect_work))
        if existing_ui is not None:
            await message.channel.send(
                "ไฟล์ที่อัปโหลดเป็น addon ที่มีระบบ UI อยู่แล้ว จึงไม่ควรใช้เป็นแหล่งเพิ่มไอเท็ม เพราะอาจทำให้เกิดรายการซ้ำ\n"
                "กรุณาอัปโหลด addon ต้นฉบับ/ addon ปกติที่ยังไม่ได้รวมไอเท็มเป็น UI"
            )
            try:
                source_file.unlink(missing_ok=True)
            except Exception:
                pass
            return True
        inspection = await asyncio.to_thread(inspect_addon, str(source_file), str(inspect_work))
        candidates = [asdict(c) for c in inspection.candidates]
        state["ui_edit_add_source_file"] = str(source_file)
        state["ui_edit_add_candidates"] = candidates
        state.pop("ui_edit_add_selected_ids", None)
        desc = "\n".join(
            f"`{i+1}.` **{c['display_name']}** - `{c.get('wearable_slot') or ('มือซ้าย' if c.get('item_kind') == 'offhand' else 'ไม่ระบุ')}`\n-# {c['file_path']}"
            for i, c in enumerate(candidates[:25])
        )
        if len(candidates) > 25:
            desc += f"\n\nพบ {len(candidates)} ไอเท็ม แต่ Discord dropdown แสดงได้ครั้งละ 25 ตัวเลือก ตอนนี้แสดง 25 รายการแรก"
        embed = discord.Embed(title="➕ เลือกไอเท็มที่จะเพิ่มเข้า UI เดิม", description=desc or "ไม่พบรายการ", color=discord.Color.green())
        await message.channel.send(embed=embed, view=ExistingUiAddItemView(message.channel.id, candidates))
        if isinstance(message.channel, discord.TextChannel):
            await _update_ui_edit_preview_message(message.channel, state)
            reset_ticket_timer(message.channel, state, ACTIVE_TICKET_TTL)
        return True
    except Exception as exc:
        user_message = _humanize_addon_error(exc, mode="ui")
        await message.channel.send(f"อ่าน addon สำหรับเพิ่มไอเท็มไม่สำเร็จ:\n```text\n{user_message[:1800]}\n```\nJob ID: {_job_label(state)}")
        try:
            source_file.unlink(missing_ok=True)
        except Exception:
            pass
        if isinstance(message.channel, discord.TextChannel):
            reset_ticket_timer(message.channel, state, ACTIVE_TICKET_TTL)
        return True


async def handle_ui_edit_icon_message(message: discord.Message, state: dict) -> bool:
    if not state.get("awaiting_ui_edit_icon"):
        return False
    images = _valid_image_attachments(message)
    if not images:
        if message.attachments:
            await message.channel.send("กรุณาอัปโหลดไฟล์รูป `.png`, `.jpg`, `.jpeg` หรือ `.webp`")
        return True
    image = images[0]
    if image.size and image.size > MAX_UPLOAD_BYTES:
        await message.channel.send(f"รูปใหญ่เกินกำหนด ({_format_bytes(image.size)} / สูงสุด {_format_bytes(MAX_UPLOAD_BYTES)}) กรุณาอัปโหลดรูปที่เล็กลง")
        return True
    job_dir = Path(state["work_dir"])
    raw_icon_path = job_dir / f"uploaded_ui_edit_pack_icon{Path(image.filename).suffix.lower()}"
    icon_path = job_dir / "ui_edit_custom_pack_icon.png"
    await image.save(raw_icon_path)
    await asyncio.to_thread(resize_discord_icon, raw_icon_path, icon_path)
    state["ui_edit_icon_path"] = str(icon_path)
    state["ui_edit_icon_url"] = image.url
    state["awaiting_ui_edit_icon"] = False
    await message.channel.send("อัปเดตรูป pack icon สำหรับ Edit Mode แล้ว กด **ส่งออกไฟล์ที่แก้แล้ว** เมื่อต้องการสร้างไฟล์")
    if isinstance(message.channel, discord.TextChannel):
        await _update_ui_edit_preview_message(message.channel, state)
        reset_ticket_timer(message.channel, state, ACTIVE_TICKET_TTL)
    return True


def _merge_preview_items_text(preview: dict, limit: int = 45) -> str:
    lines: list[str] = []
    total = 0
    for addon in preview.get("addons", []):
        addon_name = addon.get("pack_name", f"Addon {addon.get('index', '?')}")
        for item in addon.get("items", []):
            total += 1
            if len(lines) >= limit:
                continue
            name = item.get("display_name") or item.get("identifier") or "item"
            slot = item.get("wearable_slot") or "ไม่ระบุช่อง"
            file_path = item.get("file_path") or "ไม่ระบุตำแหน่ง"
            lines.append(f"• **{name}** จาก **{addon_name}** `{slot}`\n-# {file_path}")
    if total > limit:
        lines.append(f"\n-# แสดง {limit}/{total} ไอเท็ม ที่เหลือจะถูกรวมในไฟล์จริงด้วย")
    return "\n".join(lines) or "ไม่พบไอเท็มในไฟล์ที่อัปโหลด"


def _merge_preview_embed(state: dict) -> discord.Embed:
    preview = state.get("merge_preview") or {}
    pack_name = state.get("merge_pack_name") or preview.get("default_pack_name") or "Merged Addons"
    addons = preview.get("addons", [])
    file_count = len(state.get("source_files", []))
    description_lines = [
        f"**ชื่อแพคที่จะสร้าง:** `{pack_name}`",
        "**Description:** ไม่สามารถแก้ไขได้ ระบบจะใช้ description มาตรฐานของ merged addon",
        f"**จำนวนไฟล์:** `{file_count}/{MAX_MERGE_ADDONS}`",
        "",
        "กด **แก้ไขชื่อแพค** เพื่อเปลี่ยนชื่อ output pack หรือกด **แก้ไขรูป** เพื่ออัปโหลด pack icon ใหม่",
    ]
    embed = discord.Embed(
        title="📦 ตรวจสอบข้อมูลก่อนรวมแอดออน",
        description="\n".join(description_lines),
        color=discord.Color.gold(),
    )
    if state.get("merge_icon_url"):
        embed.set_thumbnail(url=state["merge_icon_url"])
    for addon in addons[:10]:
        icon_text = "มี" if addon.get("pack_icon") else "ไม่มี"
        value = (
            f"**ชื่อแพค:** {addon.get('pack_name', 'ไม่ระบุ')}\n"
            f"**Description:** {str(addon.get('description', 'ไม่ระบุ'))[:300]}\n"
            f"**Version:** {addon.get('version', 'ไม่ระบุ')}\n"
            f"**Pack icon:** {icon_text}\n"
            f"**Items:** {len(addon.get('items', []))}"
        )
        embed.add_field(name=f"Addon {addon.get('index', '?')}: {addon.get('file_name', '')}"[:256], value=value[:1024], inline=False)
    embed.add_field(name="ไอเท็มทั้งหมดที่พบ", value=_merge_preview_items_text(preview)[:1024], inline=False)
    if file_count < MIN_MERGE_ADDONS:
        embed.set_footer(text=f"ต้องอัปโหลดอย่างน้อย {MIN_MERGE_ADDONS} ไฟล์ก่อนเริ่มสร้าง")
    else:
        embed.set_footer(text="พร้อมสร้างแล้ว • ระบบจะใช้ชื่อแพคและรูปที่แก้ในหน้านี้")
    return embed


async def _update_merge_preview_message(channel: discord.TextChannel, state: dict, *, disabled: bool = False) -> None:
    embed = _merge_preview_embed(state)
    view = MergePreviewView(channel.id, disabled=disabled)
    message_id = state.get("merge_preview_message_id")
    if message_id:
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.edit(embed=embed, view=view)
            return
        except Exception:
            pass
    msg = await channel.send(embed=embed, view=view)
    state["merge_preview_message_id"] = msg.id


async def _refresh_merge_preview(channel: discord.TextChannel, state: dict) -> None:
    source_files: list[str] = state.get("source_files", [])
    if not source_files:
        return
    preview = await asyncio.to_thread(inspect_merge_addons, source_files, str(state["work_dir"]))
    state["merge_preview"] = preview
    state.setdefault("merge_pack_name", preview.get("default_pack_name") or "Merged Addons")
    if not state.get("merge_pack_icon_path") and preview.get("default_pack_icon_path"):
        state["merge_pack_icon_path"] = preview.get("default_pack_icon_path")
    await _update_merge_preview_message(channel, state)


class MergePackNameModal(discord.ui.Modal, title="แก้ไขชื่อแพค"):
    def __init__(self, ticket_channel_id: int, default_name: str):
        super().__init__(timeout=300)
        self.ticket_channel_id = ticket_channel_id
        self.pack_name = discord.ui.TextInput(
            label="ชื่อแพคใหม่",
            placeholder="เช่น My Merged Addon",
            default=default_name[:100],
            max_length=80,
            required=True,
        )
        self.add_item(self.pack_name)

    async def on_submit(self, interaction: discord.Interaction):
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return
        if interaction.user.id != state.get("user_id"):
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่แก้ไขได้", ephemeral=True)
            return
        state["merge_pack_name"] = str(self.pack_name.value).strip() or "Merged Addons"
        await interaction.response.defer(thinking=False)
        if isinstance(interaction.channel, discord.TextChannel):
            await _update_merge_preview_message(interaction.channel, state)


class MergePreviewView(discord.ui.View):
    def __init__(self, ticket_channel_id: int, *, disabled: bool = False):
        super().__init__(timeout=900)
        self.ticket_channel_id = ticket_channel_id
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = disabled

    async def _get_state(self, interaction: discord.Interaction) -> Optional[dict]:
        state = TICKETS.get(self.ticket_channel_id)
        if not state:
            await interaction.response.send_message("ticket หมดอายุแล้ว", ephemeral=True)
            return None
        if interaction.user.id != state.get("user_id"):
            await interaction.response.send_message("เฉพาะเจ้าของ ticket เท่านั้นที่ใช้ปุ่มนี้ได้", ephemeral=True)
            return None
        return state

    @discord.ui.button(label="แก้ไขชื่อแพค", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._get_state(interaction)
        if not state:
            return
        preview = state.get("merge_preview") or {}
        default_name = state.get("merge_pack_name") or preview.get("default_pack_name") or "Merged Addons"
        await interaction.response.send_modal(MergePackNameModal(self.ticket_channel_id, default_name))

    @discord.ui.button(label="แก้ไขรูป", style=discord.ButtonStyle.secondary, emoji="🖼️")
    async def edit_icon(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._get_state(interaction)
        if not state:
            return
        state["awaiting_merge_icon"] = True
        if isinstance(interaction.channel, discord.TextChannel):
            await _update_merge_preview_message(interaction.channel, state, disabled=True)
        await interaction.response.send_message("อัปโหลดรูป pack icon ในช่องนี้ได้เลย ระบบจะย่อรูปที่ใหญ่กว่า 128x128 เป็น `.png` อัตโนมัติ และจะไม่ขยายรูปที่เล็กกว่า 128", ephemeral=False)

    @discord.ui.button(label="เริ่มสร้าง", style=discord.ButtonStyle.success, emoji="📦")
    async def start_build(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self._get_state(interaction)
        if not state:
            return
        if state.get("merge_running"):
            await interaction.response.send_message("กำลังรวมแอดออนอยู่แล้ว", ephemeral=True)
            return
        if len(state.get("source_files", [])) < MIN_MERGE_ADDONS:
            await interaction.response.send_message(f"ต้องอัปโหลดอย่างน้อย {MIN_MERGE_ADDONS} ไฟล์ก่อนเริ่มสร้าง", ephemeral=True)
            return
        await interaction.response.defer(thinking=False)
        if isinstance(interaction.channel, discord.TextChannel):
            await run_merge_job(interaction.channel, interaction.user, state)


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
    if isinstance(channel, discord.TextChannel):
        start_ticket_processing(channel, state, "merge_addons")
    state["awaiting_merge_icon"] = False
    await _update_merge_preview_message(channel, state, disabled=True)
    progress = ProgressReporter(channel, state, "📦 สถานะงานรวมแอดออน")
    await progress.set(
        "⏳ งานของคุณกำลังรอคิวประมวลผล..." if convert_semaphore.locked() else f"รับ {len(source_files)} ไฟล์แล้ว กำลังเตรียมรวมแอดออน...",
        force=True,
    )
    try:
        async with convert_semaphore:
            await progress.set("📦 กำลังรวมแอดออนตามชื่อ/รูปที่ตั้งไว้ อาจใช้เวลาสักครู่...", force=True)
            merged = await asyncio.to_thread(
                merge_addons,
                source_files,
                str(state["work_dir"]),
                state.get("merge_pack_name"),
                state.get("merge_pack_icon_path"),
            )
        merged_path = Path(merged)
        sent_ok = await send_output_file_or_retry(
            channel,
            user,
            state,
            merged_path,
            "รวมแอดออนเสร็จแล้ว มีเวลาดาวน์โหลด 1 นาทีก่อนช่องจะถูกลบ",
            progress=progress,
        )
        guild = channel.guild if isinstance(channel, discord.TextChannel) else None
        report_text = _read_webhook_report(state.get("work_dir"), "MERGE_REPORT_WEBHOOK.txt")
        await send_user_warnings_from_report(channel, state, report_text, context_title="Warnings จากการรวมแอดออน")
        webhook_description = f"รวม addon {len(source_files)} ไฟล์สำเร็จ"
        if report_text:
            webhook_description += "\n\nReport:\n```text\n" + _fit_embed_text(report_text, 3200) + "\n```"
        await send_webhook_log(
            title="Addons Merged",
            description=webhook_description,
            color=0x57F287,
            fields=[
                ("User", f"{user}\n`{user.id}`", False),
                ("Job ID", str(state.get("job_id") or "-"), True),
                ("Guild", f"{guild.name if guild else 'DM'}\n`{guild.id if guild else 'DM'}`", False),
                ("Pack Name", str(state.get("merge_pack_name") or "Merged Addons"), True),
                ("Mode", f"รวมแอดออน {len(source_files)} ไฟล์", True),
            ],
            files=[Path(p) for p in source_files if Path(p).exists()] + [merged_path],
        )
    except Exception as exc:
        state["merge_running"] = False
        await _update_merge_preview_message(channel, state)
        user_message = _humanize_addon_error(exc, mode="merge")
        await channel.send(f"รวมแอดออนไม่สำเร็จ:\n```text\n{user_message[:1800]}\n```\nJob ID: {_job_label(state)}")
        await progress.set("❌ งานล้มเหลว ตรวจข้อความด้านล่างเพื่อดูวิธีแก้", force=True)
        await send_webhook_log(
            title="Addon Merge Failed",
            description=str(exc),
            color=0xED4245,
            fields=[("User", f"{user}\n`{user.id}`", False), ("Job ID", str(state.get("job_id") or "-"), True)],
            files=[Path(p) for p in source_files if Path(p).exists()],
        )
        reset_ticket_timer(channel, state, ACTIVE_TICKET_TTL)
        return
    state["merge_running"] = False
    reset_ticket_timer(channel, state, FINISHED_TICKET_TTL if sent_ok else ACTIVE_TICKET_TTL)


async def handle_merge_icon_message(message: discord.Message, state: dict) -> bool:
    if not state.get("awaiting_merge_icon"):
        return False
    images = _valid_image_attachments(message)
    if not images:
        if message.attachments:
            await message.channel.send("กรุณาอัปโหลดไฟล์รูป `.png`, `.jpg`, `.jpeg` หรือ `.webp`")
        return True
    image = images[0]
    if image.size and image.size > MAX_UPLOAD_BYTES:
        await message.channel.send(f"รูปใหญ่เกินกำหนด ({_format_bytes(image.size)} / สูงสุด {_format_bytes(MAX_UPLOAD_BYTES)}) กรุณาอัปโหลดรูปที่เล็กลง")
        return True
    job_dir = Path(state["work_dir"])
    raw_icon_path = job_dir / f"uploaded_pack_icon{Path(image.filename).suffix.lower()}"
    icon_path = job_dir / "custom_pack_icon.png"
    await image.save(raw_icon_path)
    await asyncio.to_thread(resize_discord_icon, raw_icon_path, icon_path)
    state["merge_pack_icon_path"] = str(icon_path)
    state["merge_icon_url"] = image.url
    state["awaiting_merge_icon"] = False
    await message.channel.send("อัปเดตรูป pack icon แล้ว")
    if isinstance(message.channel, discord.TextChannel):
        await _update_merge_preview_message(message.channel, state)
        reset_ticket_timer(message.channel, state, ACTIVE_TICKET_TTL)
    return True


async def handle_merge_addons_message(message: discord.Message, state: dict, attachments: list[discord.Attachment]) -> None:
    if state.get("merge_running"):
        await message.channel.send("กำลังรวมแอดออนอยู่แล้ว กรุณารอให้เสร็จก่อน")
        return
    too_large = [a for a in attachments if a.size and a.size > MAX_UPLOAD_BYTES]
    if too_large:
        names = ", ".join(f"{a.filename} ({_format_bytes(a.size)})" for a in too_large[:3])
        await message.channel.send(f"มีไฟล์ใหญ่เกินกำหนดสูงสุด {_format_bytes(MAX_UPLOAD_BYTES)} ต่อไฟล์: {names}")
        if isinstance(message.channel, discord.TextChannel):
            reset_ticket_timer(message.channel, state, INITIAL_TICKET_TTL)
        return
    current_total = sum(Path(p).stat().st_size for p in state.get("source_files", []) if Path(p).exists())
    incoming_total = sum(int(a.size or 0) for a in attachments)
    if current_total + incoming_total > MAX_MERGE_TOTAL_UPLOAD_BYTES:
        await message.channel.send(f"ขนาดไฟล์รวมเกินกำหนด ({_format_bytes(current_total + incoming_total)} / สูงสุด {_format_bytes(MAX_MERGE_TOTAL_UPLOAD_BYTES)}) กรุณาลดจำนวนหรือขนาดไฟล์")
        if isinstance(message.channel, discord.TextChannel):
            reset_ticket_timer(message.channel, state, INITIAL_TICKET_TTL)
        return
    if isinstance(message.channel, discord.TextChannel):
        reset_ticket_timer(message.channel, state, ACTIVE_TICKET_TTL)
    if not state.get("work_dir"):
        state["work_dir"] = tempfile.mkdtemp(prefix=f"addon_merge_{message.author.id}_", dir=TEMP_ROOT)
    job_dir = Path(state["work_dir"])
    source_files: list[str] = state.setdefault("source_files", [])
    saved_count = 0
    saved_paths: list[str] = []
    for attachment in attachments:
        if len(source_files) >= MAX_MERGE_ADDONS:
            break
        target = job_dir / attachment.filename
        if target.exists():
            target = job_dir / f"{len(source_files)+1}_{attachment.filename}"
        await attachment.save(target)
        source_files.append(str(target))
        saved_paths.append(str(target))
        saved_count += 1

    if saved_count == 0:
        await message.channel.send(f"รับไฟล์ครบสูงสุด {MAX_MERGE_ADDONS} ไฟล์แล้ว กด **เริ่มสร้าง** ใน embed เพื่อรวมแอดออน")
        return

    try:
        await _refresh_merge_preview(message.channel, state)  # type: ignore[arg-type]
        if isinstance(message.channel, discord.TextChannel):
            reset_ticket_timer(message.channel, state, ACTIVE_TICKET_TTL)
    except Exception as exc:
        user_message = _humanize_addon_error(exc, mode="merge")
        await message.channel.send(f"ตรวจสอบไฟล์รวมแอดออนไม่สำเร็จ:\n```text\n{user_message[:1800]}\n```\nJob ID: {_job_label(state)}")
        for saved in saved_paths:
            try:
                if saved in source_files:
                    source_files.remove(saved)
                Path(saved).unlink(missing_ok=True)
            except Exception:
                pass
        if not source_files:
            _cleanup_work_dir(state.get("work_dir"))
            state["work_dir"] = None
        if isinstance(message.channel, discord.TextChannel):
            reset_ticket_timer(message.channel, state, INITIAL_TICKET_TTL)
        return

    if len(source_files) < MIN_MERGE_ADDONS:
        await message.channel.send(f"ได้รับ {len(source_files)}/{MAX_MERGE_ADDONS} ไฟล์แล้ว ต้องอัปโหลดอย่างน้อยอีก {MIN_MERGE_ADDONS-len(source_files)} ไฟล์ก่อนเริ่มสร้าง")
    elif len(source_files) < MAX_MERGE_ADDONS:
        await message.channel.send(f"ได้รับ {len(source_files)}/{MAX_MERGE_ADDONS} ไฟล์แล้ว จะอัปโหลดเพิ่มหรือกด **เริ่มสร้าง** ใน embed ก็ได้")
    else:
        await message.channel.send(f"ได้รับครบ {MAX_MERGE_ADDONS} ไฟล์แล้ว กด **เริ่มสร้าง** ใน embed เพื่อรวมแอดออน")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    state = TICKETS.get(message.channel.id)
    if not state or message.author.id != state["user_id"]:
        return
    if state.get("mode") == "merge_addons" and await handle_merge_icon_message(message, state):
        return
    if state.get("mode") == "combine_ui" and await handle_ui_edit_icon_message(message, state):
        return
    if state.get("mode") == "combine_ui" and await handle_ui_edit_addon_message(message, state):
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
    await cleanup_old_temp_dirs()
    bot.add_view(StartPanelView())
    await start_health_server()
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
