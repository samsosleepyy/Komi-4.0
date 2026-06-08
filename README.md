# Addon UI Discord Bot

บอท Discord สำหรับแปลง Minecraft Bedrock addon ที่มีไอเท็มเกราะหลายชิ้น ให้รวมเป็นระบบ UI อัตโนมัติ

## ฟีเจอร์

- จำกัดให้บอททำงานเฉพาะ server ID ที่กำหนดใน `ALLOWED_GUILDS`
- ถ้าบอทอยู่ server นอก allowlist จะรายงานไป webhook และออกทันที
- คำสั่ง `/setup` ใช้ได้เฉพาะ Administrator
- สร้าง panel embed พร้อมปุ่ม `เริ่มรวมแอดออน`
- กดปุ่มแล้วสร้างห้องส่วนตัวแบบ ticket ชั่วคราว
- ผู้ใช้อัปโหลด `.mcaddon` หรือ `.zip`
- บอทตรวจหา item ที่เป็น armor จาก `minecraft:wearable`
- Review mode ด้วย dropdown แบบเลือกหลายอัน
- บอท copy item/attachable/geometry/animation/texture ตามช่องหัว/ตัว/กางเกง/รองเท้า
- สร้างไอเท็มเปิด UI เพียงอันเดียว
- script UI ใช้ `replaceitem`
- ก่อนใส่ชิ้นใหม่ จะลบเกราะจาก addon เดียวกันออกจากช่องอื่นทั้งหมด
- ถ้าช่องปลายทางมีไอเท็มอยู่แล้ว จะถามก่อนทับ
- สุ่ม UUID ใหม่ให้ addon ก่อนส่งออก
- ส่ง log และไฟล์ต้นฉบับ/ไฟล์แปลงแล้วไป webhook
- ไม่มีระบบ point ตามที่ขอ

## Environment Variables บน Render

ตั้งค่าใน Render > Environment:

```env
DISCORD_TOKEN=token ของบอท
WEBHOOK_URL=webhook สำหรับ log
ALLOWED_GUILDS=1420339720277463112,1441795602550882334
MAX_PARALLEL_JOBS=1
```

> หมายเหตุ: ถ้า webhook URL เคยถูกส่งในแชทหรือหลุดออกไปแล้ว ให้ลบ/rotate webhook เดิม และสร้างอันใหม่ทันที

## Deploy บน Render

แนะนำใช้ Background Worker

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
python bot.py
```

หรือใช้ `render.yaml` ในโปรเจกต์นี้ได้เลย

## Discord Developer Portal

เปิด intent ที่แนะนำ:

- Server Members Intent เพื่ออ่าน owner/member count ได้เสถียรขึ้น
- Message Content Intent เพื่อให้บอทอ่าน ticket message และ attachment ได้ชัวร์

ให้สิทธิ์บอทใน server:

- Manage Channels
- Send Messages
- Embed Links
- Attach Files
- Read Message History
- Use Slash Commands
- Create Instant Invite ไม่จำเป็น เพราะโปรเจกต์นี้ไม่สร้าง invite ใน server นอก allowlist เพื่อความปลอดภัย

## วิธีใช้

ใช้คำสั่ง:

```text
/setup category:<หมวดหมู่> channel:<ช่องที่จะส่ง panel> image_url:<ลิงก์รูป embed>
```

หลังจากนั้นผู้ใช้กดปุ่ม `เริ่มรวมแอดออน` แล้วอัปโหลดไฟล์ addon ใน ticket channel

## ความปลอดภัย

โปรเจกต์นี้จะไม่ก๊อปหรือสร้าง invite link จาก server ที่ไม่ได้รับอนุญาต เพราะเป็นข้อมูลทางเข้าของ server อื่น ระบบจะรายงานเฉพาะชื่อ server, id, owner, จำนวนสมาชิก แล้วออกทันที


## Render Python version note

This project pins Python to 3.12.8 using both `.python-version` and `PYTHON_VERSION` in `render.yaml`. New Render services may default to Python 3.14, where the old stdlib `audioop` module is removed. `requirements.txt` also includes `audioop-lts` for Python >= 3.13 as a fallback.

## Render Web Service port note

เวอร์ชันนี้มี health server เล็ก ๆ ที่ bind กับ `PORT` ของ Render อัตโนมัติ (`/` และ `/healthz`) ดังนั้นถ้า deploy เป็น Web Service จะไม่เจอปัญหา `No open ports detected` แล้ว

อย่างไรก็ตาม สำหรับ Discord bot แนะนำใช้ Background Worker มากกว่า เพราะบอทไม่ได้ต้องรับ traffic HTTP จริง ๆ

## Update notes

- Original wearable addon items are hidden from Creative inventory after conversion.
- Only the generated UI item is visible in Creative.
- Generated UI item name is based on the addon pack name without BP/RP suffix: `<Addon Name> item ui`.
- Armor selection UI includes the SamSoSleepy branding line.
