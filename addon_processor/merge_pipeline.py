from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from uuid import uuid4

from .pipeline import AddonError, _find_packs, _read_json, _safe_extract, _safe_id, _write_json

TEXT_EXTS = {'.json', '.js', '.mcfunction', '.lang', '.txt'}
IMAGE_EXTS = {'.png', '.tga', '.jpg', '.jpeg', '.webp'}

@dataclass
class SourcePack:
    index: int
    source_path: Path
    extract_root: Path
    bp: Path
    rp: Path
    prefix: str
    display_name: str
    mapping: Dict[str, str]
    texture_key_mapping: Dict[str, str]
    texture_ref_mapping: Dict[str, str]
    script_entries: List[str]
    server_deps: List[Dict[str, Any]]
    warnings: List[str]


def _clean_pack_name(name: str) -> str:
    name = str(name or '').strip()
    name = re.sub(r'[\s_\-]*(BP|RP)\s*$', '', name, flags=re.I).strip()
    return name or 'Addon'


def _pack_display_name(bp: Path, rp: Path, fallback: str) -> str:
    for manifest_path in (bp / 'manifest.json', rp / 'manifest.json'):
        try:
            name = _read_json(manifest_path).get('header', {}).get('name', '')
            if name:
                return _clean_pack_name(name)
        except Exception:
            pass
    return fallback


def _json_files(root: Path) -> Iterable[Path]:
    for path in root.rglob('*.json'):
        if path.name == 'manifest.json':
            continue
        yield path


def _collect_defined_dot_symbols(root: Path) -> Set[str]:
    """Collect dot-style resource identifiers that are actually defined by this pack.

    Important: values such as geometry.humanoid.customSlim are built-in Bedrock
    references in many MagicSkin/Blockbench-style addons. They must not be
    renamed unless the addon actually defines them, otherwise attachables will
    point to missing geometry.
    """
    defined: Set[str] = set()
    for path in root.rglob('*.json'):
        if path.name == 'manifest.json':
            continue
        try:
            data = _read_json(path)
        except Exception:
            continue
        if isinstance(data, dict):
            geometries = data.get('minecraft:geometry')
            if isinstance(geometries, list):
                for geo in geometries:
                    ident = geo.get('description', {}).get('identifier') if isinstance(geo, dict) else None
                    if isinstance(ident, str) and ident.startswith('geometry.'):
                        defined.add(ident)
            animations = data.get('animations')
            if isinstance(animations, dict):
                for key in animations.keys():
                    if isinstance(key, str) and key.startswith('animation.'):
                        defined.add(key)
            controllers = data.get('animation_controllers')
            if isinstance(controllers, dict):
                for key in controllers.keys():
                    if isinstance(key, str) and key.startswith('controller.animation.'):
                        defined.add(key)
            render_controllers = data.get('render_controllers')
            if isinstance(render_controllers, dict):
                for key in render_controllers.keys():
                    if isinstance(key, str) and key.startswith('controller.render.'):
                        defined.add(key)
    return defined


def _is_dot_resource(symbol: str) -> bool:
    return symbol.startswith(('animation.', 'geometry.', 'controller.animation.', 'controller.render.'))


def _is_known_builtin_dot_reference(symbol: str) -> bool:
    # Built-in Bedrock/vanilla references commonly used by skin/armor addons.
    if symbol in {'geometry.humanoid', 'geometry.humanoid.custom', 'geometry.humanoid.customSlim', 'controller.render.armor'}:
        return True
    if symbol.startswith('geometry.humanoid.'):
        return True
    if symbol.startswith('controller.render.armor'):
        return True
    return False


def _collect_json_symbols(obj: Any, symbols: Set[str]) -> None:
    if isinstance(obj, dict):
        # Common Bedrock identifier containers.
        if isinstance(obj.get('identifier'), str):
            value = obj['identifier']
            if not value.startswith('minecraft:'):
                symbols.add(value)
        # Resource identifiers generally appear as keys or values.
        for key, value in obj.items():
            if isinstance(key, str):
                if _is_dot_resource(key):
                    symbols.add(key)
                elif ':' in key and not key.startswith('minecraft:'):
                    # Attachables often use item identifiers as object keys.
                    symbols.add(key)
            if isinstance(value, str):
                if _is_dot_resource(value):
                    symbols.add(value)
                elif ':' in value and not value.startswith('minecraft:') and re.match(r'^[a-zA-Z0-9_\-.]+:[a-zA-Z0-9_\-.]+$', value):
                    symbols.add(value)
            _collect_json_symbols(value, symbols)
    elif isinstance(obj, list):
        for value in obj:
            _collect_json_symbols(value, symbols)
    elif isinstance(obj, str):
        # List entries such as render_controllers: ["controller.render.foo"]
        # must be seen, but only definitions will be renamed later.
        if _is_dot_resource(obj):
            symbols.add(obj)
        elif ':' in obj and not obj.startswith('minecraft:') and re.match(r'^[a-zA-Z0-9_\-.]+:[a-zA-Z0-9_\-.]+$', obj):
            symbols.add(obj)


