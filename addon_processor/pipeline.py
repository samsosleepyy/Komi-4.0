from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4

SLOTS = [
    ("head", "หัว", "slot.armor.head", "Head"),
    ("chest", "ตัว", "slot.armor.chest", "Chest"),
    ("legs", "กางเกง", "slot.armor.legs", "Legs"),
    ("feet", "รองเท้า", "slot.armor.feet", "Feet"),
]

SLOT_BY_WEARABLE = {
    "slot.armor.head": "head",
    "slot.armor.chest": "chest",
    "slot.armor.body": "chest",
    "slot.armor.legs": "legs",
    "slot.armor.feet": "feet",
}

@dataclass
class AddonItemCandidate:
    index: int
    identifier: str
    display_name: str
    file_path: str
    wearable_slot: str
    icon: str

@dataclass
class AddonInspection:
    source_path: str
    bp_dir: str
    rp_dir: str
    candidates: List[AddonItemCandidate]

class AddonError(Exception):
    pass

def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))

def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def _safe_extract(zip_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            member = Path(info.filename)
            if info.filename.startswith("/") or ".." in member.parts:
                raise AddonError(f"Unsafe zip member: {info.filename}")
            target = out_dir / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

def _find_packs(root: Path) -> Tuple[Path, Path]:
    bp: Optional[Path] = None
    rp: Optional[Path] = None
    for manifest_path in root.rglob("manifest.json"):
        try:
            data = _read_json(manifest_path)
        except Exception:
            continue
        modules = data.get("modules", [])
        types = {m.get("type") for m in modules if isinstance(m, dict)}
        if "data" in types and bp is None:
            bp = manifest_path.parent
        if "resources" in types and rp is None:
            rp = manifest_path.parent
    if bp is None:
        raise AddonError("ไม่พบ Behavior Pack manifest ที่มี module type=data")
    if rp is None:
        raise AddonError("ไม่พบ Resource Pack manifest ที่มี module type=resources")
    return bp, rp

def _display_name_from_item(item_data: Dict[str, Any], fallback: str) -> str:
    comps = item_data.get("minecraft:item", {}).get("components", {})
    display = comps.get("minecraft:display_name")
    if isinstance(display, dict) and display.get("value"):
        return str(display["value"])
    if isinstance(display, str):
        return display
    return fallback

def _icon_from_item(item_data: Dict[str, Any]) -> str:
    comps = item_data.get("minecraft:item", {}).get("components", {})
    icon = comps.get("minecraft:icon")
    if isinstance(icon, str):
        return icon
    if isinstance(icon, dict):
        for key in ("texture", "default"):
            if isinstance(icon.get(key), str):
                return icon[key]
    return ""

def _scan_candidates(root: Path, bp: Path) -> List[AddonItemCandidate]:
    items_dir = bp / "items"
    candidates: List[AddonItemCandidate] = []
    if not items_dir.exists():
        raise AddonError("ไม่พบโฟลเดอร์ BP/items")
    for path in sorted(items_dir.rglob("*.json")):
        try:
            data = _read_json(path)
        except Exception:
            continue
        item = data.get("minecraft:item")
        if not isinstance(item, dict):
            continue
        desc = item.get("description", {})
        comps = item.get("components", {})
        wearable = comps.get("minecraft:wearable")
        if not isinstance(wearable, dict):
            continue
        identifier = desc.get("identifier")
        if not identifier:
            continue
        slot = wearable.get("slot", "")
        display = _display_name_from_item(data, identifier.split(":")[-1])
        candidates.append(AddonItemCandidate(
            index=len(candidates),
            identifier=identifier,
            display_name=display,
            file_path=str(path.relative_to(root)).replace(os.sep, "/"),
            wearable_slot=slot,
            icon=_icon_from_item(data),
        ))
    return candidates

def inspect_addon(addon_path: str, work_dir: str) -> AddonInspection:
    src = Path(addon_path)
    if not src.exists():
        raise AddonError("ไม่พบไฟล์ addon")
    if not zipfile.is_zipfile(src):
        raise AddonError("ไฟล์นี้ไม่ใช่ zip/mcaddon ที่เปิดได้")
    root = Path(work_dir) / "inspect"
    if root.exists():
        shutil.rmtree(root)
    _safe_extract(src, root)
    bp, rp = _find_packs(root)
    candidates = _scan_candidates(root, bp)
    if not candidates:
        raise AddonError("ไม่พบไอเท็มเกราะที่มี minecraft:wearable")
    return AddonInspection(
        source_path=str(src),
        bp_dir=str(bp.relative_to(root)).replace(os.sep, "/"),
        rp_dir=str(rp.relative_to(root)).replace(os.sep, "/"),
        candidates=candidates,
    )

def _safe_id(value: str, default: str = "item") -> str:
    value = value.lower().replace("-", "_")
    value = re.sub(r"[^a-z0-9_]+", "_", value).strip("_")
    value = re.sub(r"_+", "_", value)
    return value or default

def _identifier_parts(identifier: str) -> Tuple[str, str]:
    if ":" in identifier:
        ns, name = identifier.split(":", 1)
    else:
        ns, name = "addon", identifier
    return _safe_id(ns, "addon"), _safe_id(name, "item")

def _find_attachable_for_item(rp: Path, item_id: str) -> Optional[Path]:
    attach_dir = rp / "attachables"
    if not attach_dir.exists():
        return None
    for path in sorted(attach_dir.rglob("*.json")):
        try:
            data = _read_json(path)
        except Exception:
            continue
        desc = data.get("minecraft:attachable", {}).get("description", {})
        item_map = desc.get("item", {})
        if isinstance(item_map, dict) and item_id in item_map:
            return path
        if desc.get("identifier") == item_id:
            return path
    return None

def _load_item_texture_data(rp: Path) -> Tuple[Path, Dict[str, Any]]:
    path = rp / "textures" / "item_texture.json"
    if path.exists():
        try:
            return path, _read_json(path)
        except Exception:
            pass
    data = {"resource_pack_name": "vanilla", "texture_name": "atlas.items", "texture_data": {}}
    return path, data

def _textures_value_to_first_path(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return _textures_value_to_first_path(value[0])
    if isinstance(value, dict):
        for key in ("textures", "path", "texture"):
            if key in value:
                return _textures_value_to_first_path(value[key])
    return None

def _copy_texture_path(rp: Path, texture_ref: str, new_ref: str) -> str:
    # texture_ref normally has no extension, e.g. textures/skin/foo
    for ext in (".png", ".tga"):
        src = rp / (texture_ref + ext)
        if src.exists():
            dst = rp / (new_ref + ext)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return new_ref
    return texture_ref

def _find_geometry_file(rp: Path, geometry_id: str) -> Optional[Path]:
    models = rp / "models"
    if not models.exists():
        return None
    for path in sorted(models.rglob("*.json")):
        try:
            data = _read_json(path)
        except Exception:
            continue
        geometries = data.get("minecraft:geometry")
        if isinstance(geometries, list):
            for geo in geometries:
                desc = geo.get("description", {}) if isinstance(geo, dict) else {}
                if desc.get("identifier") == geometry_id:
                    return path
    return None

def _copy_geometry(rp: Path, old_id: str, safe_name: str, slot_key: str) -> str:
    path = _find_geometry_file(rp, old_id)
    if not path:
        return old_id
    data = _read_json(path)
    new_id = f"geometry.{safe_name}_{slot_key}"
    geos = data.get("minecraft:geometry")
    if isinstance(geos, list):
        for geo in geos:
            if isinstance(geo, dict):
                desc = geo.get("description", {})
                if desc.get("identifier") == old_id:
                    desc["identifier"] = new_id
    new_path = path.parent / f"geometry.{safe_name}_{slot_key}.json"
    _write_json(new_path, data)
    return new_id

def _find_animation_file(rp: Path, animation_id: str) -> Optional[Path]:
    anim_dir = rp / "animations"
    if not anim_dir.exists():
        return None
    for path in sorted(anim_dir.rglob("*.json")):
        try:
            data = _read_json(path)
        except Exception:
            continue
        animations = data.get("animations")
        if isinstance(animations, dict) and animation_id in animations:
            return path
    return None

def _copy_animation(rp: Path, old_id: str, safe_name: str, slot_key: str) -> str:
    path = _find_animation_file(rp, old_id)
    if not path:
        return old_id
    data = _read_json(path)
    animations = data.get("animations")
    if not isinstance(animations, dict) or old_id not in animations:
        return old_id
    suffix = old_id.split(".", 1)[0] if "." in old_id else "animation"
    new_id = f"{suffix}.{safe_name}_{slot_key}"
    animations[new_id] = animations.pop(old_id)
    new_path = path.parent / f"{safe_name}_{slot_key}.animation.json"
    _write_json(new_path, data)
    return new_id

def _patch_attachable_for_slot(rp: Path, src_attach: Optional[Path], original_id: str, new_id: str, safe_name: str, slot_key: str) -> Optional[Path]:
    if not src_attach or not src_attach.exists():
        return None
    data = _read_json(src_attach)
    desc = data.get("minecraft:attachable", {}).get("description", {})
    desc["identifier"] = new_id
    desc["item"] = {new_id: "query.owner_identifier=='minecraft:player'"}

    textures = desc.get("textures")
    if isinstance(textures, dict):
        for key, value in list(textures.items()):
            if isinstance(value, str) and value.startswith("textures/") and "enchanted" not in value:
                new_ref = f"textures/skin/{safe_name}_{slot_key}"
                textures[key] = _copy_texture_path(rp, value, new_ref)

    geometries = desc.get("geometry")
    if isinstance(geometries, dict):
        for key, value in list(geometries.items()):
            if isinstance(value, str) and value.startswith("geometry."):
                geometries[key] = _copy_geometry(rp, value, safe_name, slot_key)

    animations = desc.get("animations")
    if isinstance(animations, dict):
        for key, value in list(animations.items()):
            if isinstance(value, str) and value.startswith("animation."):
                animations[key] = _copy_animation(rp, value, safe_name, slot_key)

    out = src_attach.parent / f"{safe_name}_{slot_key}.json"
    _write_json(out, data)
    return out

def _patch_item_texture(rp: Path, original_icon: str, new_icon: str, safe_name: str, slot_key: str) -> None:
    path, data = _load_item_texture_data(rp)
    tex_data = data.setdefault("texture_data", {})
    original = tex_data.get(original_icon)
    if original is None:
        tex_data[new_icon] = {"textures": f"textures/item/{safe_name}_{slot_key}"}
        _write_json(path, data)
        return
    copied = json.loads(json.dumps(original))
    texture_ref = _textures_value_to_first_path(copied)
    if texture_ref:
        new_ref = f"textures/item/{safe_name}_{slot_key}"
        copied["textures"] = _copy_texture_path(rp, texture_ref, new_ref)
    tex_data[new_icon] = copied
    _write_json(path, data)

def _patch_manifests(root: Path, bp: Path, rp: Path) -> None:
    manifests = [p for p in root.rglob("manifest.json")]
    uuid_map: Dict[str, str] = {}
    for path in manifests:
        data = _read_json(path)
        header = data.get("header", {})
        if isinstance(header.get("uuid"), str):
            uuid_map[header["uuid"]] = str(uuid4())
        for mod in data.get("modules", []):
            if isinstance(mod, dict) and isinstance(mod.get("uuid"), str):
                uuid_map[mod["uuid"]] = str(uuid4())
    for path in manifests:
        data = _read_json(path)
        header = data.get("header", {})
        if header.get("uuid") in uuid_map:
            header["uuid"] = uuid_map[header["uuid"]]
        if header.get("min_engine_version", [0,0,0]) < [1, 20, 50]:
            header["min_engine_version"] = [1, 20, 50]
        for mod in data.get("modules", []):
            if isinstance(mod, dict) and mod.get("uuid") in uuid_map:
                mod["uuid"] = uuid_map[mod["uuid"]]
        for dep in data.get("dependencies", []):
            if isinstance(dep, dict) and dep.get("uuid") in uuid_map:
                dep["uuid"] = uuid_map[dep["uuid"]]
        _write_json(path, data)

    bp_manifest = _read_json(bp / "manifest.json")
    bp_manifest["header"]["description"] = "Addon UI generated by Discord bot"
    modules = bp_manifest.setdefault("modules", [])
    if not any(m.get("type") == "script" for m in modules if isinstance(m, dict)):
        modules.append({
            "type": "script",
            "language": "javascript",
            "entry": "scripts/main.js",
            "uuid": str(uuid4()),
            "version": [1, 0, 0],
        })
    deps = bp_manifest.setdefault("dependencies", [])
    if not any(d.get("module_name") == "@minecraft/server" for d in deps if isinstance(d, dict)):
        deps.append({"module_name": "@minecraft/server", "version": "1.10.0"})
    if not any(d.get("module_name") == "@minecraft/server-ui" for d in deps if isinstance(d, dict)):
        deps.append({"module_name": "@minecraft/server-ui", "version": "1.2.0"})
    _write_json(bp / "manifest.json", bp_manifest)

def _merge_main_js(bp: Path) -> None:
    scripts = bp / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    main = scripts / "main.js"
    import_line = 'import "./auto_ui_system.js";'
    if main.exists():
        text = main.read_text(encoding="utf-8")
        if import_line not in text:
            text = import_line + "\n" + text
        main.write_text(text, encoding="utf-8")
    else:
        main.write_text(import_line + "\n", encoding="utf-8")

def _generate_ui_script(selector_id: str, armors: List[Dict[str, Any]], all_item_ids: List[str]) -> str:
    data = json.dumps({"menuItem": selector_id, "armors": armors, "allItems": all_item_ids}, ensure_ascii=False, indent=2)
    return f'''import {{ world, system, EquipmentSlot }} from "@minecraft/server";
import {{ ActionFormData, MessageFormData }} from "@minecraft/server-ui";

const CONFIG = {data};
const SLOTS = [
  {{ key: "head", label: "หัว", commandSlot: "slot.armor.head", equipmentSlot: EquipmentSlot.Head }},
  {{ key: "chest", label: "ตัว", commandSlot: "slot.armor.chest", equipmentSlot: EquipmentSlot.Chest }},
  {{ key: "legs", label: "กางเกง", commandSlot: "slot.armor.legs", equipmentSlot: EquipmentSlot.Legs }},
  {{ key: "feet", label: "รองเท้า", commandSlot: "slot.armor.feet", equipmentSlot: EquipmentSlot.Feet }},
];
const ALL_ADDON_ARMOR_ITEMS = new Set(CONFIG.allItems);

function getEquippedItem(player, slot) {{
  try {{
    const equipment = player.getComponent("minecraft:equippable");
    if (!equipment) return undefined;
    return equipment.getEquipment(slot.equipmentSlot);
  }} catch (error) {{
    return undefined;
  }}
}}

function isAddonArmor(item) {{
  return !!item && ALL_ADDON_ARMOR_ITEMS.has(item.typeId);
}}

function getItemLabel(item) {{
  if (!item || item.typeId === "minecraft:air") return "";
  if (item.nameTag) return item.nameTag;
  return item.typeId;
}}

async function runPlayerCommand(player, command) {{
  try {{
    if (typeof player.runCommandAsync === "function") await player.runCommandAsync(command);
    else player.runCommand(command);
    return true;
  }} catch (error) {{
    return false;
  }}
}}

async function clearArmorSlot(player, slot) {{
  const ok = await runPlayerCommand(player, `replaceitem entity @s ${{slot.commandSlot}} 0 minecraft:air 1 0`);
  if (ok) return true;
  try {{
    const equipment = player.getComponent("minecraft:equippable");
    if (!equipment) return false;
    equipment.setEquipment(slot.equipmentSlot, undefined);
    return true;
  }} catch (error) {{
    return false;
  }}
}}

async function clearAllAddonArmorFromOtherSlots(player, targetSlot) {{
  for (const slot of SLOTS) {{
    if (slot.key === targetSlot.key) continue;
    const existing = getEquippedItem(player, slot);
    if (isAddonArmor(existing)) await clearArmorSlot(player, slot);
  }}
}}

async function replaceArmor(player, slot, itemId) {{
  return await runPlayerCommand(player, `replaceitem entity @s ${{slot.commandSlot}} 0 ${{itemId}} 1 0`);
}}

async function showArmorMenu(player) {{
  const form = new ActionFormData().title("รวมไอเท็มใส่ UI").body("เลือกไอเท็มที่ต้องการใส่");
  for (const armor of CONFIG.armors) form.button(armor.name, armor.icon || undefined);
  const response = await form.show(player);
  if (response.canceled || response.selection === undefined) return;
  await showSlotMenu(player, CONFIG.armors[response.selection]);
}}

async function showSlotMenu(player, armor) {{
  const form = new ActionFormData().title(armor.name).body("เลือกช่องที่จะใส่");
  for (const slot of SLOTS) form.button(slot.label);
  const response = await form.show(player);
  if (response.canceled || response.selection === undefined) return;
  const slot = SLOTS[response.selection];
  const itemId = armor.items[slot.key];
  const existing = getEquippedItem(player, slot);
  if (existing && existing.typeId !== "minecraft:air") {{
    await showOverwriteMenu(player, armor, slot, existing, itemId);
    return;
  }}
  await clearAllAddonArmorFromOtherSlots(player, slot);
  const ok = await replaceArmor(player, slot, itemId);
  player.sendMessage(ok ? `§aใส่ ${{armor.name}} ที่ช่อง${{slot.label}}แล้ว` : "§cใส่ไอเท็มไม่สำเร็จ");
}}

async function showOverwriteMenu(player, armor, slot, existing, itemId) {{
  const existingName = getItemLabel(existing);
  const note = isAddonArmor(existing)
    ? "ไอเท็มนี้มาจาก addon นี้"
    : "ไอเท็มนี้ไม่ใช่ addon นี้ แต่จะหายไปถ้าทับ";
  const form = new MessageFormData()
    .title("ช่องนี้มีไอเท็มอยู่แล้ว")
    .body(`ช่อง${{slot.label}}มี ${{existingName}} อยู่แล้ว\n${{note}}\n\nต้องการทับเลยไหม?\n§cไอเท็มที่ถูกทับจะหายไป`)
    .button1("ทับเลย")
    .button2("ยกเลิก");
  const response = await form.show(player);
  if (response.canceled || response.selection !== 0) return;
  await clearAllAddonArmorFromOtherSlots(player, slot);
  const ok = await replaceArmor(player, slot, itemId);
  player.sendMessage(ok ? `§aทับไอเท็มเดิมและใส่ ${{armor.name}} ที่ช่อง${{slot.label}}แล้ว` : "§cใส่ไอเท็มไม่สำเร็จ");
}}

world.afterEvents.itemUse.subscribe((event) => {{
  const player = event.source;
  const item = event.itemStack;
  if (!player || !item || item.typeId !== CONFIG.menuItem) return;
  system.run(() => {{
    showArmorMenu(player).catch(() => {{
      try {{ player.sendMessage("§cไม่สามารถเปิด UI ได้ ลองกดใช้อีกครั้ง"); }} catch (error) {{}}
    }});
  }});
}});
'''

def _lang_label(candidate: AddonItemCandidate, slot_key: str) -> str:
    slot_th = next((label for key, label, _, _ in SLOTS if key == slot_key), slot_key)
    return f"{candidate.display_name} ({slot_th})"

def convert_addon(addon_path: str, selected_identifiers: List[str], work_dir: str) -> str:
    src = Path(addon_path)
    root = Path(work_dir) / "convert"
    if root.exists():
        shutil.rmtree(root)
    _safe_extract(src, root)
    bp, rp = _find_packs(root)
    candidates = _scan_candidates(root, bp)
    selected = [c for c in candidates if c.identifier in set(selected_identifiers)]
    if not selected:
        raise AddonError("ไม่ได้เลือกไอเท็มที่แปลงได้")

    _patch_manifests(root, bp, rp)
    items_dir = bp / "items"
    selector_ns = "addon_ui"
    selector_id = f"{selector_ns}:selector"

    armors: List[Dict[str, Any]] = []
    all_item_ids: List[str] = []
    lang_lines: List[str] = [f"item.{selector_id}.name=รวมไอเท็มใส่ UI"]

    for candidate in selected:
        item_path = root / candidate.file_path
        original_data = _read_json(item_path)
        orig_ns, orig_name = _identifier_parts(candidate.identifier)
        safe_name = f"{orig_ns}_{orig_name}"
        new_ns = _safe_id(f"{orig_ns}_ui", "addon_ui")
        src_attach = _find_attachable_for_item(rp, candidate.identifier)
        armor_entry = {"name": candidate.display_name, "icon": "", "items": {}}
        original_icon = candidate.icon or safe_name

        for slot_key, slot_label, wearable_slot, _slot_enum in SLOTS:
            new_item_name = f"{orig_name}_{slot_key}"
            new_item_id = f"{new_ns}:{new_item_name}"
            new_icon = f"{safe_name}_{slot_key}"
            all_item_ids.append(new_item_id)
            armor_entry["items"][slot_key] = new_item_id
            if not armor_entry["icon"]:
                armor_entry["icon"] = f"textures/item/{new_icon}"

            new_item = json.loads(json.dumps(original_data))
            item = new_item.setdefault("minecraft:item", {})
            desc = item.setdefault("description", {})
            desc["identifier"] = new_item_id
            desc.pop("menu_category", None)
            comps = item.setdefault("components", {})
            comps["minecraft:wearable"] = {"slot": wearable_slot}
            comps["minecraft:icon"] = new_icon
            comps["minecraft:display_name"] = {"value": _lang_label(candidate, slot_key)}
            out_item = items_dir / f"{new_item_name}.json"
            _write_json(out_item, new_item)

            _patch_item_texture(rp, original_icon, new_icon, f"{safe_name}", slot_key)
            _patch_attachable_for_slot(rp, src_attach, candidate.identifier, new_item_id, safe_name, slot_key)
            lang_lines.append(f"item.{new_item_id}.name={_lang_label(candidate, slot_key)}")
            lang_lines.append(f"item.{new_item_name}.name={_lang_label(candidate, slot_key)}")

        # Hide original selected item from creative list.
        try:
            original_data["minecraft:item"].get("description", {}).pop("menu_category", None)
            _write_json(item_path, original_data)
        except Exception:
            pass
        armors.append(armor_entry)

    # Selector visible item.
    selector_item = {
        "format_version": "1.20.50",
        "minecraft:item": {
            "description": {
                "identifier": selector_id,
                "menu_category": {"category": "equipment", "group": "itemGroup.name.leggings"},
            },
            "components": {
                "minecraft:icon": selected[0].icon or "diamond",
                "minecraft:display_name": {"value": "รวมไอเท็มใส่ UI"},
                "minecraft:max_stack_size": 1,
                "minecraft:allow_off_hand": True,
            },
        },
    }
    _write_json(items_dir / "addon_ui_selector.json", selector_item)

    scripts = bp / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "auto_ui_system.js").write_text(_generate_ui_script(selector_id, armors, all_item_ids), encoding="utf-8")
    _merge_main_js(bp)

    texts = rp / "texts"
    texts.mkdir(parents=True, exist_ok=True)
    for lang_name in ["en_US.lang", "th_TH.lang"]:
        (texts / lang_name).write_text("\n".join(dict.fromkeys(lang_lines)) + "\n", encoding="utf-8")
    (texts / "languages.json").write_text(json.dumps(["en_US", "th_TH"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    out_path = Path(work_dir) / f"converted_{src.stem}.mcaddon"
    if out_path.exists():
        out_path.unlink()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(root).as_posix())
    return str(out_path)
