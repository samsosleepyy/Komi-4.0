# Addon UI Discord Bot

บอท Discord สำหรับแปลง Minecraft Bedrock addon และรวม addon โดยเตรียมไว้สำหรับรันบน Render

## ฟีเจอร์หลัก

- จำกัดให้บอททำงานเฉพาะ server ID ที่กำหนดใน `ALLOWED_GUILDS`
- ถ้าอยู่ server นอก allowlist จะรายงานไป webhook และออกทันที
- `/setup` ใช้ได้เฉพาะ Administrator
- Panel เป็น dropdown:
  - 🎨 รวมไอเท็มเป็น UI
  - 📦 รวมแอดออน
- Ticket channel ส่วนตัวชั่วคราว
- ส่ง log และไฟล์ไป webhook
- ไม่มีระบบ point ตามที่ขอ

## รวมไอเท็มเป็น UI: Normalize/Rebuild Mode

เวอร์ชันนี้ใช้แนวคิดจากโครงสร้าง Seraphim template ที่แปลง UI ได้เสถียร ไม่ patch addon เดิมโดยตรง แต่จะ:

1. แตก addon ต้นฉบับ
2. ตรวจหา `BP/items/*.json` ที่มี `minecraft:wearable`
3. ให้ผู้ใช้เลือก item ผ่าน Review dropdown
4. ดึงเฉพาะ asset สำคัญ:
   - item metadata
   - attachable metadata
   - geometry ที่ attachable อ้างถึง
   - animations ที่ attachable อ้างถึง
   - render controllers custom ถ้ามี
   - texture skin/model
   - icon texture จาก `item_texture.json`
5. สร้าง addon ใหม่ทั้งหมดเป็นโครงสร้างมาตรฐาน:
   - `BP_auto_ui`
   - `RP_auto_ui`
6. สร้าง item จริงซ้ำครบ 4 ช่อง:
   - head
   - chest
   - legs
   - feet
7. ซ่อนไอเท็ม armor จริงทั้งหมดจาก Creative
8. สร้างไอเท็ม UI อันเดียวที่มองเห็นใน Creative
9. สร้าง `scripts/auto_ui_system.js` สำหรับ UI + `replaceitem`
10. ก่อนใส่ชิ้นใหม่ จะลบเฉพาะเกราะจาก addon ที่สร้างใหม่นี้ออกจากช่องอื่นทั้งหมด
11. ถ้าช่องปลายทางมีของอยู่ จะถามก่อนทับ

ชื่อ item UI จะเป็น:

```text
<Addon Name> item ui
```

หน้า UI มีข้อความ branding:

```text
Auto convert skin ui by SamSoSleepy
Discord : https://discord.gg/FnmWw7nWyq
```

ผลลัพธ์จะมี `NORMALIZE_REPORT.txt` แนบอยู่ใน addon เพื่อดู mapping/warning

## รวมแอดออน 2-5 ไฟล์

โหมดนี้ยังคงอยู่จากเวอร์ชันก่อน:

- รับ addon 2-5 ไฟล์
- รวมเป็น `BP_merged` และ `RP_merged`
- สุ่ม UUID ใหม่
- แยก scripts เดิมเป็น `scripts/addon_<prefix>/...`
- สร้าง main.js ใหม่สำหรับ import script entry ของแต่ละ addon
- prefix identifiers/geometry/animation/controller/texture เพื่อลดการชนกัน
- สร้าง `MERGE_REPORT.txt`

## Environment Variables บน Render

```env
DISCORD_TOKEN=token ของบอท
WEBHOOK_URL=webhook สำหรับ log
ALLOWED_GUILDS=1420339720277463112,1441795602550882334
MAX_PARALLEL_JOBS=1
```

ถ้า webhook URL เคยหลุดหรือเคยส่งในแชท ให้ rotate webhook ก่อนใช้จริง

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

โปรเจกต์ pin Python 3.12.8 เพื่อหลีกเลี่ยงปัญหา `audioop` กับ `discord.py`

## Discord Developer Portal

เปิด intents ที่แนะนำ:

- Server Members Intent
- Message Content Intent

สิทธิ์บอทที่ควรมี:

- Manage Channels
- Send Messages
- Embed Links
- Attach Files
- Read Message History
- Use Slash Commands

## วิธีใช้

```text
/setup category:<หมวดหมู่> channel:<ช่องที่จะส่ง panel> image_url:<ลิงก์รูป embed>
```

หลังจากนั้นเลือกโหมดจาก dropdown แล้วอัปโหลดไฟล์ใน ticket channel

## หมายเหตุเรื่องความเสถียร

Normalize/Rebuild Mode ลดปัญหา addon โครงสร้างแปลก เพราะไม่แก้ pack เดิมตรง ๆ แต่สร้าง pack ใหม่ตามทรงมาตรฐาน Seraphim อย่างไรก็ตาม addon ที่พึ่ง script เดิมเพื่อคุม animation variable แบบ dynamic อาจต้องทดสอบในเกมและตรวจ `NORMALIZE_REPORT.txt`