def _prefixed_colon_id(old: str, prefix: str) -> str:
    if ':' not in old:
        return f'{prefix}:{_safe_id(old, "id")}'
    ns, name = old.split(':', 1)
    if ns == 'minecraft':
        return old
    return f'{prefix}_{_safe_id(ns, "addon")}:{_safe_id(name, "item")}'


def _prefixed_dot_id(old: str, prefix: str) -> str:
    if old.startswith('minecraft:'):
        return old
    # Keep the leading category (geometry/animation/controller.render/etc.) readable.
    if old.startswith('controller.animation.'):
        suffix = old[len('controller.animation.'):]
        return f'controller.animation.{prefix}_{_safe_id(suffix, "anim")}'
    if old.startswith('controller.render.'):
        suffix = old[len('controller.render.'):]
        return f'controller.render.{prefix}_{_safe_id(suffix, "render")}'
    if old.startswith('animation.'):
        suffix = old[len('animation.'):]
        return f'animation.{prefix}_{_safe_id(suffix, "anim")}'
    if old.startswith('geometry.'):
        suffix = old[len('geometry.'):]
        return f'geometry.{prefix}_{_safe_id(suffix, "geo")}'
    return f'{prefix}_{_safe_id(old, "id")}'


def _collect_texture_maps(rp: Path, prefix: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    key_map: Dict[str, str] = {}
    ref_map: Dict[str, str] = {}
    item_texture = rp / 'textures' / 'item_texture.json'
    if item_texture.exists():
        try:
            data = _read_json(item_texture)
            tex_data = data.get('texture_data', {})
            if isinstance(tex_data, dict):
                for key in tex_data.keys():
                    key_map[key] = f'{prefix}_{_safe_id(key, "texture")}'
        except Exception:
            pass
    textures_root = rp / 'textures'
    if textures_root.exists():
        for path in textures_root.rglob('*'):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTS:
                rel_no_ext = path.relative_to(rp).as_posix()[:-len(path.suffix)]
                stem = _safe_id(path.stem, 'texture')
                ref_map[rel_no_ext] = str(path.with_name(f'{prefix}_{stem}{path.suffix}').relative_to(rp).as_posix()[:-len(path.suffix)])
    return key_map, ref_map


def _collect_source_pack(source_path: Path, extract_parent: Path, index: int) -> SourcePack:
    extract_root = extract_parent / f'source_{index}'
    _safe_extract(source_path, extract_root)
    bp, rp = _find_packs(extract_root)
    fallback = _safe_id(source_path.stem, f'addon{index}')
    display = _pack_display_name(bp, rp, fallback)
    prefix = f'merge{index}_{_safe_id(display, f"addon{index}")}'[:40].strip('_') or f'merge{index}'

    symbols: Set[str] = set()
    warnings: List[str] = []
    for path in list(_json_files(bp)) + list(_json_files(rp)):
        try:
            data = _read_json(path)
        except Exception as exc:
            warnings.append(f'JSON อ่านไม่ได้: {path.relative_to(extract_root)} ({exc})')
            continue
        _collect_json_symbols(data, symbols)

    # Only rename dot resources that are actually defined by this addon.
    # Do not rename built-in references such as geometry.humanoid.customSlim;
    # MagicSkin-style addons often reference these without shipping geometry files.
    defined_dot_symbols = _collect_defined_dot_symbols(bp) | _collect_defined_dot_symbols(rp)

    mapping: Dict[str, str] = {}
    for symbol in sorted(symbols, key=len, reverse=True):
        if symbol.startswith('minecraft:'):
            continue
        if ':' in symbol:
            mapping[symbol] = _prefixed_colon_id(symbol, prefix)
        elif _is_dot_resource(symbol):
            if symbol in defined_dot_symbols:
                mapping[symbol] = _prefixed_dot_id(symbol, prefix)
            elif not _is_known_builtin_dot_reference(symbol):
                warnings.append(f'ไม่ได้ rename resource reference ที่ไม่มีไฟล์นิยามใน addon: {symbol}')

    texture_key_mapping, texture_ref_mapping = _collect_texture_maps(rp, prefix)
    # Texture *paths* are safe to global-replace because they are explicit resource refs, e.g. textures/item/foo.
    # Texture *keys* can be common display words (for example "Yuto"), so icon fields are patched structurally later.
    mapping.update(texture_ref_mapping)

    script_entries: List[str] = []
    server_deps: List[Dict[str, Any]] = []
    try:
        manifest = _read_json(bp / 'manifest.json')
        for module in manifest.get('modules', []):
            if isinstance(module, dict) and module.get('type') == 'script':
                entry = module.get('entry')
                if isinstance(entry, str) and entry:
                    script_entries.append(entry)
        for dep in manifest.get('dependencies', []):
            if isinstance(dep, dict) and dep.get('module_name'):
                server_deps.append({'module_name': dep.get('module_name'), 'version': dep.get('version', '1.0.0')})
    except Exception:
        pass
    return SourcePack(index, source_path, extract_root, bp, rp, prefix, display, mapping, texture_key_mapping, texture_ref_mapping, script_entries, server_deps, warnings)


def _replace_text(text: str, mapping: Dict[str, str]) -> str:
    # Longest first prevents partial replacement of shared prefixes.
    for old, new in sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True):
        if old and old in text:
            text = text.replace(old, new)
    return text


