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

try:
    from PIL import Image
except Exception:  # Pillow is optional at import time; resize helpers no-op if unavailable.
    Image = None

ARMOR_SLOTS = [
    ("head", "หัว", "slot.armor.head", "Head", "helmet"),
    ("chest", "ตัว", "slot.armor.chest", "Chest", "chest"),
    ("legs", "กางเกง", "slot.armor.legs", "Legs", "leg"),
    ("feet", "รองเท้า", "slot.armor.feet", "Feet", "boot"),
]
OFFHAND_SLOT = ("offhand", "มือซ้าย", "slot.weapon.offhand", "Offhand", "offhand")
SLOTS = ARMOR_SLOTS + [OFFHAND_SLOT]

SLOT_BY_WEARABLE = {
    "slot.armor.head": "head",
    "slot.armor.chest": "chest",
    "slot.armor.body": "chest",
    "slot.armor.legs": "legs",
    "slot.armor.feet": "feet",
    "slot.weapon.offhand": "offhand",
}

ARMOR_SLOT_KEYS = [slot[0] for slot in ARMOR_SLOTS]
SLOT_KEYS = [slot[0] for slot in SLOTS]
SLOTS_BY_KEY = {slot[0]: slot for slot in SLOTS}


def _slot_keys_for_candidate(candidate: "AddonItemCandidate", slot_mode: str = "all", custom_slots: Optional[List[str]] = None) -> List[str]:
    """Return the output wearable slots for one selected source item.

    original: keep only the source item's original wearable slot.
    all: create all four armor variants. Offhand is kept for original/custom only.
    custom: create only the user-selected slots, including offhand when selected.
    """
    mode = (slot_mode or "all").lower()
    if mode == "original":
        return [SLOT_BY_WEARABLE.get(candidate.wearable_slot, "legs")]
    if mode == "custom":
        slots = [s for s in (custom_slots or []) if s in SLOT_KEYS]
        if not slots:
            raise AddonError("ยังไม่ได้เลือกช่องที่จะให้ไอเท็มใส่ได้")
        return list(dict.fromkeys(slots))
    return list(ARMOR_SLOT_KEYS)


TEXTURE_EXTS = (".png", ".tga", ".jpg", ".jpeg", ".webp")
ICON_SIZE = 128
MAX_ZIP_MEMBERS = int(os.getenv("MAX_ZIP_MEMBERS", "2000"))
MAX_ZIP_UNCOMPRESSED_BYTES = int(os.getenv("MAX_ZIP_UNCOMPRESSED_BYTES", str(150 * 1024 * 1024)))
MAX_ZIP_SINGLE_FILE_BYTES = int(os.getenv("MAX_ZIP_SINGLE_FILE_BYTES", str(50 * 1024 * 1024)))
MAX_ZIP_MEMBER_NAME_LENGTH = int(os.getenv("MAX_ZIP_MEMBER_NAME_LENGTH", "240"))


def _resize_image_file(path: Path, size: int = ICON_SIZE) -> bool:
    """Downscale pack icons and item icons so generated addons stay small.

    Only shrinks images larger than size x size. PNG is used for converted
    pack/custom icons elsewhere; for existing icon files this preserves the
    original extension where possible.
    """
    if Image is None or not path.exists() or path.suffix.lower() not in TEXTURE_EXTS:
        return False
    try:
        with Image.open(path) as img:
            if img.width <= size and img.height <= size:
                return False
            img = img.convert("RGBA")
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
            save_path = path
            save_kwargs = {}
            if path.suffix.lower() in (".jpg", ".jpeg"):
                canvas = canvas.convert("RGB")
                save_kwargs.update({"quality": 85, "optimize": True})
            elif path.suffix.lower() == ".png":
                save_kwargs.update({"optimize": True})
            canvas.save(save_path, **save_kwargs)
            return True
    except Exception:
        return False


def _resize_item_icon_tree(rp: Path) -> None:
    for folder_name in ("item", "items"):
        root = rp / "textures" / folder_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXTURE_EXTS:
                _resize_image_file(path)


def _write_resized_icon(src: Path, dst: Path, size: int = ICON_SIZE) -> bool:
    """Write an icon without upscaling small images.

    If the source is already 128x128 or smaller, keep its original pixel size
    (only converting container format when the destination extension requires it).
    If it is larger, shrink it into a 128x128 transparent canvas.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if Image is not None and src.exists():
        try:
            with Image.open(src) as img:
                img = img.convert("RGBA")
                if img.width <= size and img.height <= size:
                    img.save(dst, optimize=True)
                    return True
                img.thumbnail((size, size), Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
                canvas.save(dst, optimize=True)
                return True
        except Exception:
            pass
    try:
        shutil.copy2(src, dst)
        _resize_image_file(dst, size)
        return True
    except Exception:
        return False

BUILTIN_GEOMETRY_PREFIXES = ("geometry.humanoid", "geometry.player")
BUILTIN_RENDER_PREFIXES = ("controller.render.armor", "controller.render.item")
BUILTIN_TEXTURE_PREFIXES = ("textures/misc/", "textures/ui/")

@dataclass
class AddonItemCandidate:
    index: int
    identifier: str
    display_name: str
    file_path: str
    wearable_slot: str
    icon: str
    item_kind: str = "wearable"

@dataclass
class AddonInspection:
    source_path: str
    bp_dir: str
    rp_dir: str
    candidates: List[AddonItemCandidate]

@dataclass
class ExistingUiInspection:
    source_path: str
    bp_dir: str
    rp_dir: str
    script_path: str
    selector_id: str
    ui_title: str
    pack_name: str
    has_pack_icon: bool
    current_slot_mode: str
    current_slot_mode_label: str
    item_count: int
    item_variant_count: int
    items: List[Dict[str, Any]]
    repairable_duplicate_groups: int = 0

class AddonError(Exception):
    pass


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe_extract(zip_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_root = out_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise AddonError(f"ไฟล์ zip มีจำนวนไฟล์มากเกินไป ({len(infos)} / สูงสุด {MAX_ZIP_MEMBERS})")
        total_uncompressed = 0
        for info in infos:
            member = Path(info.filename)
            if not info.filename or len(info.filename) > MAX_ZIP_MEMBER_NAME_LENGTH:
                raise AddonError(f"ชื่อไฟล์ใน zip ยาวหรือว่างผิดปกติ: {info.filename[:120]}")
            if info.filename.startswith("/") or ".." in member.parts:
                raise AddonError(f"Unsafe zip member: {info.filename}")
            if info.file_size > MAX_ZIP_SINGLE_FILE_BYTES:
                raise AddonError(f"ไฟล์ภายใน zip ใหญ่เกินกำหนด: {info.filename} ({info.file_size} bytes)")
            total_uncompressed += int(info.file_size or 0)
            if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise AddonError(f"ขนาดหลังแตก zip ใหญ่เกินกำหนด ({total_uncompressed} bytes / สูงสุด {MAX_ZIP_UNCOMPRESSED_BYTES} bytes)")
            target = out_dir / info.filename
            try:
                target.resolve().relative_to(out_root)
            except Exception:
                raise AddonError(f"Unsafe zip member: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def _extract_addon_zip(zip_path: Path, out_dir: Path) -> None:
    """Extract an addon and unwrap one common nested-zip layer if needed.

    Users sometimes upload a .zip that contains a single .mcaddon/.zip inside.
    We only unwrap one nested archive and still apply the same safe extraction
    limits, so this stays cheap enough for free Render deployments.
    """
    _safe_extract(zip_path, out_dir)
    try:
        _find_packs(out_dir)
        return
    except AddonError as first_error:
        nested_archives = [
            p for p in out_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in {".mcaddon", ".zip"} and zipfile.is_zipfile(p)
        ]
        if len(nested_archives) != 1:
            raise first_error
        nested_root = out_dir.parent / f"{out_dir.name}_nested"
        if nested_root.exists():
            shutil.rmtree(nested_root)
        try:
            _safe_extract(nested_archives[0], nested_root)
            _find_packs(nested_root)
        except Exception:
            shutil.rmtree(nested_root, ignore_errors=True)
            raise first_error
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.move(str(nested_root), str(out_dir))


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
        raise AddonError("ไม่พบ Behavior Pack manifest ที่มี module type=data (ไฟล์อาจเป็น Resource Pack/texture pack อย่างเดียว หรือ manifest ระบุ module ไม่ถูกต้อง)")
    if rp is None:
        raise AddonError("ไม่พบ Resource Pack manifest ที่มี module type=resources (ไฟล์อาจไม่มี Resource Pack หรือ manifest ระบุ module ไม่ถูกต้อง)")
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


def _allow_off_hand_from_item(item_data: Dict[str, Any]) -> bool:
    comps = item_data.get("minecraft:item", {}).get("components", {})
    value = comps.get("minecraft:allow_off_hand")
    # Bedrock packs usually use true, but some generators emit an empty object.
    return value is True or isinstance(value, dict)


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
        if not isinstance(desc, dict) or not isinstance(comps, dict):
            continue
        identifier = desc.get("identifier")
        if not identifier:
            continue
        wearable = comps.get("minecraft:wearable")
        is_wearable = isinstance(wearable, dict)
        is_offhand = (not is_wearable) and _allow_off_hand_from_item(data)
        if not is_wearable and not is_offhand:
            continue
        slot = wearable.get("slot", "") if is_wearable else "slot.weapon.offhand"
        display = _display_name_from_item(data, identifier.split(":")[-1])
        candidates.append(AddonItemCandidate(
            index=len(candidates),
            identifier=identifier,
            display_name=display,
            file_path=str(path.relative_to(root)).replace(os.sep, "/"),
            wearable_slot=slot,
            icon=_icon_from_item(data),
            item_kind="wearable" if is_wearable else "offhand",
        ))
    return candidates


def _extract_json_const_from_js(text: str, const_name: str = "CONFIG") -> Tuple[Dict[str, Any], int, int]:
    """Extract a JSON-style object assigned to a JS const.

    The generated UI script writes CONFIG with json.dumps, so it is valid JSON
    inside JavaScript. This parser only scans enough to find the matching brace
    while respecting JSON strings.
    """
    marker = f"const {const_name} ="
    start = text.find(marker)
    if start < 0:
        raise AddonError(f"ไม่พบ const {const_name} ในไฟล์สคริปต์ UI")
    brace_start = text.find("{", start)
    if brace_start < 0:
        raise AddonError(f"รูปแบบ const {const_name} ไม่ถูกต้อง")
    depth = 0
    in_string = False
    escape = False
    for idx in range(brace_start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                obj_text = text[brace_start:idx + 1]
                try:
                    return json.loads(obj_text), start, idx + 1
                except Exception as exc:
                    raise AddonError(f"อ่านข้อมูล UI เดิมไม่สำเร็จ: {exc}") from exc
    raise AddonError(f"อ่าน const {const_name} ไม่สำเร็จ: วงเล็บไม่ครบ")


def _replace_json_const_in_js(text: str, const_name: str, data: Dict[str, Any]) -> str:
    _old, start, end = _extract_json_const_from_js(text, const_name)
    # Preserve the trailing semicolon if it already exists after the object.
    semi_end = end
    while semi_end < len(text) and text[semi_end].isspace():
        semi_end += 1
    if semi_end < len(text) and text[semi_end] == ";":
        semi_end += 1
    new_obj = f"const {const_name} = " + json.dumps(data, ensure_ascii=False, indent=2) + ";"
    return text[:start] + new_obj + text[semi_end:]


def _find_existing_ui_script(bp: Path) -> Optional[Path]:
    scripts_dir = bp / "scripts"
    roots = [scripts_dir] if scripts_dir.exists() else [bp]
    for root in roots:
        for script in root.rglob("*.js"):
            try:
                text = script.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if "const CONFIG" in text and "menuItem" in text and "armors" in text and "allItems" in text:
                try:
                    cfg, _, _ = _extract_json_const_from_js(text, "CONFIG")
                except Exception:
                    continue
                if isinstance(cfg.get("armors"), list) and cfg.get("menuItem"):
                    return script
    return None


_SLOT_LABEL_BY_KEY = {"head": "หัว", "chest": "ตัว", "legs": "กางเกง", "feet": "รองเท้า", "offhand": "มือซ้าย"}
_SLOT_KEY_BY_LABEL = {v: k for k, v in _SLOT_LABEL_BY_KEY.items()}
_ARMOR_SLOT_SET = set(SLOT_KEYS)


def _ui_slot_label(slot_key: str) -> str:
    return _SLOT_LABEL_BY_KEY.get(slot_key, slot_key)


def _ui_item_base_and_suffix(name: str) -> Tuple[str, Optional[str]]:
    match = re.match(r"^(.*?)\s*\((หัว|ตัว|กางเกง|รองเท้า)\)\s*$", str(name or ""))
    if not match:
        return str(name or ""), None
    return match.group(1).strip(), _SLOT_KEY_BY_LABEL.get(match.group(2))


def _detect_duplicate_ui_groups(armors: List[Dict[str, Any]]) -> int:
    groups: Dict[str, set[str]] = {}
    for armor in armors:
        if not isinstance(armor, dict) or (armor.get("kind") or "wearable") == "offhand":
            continue
        base, suffix = _ui_item_base_and_suffix(str(armor.get("name") or ""))
        if not base or not suffix:
            continue
        groups.setdefault(base, set()).add(suffix)
    return sum(1 for suffixes in groups.values() if len(suffixes) >= 2)


def _collapse_duplicate_ui_armors(armors: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Repair the common mistake: feeding a generated UI addon back into UI mode.

    Double conversion creates groups like "Name (หัว)", "Name (ตัว)" and then
    gives each group all four slots again. We collapse those groups back to one
    menu entry by taking the matching slot from the matching suffixed group.
    """
    grouped: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    passthrough: List[Dict[str, Any]] = []
    for armor in armors:
        if not isinstance(armor, dict) or (armor.get("kind") or "wearable") == "offhand":
            passthrough.append(armor)
            continue
        base, suffix = _ui_item_base_and_suffix(str(armor.get("name") or ""))
        if not base or not suffix:
            passthrough.append(armor)
            continue
        grouped.setdefault(base, []).append((suffix, armor))

    repaired = 0
    collapsed: List[Dict[str, Any]] = []
    for base, entries in grouped.items():
        suffixes = {slot for slot, _armor in entries}
        if len(suffixes) < 2:
            collapsed.extend(armor for _slot, armor in entries)
            continue
        by_suffix = {slot: armor for slot, armor in entries}
        template = dict(entries[0][1])
        template["name"] = base
        template["kind"] = template.get("kind") or "wearable"
        new_items: Dict[str, str] = {}
        for slot_key in SLOT_KEYS:
            source = by_suffix.get(slot_key)
            if isinstance(source, dict):
                source_items = source.get("items") or {}
                if isinstance(source_items, dict) and source_items.get(slot_key):
                    new_items[slot_key] = source_items[slot_key]
                    continue
            for _suffix, candidate in entries:
                source_items = candidate.get("items") or {}
                if isinstance(source_items, dict) and source_items.get(slot_key):
                    new_items[slot_key] = source_items[slot_key]
                    break
        if new_items:
            template["items"] = new_items
            collapsed.append(template)
            repaired += 1
        else:
            collapsed.extend(armor for _slot, armor in entries)

    # Preserve the original broad ordering: non-duplicated passthrough first,
    # then repaired duplicate groups. This is stable enough for UI display and
    # avoids losing any unusual entries.
    return passthrough + collapsed, repaired


