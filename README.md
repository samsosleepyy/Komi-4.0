# Addon UI Discord Bot

บอท Discord สำหรับแปลง Minecraft Bedrock addon และรวม addon โดยเตรียมไว้สำหรับรันบน Render

## ฟีเจอร์หลัก

- จำกัดให้บอททำงานเฉพาะ server ID ที่กำหนดใน `ALLOWED_GUILDS`
- ถ้าอยู่ server นอก allowlist จะรายงานไป webhook และออกทันที
- `/setup` ใช้ได้เฉพาะ Administrator
- Panel เป็น dropdown:
  - 🎨 รวมไอเท็มเป็น UI
  - 📦 รวมแอดออน
- Ticket channel ส่วนตัวชั่วคราว พร้อม cleanup temp folder หลังจบงาน/หมดเวลา
- ส่ง log และไฟล์ไป webhook
- จำกัดขนาดไฟล์อัปโหลดและตรวจ zip ก่อนแตกไฟล์ เพื่อลดความเสี่ยง zip bomb / disk เต็ม
- ไม่มีระบบ point ตามที่ขอ

## รวมไอเท็มเป็น UI: Normalize/Rebuild Mode

เวอร์ชันนี้ใช้แนวคิดจากโครงสร้าง Seraphim template ที่แปลง UI ได้เสถียร ไม่ patch addon เดิมโดยตรง แต่จะ:

1. แตก addon ต้นฉบับ
2. ตรวจหา Behavior Pack manifest ที่มี module `type: data` แล้วอ่าน `BP/items/*.json` ที่มี `minecraft:wearable` หรือ `minecraft:allow_off_hand`
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

ผลลัพธ์จะไม่มีไฟล์ report `.txt` อยู่ใน pack แล้ว ระบบจะส่งข้อความ report ลง webhook แทน

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
- ไม่ใส่ `MERGE_REPORT.txt` ใน pack แล้ว แต่ส่ง report ลง webhook เป็นข้อความ

## Environment Variables บน Render

```env
DISCORD_TOKEN=token ของบอท
WEBHOOK_URL=webhook สำหรับ log
ALLOWED_GUILDS=1420339720277463112,1441795602550882334
MAX_PARALLEL_JOBS=1

# ปกติไม่จำเป็นต้องเปิด Server Members Intent
ENABLE_MEMBER_INTENT=0

# Upload / zip safety limits
MAX_UPLOAD_BYTES=52428800
MAX_MERGE_TOTAL_UPLOAD_BYTES=157286400
MAX_ZIP_MEMBERS=3000
MAX_ZIP_UNCOMPRESSED_BYTES=262144000
MAX_ZIP_SINGLE_FILE_BYTES=83886080
MAX_ZIP_MEMBER_NAME_LENGTH=240

# Ticket cleanup
INITIAL_TICKET_TTL=180
ACTIVE_TICKET_TTL=900
FINISHED_TICKET_TTL=60
PROCESSING_TICKET_TTL=3600
STALE_TICKET_CLEANUP_MINUTES=30
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

เปิด intents ที่จำเป็น/แนะนำ:

- Message Content Intent: จำเป็น เพราะบอทต้องอ่านไฟล์แนบที่ผู้ใช้อัปโหลดใน ticket
- Server Members Intent: ไม่จำเป็นในค่าเริ่มต้นของเวอร์ชันนี้ เปิดเฉพาะถ้าตั้ง `ENABLE_MEMBER_INTENT=1` และเพิ่มฟีเจอร์ที่ต้องอ่าน member list จริง ๆ

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

Normalize/Rebuild Mode ลดปัญหา addon โครงสร้างแปลก เพราะไม่แก้ pack เดิมตรง ๆ แต่สร้าง pack ใหม่ตามทรงมาตรฐาน Seraphim อย่างไรก็ตาม addon ที่พึ่ง script เดิมเพื่อคุม animation variable แบบ dynamic อาจต้องทดสอบในเกมและดูข้อความ report ใน webhook

ถ้าเจอข้อความ `ไม่พบ Behavior Pack manifest ที่มี module type=data` ในโหมดรวมไอเท็มเป็น UI แปลว่าไฟล์นั้นไม่มี Behavior Pack ที่บอทอ่าน `BP/items/*.json` ได้ หรือเป็น Resource Pack/texture pack อย่างเดียว กรณีนี้ถือว่าจำเป็นต้องแจ้งผู้ใช้ให้อัปโหลด `.mcaddon` ที่มีทั้ง BP และ RP เพราะโหมด UI ต้องใช้ item definition จาก Behavior Pack เพื่อสร้าง wearable/offhand item ใหม่

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

## Update: slot stacking setting and image downsizing

- Merge Addons mode keeps generated/merged BP item entries visible in top-level `Equipment`.
- Combine UI mode keeps wearable target items hidden with `menu_category: { "category": "none" }`; only the UI selector item is visible in `Equipment`.
- The generated in-game UI now has a top **ตั้งค่า** button using a real modal toggle for “ใส่ซ้อนกันได้”.
  - Default: stacking is off, so equipping another slot removes this addon’s armor from the other slots.
  - When enabled: armor from the same UI can stay equipped on multiple slots at the same time.
- The generated in-game UI has a red **ถอดออก** button under settings. If only one addon armor piece is equipped it removes immediately; if multiple are equipped it asks which one to remove.
- The UI selector item uses the output pack icon as its inventory icon and hides its held-hand render with `minecraft:render_offsets`.
- Uploaded pack icons, copied pack icons, and item icons are automatically downscaled to 128x128 to reduce Discord upload size and avoid `413 Payload Too Large` where possible.
- UI conversion reuses the same model texture file across generated head/chest/legs/feet variants when they come from the same source item, reducing duplicated texture size.


## Update: safety and ticket cleanup

- เพิ่มตัวแปรจำกัดขนาดไฟล์อัปโหลดต่อไฟล์และรวมทั้งงาน merge
- `_safe_extract()` ตรวจจำนวนไฟล์ใน zip, ขนาดหลังแตก, ขนาดไฟล์เดี่ยว, path traversal และชื่อไฟล์ที่ยาวผิดปกติ
- หลังส่งไฟล์สำเร็จหรือ ticket หมดเวลา ระบบจะลบ temp work directory อัตโนมัติ
- ถ้าผู้ใช้อัปโหลดแล้วไม่กด dropdown/button ต่อ ticket จะถูกลบหลัง `ACTIVE_TICKET_TTL`
- ระหว่างที่บอทกำลังแปลงหรือรวม addon อยู่ ticket จะถูกล็อกไว้ด้วย `PROCESSING_TICKET_TTL` เพื่อไม่ให้ถูกลบกลางงาน
- ถ้าบอท restart แล้วเหลือ ticket channel เก่าที่ state หาย ระบบจะลบ ticket เก่าใน category ที่ตั้งไว้หลังเกิน `STALE_TICKET_CLEANUP_MINUTES`
- `/setup` แก้ข้อความ merge report ให้ตรงกับพฤติกรรมจริง: report ถูกส่งไป webhook ไม่ได้ฝัง `MERGE_REPORT.txt` ใน pack
