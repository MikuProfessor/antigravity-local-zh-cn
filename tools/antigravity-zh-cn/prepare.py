"""Build Antigravity 2.29.0 Chinese UI files without installing or launching them.

The original application is read-only input. All generated candidates, resource
backups, and the review report are written beneath --output. macOS installation
and signing are separate operations; local signing may require relaxing the
application's library validation and does not preserve Google's signing trust.
"""
from pathlib import Path
import argparse
import copy
import hashlib
import json
import plistlib
import shutil
import struct
import subprocess

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT.parent.parent / 'runtime/antigravity-zh-cn-build'
COMMIT = '229658784a100e024325b6bb2ae49f574eae86d1'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unpack(data):
    _, header_size, _, json_size = struct.unpack('<4I', data[:16])
    return json.loads(data[16:16 + json_size]), 8 + header_size


def entries(node, prefix=''):
    for name, entry in node.get('files', {}).items():
        path = prefix + name
        if 'files' in entry:
            yield from entries(entry, path + '/')
        else:
            yield path, entry


def file_bytes(data, base, entry):
    start = base + int(entry['offset'])
    return data[start:start + entry['size']]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True, help='Path to an unmodified official Antigravity 2.29.0 .app bundle (read only)')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT, help='Build output directory; defaults to the project runtime/antigravity-zh-cn-build directory')
    parser.add_argument('--node', default=shutil.which('node'), help='Node.js executable used only for --check; defaults to node from PATH')
    args = parser.parse_args()
    app = args.app.expanduser().resolve()
    build_root = args.output.expanduser().resolve()
    if app == build_root or app in build_root.parents:
        parser.error('--output must not be inside the original application')
    if ROOT == build_root or ROOT in build_root.parents:
        parser.error('--output must not be inside this tool source directory')
    if not args.node:
        parser.error('Node.js was not found in PATH; install it or supply --node')
    source = app / 'Contents/Resources/app.asar'
    if not source.is_file() or not (app / 'Contents/Info.plist').is_file():
        parser.error('--app must point to an Antigravity .app bundle containing app.asar and Info.plist')
    out = build_root / 'candidate'
    backup = build_root / 'original-resources'
    original = source.read_bytes()
    plist_bytes = (app / 'Contents/Info.plist').read_bytes()
    plist = plistlib.loads(plist_bytes)
    assert plist['CFBundleShortVersionString'] == '2.29.0'
    header, base = unpack(original)
    json_size = struct.unpack('<I', original[12:16])[0]
    assert digest(original[16:16 + json_size]) == plist['ElectronAsarIntegrity']['Resources/app.asar']['hash']
    items = dict(entries(header))
    locale = json.loads((ROOT / 'source/zh-CN.json').read_text())
    engine = (ROOT / 'source/engine.jsfrag').read_text()
    menu_engine = (ROOT / 'source/menu.jsfrag').read_text()
    locale_fields = {k: v for k, v in locale.items() if k not in ('$schema', 'menu', 'dialogs', 'allowSourceEqual')}
    engine = engine.replace('LOCALE_DATA_PLACEHOLDER', json.dumps(locale_fields, ensure_ascii=False))
    # Keep translated labels inside the official local application surface only.
    # Isolate declarations and contain failures so the official preload continues.
    engine = '\n(() => {\ntry {\nif (location.hostname !== "127.0.0.1" && location.hostname !== "localhost") return;\n' + engine + '\n} catch (error) { console.warn("Chinese UI translation failed", error); }\n})();\n'
    menu_labels = dict(locale['menu'])
    menu_labels.update({'About Antigravity': '关于 Antigravity', 'Quit Antigravity': '退出 Antigravity', 'Hide Antigravity': '隐藏 Antigravity', 'Hide Others': '隐藏其他应用', 'Show All': '显示全部', 'Services': '服务', 'Bring All to Front': '全部置于前台', 'Paste and Match Style': '粘贴并匹配样式'})
    menu_engine = menu_engine.replace('MENU_DATA_PLACEHOLDER', json.dumps(menu_labels, ensure_ascii=False))
    preload = file_bytes(original, base, items['dist/preload.js']).decode()
    menu = file_bytes(original, base, items['dist/menu.js']).decode()
    assert preload.count('const updaterAPI = {') == 1
    assert menu.count('// Re-apply the menu so the change takes effect.') == 1
    assert 'antigravity-i18n:begin' not in preload + menu
    patched_preload = preload.replace('const updaterAPI = {', engine + 'const updaterAPI = {')
    patched_menu = menu.replace('// Re-apply the menu so the change takes effect.', 'try { translateMenu(menu); } catch (error) { console.warn("Chinese menu translation failed", error); }\n    // Re-apply the menu so the change takes effect.') + '\n' + menu_engine + '\n'
    # The macOS updater dispatches by its English label. Resolve translated
    # display labels back to the original key without changing update actions.
    update_labels = {menu_labels[key]: key for key in ('Check for Updates', 'Checking for Updates...', 'Downloading Update...', 'Restart to Update')}
    assert patched_menu.count('updater_1.updateActions[menuItem.label]') == 1
    patched_menu = patched_menu.replace('updater_1.updateActions[menuItem.label]', 'updater_1.updateActions[agOriginalUpdateLabel(menuItem.label)]')
    patched_menu += '\nfunction agOriginalUpdateLabel(label) {\n    const labels = ' + json.dumps(update_labels, ensure_ascii=False) + ';\n    return labels[label] || label;\n}\n'
    replacements = {'dist/preload.js': patched_preload.encode(), 'dist/menu.js': patched_menu.encode()}
    for folder in (out, backup, out / 'dist'):
        folder.mkdir(parents=True, exist_ok=True)
    for name, content in [('app.asar', original), ('Info.plist', plist_bytes)]:
        backup_path = backup / name
        if backup_path.exists():
            assert backup_path.read_bytes() == content, 'Refuse to overwrite a different backup'
        else:
            backup_path.write_bytes(content)
    for name, content in replacements.items():
        (out / name).write_bytes(content)
        subprocess.run([args.node, '--check', str(out / name)], check=True)
    rebuilt_header = copy.deepcopy(header)
    payloads = []
    offset = 0
    for name, entry in entries(rebuilt_header):
        if 'offset' not in entry or entry.get('unpacked'):
            continue
        payload = replacements.get(name, file_bytes(original, base, items[name]))
        entry['offset'] = str(offset)
        entry['size'] = len(payload)
        if name in replacements and 'integrity' in entry:
            integrity = entry['integrity']
            assert integrity['algorithm'] == 'SHA256'
            chunk = integrity['blockSize']
            integrity['hash'] = digest(payload)
            integrity['blocks'] = [digest(payload[i:i + chunk]) for i in range(0, len(payload), chunk)]
        payloads.append(payload)
        offset += len(payload)
    header_bytes = json.dumps(rebuilt_header, separators=(',', ':'), ensure_ascii=False).encode()
    padded = struct.pack('<I', len(header_bytes)) + header_bytes
    padded += b'\x00' * ((-len(padded)) % 4)
    header_pickle = struct.pack('<I', len(padded)) + padded
    archive = struct.pack('<II', 4, len(header_pickle)) + header_pickle + b''.join(payloads)
    verify_header, verify_base = unpack(archive)
    changed = []
    for name, entry in entries(verify_header):
        if 'offset' not in entry or entry.get('unpacked'):
            assert entry == items[name]
            continue
        content = file_bytes(archive, verify_base, entry)
        before = file_bytes(original, base, items[name])
        if content != before:
            changed.append(name)
            assert content == replacements[name]
    assert sorted(changed) == sorted(replacements)
    (out / 'app.asar').write_bytes(archive)
    plist['ElectronAsarIntegrity']['Resources/app.asar']['hash'] = digest(header_bytes)
    (out / 'Info.plist').write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_BINARY, sort_keys=False))
    report = {
        'status': 'prepared_only_not_installed_or_executed',
        'target_version': '2.29.0',
        'source_repository': 'https://github.com/wadewu-ml/antigravity-i18n',
        'source_commit': COMMIT,
        'changed_archive_files': changed,
        'settings_translation_entries': len(locale['text']),
        'native_menu_translation_entries': len(menu_labels),
        'source_asar_sha256': digest(original),
        'candidate_asar_sha256': digest(archive),
        'validation': ['Both modified JavaScript files pass node --check', 'Only two packed archive files changed', 'Unpacked resources and links preserved', 'Original header integrity matches official Info.plist'],
        'remaining': ['Review installation and macOS signing separately; this tool only prepares files', 'Preserve an entire official application backup before any separate installation', 'Verify visible Chinese menus and settings after any separate installation'],
        'limitations': ['Unofficial UI text replacement; not every string is covered', 'Future updates may overwrite the patch', 'UI translation is not verified by this build tool', 'Local macOS signing does not preserve Google signing trust and may require separately approved per-app library-validation changes'],
        'source_hashes': {p.name: digest(p.read_bytes()) for p in sorted((ROOT / 'source').iterdir()) if p.is_file()},
    }
    (build_root / 'review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