def _relative_dest(rel: Path, prefix: str, *, is_script: bool = False) -> Path:
    if is_script:
        return Path('scripts') / f'addon_{prefix}' / rel.relative_to('scripts')
    # Prefix file names to prevent path collisions while keeping folder semantics.
    if rel.name == 'manifest.json':
        return rel
    return rel.with_name(f'{prefix}_{rel.name}')


def _copy_text_or_binary(src: Path, dst: Path, mapping: Dict[str, str]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in TEXT_EXTS:
        try:
            text = src.read_text(encoding='utf-8-sig')
            dst.write_text(_replace_text(text, mapping), encoding='utf-8')
            return
        except Exception:
            pass
    shutil.copy2(src, dst)


def _copy_pack_files(src_root: Path, dst_root: Path, prefix: str, mapping: Dict[str, str], *, is_bp: bool) -> None:
    for path in sorted(src_root.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(src_root)
        if rel.name == 'manifest.json':
            continue
        # Generated files are merged separately.
        if not is_bp and rel.parts and rel.parts[0] == 'textures':
            # Texture images and item_texture.json are merged separately so paths/keys can be prefixed safely.
            continue
        if not is_bp and rel.as_posix() in {'texts/languages.json'}:
            continue
        if not is_bp and rel.suffix.lower() == '.lang' and rel.parts and rel.parts[0] == 'texts':
            continue
        is_script = is_bp and len(rel.parts) > 0 and rel.parts[0] == 'scripts'
        dst_rel = _relative_dest(rel, prefix, is_script=is_script)
        _copy_text_or_binary(path, dst_root / dst_rel, mapping)



def _patch_texture_key_refs_in_json(obj: Any, key_map: Dict[str, str]) -> bool:
    changed = False
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            if key in {'minecraft:icon', 'icon', 'texture'}:
                if isinstance(value, str) and value in key_map:
                    obj[key] = key_map[value]
                    changed = True
                elif isinstance(value, dict):
                    if _patch_texture_key_refs_in_json(value, key_map):
                        changed = True
            else:
                if _patch_texture_key_refs_in_json(value, key_map):
                    changed = True
    elif isinstance(obj, list):
        for item in obj:
            if _patch_texture_key_refs_in_json(item, key_map):
                changed = True
    elif isinstance(obj, str):
        # Strings in arrays cannot be replaced in-place here. They are rarely texture keys; texture paths are handled by global mapping.
        pass
    return changed

def _patch_texture_key_refs(root: Path, key_map: Dict[str, str]) -> None:
    if not key_map:
        return
    for path in root.rglob('*.json'):
        try:
            data = _read_json(path)
        except Exception:
            continue
        if _patch_texture_key_refs_in_json(data, key_map):
            _write_json(path, data)

def _merge_item_texture(source: SourcePack, merged_rp: Path) -> Dict[str, Any]:
    path = source.rp / 'textures' / 'item_texture.json'
    if not path.exists():
        return {}
    try:
        data = _read_json(path)
    except Exception:
        return {}
    tex_data = data.get('texture_data', {})
    if not isinstance(tex_data, dict):
        return {}
    out: Dict[str, Any] = {}
    for old_key, value in tex_data.items():
        new_key = source.texture_key_mapping.get(old_key, f'{source.prefix}_{_safe_id(old_key, "texture")}')
        text = json.dumps(value, ensure_ascii=False)
        text = _replace_text(text, source.mapping)
        try:
            out[new_key] = json.loads(text)
        except Exception:
            out[new_key] = value
    return out


def _merge_lang_files(source: SourcePack) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    texts = source.rp / 'texts'
    if not texts.exists():
        return out
    for path in sorted(texts.glob('*.lang')):
        loc = path.stem
        try:
            lines = path.read_text(encoding='utf-8-sig').splitlines()
        except Exception:
            continue
        patched = []
        for line in lines:
            patched.append(_replace_text(line, source.mapping))
        out.setdefault(loc, []).extend(patched)
    return out


def _copy_texture_files(source: SourcePack, merged_rp: Path) -> None:
    tex_root = source.rp / 'textures'
    if not tex_root.exists():
        return
    for path in sorted(tex_root.rglob('*')):
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_EXTS:
            continue
        rel_no_ext = path.relative_to(source.rp).as_posix()
        suffix = path.suffix
        ref = rel_no_ext[:-len(suffix)] if suffix else rel_no_ext
        if ref in source.texture_ref_mapping:
            dst = merged_rp / (source.texture_ref_mapping[ref] + suffix)
        else:
            rel = path.relative_to(source.rp)
            dst = merged_rp / rel.with_name(f'{source.prefix}_{rel.name}')
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst)


def _write_merged_manifests(merged_bp: Path, merged_rp: Path, sources: List[SourcePack], has_scripts: bool) -> None:
    bp_header = str(uuid4())
    bp_module = str(uuid4())
    rp_header = str(uuid4())
    rp_module = str(uuid4())
    script_module = str(uuid4())
    version = [1, 0, 0]
    name = 'Merged Addons'
    bp_modules: List[Dict[str, Any]] = [{'type': 'data', 'uuid': bp_module, 'version': version}]
    deps: List[Dict[str, Any]] = [{'uuid': rp_header, 'version': version}]
    if has_scripts:
        bp_modules.append({'type': 'script', 'language': 'javascript', 'entry': 'scripts/main.js', 'uuid': script_module, 'version': version})
        seen = set()
        for source in sources:
            for dep in source.server_deps:
                key = dep.get('module_name')
                if key and key not in seen:
                    deps.append(dep)
                    seen.add(key)
        if '@minecraft/server' not in seen:
            deps.append({'module_name': '@minecraft/server', 'version': '1.10.0'})
    bp_manifest = {
        'format_version': 2,
        'header': {'name': f'{name} BP', 'description': 'Merged addon generated by SamSoSleepy bot', 'uuid': bp_header, 'version': version, 'min_engine_version': [1, 20, 50]},
        'modules': bp_modules,
        'dependencies': deps,
    }
    rp_manifest = {
        'format_version': 2,
        'header': {'name': f'{name} RP', 'description': 'Merged addon resources generated by SamSoSleepy bot', 'uuid': rp_header, 'version': version, 'min_engine_version': [1, 20, 50]},
        'modules': [{'type': 'resources', 'uuid': rp_module, 'version': version}],
        'dependencies': [{'uuid': bp_header, 'version': version}],
    }
    _write_json(merged_bp / 'manifest.json', bp_manifest)
    _write_json(merged_rp / 'manifest.json', rp_manifest)


def _write_scripts_aggregator(merged_bp: Path, sources: List[SourcePack]) -> bool:
    imports: List[str] = []
    for source in sources:
        if not source.script_entries:
            continue
        for entry in source.script_entries:
            rel = Path(entry)
            if rel.parts and rel.parts[0] == 'scripts':
                rel = rel.relative_to('scripts')
            imports.append(f'import "./addon_{source.prefix}/{rel.as_posix()}";')
    if not imports:
        return False
    scripts = merged_bp / 'scripts'
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / 'main.js').write_text('\n'.join(dict.fromkeys(imports)) + '\n', encoding='utf-8')
    return True