def _summarize_ui_slot_mode(armors: List[Dict[str, Any]]) -> Tuple[str, str]:
    wearable_sets: List[set[str]] = []
    offhand_count = 0
    for armor in armors:
        if not isinstance(armor, dict):
            continue
        items = armor.get("items") or {}
        if not isinstance(items, dict):
            continue
        keys = {k for k in items if k in _ARMOR_SLOT_SET}
        if keys:
            wearable_sets.append(keys)
        if "offhand" in items or (armor.get("kind") == "offhand"):
            offhand_count += 1
    if not wearable_sets and offhand_count:
        return "offhand", "มือซ้ายเท่านั้น"
    if wearable_sets and all(keys == _ARMOR_SLOT_SET for keys in wearable_sets):
        return "all", "ใส่ได้ทุกช่อง"
    if wearable_sets and all(len(keys) == 1 for keys in wearable_sets):
        return "original", "ช่องเดียว/คงช่องเดิม"
    if wearable_sets:
        return "custom", "กำหนดช่องเอง/ผสมหลายแบบ"
    return "unknown", "ไม่พบข้อมูลช่อง"


def _existing_ui_pack_name(bp: Path, rp: Path) -> str:
    for pack in (bp, rp):
        try:
            manifest = _read_json(pack / "manifest.json")
            name = str(manifest.get("header", {}).get("name") or "").strip()
            if name:
                name = re.sub(r"\s+(BP|RP|Behavior Pack|Resource Pack)\s*$", "", name, flags=re.I).strip()
                return name or str(manifest.get("header", {}).get("name") or "Addon UI")
        except Exception:
            continue
    return "Addon UI"


def _ui_config_to_inspection(src: Path, root: Path, bp: Path, rp: Path, script: Path, cfg: Dict[str, Any]) -> ExistingUiInspection:
    armors = [a for a in cfg.get("armors", []) if isinstance(a, dict)]
    mode, mode_label = _summarize_ui_slot_mode(armors)
    items: List[Dict[str, Any]] = []
    variant_count = 0
    for idx, armor in enumerate(armors, start=1):
        slot_items = armor.get("items") if isinstance(armor.get("items"), dict) else {}
        slots = [k for k in ["head", "chest", "legs", "feet", "offhand"] if k in slot_items]
        variant_count += len(slots)
        items.append({
            "entry_id": str(idx),
            "name": str(armor.get("name") or f"Item {idx}"),
            "kind": str(armor.get("kind") or ("offhand" if slots == ["offhand"] else "wearable")),
            "slots": slots,
            "slot_labels": ", ".join(_ui_slot_label(s) for s in slots) or "-",
            "icon": armor.get("icon") or "",
        })
    return ExistingUiInspection(
        source_path=str(src),
        bp_dir=str(bp),
        rp_dir=str(rp),
        script_path=str(script.relative_to(root).as_posix()),
        selector_id=str(cfg.get("menuItem") or ""),
        ui_title=str(cfg.get("uiTitle") or "Addon UI"),
        pack_name=_existing_ui_pack_name(bp, rp),
        has_pack_icon=(bp / "pack_icon.png").exists() or (rp / "pack_icon.png").exists(),
        current_slot_mode=mode,
        current_slot_mode_label=mode_label,
        item_count=len(items),
        item_variant_count=variant_count,
        items=items,
        repairable_duplicate_groups=_detect_duplicate_ui_groups(armors),
    )


def inspect_existing_ui_addon(addon_path: str, work_dir: str) -> Optional[ExistingUiInspection]:
    """Return existing generated-UI information, or None for a normal addon."""
    src = Path(addon_path)
    root = Path(work_dir) / "ui_edit_inspect_src"
    if root.exists():
        shutil.rmtree(root)
    _extract_addon_zip(src, root)
    bp, rp = _find_packs(root)
    script = _find_existing_ui_script(bp)
    if not script:
        return None
    text = script.read_text(encoding="utf-8", errors="replace")
    cfg, _, _ = _extract_json_const_from_js(text, "CONFIG")
    return _ui_config_to_inspection(src, root, bp, rp, script, cfg)


def _set_existing_ui_creative_visibility(bp: Path, selector_id: str, visible_item_ids: set[str]) -> None:
    for item_path in (bp / "items").rglob("*.json") if (bp / "items").exists() else []:
        try:
            data = _read_json(item_path)
            desc = data.get("minecraft:item", {}).get("description", {})
        except Exception:
            continue
        item_id = desc.get("identifier")
        if not item_id:
            continue
        if item_id == selector_id:
            desc["menu_category"] = {"category": "equipment"}
        elif item_id in visible_item_ids:
            desc["menu_category"] = {"category": "none"}
        _write_json(item_path, data)


def _set_manifest_pack_name(pack_dir: Path, pack_name: str, suffix: str) -> None:
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.exists():
        return
    data = _read_json(manifest_path)
    header = data.setdefault("header", {})
    header["name"] = f"{pack_name} {suffix}".strip()
    _write_json(manifest_path, data)


def _update_lang_key(lines: List[str], key: str, value: str) -> List[str]:
    prefix = f"{key}="
    out: List[str] = []
    replaced = False
    for line in lines:
        if line.startswith(prefix):
            if not replaced:
                out.append(prefix + value)
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(prefix + value)
    return out


def _update_existing_ui_lang_names(rp: Path, selector_id: str, selector_label: Optional[str], item_labels: Dict[str, str]) -> None:
    texts = rp / "texts"
    if not texts.exists():
        return
    for lang_path in texts.glob("*.lang"):
        try:
            lines = lang_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        if selector_label and selector_id:
            lines = _update_lang_key(lines, f"item.{selector_id}.name", selector_label)
            short = selector_id.split(":", 1)[-1]
            lines = _update_lang_key(lines, f"item.{short}.name", selector_label)
        for item_id, label in item_labels.items():
            if not item_id or not label:
                continue
            lines = _update_lang_key(lines, f"item.{item_id}.name", label)
            short = item_id.split(":", 1)[-1]
            lines = _update_lang_key(lines, f"item.{short}.name", label)
        lang_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _update_existing_ui_item_display_names(bp: Path, labels_by_item_id: Dict[str, str]) -> None:
    if not labels_by_item_id:
        return
    for item_path in (bp / "items").rglob("*.json") if (bp / "items").exists() else []:
        try:
            data = _read_json(item_path)
        except Exception:
            continue
        item = data.get("minecraft:item") if isinstance(data, dict) else None
        if not isinstance(item, dict):
            continue
        desc = item.get("description") if isinstance(item.get("description"), dict) else {}
        item_id = str(desc.get("identifier") or "")
        label = labels_by_item_id.get(item_id)
        if not label:
            continue
        comps = item.setdefault("components", {})
        comps["minecraft:display_name"] = {"value": label}
        _write_json(item_path, data)


def _update_existing_ui_selector_name(bp: Path, selector_id: str, selector_label: str) -> None:
    if not selector_id or not selector_label:
        return
    for item_path in (bp / "items").rglob("*.json") if (bp / "items").exists() else []:
        try:
            data = _read_json(item_path)
        except Exception:
            continue
        item = data.get("minecraft:item") if isinstance(data, dict) else None
        if not isinstance(item, dict):
            continue
        desc = item.get("description") if isinstance(item.get("description"), dict) else {}
        if str(desc.get("identifier") or "") != selector_id:
            continue
        comps = item.setdefault("components", {})
        comps["minecraft:display_name"] = {"value": selector_label}
        _write_json(item_path, data)
        return


def _update_existing_ui_selector_icon_texture(bp: Path, rp: Path, selector_id: str, icon_src: Path) -> None:
    """Also refresh the visible inventory icon of the UI selector when possible."""
    if not selector_id or not icon_src.exists():
        return
    icon_key = ""
    for item_path in (bp / "items").rglob("*.json") if (bp / "items").exists() else []:
        try:
            data = _read_json(item_path)
        except Exception:
            continue
        item = data.get("minecraft:item") if isinstance(data, dict) else None
        if not isinstance(item, dict):
            continue
        desc = item.get("description") if isinstance(item.get("description"), dict) else {}
        if str(desc.get("identifier") or "") != selector_id:
            continue
        comps = item.get("components") if isinstance(item.get("components"), dict) else {}
        raw_icon = comps.get("minecraft:icon")
        if isinstance(raw_icon, str):
            icon_key = raw_icon
        elif isinstance(raw_icon, dict):
            icon_key = str(raw_icon.get("texture") or raw_icon.get("default") or "")
        break
    targets = []
    atlas = rp / "textures" / "item_texture.json"
    if icon_key and atlas.exists():
        try:
            data = _read_json(atlas)
            entry = data.get("texture_data", {}).get(icon_key)
            texture_ref = ""
            if isinstance(entry, dict):
                raw = entry.get("textures")
                if isinstance(raw, str):
                    texture_ref = raw
                elif isinstance(raw, list) and raw and isinstance(raw[0], str):
                    texture_ref = raw[0]
            if texture_ref.startswith("textures/"):
                targets.append(rp / f"{texture_ref}.png")
        except Exception:
            pass
    fallback = rp / "textures" / "item" / "auto_ui_pack_icon.png"
    if fallback.exists():
        targets.append(fallback)
    seen = set()
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        _write_resized_icon(icon_src, target)


