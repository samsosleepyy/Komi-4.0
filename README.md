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
4. หลังเลือก item แล้ว บอทจะแสดงขั้นตอนเลือกช่องสวมใส่:
   - คงไอเท็มใส่ช่องเดิมและช่องเดียว: รวมเข้า UI อย่างเดียว และตอนกดใช้จะใส่ทันทีโดยไม่ถามช่อง
   - ทำให้ไอเท็มที่เลือกใส่ได้ทุกช่อง: สร้าง head/chest/legs/feet และถามช่องตอนใช้ UI
   - เปลี่ยนช่องที่จะใส่และรวมเป็น UI: เลือกช่องเองได้มากกว่า 1 ช่อง
5. ดึงเฉพาะ asset สำคัญ:
   - item metadata
   - attachable metadata
   - geometry ที่ attachable อ้างถึง
   - animations ที่ attachable อ้างถึง
   - render controllers custom ถ้ามี
   - texture skin/model
   - icon texture จาก `item_texture.json`
6. สร้าง addon ใหม่ทั้งหมดเป็นโครงสร้างมาตรฐาน:
   - `BP_auto_ui`
   - `RP_auto_ui`
7. สร้าง item จริงเฉพาะช่องที่ผู้ใช้เลือก เช่น ช่องเดิมช่องเดียว / ครบ 4 ช่อง / ช่อง custom
8. ซ่อนไอเท็ม armor จริงทั้งหมดจาก Creative ด้วย `menu_category: {"category":"none"}`
9. สร้างไอเท็ม UI อันเดียวที่มองเห็นในหมวด Equipment
10. สร้าง `scripts/auto_ui_system.js` สำหรับ UI + Eldoria-style `equippable.setEquipment()`
11. ถ้า item มีช่องเดียว ตอนใช้ UI จะใส่ทันทีโดยไม่ถามช่อง
12. ถ้า item มีหลายช่อง ตอนใช้ UI จะให้เลือกช่องตามที่เปิดไว้เท่านั้น
13. ก่อนใส่ชิ้นใหม่ จะลบเฉพาะเกราะจาก addon ที่สร้างใหม่นี้ออกจากช่องอื่นทั้งหมด
14. ถ้าช่องปลายทางมีของอยู่ จะถามก่อนทับ

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

โหมดนี้รองรับ preview/edit ก่อนเริ่มสร้างจริง:

- รับ addon 2-5 ไฟล์
- หลังอัปโหลดไฟล์ บอทจะยังไม่เริ่มรวมทันที
- บอทจะแสดง embed preview ที่มี:
  - ชื่อ pack ที่จะสร้าง
  - description/version/icon status ของแต่ละ addon
  - รายการ item ทั้งหมด พร้อมระบุว่าอยู่ใน addon ไหนและ path ไฟล์ไหน
- ปุ่ม **แก้ไขชื่อแพค** เปิด modal ให้ใส่ชื่อ output pack ใหม่
- ปุ่ม **แก้ไขรูป** จะ disabled ปุ่มชั่วคราวและรอให้ผู้ใช้อัปโหลดรูป pack icon ใน ticket
- ปุ่ม **เริ่มสร้าง** จะเริ่มรวม addon โดยใช้ชื่อแพคและรูปที่แก้ไว้
- รวมเป็น `BP_merged` และ `RP_merged`
- สุ่ม UUID ใหม่
- ถ้ามี pack icon จะใส่เป็น `pack_icon.png` ทั้ง BP/RP และ patch manifest header.icon
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

## Creative inventory visibility update

- UI selector items are shown in the top-level `Equipment` category with no armor sub-group.
- Generated/copied wearable armor items have no `menu_category`, so they do not appear in Creative tabs and are intended for `/give` or `replaceitem` only.

## Update: hidden armor item category
Wearable/generated armor items now use:

```json
"menu_category": { "category": "none" }
```

This matches creator-tool output that hides items from every Creative category while keeping them usable by `/give` and `replaceitem`. The UI selector item remains visible under the top-level Equipment category.

### Eldoria-style hidden item equip fix

Hidden wearable items use `menu_category: { "category": "none" }` like Eldoria.
The generated UI now equips those hidden items through the Script API first:
`equippable.setEquipment(slot, new ItemStack(itemId, 1))`, then falls back to `/replaceitem` only if the API path fails.
This avoids the Bedrock issue where hidden custom wearables can remain give-able but fail when inserted by command.