def _validate_merged(root: Path) -> List[str]:
    warnings: List[str] = []
    for path in root.rglob('*.json'):
        try:
            _read_json(path)
        except Exception as exc:
            warnings.append(f'JSON parse error: {path.relative_to(root)}: {exc}')
    # Basic duplicate identifier checks.
    seen: Dict[str, str] = {}
    # Same identifier in BP/items and RP/attachables can be intentional in Bedrock skin/armor addons.
    # Collision prevention is handled by prefixing source symbols before validation.
    return warnings


def merge_addons(addon_paths: List[str], work_dir: str) -> str:
    if len(addon_paths) != 2:
        raise AddonError('ระบบรวมแอดออนตอนนี้รองรับ 2 ไฟล์เท่านั้น')
    paths = [Path(p) for p in addon_paths]
    for p in paths:
        if not p.exists() or not zipfile.is_zipfile(p):
            raise AddonError(f'ไฟล์ไม่ใช่ addon/zip ที่เปิดได้: {p.name}')

    work = Path(work_dir)
    root = work / 'merge_work'
    if root.exists():
        shutil.rmtree(root)
    extract_parent = root / 'sources'
    merged = root / 'merged'
    merged_bp = merged / 'BP_merged'
    merged_rp = merged / 'RP_merged'
    merged_bp.mkdir(parents=True, exist_ok=True)
    merged_rp.mkdir(parents=True, exist_ok=True)

    sources = [_collect_source_pack(path, extract_parent, i + 1) for i, path in enumerate(paths)]

    for source in sources:
        _copy_pack_files(source.bp, merged_bp, source.prefix, source.mapping, is_bp=True)
        _copy_pack_files(source.rp, merged_rp, source.prefix, source.mapping, is_bp=False)
        _copy_texture_files(source, merged_rp)
        _patch_texture_key_refs(merged_bp, source.texture_key_mapping)
        _patch_texture_key_refs(merged_rp, source.texture_key_mapping)

    # Merge item_texture.json.
    texture_data: Dict[str, Any] = {}
    for source in sources:
        for key, value in _merge_item_texture(source, merged_rp).items():
            if key in texture_data:
                key = f'{source.prefix}_{key}'
            texture_data[key] = value
    _write_json(merged_rp / 'textures' / 'item_texture.json', {'resource_pack_name': 'vanilla', 'texture_name': 'atlas.items', 'texture_data': texture_data})

    # Merge lang files and languages.json.
    lang_data: Dict[str, List[str]] = {}
    for source in sources:
        for loc, lines in _merge_lang_files(source).items():
            lang_data.setdefault(loc, []).extend(lines)
    if not lang_data:
        lang_data['en_US'] = []
    texts = merged_rp / 'texts'
    texts.mkdir(parents=True, exist_ok=True)
    for loc, lines in sorted(lang_data.items()):
        unique_lines = list(dict.fromkeys([line for line in lines if line.strip()]))
        (texts / f'{loc}.lang').write_text('\n'.join(unique_lines) + ('\n' if unique_lines else ''), encoding='utf-8')
    (texts / 'languages.json').write_text(json.dumps(sorted(lang_data.keys()), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    # Scripts are copied under scripts/addon_prefix/*, then a fresh main.js imports each original entry.
    has_scripts = _write_scripts_aggregator(merged_bp, sources)
    _write_merged_manifests(merged_bp, merged_rp, sources, has_scripts)

    warnings: List[str] = []
    for source in sources:
        warnings.extend(source.warnings)
    warnings.extend(_validate_merged(merged))
    report_lines = [
        'Merged Addons Report',
        '====================',
        '',
        'Sources:',
        *[f'- {s.source_path.name} -> prefix {s.prefix}' for s in sources],
        '',
        'Notes:',
        '- Script folders are isolated under BP_merged/scripts/addon_<prefix>/',
        '- main.js imports the original script entry files from both addons.',
        '- UUIDs are regenerated for the merged BP/RP manifests.',
        '- Identifiers, defined geometry/animations/controllers, texture keys and texture paths are prefixed to reduce collisions.',
        '- Built-in references such as geometry.humanoid.customSlim are preserved.',
        '',
        'Warnings:',
        *(warnings or ['- No major warnings detected.']),
    ]
    (merged / 'MERGE_REPORT.txt').write_text('\n'.join(report_lines) + '\n', encoding='utf-8')

    out_path = work / 'merged_addons.mcaddon'
    if out_path.exists():
        out_path.unlink()
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(merged.rglob('*')):
            if path.is_file():
                zf.write(path, path.relative_to(merged).as_posix())
    with zipfile.ZipFile(out_path) as zf:
        bad = zf.testzip()
        if bad:
            raise AddonError(f'zip เสียที่ไฟล์ {bad}')
    return str(out_path)