def _get_selector_item_file_and_data(bp: Path, selector_id: str) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    if not selector_id or not (bp / "items").exists():
        return None, None
    for item_path in (bp / "items").rglob("*.json"):
        try:
            data = _read_json(item_path)
        except Exception:
            continue
        item = data.get("minecraft:item") if isinstance(data, dict) else None
        if not isinstance(item, dict):
            continue
        desc = item.get("description") if isinstance(item.get("description"), dict) else {}
        if str(desc.get("identifier") or "") == selector_id:
            return item_path, data
    return None, None


def _extract_icon_key_from_item_data(data: Dict[str, Any]) -> str:
    item = data.get("minecraft:item") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        return ""
    comps = item.get("components") if isinstance(item.get("components"), dict) else {}
    raw_icon = comps.get("minecraft:icon")
    if isinstance(raw_icon, str):
        return raw_icon
    if isinstance(raw_icon, dict):
        return str(raw_icon.get("texture") or raw_icon.get("default") or "")
    return ""


def _set_icon_key_on_item_data(data: Dict[str, Any], icon_key: str) -> None:
    item = data.setdefault("minecraft:item", {})
    comps = item.setdefault("components", {})
    comps["minecraft:icon"] = icon_key


def _texture_ref_for_icon_key(rp: Path, icon_key: str) -> str:
    if not icon_key:
        return ""
    atlas = rp / "textures" / "item_texture.json"
    if not atlas.exists():
        return ""
    try:
        data = _read_json(atlas)
        entry = data.get("texture_data", {}).get(icon_key)
        if isinstance(entry, dict):
            raw = entry.get("textures")
            if isinstance(raw, str):
                return raw
            if isinstance(raw, list) and raw and isinstance(raw[0], str):
                return raw[0]
    except Exception:
        return ""
    return ""


def _ensure_existing_ui_selector_icon_unique(bp: Path, rp: Path, selector_id: str, warnings: List[str]) -> None:
    """Upgrade old UI addons so multiple UI selectors do not share one atlas key/path."""
    item_path, item_data = _get_selector_item_file_and_data(bp, selector_id)
    if item_path is None or item_data is None:
        return
    old_key = _extract_icon_key_from_item_data(item_data)
    old_ref = _texture_ref_for_icon_key(rp, old_key)
    if old_key.startswith("auto_ui_pack_icon_") and old_ref.startswith("textures/item/auto_ui_pack_icon_"):
        return

    token = _safe_id(f"{selector_id.replace(':', '_')}_{uuid4().hex[:8]}", "ui_selector")
    new_key = f"auto_ui_pack_icon_{token}"
    new_ref = f"textures/item/{new_key}"
    new_file = rp / "textures" / "item" / f"{new_key}.png"

    old_file = rp / f"{old_ref}.png" if old_ref.startswith("textures/") else None
    src: Optional[Path] = None
    for candidate in (old_file, rp / "textures" / "item" / "auto_ui_pack_icon.png", rp / "pack_icon.png", bp / "pack_icon.png"):
        if candidate and candidate.exists():
            src = candidate
            break
    if src:
        _write_resized_icon(src, new_file)
        atlas_path, atlas = _load_item_texture_data(rp)
        texture_data = atlas.setdefault("texture_data", {})
        if not isinstance(texture_data, dict):
            texture_data = {}
            atlas["texture_data"] = texture_data
        texture_data[new_key] = {"textures": new_ref}
        _write_json(atlas_path, atlas)
        _set_icon_key_on_item_data(item_data, new_key)
        _write_json(item_path, item_data)
        if old_key == "auto_ui_pack_icon" or old_ref == "textures/item/auto_ui_pack_icon":
            warnings.append("ปรับ icon key/path ของไอเท็ม UI ให้ไม่ซ้ำกับ addon UI อื่นโดยอัตโนมัติ")
    else:
        warnings.append("ไม่พบไฟล์ icon เดิมของไอเท็ม UI จึงไม่สามารถอัปเกรด icon key/path ให้ unique ได้")


def _slot_label_for_existing_ui(slot_key: str) -> str:
    return _SLOT_LABEL_BY_KEY.get(slot_key, slot_key)


def _label_for_existing_ui_item(base_name: str, slot_key: str, kind: str = "wearable") -> str:
    if slot_key == "offhand" or kind == "offhand":
        return f"{base_name} (มือซ้าย)"
    return f"{base_name} ({_slot_label_for_existing_ui(slot_key)})"



def _append_added_items_to_existing_ui(
    *,
    dst_bp: Path,
    dst_rp: Path,
    work_dir: Path,
    armors: List[Dict[str, Any]],
    additions: Optional[List[Dict[str, Any]]],
    warnings: List[str],
) -> int:
    """Append newly uploaded normal-addon items into an existing generated UI addon.

    Each addition is expected to contain:
      addon_path, selected_identifiers, slot_mode, custom_slots

    This reuses the same normalize/rebuild logic as first-time UI conversion, but
    writes the generated hidden items/assets into the already extracted UI addon.
    """
    if not additions:
        return 0

    atlas_path, atlas = _load_item_texture_data(dst_rp)
    texture_data = atlas.setdefault("texture_data", {})
    if not isinstance(texture_data, dict):
        texture_data = {}
        atlas["texture_data"] = texture_data

    added_entries = 0
    existing_count = len(armors)
    for add_index, addition in enumerate(additions, start=1):
        addon_path = Path(str(addition.get("addon_path") or ""))
        if not addon_path.exists():
            warnings.append(f"ข้าม addon เพิ่มไอเท็ม #{add_index}: ไม่พบไฟล์ต้นทาง")
            continue
        selected_ids = [str(x) for x in (addition.get("selected_identifiers") or []) if str(x)]
        if not selected_ids:
            warnings.append(f"ข้าม addon เพิ่มไอเท็ม #{add_index}: ยังไม่ได้เลือกไอเท็ม")
            continue
        add_root = work_dir / f"ui_add_src_{add_index:02d}"
        if add_root.exists():
            shutil.rmtree(add_root)
        _extract_addon_zip(addon_path, add_root)
        src_bp, src_rp = _find_packs(add_root)
        candidates = _scan_candidates(add_root, src_bp)
        selected = [c for c in candidates if c.identifier in set(selected_ids)]
        if not selected:
            warnings.append(f"ข้าม addon เพิ่มไอเท็ม #{add_index}: ไม่พบไอเท็มที่เลือกไว้ในไฟล์")
            continue

        addon_name = _addon_base_name(src_bp, src_rp)
        safe_addon = _safe_id(addon_name, f"addon_{add_index}")
        add_token = _safe_id(str(addition.get("token") or uuid4().hex[:6]), "add")
        slot_mode = str(addition.get("slot_mode") or "original").lower()
        custom_slots = [s for s in (addition.get("custom_slots") or []) if s in SLOT_KEYS]

        for local_idx, candidate in enumerate(selected, start=1):
            global_idx = existing_count + added_entries + 1
            orig_ns, orig_name = _identifier_parts(candidate.identifier)
            item_kind = getattr(candidate, "item_kind", "wearable")
            base_prefix = "offhand" if item_kind == "offhand" else "armor"
            base = f"add_{add_index:02d}_{global_idx:03d}_{base_prefix}_{safe_addon}_{add_token}_{orig_ns}_{orig_name}"
            new_ns = _safe_id(f"{safe_addon}_uiadd_{add_token}_{global_idx:03d}", "uiadd")
            item_path = add_root / candidate.file_path
            try:
                item_data = _read_json(item_path)
            except Exception:
                item_data = {}
            src_attach = _find_attachable_for_item(src_rp, candidate.identifier, candidate.file_path)
            if not src_attach and item_kind != "offhand":
                warnings.append(f"ไม่พบ attachable สำหรับไอเท็มที่เพิ่ม {candidate.identifier}; จะสร้าง attachable พื้นฐาน")

            fallback_texture = ""
            if src_attach:
                try:
                    desc = _extract_attachable_desc(src_attach)
                    texs = desc.get("textures", {})
                    if isinstance(texs, dict):
                        fallback_texture = next((v for v in texs.values() if isinstance(v, str) and v.startswith("textures/") and "enchanted" not in v), "")
                except Exception:
                    pass
            generated_icon_key = f"{base}_icon"
            icon_ref = _copy_icon(src_rp, dst_rp, candidate.icon or generated_icon_key, generated_icon_key, fallback_texture, warnings)
            texture_data[generated_icon_key] = {"textures": icon_ref}

            armor_entry: Dict[str, Any] = {
                "name": candidate.display_name,
                "icon": icon_ref,
                "items": {},
                "kind": item_kind,
            }

            if item_kind == "offhand":
                new_item_name = f"{base}_offhand"
                new_item_id = f"{new_ns}:{new_item_name}"
                armor_entry["items"]["offhand"] = new_item_id
                label = f"{candidate.display_name} (มือซ้าย)"
                item_json = _make_generated_offhand_item(item_data, new_item_id, generated_icon_key, label)
                _write_json(dst_bp / "items" / f"{new_item_name}.json", item_json)
                _normalize_attachable_for_item(src_rp, dst_rp, src_attach, new_item_id, base, warnings)
                armors.append(armor_entry)
                added_entries += 1
                continue

            output_slot_keys = _slot_keys_for_candidate(candidate, slot_mode, custom_slots)
            if slot_mode == "original" and candidate.wearable_slot not in SLOT_BY_WEARABLE:
                warnings.append(f"ไม่รู้จัก wearable slot เดิมของไอเท็มที่เพิ่ม {candidate.identifier}: {candidate.wearable_slot}; fallback เป็นกางเกง")
            if output_slot_keys == ["offhand"]:
                armor_entry["kind"] = "offhand"
            for slot_key in output_slot_keys:
                wearable_slot = SLOTS_BY_KEY[slot_key][2]
                new_item_name = f"{base}_{slot_key}"
                new_item_id = f"{new_ns}:{new_item_name}"
                armor_entry["items"][slot_key] = new_item_id
                if slot_key == "offhand":
                    label = f"{candidate.display_name} (มือซ้าย)"
                    item_json = _make_generated_offhand_item(item_data, new_item_id, generated_icon_key, label)
                    _write_json(dst_bp / "items" / f"{new_item_name}.json", item_json)
                    _normalize_attachable_for_item(src_rp, dst_rp, src_attach, new_item_id, base, warnings)
                else:
                    label = _lang_label(candidate.display_name, slot_key)
                    item_json = _make_generated_item(new_item_id, generated_icon_key, label, wearable_slot)
                    _write_json(dst_bp / "items" / f"{new_item_name}.json", item_json)
                    _normalize_attachable_for_slot(src_rp, dst_rp, src_attach, new_item_id, base, slot_key, warnings)
            armors.append(armor_entry)
            added_entries += 1

    _write_json(atlas_path, atlas)
    _resize_item_icon_tree(dst_rp)
    return added_entries

def edit_existing_ui_addon(
    addon_path: str,
    work_dir: str,
    slot_mode: str = "keep",
    custom_slots: Optional[List[str]] = None,
    repair_duplicates: bool = False,
    ui_title: Optional[str] = None,
    pack_name: Optional[str] = None,
    pack_icon_path: Optional[str] = None,
    item_renames: Optional[Dict[str, str]] = None,
    additions: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Edit a previously generated UI addon without converting hidden items again.

    This edits the existing UI CONFIG and pack metadata only. It preserves the
    original generated item variants, so uploading a UI addon into UI mode again
    will not create duplicate hidden items.
    """
    src = Path(addon_path)
    root = Path(work_dir) / "ui_edit_src"
    if root.exists():
        shutil.rmtree(root)
    _extract_addon_zip(src, root)
    bp, rp = _find_packs(root)
    script = _find_existing_ui_script(bp)
    if not script:
        raise AddonError("ไฟล์นี้ไม่ใช่ addon UI ที่บอทเคยสร้างไว้ จึงใช้โหมดแก้ไขไม่ได้")
    text = script.read_text(encoding="utf-8", errors="replace")
    cfg, _, _ = _extract_json_const_from_js(text, "CONFIG")
    armors = [a for a in cfg.get("armors", []) if isinstance(a, dict)]
    warnings: List[str] = []
    repaired = 0
    if repair_duplicates:
        armors, repaired = _collapse_duplicate_ui_armors(armors)
        if repaired:
            warnings.append(f"ซ่อมรายการที่น่าจะเกิดจากการทำ UI ซ้ำ {repaired} กลุ่ม")

    added_entries = _append_added_items_to_existing_ui(
        dst_bp=bp,
        dst_rp=rp,
        work_dir=Path(work_dir),
        armors=armors,
        additions=additions,
        warnings=warnings,
    )
    if added_entries:
        warnings.append(f"เพิ่มไอเท็มใหม่เข้า UI {added_entries} รายการ")

    mode = (slot_mode or "keep").lower()
    allowed = [s for s in (custom_slots or []) if s in SLOT_KEYS]
    renames = {str(k): str(v).strip() for k, v in (item_renames or {}).items() if str(v).strip()}

    new_armors: List[Dict[str, Any]] = []
    item_labels: Dict[str, str] = {}
    for idx, armor in enumerate(armors, start=1):
        items = armor.get("items") if isinstance(armor.get("items"), dict) else {}
        kind = armor.get("kind") or ("offhand" if "offhand" in items else "wearable")
        if str(idx) in renames:
            armor = dict(armor)
            armor["name"] = renames[str(idx)]
        if kind == "offhand" or "offhand" in items:
            item_id = items.get("offhand")
            if isinstance(item_id, str) and item_id:
                item_labels[item_id] = _label_for_existing_ui_item(str(armor.get("name") or f"Item {idx}"), "offhand", "offhand")
            new_armors.append(armor)
            continue
        if mode == "custom":
            filtered = {slot: items[slot] for slot in allowed if slot in items}
            if not filtered:
                warnings.append(f"ซ่อน {armor.get('name', 'item')} จาก UI เพราะไม่มีช่องที่เลือก")
                continue
            armor = dict(armor)
            armor["items"] = filtered
            items = filtered
        # keep/all intentionally keep every currently available slot. Existing UI
        # addons do not always contain enough source data to safely synthesize new
        # missing slots, so we never duplicate hidden items here.
        for slot_key, item_id in items.items():
            if isinstance(item_id, str) and item_id:
                item_labels[item_id] = _label_for_existing_ui_item(str(armor.get("name") or f"Item {idx}"), slot_key, kind)
        new_armors.append(armor)

    cfg["armors"] = new_armors
    all_items: List[str] = []
    for armor in new_armors:
        items = armor.get("items") if isinstance(armor.get("items"), dict) else {}
        for item_id in items.values():
            if isinstance(item_id, str) and item_id:
                all_items.append(item_id)
    cfg["allItems"] = list(dict.fromkeys(all_items))

    clean_ui_title = str(ui_title or cfg.get("uiTitle") or "Addon UI").strip() or "Addon UI"
    cfg["uiTitle"] = clean_ui_title

    clean_pack_name = str(pack_name or "").strip()
    if clean_pack_name:
        _set_manifest_pack_name(bp, clean_pack_name, "BP")
        _set_manifest_pack_name(rp, clean_pack_name, "RP")

    if pack_icon_path:
        icon_src = Path(pack_icon_path)
        if icon_src.exists():
            _write_resized_icon(icon_src, bp / "pack_icon.png")
            _write_resized_icon(icon_src, rp / "pack_icon.png")
            _update_existing_ui_selector_icon_texture(bp, rp, str(cfg.get("menuItem") or ""), icon_src)
        else:
            warnings.append("ไม่พบไฟล์รูป pack icon ที่เลือกไว้ จึงคงรูปเดิม")

    selector_id = str(cfg.get("menuItem") or "")
    _ensure_existing_ui_selector_icon_unique(bp, rp, selector_id, warnings)

    selector_label = clean_ui_title
    _update_existing_ui_selector_name(bp, selector_id, selector_label)
    _update_existing_ui_item_display_names(bp, item_labels)
    _update_existing_ui_lang_names(rp, selector_id, selector_label, item_labels)

    script.write_text(_replace_json_const_in_js(text, "CONFIG", cfg), encoding="utf-8")
    _set_existing_ui_creative_visibility(bp, selector_id, set(cfg["allItems"]))

    mode_label = {"keep": "คงสถานะเดิม", "all": "แสดงทุกช่องที่มีอยู่", "custom": "กำหนดช่องเอง"}.get(mode, mode)
    report = [
        "Existing UI Edit Mode report",
        f"Source: {src.name}",
        f"Pack name: {clean_pack_name or '-'}",
        f"UI title: {cfg.get('uiTitle', '-')}",
        f"Selector: {cfg.get('menuItem', '-')}",
        f"Edit mode: {mode_label}",
        f"Custom slots: {', '.join(allowed) if allowed else '-'}",
        f"Renamed menu entries: {len(renames)}",
        f"Pack icon changed: {'yes' if pack_icon_path else 'no'}",
        f"Added menu entries: {added_entries}",
        f"Menu entries: {len(new_armors)}",
        f"Visible item variants in UI: {len(cfg['allItems'])}",
    ]
    if warnings:
        report.extend(["", "Warnings:"])
        report.extend(f"- {w}" for w in warnings)
    (Path(work_dir) / "UI_EDIT_REPORT_WEBHOOK.txt").write_text("\n".join(report) + "\n", encoding="utf-8")

    output_stem = src.stem
    out_path = Path(work_dir) / f"edited_{output_stem}.mcaddon"
    if out_path.exists():
        out_path.unlink()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(root).as_posix())
    with zipfile.ZipFile(out_path, "r") as zf:
        if not zf.namelist():
            raise AddonError("สร้างไฟล์ output ไม่สำเร็จ")
    return str(out_path)

def inspect_addon(addon_path: str, work_dir: str) -> AddonInspection:
    src = Path(addon_path)
    if not src.exists():
        raise AddonError("ไม่พบไฟล์ addon")
    if not zipfile.is_zipfile(src):
        raise AddonError("ไฟล์นี้ไม่ใช่ zip/mcaddon ที่เปิดได้")
    root = Path(work_dir) / "inspect"
    if root.exists():
        shutil.rmtree(root)
    _extract_addon_zip(src, root)
    bp, rp = _find_packs(root)
    candidates = _scan_candidates(root, bp)
    if not candidates:
        raise AddonError("ไม่พบไอเท็มที่แปลงได้ (ต้องเป็นเกราะ minecraft:wearable หรือไอเท็มที่เปิด minecraft:allow_off_hand)")
    return AddonInspection(
        source_path=str(src),
        bp_dir=str(bp.relative_to(root)).replace(os.sep, "/"),
        rp_dir=str(rp.relative_to(root)).replace(os.sep, "/"),
        candidates=candidates,
    )


def _safe_id(value: str, default: str = "item") -> str:
    value = str(value or "").lower().replace("-", "_")
    value = re.sub(r"[^a-z0-9_]+", "_", value).strip("_")
    value = re.sub(r"_+", "_", value)
    return value or default


def _identifier_parts(identifier: str) -> Tuple[str, str]:
    if ":" in identifier:
        ns, name = identifier.split(":", 1)
    else:
        ns, name = "addon", identifier
    return _safe_id(ns, "addon"), _safe_id(name, "item")


def _addon_base_name(bp: Path, rp: Path) -> str:
    for manifest_path in (bp / "manifest.json", rp / "manifest.json"):
        try:
            data = _read_json(manifest_path)
            name = str(data.get("header", {}).get("name", "")).strip()
        except Exception:
            continue
        if not name:
            continue
        name = re.sub(r"[\s_\-]*(BP|RP)\s*$", "", name, flags=re.IGNORECASE).strip()
        if name:
            return name
    return "Addon"


def _find_attachable_for_item(rp: Path, item_id: str, candidate_file: str = "") -> Optional[Path]:
    attach_dir = rp / "attachables"
    if not attach_dir.exists():
        return None
    candidates: List[Path] = []
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
        candidates.append(path)
    # Fallback: many generated skin packs use matching file stems but no description.item.
    stem = Path(candidate_file).stem.lower()
    if stem:
        for path in candidates:
            if path.stem.lower() == stem:
                return path
    item_short = item_id.split(":")[-1].lower()
    for path in candidates:
        if path.stem.lower() == item_short:
            return path
    return None


def _load_item_texture_data(rp: Path) -> Tuple[Path, Dict[str, Any]]:
    path = rp / "textures" / "item_texture.json"
    if path.exists():
        try:
            return path, _read_json(path)
        except Exception:
            pass
    return path, {"resource_pack_name": "auto_ui", "texture_name": "atlas.items", "texture_data": {}}


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


def _copy_texture_between(src_rp: Path, dst_rp: Path, texture_ref: str, new_ref: str, warnings: List[str]) -> str:
    if not isinstance(texture_ref, str) or not texture_ref.startswith("textures/"):
        return texture_ref
    if texture_ref.startswith(BUILTIN_TEXTURE_PREFIXES):
        return texture_ref
    for ext in TEXTURE_EXTS:
        src = src_rp / (texture_ref + ext)
        if src.exists():
            dst = dst_rp / (new_ref + ext)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return new_ref
    warnings.append(f"ไม่พบ texture file: {texture_ref}")
    return texture_ref


def _find_geometry_file(rp: Path, geometry_id: str) -> Optional[Path]:
    if geometry_id.startswith(BUILTIN_GEOMETRY_PREFIXES):
        return None
    models = rp / "models"
    if not models.exists():
        return None
    for path in sorted(models.rglob("*.json")):
        try:
            data = _read_json(path)
        except Exception:
            continue
        geos = data.get("minecraft:geometry")
        if isinstance(geos, list):
            for geo in geos:
                if isinstance(geo, dict) and geo.get("description", {}).get("identifier") == geometry_id:
                    return path
    return None


def _copy_geometry_normalized(src_rp: Path, dst_rp: Path, old_id: str, new_id: str, out_name: str, warnings: List[str]) -> str:
    if not isinstance(old_id, str) or not old_id.startswith("geometry."):
        return old_id
    if old_id.startswith(BUILTIN_GEOMETRY_PREFIXES):
        return old_id
    path = _find_geometry_file(src_rp, old_id)
    if not path:
        warnings.append(f"ไม่พบ geometry file สำหรับ {old_id}; preserve reference เดิม")
        return old_id
    data = _read_json(path)
    geos = data.get("minecraft:geometry")
    if isinstance(geos, list):
        for geo in geos:
            if isinstance(geo, dict) and geo.get("description", {}).get("identifier") == old_id:
                geo.setdefault("description", {})["identifier"] = new_id
    out = dst_rp / "models" / "entity" / f"geometry.{out_name}.json"
    _write_json(out, data)
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


def _copy_animation_normalized(src_rp: Path, dst_rp: Path, old_id: str, new_id: str, out_name: str, warnings: List[str]) -> str:
    if not isinstance(old_id, str) or not old_id.startswith("animation."):
        return old_id
    path = _find_animation_file(src_rp, old_id)
    if not path:
        warnings.append(f"ไม่พบ animation file สำหรับ {old_id}; preserve reference เดิม")
        return old_id
    data = _read_json(path)
    animations = data.get("animations")
    if isinstance(animations, dict) and old_id in animations:
        animations[new_id] = animations.pop(old_id)
    out = dst_rp / "animations" / f"{out_name}.animation.json"
    _write_json(out, data)
    return new_id


def _find_render_controller_file(rp: Path, controller_id: str) -> Optional[Path]:
    if controller_id.startswith(BUILTIN_RENDER_PREFIXES):
        return None
    rc_dir = rp / "render_controllers"
    if not rc_dir.exists():
        return None
    for path in sorted(rc_dir.rglob("*.json")):
        try:
            data = _read_json(path)
        except Exception:
            continue
        controllers = data.get("render_controllers")
        if isinstance(controllers, dict) and controller_id in controllers:
            return path
    return None


def _copy_render_controller_normalized(src_rp: Path, dst_rp: Path, old_id: str, new_id: str, out_name: str, warnings: List[str]) -> str:
    if not isinstance(old_id, str) or not old_id.startswith("controller.render."):
        return old_id
    if old_id.startswith(BUILTIN_RENDER_PREFIXES):
        return old_id
    path = _find_render_controller_file(src_rp, old_id)
    if not path:
        warnings.append(f"ไม่พบ render controller file สำหรับ {old_id}; preserve reference เดิม")
        return old_id
    data = _read_json(path)
    controllers = data.get("render_controllers")
    if isinstance(controllers, dict) and old_id in controllers:
        controllers[new_id] = controllers.pop(old_id)
    out = dst_rp / "render_controllers" / f"{out_name}.render_controllers.json"
    _write_json(out, data)
    return new_id


def _slot_parent_setup(slot_key: str) -> str:
    return {
        "head": "variable.helmet_layer_visible = 0.0;",
        "chest": "variable.chest_layer_visible = 0.0;",
        "legs": "variable.leg_layer_visible = 0.0;",
        "feet": "variable.boot_layer_visible = 0.0;",
    }.get(slot_key, "variable.leg_layer_visible = 0.0;")


def _retarget_parent_setup(desc: Dict[str, Any], slot_key: str) -> None:
    scripts = desc.setdefault("scripts", {})
    if not isinstance(scripts, dict):
        desc["scripts"] = {"parent_setup": _slot_parent_setup(slot_key)}
        return
    parent_setup = scripts.get("parent_setup")
    layer_vars = (
        "variable.helmet_layer_visible",
        "variable.chest_layer_visible",
        "variable.leg_layer_visible",
        "variable.boot_layer_visible",
    )
    if not isinstance(parent_setup, str) or any(v in parent_setup for v in layer_vars):
        scripts["parent_setup"] = _slot_parent_setup(slot_key)


def _extract_attachable_desc(src_attach: Optional[Path]) -> Dict[str, Any]:
    if not src_attach or not src_attach.exists():
        return {
            "materials": {"default": "armor"},
            "textures": {},
            "geometry": {"default": "geometry.humanoid.customSlim"},
            "render_controllers": ["controller.render.armor"],
        }
    data = _read_json(src_attach)
    desc = data.get("minecraft:attachable", {}).get("description", {})
    return json.loads(json.dumps(desc)) if isinstance(desc, dict) else {}


def _normalize_attachable_for_slot(src_rp: Path, dst_rp: Path, src_attach: Optional[Path], new_item_id: str, base: str, slot_key: str, warnings: List[str]) -> None:
    desc = _extract_attachable_desc(src_attach)
    desc["identifier"] = new_item_id
    desc["item"] = {new_item_id: "query.owner_identifier=='minecraft:player'"}
    _retarget_parent_setup(desc, slot_key)
    desc.setdefault("materials", {"default": "armor"})
    desc.setdefault("render_controllers", ["controller.render.armor"])

    textures = desc.get("textures")
    if isinstance(textures, dict):
        for key, value in list(textures.items()):
            if isinstance(value, str) and value.startswith("textures/") and "enchanted" not in value:
                textures[key] = _copy_texture_between(src_rp, dst_rp, value, f"textures/models/armor/{base}_{_safe_id(key, 'tex')}", warnings)
    else:
        desc["textures"] = {}

    geometries = desc.get("geometry")
    if isinstance(geometries, dict):
        for key, value in list(geometries.items()):
            if isinstance(value, str) and value.startswith("geometry."):
                new_geo_id = f"geometry.{base}_{slot_key}_{_safe_id(key, 'geo')}"
                geometries[key] = _copy_geometry_normalized(src_rp, dst_rp, value, new_geo_id, f"{base}_{slot_key}_{_safe_id(key, 'geo')}", warnings)
    else:
        desc["geometry"] = {"default": "geometry.humanoid.customSlim"}

    animations = desc.get("animations")
    if isinstance(animations, dict):
        for key, value in list(animations.items()):
            if isinstance(value, str) and value.startswith("animation."):
                new_anim_id = f"animation.{base}_{slot_key}_{_safe_id(key, 'anim')}"
                animations[key] = _copy_animation_normalized(src_rp, dst_rp, value, new_anim_id, f"{base}_{slot_key}_{_safe_id(key, 'anim')}", warnings)

    render_controllers = desc.get("render_controllers")
    if isinstance(render_controllers, list):
        new_rcs = []
        for i, value in enumerate(render_controllers):
            if isinstance(value, str) and value.startswith("controller.render."):
                new_rc_id = f"controller.render.{base}_{slot_key}_{i}"
                new_rcs.append(_copy_render_controller_normalized(src_rp, dst_rp, value, new_rc_id, f"{base}_{slot_key}_{i}", warnings))
            else:
                new_rcs.append(value)
        desc["render_controllers"] = new_rcs

    attachable = {
        "format_version": "1.10.0",
        "minecraft:attachable": {
            "description": desc
        }
    }
    _write_json(dst_rp / "attachables" / f"{base}_{slot_key}.json", attachable)


def _normalize_attachable_for_item(src_rp: Path, dst_rp: Path, src_attach: Optional[Path], new_item_id: str, base: str, warnings: List[str]) -> None:
    if not src_attach:
        return
    desc = _extract_attachable_desc(src_attach)
    desc["identifier"] = new_item_id
    desc["item"] = {new_item_id: "query.owner_identifier=='minecraft:player'"}

    textures = desc.get("textures")
    if isinstance(textures, dict):
        for key, value in list(textures.items()):
            if isinstance(value, str) and value.startswith("textures/") and "enchanted" not in value:
                textures[key] = _copy_texture_between(src_rp, dst_rp, value, f"textures/models/item/{base}_{_safe_id(key, 'tex')}", warnings)

    geometries = desc.get("geometry")
    if isinstance(geometries, dict):
        for key, value in list(geometries.items()):
            if isinstance(value, str) and value.startswith("geometry."):
                new_geo_id = f"geometry.{base}_{_safe_id(key, 'geo')}"
                geometries[key] = _copy_geometry_normalized(src_rp, dst_rp, value, new_geo_id, f"{base}_{_safe_id(key, 'geo')}", warnings)

    animations = desc.get("animations")
    if isinstance(animations, dict):
        for key, value in list(animations.items()):
            if isinstance(value, str) and value.startswith("animation."):
                new_anim_id = f"animation.{base}_{_safe_id(key, 'anim')}"
                animations[key] = _copy_animation_normalized(src_rp, dst_rp, value, new_anim_id, f"{base}_{_safe_id(key, 'anim')}", warnings)

    render_controllers = desc.get("render_controllers")
    if isinstance(render_controllers, list):
        new_rcs = []
        for i, value in enumerate(render_controllers):
            if isinstance(value, str) and value.startswith("controller.render."):
                new_rc_id = f"controller.render.{base}_{i}"
                new_rcs.append(_copy_render_controller_normalized(src_rp, dst_rp, value, new_rc_id, f"{base}_{i}", warnings))
            else:
                new_rcs.append(value)
        desc["render_controllers"] = new_rcs

    attachable = {"format_version": "1.10.0", "minecraft:attachable": {"description": desc}}
    _write_json(dst_rp / "attachables" / f"{base}_offhand.json", attachable)


def _make_generated_offhand_item(src_item_data: Dict[str, Any], identifier: str, icon: str, display_name: str) -> Dict[str, Any]:
    data = json.loads(json.dumps(src_item_data)) if isinstance(src_item_data, dict) else {}
    item = data.setdefault("minecraft:item", {})
    desc = item.setdefault("description", {})
    comps = item.setdefault("components", {})
    desc["identifier"] = identifier
    desc["menu_category"] = {"category": "none"}
    comps["minecraft:icon"] = icon
    comps["minecraft:display_name"] = {"value": display_name}
    comps["minecraft:allow_off_hand"] = True
    comps.setdefault("minecraft:max_stack_size", 1)
    comps.pop("minecraft:wearable", None)
    data.setdefault("format_version", "1.20.50")
    return data


def _item_texture_lookup(src_rp: Path, icon_key: str) -> Optional[str]:
    _, data = _load_item_texture_data(src_rp)
    tex = data.get("texture_data", {}) if isinstance(data, dict) else {}
    entry = tex.get(icon_key) if isinstance(tex, dict) else None
    return _textures_value_to_first_path(entry)


def _copy_icon(src_rp: Path, dst_rp: Path, icon_key: str, new_icon_key: str, fallback_texture_ref: str, warnings: List[str]) -> str:
    texture_ref = _item_texture_lookup(src_rp, icon_key) if icon_key else None
    if not texture_ref:
        texture_ref = fallback_texture_ref
    if texture_ref:
        new_ref = _copy_texture_between(src_rp, dst_rp, texture_ref, f"textures/item/{new_icon_key}", warnings)
    else:
        new_ref = f"textures/item/{new_icon_key}"
        warnings.append(f"ไม่พบ icon texture สำหรับ {icon_key}")
    return new_ref


def _make_manifest_pair(addon_name: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    bp_header_uuid = str(uuid4())
    bp_module_uuid = str(uuid4())
    rp_header_uuid = str(uuid4())
    rp_module_uuid = str(uuid4())
    bp_manifest = {
        "format_version": 2,
        "header": {
            "name": f"{addon_name} BP",
            "description": "Normalize/Rebuild UI addon generated by Discord bot",
            "uuid": bp_header_uuid,
            "version": [1, 0, 0],
            "min_engine_version": [1, 20, 50],
            "icon": "pack_icon.png",
        },
        "modules": [
            {"type": "data", "uuid": bp_module_uuid, "version": [1, 0, 0]},
            {"type": "script", "language": "javascript", "entry": "scripts/main.js", "uuid": str(uuid4()), "version": [1, 0, 0]},
        ],
        "dependencies": [
            {"uuid": rp_header_uuid, "version": [1, 0, 0]},
            {"module_name": "@minecraft/server", "version": "1.10.0"},
            {"module_name": "@minecraft/server-ui", "version": "1.2.0"},
        ],
    }
    rp_manifest = {
        "format_version": 2,
        "header": {
            "name": f"{addon_name} RP",
            "description": "Normalize/Rebuild UI resources generated by Discord bot",
            "uuid": rp_header_uuid,
            "version": [1, 0, 0],
            "min_engine_version": [1, 20, 50],
            "icon": "pack_icon.png",
        },
        "modules": [
            {"type": "resources", "uuid": rp_module_uuid, "version": [1, 0, 0]},
        ],
        "dependencies": [
            {"uuid": bp_header_uuid, "version": [1, 0, 0]},
        ],
    }
    return bp_manifest, rp_manifest


def _copy_pack_icon(src_bp: Path, src_rp: Path, dst_bp: Path, dst_rp: Path) -> None:
    for src_dir in (src_bp, src_rp):
        src = src_dir / "pack_icon.png"
        if src.exists():
            _write_resized_icon(src, dst_bp / "pack_icon.png")
            _write_resized_icon(src, dst_rp / "pack_icon.png")
            return


def _prepare_selector_pack_icon(dst_bp: Path, dst_rp: Path, warnings: List[str], unique_token: str) -> Optional[Tuple[str, str]]:
    """Use the output pack icon as the visible inventory icon for the UI item.

    The held item model is hidden separately with render_offsets, but the atlas
    icon remains visible in inventory/Equipment.  The atlas key and texture path
    must be unique per generated addon; Minecraft merges item_texture atlases
    across active resource packs, so a shared key/path like auto_ui_pack_icon can
    make every UI selector show the icon from whichever pack loads first.
    """
    src = dst_rp / "pack_icon.png"
    if not src.exists():
        src = dst_bp / "pack_icon.png"
    if not src.exists():
        warnings.append("ไม่พบ pack_icon.png สำหรับใช้เป็น icon ของไอเท็ม UI; fallback เป็น icon ไอเท็มแรก")
        return None
    token = _safe_id(unique_token or uuid4().hex[:8], "ui")
    icon_key = f"auto_ui_pack_icon_{token}"
    icon_ref = f"textures/item/{icon_key}"
    _write_resized_icon(src, dst_rp / "textures" / "item" / f"{icon_key}.png")
    return icon_key, icon_ref


def _generate_ui_script(selector_id: str, armors: List[Dict[str, Any]], all_item_ids: List[str], selector_display_name: str) -> str:
    data = json.dumps({
        "menuItem": selector_id,
        "uiTitle": selector_display_name,
        "armors": armors,
        "allItems": all_item_ids,
    }, ensure_ascii=False, indent=2)
    script = r"""import { world, system, EquipmentSlot, ItemStack } from "@minecraft/server";
import { ActionFormData, MessageFormData, ModalFormData } from "@minecraft/server-ui";

const CONFIG = __CONFIG_JSON__;
const ARMOR_SLOTS = [
  { key: "head", label: "หัว", commandSlot: "slot.armor.head", equipmentSlot: EquipmentSlot.Head, isArmor: true },
  { key: "chest", label: "ตัว", commandSlot: "slot.armor.chest", equipmentSlot: EquipmentSlot.Chest, isArmor: true },
  { key: "legs", label: "กางเกง", commandSlot: "slot.armor.legs", equipmentSlot: EquipmentSlot.Legs, isArmor: true },
  { key: "feet", label: "รองเท้า", commandSlot: "slot.armor.feet", equipmentSlot: EquipmentSlot.Feet, isArmor: true },
];
const OFFHAND_SLOT = { key: "offhand", label: "มือซ้าย", commandSlot: "slot.weapon.offhand", equipmentSlot: EquipmentSlot.Offhand, isArmor: false };
const SLOTS = [...ARMOR_SLOTS, OFFHAND_SLOT];
const SLOT_BY_KEY = new Map(SLOTS.map((slot) => [slot.key, slot]));
const ALL_ADDON_ITEMS = new Set(CONFIG.allItems);
const ITEM_INFO = new Map();
for (const armor of CONFIG.armors) {
  for (const [slotKey, itemId] of Object.entries(armor.items || {})) {
    const slot = SLOT_BY_KEY.get(slotKey);
    if (itemId && slot) ITEM_INFO.set(itemId, { armor, slot, slotKey });
  }
}

function getAvailableSlots(armor) {
  const keys = Object.keys(armor.items || {}).filter((key) => armor.items[key] && SLOT_BY_KEY.has(key));
  return keys.map((key) => SLOT_BY_KEY.get(key));
}

function getEquippedItem(player, slot) {
  try {
    const equipment = player.getComponent("minecraft:equippable");
    if (!equipment) return undefined;
    return equipment.getEquipment(slot.equipmentSlot);
  } catch (error) {
    return undefined;
  }
}

function isAddonItem(item) {
  return !!item && ALL_ADDON_ITEMS.has(item.typeId);
}

function canStackArmor(player) {
  try {
    return player.hasTag("auto_ui_stack_armor");
  } catch (error) {
    return false;
  }
}

function setCanStackArmor(player, enabled) {
  try {
    if (enabled) player.addTag("auto_ui_stack_armor");
    else player.removeTag("auto_ui_stack_armor");
  } catch (error) {}
}

function getItemLabel(item) {
  if (!item || item.typeId === "minecraft:air") return "";
  if (item.nameTag) return item.nameTag;
  return item.typeId;
}

async function runPlayerCommand(player, command) {
  try {
    if (typeof player.runCommandAsync === "function") await player.runCommandAsync(command);
    else player.runCommand(command);
    return true;
  } catch (error) {
    return false;
  }
}

function getEquippable(player) {
  try {
    return player.getComponent("minecraft:equippable");
  } catch (error) {
    return undefined;
  }
}

function purgeFromInventory(player, itemId) {
  try {
    const inventory = player.getComponent("minecraft:inventory");
    const container = inventory && inventory.container;
    if (!container) return;
    for (let i = 0; i < container.size; i++) {
      const item = container.getItem(i);
      if (item && item.typeId === itemId) container.setItem(i, undefined);
    }
  } catch (error) {}
}

async function clearEquipmentSlot(player, slot) {
  try {
    const equipment = getEquippable(player);
    if (equipment) {
      equipment.setEquipment(slot.equipmentSlot, undefined);
      return true;
    }
  } catch (error) {}
  const ok = await runPlayerCommand(player, `replaceitem entity @s ${slot.commandSlot} 0 minecraft:air 1 0`);
  return ok;
}

async function clearAllAddonArmorFromOtherSlots(player, targetSlot) {
  for (const slot of ARMOR_SLOTS) {
    if (slot.key === targetSlot.key) continue;
    const existing = getEquippedItem(player, slot);
    if (isAddonItem(existing)) await clearEquipmentSlot(player, slot);
  }
}

async function replaceEquipment(player, slot, itemId) {
  try {
    const equipment = getEquippable(player);
    if (equipment) {
      purgeFromInventory(player, itemId);
      equipment.setEquipment(slot.equipmentSlot, new ItemStack(itemId, 1));
      try { await system.waitTicks(1); } catch (error) {}
      const equipped = getEquippedItem(player, slot);
      if (equipped && equipped.typeId === itemId) {
        purgeFromInventory(player, itemId);
        return true;
      }
    }
  } catch (error) {}
  return await runPlayerCommand(player, `replaceitem entity @s ${slot.commandSlot} 0 ${itemId} 1 0`);
}

async function showArmorMenu(player) {
  const stackStatus = canStackArmor(player) ? "§aเปิด" : "§cปิด";
  const form = new ActionFormData()
    .title(CONFIG.uiTitle || "รวมไอเท็มใส่ UI")
    .body(`§eเลือกไอเท็มที่ต้องการใส่\n§7ใส่ซ้อนกัน: ${stackStatus}\n\n§b§lAuto convert skin ui§r §7by §eSamSoSleepy\n§9Discord : §ahttps://discord.gg/FnmWw7nWyq`);
  form.button("ตั้งค่า");
  form.button("§cถอดออก");
  for (const armor of CONFIG.armors) form.button(armor.name, armor.icon || undefined);
  const response = await form.show(player);
  if (response.canceled || response.selection === undefined) return;
  if (response.selection === 0) {
    await showSettingsMenu(player);
    return;
  }
  if (response.selection === 1) {
    await showRemoveMenu(player);
    return;
  }
  const armor = CONFIG.armors[response.selection - 2];
  if (!armor) return;
  const availableSlots = getAvailableSlots(armor);
  if (availableSlots.length === 1) {
    await equipItemToSlot(player, armor, availableSlots[0]);
    return;
  }
  await showSlotMenu(player, armor, availableSlots);
}

async function showSettingsMenu(player) {
  const enabled = canStackArmor(player);
  const form = new ModalFormData()
    .title("ตั้งค่า")
    .toggle("ใส่ซ้อนกันได้", enabled);
  const response = await form.show(player);
  if (response.canceled) {
    await showArmorMenu(player);
    return;
  }
  const nextValue = !!(response.formValues && response.formValues[0]);
  setCanStackArmor(player, nextValue);
  player.sendMessage(nextValue ? "§aเปิดการใส่ซ้อนกันแล้ว" : "§eปิดการใส่ซ้อนกันแล้ว");
  await showArmorMenu(player);
}

function getEquippedAddonItems(player) {
  const equipped = [];
  for (const slot of SLOTS) {
    const item = getEquippedItem(player, slot);
    if (!isAddonItem(item)) continue;
    equipped.push({ slot, item, info: ITEM_INFO.get(item.typeId) });
  }
  return equipped;
}

async function removeEquippedItem(player, entry) {
  if (!entry || !entry.slot) return;
  const ok = await clearEquipmentSlot(player, entry.slot);
  if (entry.item) purgeFromInventory(player, entry.item.typeId);
  const label = entry.info && entry.info.armor ? entry.info.armor.name : (entry.item ? entry.item.typeId : "ไอเท็ม");
  player.sendMessage(ok ? `§aถอด ${label} จากช่อง${entry.slot.label}แล้ว` : "§cถอดไอเท็มไม่สำเร็จ");
}

async function showRemoveMenu(player) {
  const equipped = getEquippedAddonItems(player);
  if (!equipped.length) {
    player.sendMessage("§eตอนนี้ไม่ได้ใส่ไอเท็มของ addon นี้อยู่");
    return;
  }
  if (equipped.length === 1) {
    await removeEquippedItem(player, equipped[0]);
    return;
  }
  const form = new ActionFormData()
    .title("ถอดออก")
    .body("เลือกไอเท็มของ addon นี้ที่ต้องการถอด");
  for (const entry of equipped) {
    const name = entry.info && entry.info.armor ? entry.info.armor.name : entry.item.typeId;
    form.button(`§c${name} (${entry.slot.label})`);
  }
  const response = await form.show(player);
  if (response.canceled || response.selection === undefined) return;
  await removeEquippedItem(player, equipped[response.selection]);
}

async function showSlotMenu(player, armor, availableSlots) {
  const slots = availableSlots && availableSlots.length ? availableSlots : getAvailableSlots(armor);
  if (slots.length === 1) {
    await equipItemToSlot(player, armor, slots[0]);
    return;
  }
  const form = new ActionFormData().title(armor.name).body("เลือกช่องที่จะใส่");
  for (const slot of slots) form.button(slot.label);
  const response = await form.show(player);
  if (response.canceled || response.selection === undefined) return;
  const slot = slots[response.selection];
  await equipItemToSlot(player, armor, slot);
}

async function equipItemToSlot(player, armor, slot) {
  const itemId = armor.items[slot.key];
  if (!itemId) {
    player.sendMessage("§cไอเท็มนี้ไม่ได้เปิดให้ใส่ช่องนี้");
    return;
  }
  const existing = getEquippedItem(player, slot);
  if (existing && existing.typeId !== "minecraft:air") {
    await showOverwriteMenu(player, armor, slot, existing, itemId);
    return;
  }
  if (slot.isArmor !== false && !canStackArmor(player)) await clearAllAddonArmorFromOtherSlots(player, slot);
  const ok = await replaceEquipment(player, slot, itemId);
  player.sendMessage(ok ? `§aใส่ ${armor.name} ที่ช่อง${slot.label}แล้ว` : "§cใส่ไอเท็มไม่สำเร็จ");
}

async function showOverwriteMenu(player, armor, slot, existing, itemId) {
  const existingName = getItemLabel(existing);
  const note = isAddonItem(existing)
    ? "ไอเท็มนี้มาจาก addon นี้"
    : "ไอเท็มนี้ไม่ใช่ addon นี้ แต่จะหายไปถ้าทับ";
  const form = new MessageFormData()
    .title("ช่องนี้มีไอเท็มอยู่แล้ว")
    .body(`ช่อง${slot.label}มี ${existingName} อยู่แล้ว\n${note}\n\nต้องการทับเลยไหม?\n§cไอเท็มที่ถูกทับจะหายไป`)
    .button1("ทับเลย")
    .button2("ยกเลิก");
  const response = await form.show(player);
  if (response.canceled || response.selection !== 0) return;
  if (slot.isArmor !== false && !canStackArmor(player)) await clearAllAddonArmorFromOtherSlots(player, slot);
  const ok = await replaceEquipment(player, slot, itemId);
  player.sendMessage(ok ? `§aทับไอเท็มเดิมและใส่ ${armor.name} ที่ช่อง${slot.label}แล้ว` : "§cใส่ไอเท็มไม่สำเร็จ");
}

world.afterEvents.itemUse.subscribe((event) => {
  const player = event.source;
  const item = event.itemStack;
  if (!player || !item || item.typeId !== CONFIG.menuItem) return;
  system.run(() => {
    showArmorMenu(player).catch(() => {
      try { player.sendMessage("§cไม่สามารถเปิด UI ได้ ลองกดใช้อีกครั้ง"); } catch (error) {}
    });
  });
});
"""
    return script.replace("__CONFIG_JSON__", data)

def _lang_label(display_name: str, slot_key: str) -> str:
    slot_th = next((label for key, label, *_ in SLOTS if key == slot_key), slot_key)
    return f"{display_name} ({slot_th})"


def _make_generated_item(identifier: str, icon: str, display_name: str, wearable_slot: str) -> Dict[str, Any]:
    # Armor pieces are real wearable target items used by replaceitem. They must
    # not appear in the Creative inventory. Some Bedrock versions still show
    # custom wearable items when menu_category is omitted, so use the explicit
    # hidden category used by creator tools: {"category": "none"}.
    return {
        "format_version": "1.20.50",
        "minecraft:item": {
            "description": {"identifier": identifier, "menu_category": {"category": "none"}},
            "components": {
                "minecraft:icon": icon,
                "minecraft:max_stack_size": 1,
                "minecraft:display_name": {"value": display_name},
                "minecraft:wearable": {"slot": wearable_slot},
            },
        },
    }


def _normalize_creative_visibility(items_root: Path, selector_id: str) -> None:
    """Keep only the UI selector visible in Creative.

    Bedrock shows custom items in Creative through description.menu_category.
    Wearable generated/original armor pieces must be explicitly placed in
    category "none" so they are hidden from every Creative tab but still
    available by /give and replaceitem. The selector UI item is visible in
    the top-level Equipment category, without any armor sub-group.
    """
    if not items_root.exists():
        return
    for item_path in items_root.rglob("*.json"):
        try:
            data = _read_json(item_path)
        except Exception:
            continue
        item = data.get("minecraft:item") if isinstance(data, dict) else None
        if not isinstance(item, dict):
            continue
        desc = item.get("description")
        comps = item.get("components")
        if not isinstance(desc, dict) or not isinstance(comps, dict):
            continue
        identifier = str(desc.get("identifier") or "")
        if identifier == selector_id:
            desc["menu_category"] = {"category": "equipment"}
        elif "minecraft:wearable" in comps:
            desc["menu_category"] = {"category": "none"}
        _write_json(item_path, data)


def convert_addon(addon_path: str, selected_identifiers: List[str], work_dir: str, slot_mode: str = "all", custom_slots: Optional[List[str]] = None) -> str:
    """Normalize/Rebuild Mode.

    Instead of patching the uploaded addon in-place, this extracts only wearable item
    metadata, attachables, geometry, animations, render controllers, textures and lang
    labels, then rebuilds a clean BP/RP with one visible UI item.
    """
    src = Path(addon_path)
    root = Path(work_dir) / "convert_src"
    out_root = Path(work_dir) / "convert_rebuild"
    if root.exists():
        shutil.rmtree(root)
    if out_root.exists():
        shutil.rmtree(out_root)
    _extract_addon_zip(src, root)
    src_bp, src_rp = _find_packs(root)
    candidates = _scan_candidates(root, src_bp)
    selected = [c for c in candidates if c.identifier in set(selected_identifiers)]
    if not selected:
        raise AddonError("ไม่ได้เลือกไอเท็มที่แปลงได้")

    addon_name = _addon_base_name(src_bp, src_rp)
    safe_addon = _safe_id(addon_name, "auto_ui")
    build_token = _safe_id(uuid4().hex[:8], "ui")
    selector_display_name = f"{addon_name} item ui"

    dst_bp = out_root / "BP_auto_ui"
    dst_rp = out_root / "RP_auto_ui"
    for d in (dst_bp / "items", dst_bp / "scripts", dst_rp / "attachables", dst_rp / "textures" / "item", dst_rp / "textures" / "models" / "armor", dst_rp / "models" / "entity", dst_rp / "animations", dst_rp / "texts"):
        d.mkdir(parents=True, exist_ok=True)

    bp_manifest, rp_manifest = _make_manifest_pair(addon_name)
    _write_json(dst_bp / "manifest.json", bp_manifest)
    _write_json(dst_rp / "manifest.json", rp_manifest)
    _copy_pack_icon(src_bp, src_rp, dst_bp, dst_rp)

    selector_ns = _safe_id(f"{safe_addon}_{build_token}_ui", "auto_ui")
    selector_id = f"{selector_ns}:selector"
    armors: List[Dict[str, Any]] = []
    all_item_ids: List[str] = []
    lang_lines: List[str] = [f"item.{selector_id}.name={selector_display_name}"]
    warnings: List[str] = []
    texture_data: Dict[str, Any] = {}
    selector_icon_key: Optional[str] = None
    selector_pack_icon = _prepare_selector_pack_icon(dst_bp, dst_rp, warnings, build_token)
    if selector_pack_icon:
        selector_icon_key, selector_icon_ref = selector_pack_icon
        texture_data[selector_icon_key] = {"textures": selector_icon_ref}

    for idx, candidate in enumerate(selected, start=1):
        orig_ns, orig_name = _identifier_parts(candidate.identifier)
        item_kind = getattr(candidate, "item_kind", "wearable")
        base_prefix = "offhand" if item_kind == "offhand" else "armor"
        base = f"{base_prefix}_{build_token}_{idx:03d}_{orig_ns}_{orig_name}"
        new_ns = _safe_id(f"{safe_addon}_{build_token}_{idx:03d}", "autoarmor")
        item_path = root / candidate.file_path
        try:
            item_data = _read_json(item_path)
        except Exception:
            item_data = {}
        src_attach = _find_attachable_for_item(src_rp, candidate.identifier, candidate.file_path)
        if not src_attach and item_kind != "offhand":
            warnings.append(f"ไม่พบ attachable สำหรับ {candidate.identifier}; จะสร้าง attachable พื้นฐาน")

        # Prefer explicit icon texture, fallback to first default attachable texture.
        fallback_texture = ""
        if src_attach:
            try:
                desc = _extract_attachable_desc(src_attach)
                texs = desc.get("textures", {})
                if isinstance(texs, dict):
                    fallback_texture = next((v for v in texs.values() if isinstance(v, str) and v.startswith("textures/") and "enchanted" not in v), "")
            except Exception:
                pass
        icon_key = candidate.icon or f"{base}_icon"
        generated_icon_key = f"{base}_icon"
        icon_ref = _copy_icon(src_rp, dst_rp, icon_key, generated_icon_key, fallback_texture, warnings)
        texture_data[generated_icon_key] = {"textures": icon_ref}

        armor_entry = {
            "name": candidate.display_name,
            "icon": icon_ref,
            "items": {},
            "kind": item_kind,
        }

        if item_kind == "offhand":
            new_item_name = f"{base}_offhand"
            new_item_id = f"{new_ns}:{new_item_name}"
            all_item_ids.append(new_item_id)
            armor_entry["items"]["offhand"] = new_item_id
            label = f"{candidate.display_name} (มือซ้าย)"
            item_json = _make_generated_offhand_item(item_data, new_item_id, generated_icon_key, label)
            _write_json(dst_bp / "items" / f"{new_item_name}.json", item_json)
            _normalize_attachable_for_item(src_rp, dst_rp, src_attach, new_item_id, base, warnings)
            lang_lines.append(f"item.{new_item_id}.name={label}")
            lang_lines.append(f"item.{new_item_name}.name={label}")
            armors.append(armor_entry)
            continue

        output_slot_keys = _slot_keys_for_candidate(candidate, slot_mode, custom_slots)
        if output_slot_keys == ["offhand"]:
            armor_entry["kind"] = "offhand"
        if slot_mode == "original" and candidate.wearable_slot not in SLOT_BY_WEARABLE:
            warnings.append(f"ไม่รู้จัก wearable slot เดิมของ {candidate.identifier}: {candidate.wearable_slot}; fallback เป็นกางเกง")
        for slot_key in output_slot_keys:
            slot_label, wearable_slot = SLOTS_BY_KEY[slot_key][1], SLOTS_BY_KEY[slot_key][2]
            new_item_name = f"{base}_{slot_key}"
            new_item_id = f"{new_ns}:{new_item_name}"
            all_item_ids.append(new_item_id)
            armor_entry["items"][slot_key] = new_item_id
            if slot_key == "offhand":
                label = f"{candidate.display_name} (มือซ้าย)"
                item_json = _make_generated_offhand_item(item_data, new_item_id, generated_icon_key, label)
                _write_json(dst_bp / "items" / f"{new_item_name}.json", item_json)
                _normalize_attachable_for_item(src_rp, dst_rp, src_attach, new_item_id, base, warnings)
            else:
                label = _lang_label(candidate.display_name, slot_key)
                item_json = _make_generated_item(new_item_id, generated_icon_key, label, wearable_slot)
                _write_json(dst_bp / "items" / f"{new_item_name}.json", item_json)
                _normalize_attachable_for_slot(src_rp, dst_rp, src_attach, new_item_id, base, slot_key, warnings)
            lang_lines.append(f"item.{new_item_id}.name={label}")
            lang_lines.append(f"item.{new_item_name}.name={label}")
        armors.append(armor_entry)

    selector_item = {
        "format_version": "1.20.50",
        "minecraft:item": {
            "description": {
                "identifier": selector_id,
                "menu_category": {"category": "equipment"},
            },
            "components": {
                "minecraft:icon": selector_icon_key or next(iter(texture_data.keys()), "diamond"),
                "minecraft:display_name": {"value": selector_display_name},
                "minecraft:max_stack_size": 1,
                "minecraft:allow_off_hand": True,
                "minecraft:render_offsets": {
                    "main_hand": {
                        "first_person": {"scale": [0.0, 0.0, 0.0]},
                        "third_person": {"scale": [0.0, 0.0, 0.0]},
                    },
                    "off_hand": {
                        "first_person": {"scale": [0.0, 0.0, 0.0]},
                        "third_person": {"scale": [0.0, 0.0, 0.0]},
                    },
                },
            },
        },
    }
    _write_json(dst_bp / "items" / "addon_ui_selector.json", selector_item)
    _normalize_creative_visibility(dst_bp / "items", selector_id)

    _write_json(dst_rp / "textures" / "item_texture.json", {
        "resource_pack_name": "auto_ui",
        "texture_name": "atlas.items",
        "texture_data": texture_data,
    })
    _resize_item_icon_tree(dst_rp)

    (dst_bp / "scripts" / "main.js").write_text('import "./auto_ui_system.js";\n', encoding="utf-8")
    (dst_bp / "scripts" / "auto_ui_system.js").write_text(_generate_ui_script(selector_id, armors, all_item_ids, selector_display_name), encoding="utf-8")

    unique_lang = list(dict.fromkeys(lang_lines))
    for lang_name in ["en_US.lang", "th_TH.lang"]:
        (dst_rp / "texts" / lang_name).write_text("\n".join(unique_lang) + "\n", encoding="utf-8")
    (dst_rp / "texts" / "languages.json").write_text(json.dumps(["en_US", "th_TH"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = [
        "Normalize/Rebuild Mode report",
        f"Source: {src.name}",
        f"Addon name: {addon_name}",
        f"Selected items: {len(selected)}",
        f"Slot mode: {slot_mode}",
        f"Custom slots: {", ".join(custom_slots or []) if custom_slots else "-"}",
        "",
        "Converted identifiers:",
    ]
    for armor in armors:
        report.append(f"- {armor['name']}")
        for slot_key, item_id in armor["items"].items():
            report.append(f"  {slot_key}: {item_id}")
    if warnings:
        report.extend(["", "Warnings:"])
        report.extend(f"- {w}" for w in warnings)
    (Path(work_dir) / "NORMALIZE_REPORT_WEBHOOK.txt").write_text("\n".join(report) + "\n", encoding="utf-8")

    output_stem = src.stem
    if output_stem.lower().endswith(".mcaddon"):
        output_stem = output_stem[:-8]
    out_path = Path(work_dir) / f"converted_{output_stem}.mcaddon"
    if out_path.exists():
        out_path.unlink()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(out_root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(out_root).as_posix())
    # final validation
    with zipfile.ZipFile(out_path, "r") as zf:
        bad = zf.testzip()
        if bad:
            raise AddonError(f"zip validation failed at {bad}")
    return str(out_path)

def convert_addons_to_ui(addon_paths: List[str], selected_refs: List[str], work_dir: str, slot_mode: str = "all", custom_slots: Optional[List[str]] = None) -> str:
    """Build one UI addon from one or more uploaded normal addons.

    selected_refs values use the format ``<source_index>:<identifier>`` where
    source_index is 1-based. The function extracts each addon separately and
    copies only the selected item resource chain into one generated UI addon.
    """
    if not (1 <= len(addon_paths) <= 10):
        raise AddonError("ระบบรวมไอเท็มเป็น UI รองรับ 1-10 ไฟล์ต่อครั้ง")
    selected_ref_set = {str(x) for x in (selected_refs or []) if str(x)}
    if not selected_ref_set:
        raise AddonError("ไม่ได้เลือกไอเท็มที่แปลงได้")

    work = Path(work_dir)
    sources_root = work / "multi_convert_src"
    out_root = work / "convert_rebuild"
    if sources_root.exists():
        shutil.rmtree(sources_root)
    if out_root.exists():
        shutil.rmtree(out_root)
    sources_root.mkdir(parents=True, exist_ok=True)

    sources: List[Dict[str, Any]] = []
    selected_records: List[Tuple[int, Path, Path, Path, AddonItemCandidate]] = []
    pack_names: List[str] = []
    first_bp: Optional[Path] = None
    first_rp: Optional[Path] = None
    for source_index, addon_path in enumerate(addon_paths, start=1):
        src = Path(addon_path)
        if not src.exists() or not zipfile.is_zipfile(src):
            raise AddonError(f"ไฟล์ไม่ใช่ addon/zip ที่เปิดได้: {src.name}")
        root = sources_root / f"source_{source_index}"
        _extract_addon_zip(src, root)
        src_bp, src_rp = _find_packs(root)
        if first_bp is None:
            first_bp, first_rp = src_bp, src_rp
        addon_name = _addon_base_name(src_bp, src_rp)
        pack_names.append(addon_name)
        candidates = _scan_candidates(root, src_bp)
        for c in candidates:
            if f"{source_index}:{c.identifier}" in selected_ref_set:
                selected_records.append((source_index, root, src_bp, src_rp, c))
        sources.append({"index": source_index, "path": src, "root": root, "bp": src_bp, "rp": src_rp, "name": addon_name})

    if not selected_records:
        raise AddonError("ไม่ได้เลือกไอเท็มที่แปลงได้")

    if len(pack_names) == 1:
        addon_name = pack_names[0]
    else:
        addon_name = " + ".join(pack_names[:2])
        if len(pack_names) > 2:
            addon_name += f" + {len(pack_names)-2} addons"
    safe_addon = _safe_id(addon_name, "auto_ui")
    build_token = _safe_id(uuid4().hex[:8], "ui")
    selector_display_name = f"{addon_name} item ui"

    dst_bp = out_root / "BP_auto_ui"
    dst_rp = out_root / "RP_auto_ui"
    for d in (dst_bp / "items", dst_bp / "scripts", dst_rp / "attachables", dst_rp / "textures" / "item", dst_rp / "textures" / "models" / "armor", dst_rp / "models" / "entity", dst_rp / "animations", dst_rp / "texts"):
        d.mkdir(parents=True, exist_ok=True)

    bp_manifest, rp_manifest = _make_manifest_pair(addon_name)
    _write_json(dst_bp / "manifest.json", bp_manifest)
    _write_json(dst_rp / "manifest.json", rp_manifest)
    if first_bp is not None and first_rp is not None:
        _copy_pack_icon(first_bp, first_rp, dst_bp, dst_rp)

    selector_ns = _safe_id(f"{safe_addon}_{build_token}_ui", "auto_ui")
    selector_id = f"{selector_ns}:selector"
    armors: List[Dict[str, Any]] = []
    all_item_ids: List[str] = []
    lang_lines: List[str] = [f"item.{selector_id}.name={selector_display_name}"]
    warnings: List[str] = []
    texture_data: Dict[str, Any] = {}
    selector_icon_key: Optional[str] = None
    selector_pack_icon = _prepare_selector_pack_icon(dst_bp, dst_rp, warnings, build_token)
    if selector_pack_icon:
        selector_icon_key, selector_icon_ref = selector_pack_icon
        texture_data[selector_icon_key] = {"textures": selector_icon_ref}

    for global_idx, (source_index, root, src_bp, src_rp, candidate) in enumerate(selected_records, start=1):
        orig_ns, orig_name = _identifier_parts(candidate.identifier)
        item_kind = getattr(candidate, "item_kind", "wearable")
        base_prefix = "offhand" if item_kind == "offhand" else "armor"
        base = f"s{source_index:02d}_{base_prefix}_{build_token}_{global_idx:03d}_{orig_ns}_{orig_name}"
        source_safe = _safe_id(pack_names[source_index-1] if source_index-1 < len(pack_names) else f"source_{source_index}", f"source_{source_index}")
        new_ns = _safe_id(f"{safe_addon}_{build_token}_s{source_index:02d}_{global_idx:03d}_{source_safe}", "autoarmor")
        item_path = root / candidate.file_path
        try:
            item_data = _read_json(item_path)
        except Exception:
            item_data = {}
        src_attach = _find_attachable_for_item(src_rp, candidate.identifier, candidate.file_path)
        if not src_attach and item_kind != "offhand":
            warnings.append(f"ไม่พบ attachable สำหรับ {candidate.identifier}; จะสร้าง attachable พื้นฐาน")

        fallback_texture = ""
        if src_attach:
            try:
                desc = _extract_attachable_desc(src_attach)
                texs = desc.get("textures", {})
                if isinstance(texs, dict):
                    fallback_texture = next((v for v in texs.values() if isinstance(v, str) and v.startswith("textures/") and "enchanted" not in v), "")
            except Exception:
                pass
        icon_key = candidate.icon or f"{base}_icon"
        generated_icon_key = f"{base}_icon"
        icon_ref = _copy_icon(src_rp, dst_rp, icon_key, generated_icon_key, fallback_texture, warnings)
        texture_data[generated_icon_key] = {"textures": icon_ref}

        armor_entry = {
            "name": candidate.display_name,
            "icon": icon_ref,
            "items": {},
            "kind": item_kind,
        }

        if item_kind == "offhand":
            new_item_name = f"{base}_offhand"
            new_item_id = f"{new_ns}:{new_item_name}"
            all_item_ids.append(new_item_id)
            armor_entry["items"]["offhand"] = new_item_id
            label = f"{candidate.display_name} (มือซ้าย)"
            item_json = _make_generated_offhand_item(item_data, new_item_id, generated_icon_key, label)
            _write_json(dst_bp / "items" / f"{new_item_name}.json", item_json)
            _normalize_attachable_for_item(src_rp, dst_rp, src_attach, new_item_id, base, warnings)
            lang_lines.append(f"item.{new_item_id}.name={label}")
            lang_lines.append(f"item.{new_item_name}.name={label}")
            armors.append(armor_entry)
            continue

        output_slot_keys = _slot_keys_for_candidate(candidate, slot_mode, custom_slots)
        if output_slot_keys == ["offhand"]:
            armor_entry["kind"] = "offhand"
        if slot_mode == "original" and candidate.wearable_slot not in SLOT_BY_WEARABLE:
            warnings.append(f"ไม่รู้จัก wearable slot เดิมของ {candidate.identifier}: {candidate.wearable_slot}; fallback เป็นกางเกง")
        for slot_key in output_slot_keys:
            wearable_slot = SLOTS_BY_KEY[slot_key][2]
            new_item_name = f"{base}_{slot_key}"
            new_item_id = f"{new_ns}:{new_item_name}"
            all_item_ids.append(new_item_id)
            armor_entry["items"][slot_key] = new_item_id
            if slot_key == "offhand":
                label = f"{candidate.display_name} (มือซ้าย)"
                item_json = _make_generated_offhand_item(item_data, new_item_id, generated_icon_key, label)
                _write_json(dst_bp / "items" / f"{new_item_name}.json", item_json)
                _normalize_attachable_for_item(src_rp, dst_rp, src_attach, new_item_id, base, warnings)
            else:
                label = _lang_label(candidate.display_name, slot_key)
                item_json = _make_generated_item(new_item_id, generated_icon_key, label, wearable_slot)
                _write_json(dst_bp / "items" / f"{new_item_name}.json", item_json)
                _normalize_attachable_for_slot(src_rp, dst_rp, src_attach, new_item_id, base, slot_key, warnings)
            lang_lines.append(f"item.{new_item_id}.name={label}")
            lang_lines.append(f"item.{new_item_name}.name={label}")
        armors.append(armor_entry)

    selector_item = {
        "format_version": "1.20.50",
        "minecraft:item": {
            "description": {
                "identifier": selector_id,
                "menu_category": {"category": "equipment"},
            },
            "components": {
                "minecraft:icon": selector_icon_key or next(iter(texture_data.keys()), "diamond"),
                "minecraft:display_name": {"value": selector_display_name},
                "minecraft:max_stack_size": 1,
                "minecraft:allow_off_hand": True,
                "minecraft:render_offsets": {
                    "main_hand": {
                        "first_person": {"scale": [0.0, 0.0, 0.0]},
                        "third_person": {"scale": [0.0, 0.0, 0.0]},
                    },
                    "off_hand": {
                        "first_person": {"scale": [0.0, 0.0, 0.0]},
                        "third_person": {"scale": [0.0, 0.0, 0.0]},
                    },
                },
            },
        },
    }
    _write_json(dst_bp / "items" / "addon_ui_selector.json", selector_item)
    _normalize_creative_visibility(dst_bp / "items", selector_id)

    _write_json(dst_rp / "textures" / "item_texture.json", {
        "resource_pack_name": "auto_ui",
        "texture_name": "atlas.items",
        "texture_data": texture_data,
    })
    _resize_item_icon_tree(dst_rp)

    (dst_bp / "scripts" / "main.js").write_text('import "./auto_ui_system.js";\n', encoding="utf-8")
    (dst_bp / "scripts" / "auto_ui_system.js").write_text(_generate_ui_script(selector_id, armors, all_item_ids, selector_display_name), encoding="utf-8")

    unique_lang = list(dict.fromkeys(lang_lines))
    for lang_name in ["en_US.lang", "th_TH.lang"]:
        (dst_rp / "texts" / lang_name).write_text("\n".join(unique_lang) + "\n", encoding="utf-8")
    (dst_rp / "texts" / "languages.json").write_text(json.dumps(["en_US", "th_TH"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = [
        "Multi-source Normalize/Rebuild Mode report",
        f"Sources: {len(addon_paths)}",
        f"Addon name: {addon_name}",
        f"Selected items: {len(selected_records)}",
        f"Slot mode: {slot_mode}",
        f"Custom slots: {', '.join(custom_slots or []) if custom_slots else '-'}",
        "",
        "Converted identifiers:",
    ]
    for armor in armors:
        report.append(f"- {armor['name']}")
        for slot_key, item_id in armor["items"].items():
            report.append(f"  {slot_key}: {item_id}")
    if warnings:
        report.extend(["", "Warnings:"])
        report.extend(f"- {w}" for w in warnings)
    (Path(work_dir) / "NORMALIZE_REPORT_WEBHOOK.txt").write_text("\n".join(report) + "\n", encoding="utf-8")

    out_path = Path(work_dir) / ("converted_combined_ui.mcaddon" if len(addon_paths) > 1 else f"converted_{Path(addon_paths[0]).stem}.mcaddon")
    if out_path.exists():
        out_path.unlink()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(out_root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(out_root).as_posix())
    with zipfile.ZipFile(out_path, "r") as zf:
        bad = zf.testzip()
        if bad:
            raise AddonError(f"zip validation failed at {bad}")
    return str(out_path)

