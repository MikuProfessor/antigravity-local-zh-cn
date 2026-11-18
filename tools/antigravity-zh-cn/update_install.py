"""Verify, localize and atomically replace the existing macOS Antigravity app."""

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile

from prepare import entries, file_bytes, unpack
from update_catalog import compare_versions, validate_archive_url

APP = Path('/Applications/Antigravity.app')
ROOT = Path(__file__).resolve().parent
STATE = Path.home() / 'Library/Application Support/Antigravity 中文更新'
LIBRARY_VALIDATION = 'com.apple.security.cs.disable-library-validation'
BUNDLE_ID = 'com.google.antigravity'
GOOGLE = 'anchor apple generic and certificate leaf[subject.OU] = "EQHXZ8M8AV"'
OFFICIAL = '=identifier "com.google.antigravity" and ' + GOOGLE
ASAR = Path('Contents/Resources/app.asar')
PLIST = Path('Contents/Info.plist')
BINARY = Path('Contents/MacOS/Antigravity')


class InstallError(RuntimeError):
    """An update could not be safely completed."""


def run(command, *, timeout=120):
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(f'无法完成 {Path(command[0]).name}：{exc}') from exc
    if result.returncode:
        detail = result.stderr.decode(errors='replace').strip().splitlines()
        raise InstallError(f'{Path(command[0]).name} 执行失败：' + (detail[-1][:400] if detail else str(result.returncode)))
    return result.stdout


def _info(app):
    value = plistlib.loads((app / PLIST).read_bytes())
    if value.get('CFBundleIdentifier') != BUNDLE_ID or value.get('CFBundleExecutable') != 'Antigravity':
        raise InstallError('目标应用不是 Antigravity 独立客户端。')
    return value


def is_localized(app_path=APP):
    """Check both localized ASAR entries, including older manually built installations."""
    data = (Path(app_path) / ASAR).read_bytes()
    header, base = unpack(data)
    files = dict(entries(header))
    for name in ('dist/menu.js', 'dist/preload.js'):
        entry = files.get(name, {})
        if 'offset' not in entry or b'/* antigravity-i18n:begin */' not in file_bytes(data, base, entry):
            return False
    return True


def _verify_signature(app, requirement=None):
    args = ['/usr/bin/codesign', '--verify', '--deep', '--strict']
    if requirement:
        args.extend(['-R', requirement])
    run(args + [app])


def _entitlements(app):
    data = run(['/usr/bin/codesign', '-d', '--entitlements', ':-', app])
    result = plistlib.loads(data) if data else {}
    if not isinstance(result, dict):
        raise InstallError('应用签名中的权限信息格式异常。')
    return result


def remember_local_profile(app_path=APP):
    """Remember a pre-existing, app-specific setting only on a verified localized install."""
    app = Path(app_path).resolve()
    _info(app)
    if not is_localized(app):
        return False
    _verify_signature(app)
    if _entitlements(app).get(LIBRARY_VALIDATION) is not True:
        return False
    # Also ensure this is the Google framework from the locally signed client.
    _verify_signature(app / 'Contents/Frameworks/Electron Framework.framework', '=' + GOOGLE)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {'app': str(app), 'bundle_id': BUNDLE_ID, 'existing_library_validation_exception': True}
    target = STATE / 'local-signing-profile.json'
    if target.exists() and json.loads(target.read_text()) == record:
        return True
    with tempfile.NamedTemporaryFile('w', dir=STATE, delete=False) as output:
        json.dump(record, output)
    os.replace(output.name, target)
    return True


def has_local_profile(app_path=APP):
    """Does this user already have a verified local signing profile for this installation?"""
    app = Path(app_path).resolve()
    path = STATE / 'local-signing-profile.json'
    if not path.is_file():
        return False
    record = json.loads(path.read_text())
    return record == {'app': str(app), 'bundle_id': BUNDLE_ID, 'existing_library_validation_exception': True}


def _validate_package(archive, release):
    size = release['size']
    if type(size) is not int or not 0 < size <= 20 * 1024 ** 3 or archive.stat().st_size != size:
        raise InstallError('下载的安装包大小与官方清单不符。')
    digest = hashlib.sha512()
    with archive.open('rb') as reader:
        for block in iter(lambda: reader.read(1024 * 1024), b''):
            digest.update(block)
    if base64.b64encode(digest.digest()).decode('ascii') != release['sha512']:
        raise InstallError('下载的安装包未通过官方 SHA-512 校验。')


def _validate_zip(archive):
    # ditto preserves the signed bundle's framework symlinks and executable modes.
    # Validate paths before extraction, including case-insensitive APFS aliases.
    with zipfile.ZipFile(archive) as package:
        seen = set()
        links = set()
        total = 0
        for info in package.infolist():
            name = info.filename.rstrip('/')
            parts = name.split('/')
            if (info.orig_filename != info.filename or parts[0] != 'Antigravity.app'
                    or any(p in ('.', '..', '') for p in parts)
                    or any(ord(c) < 32 or c in ':\\' for c in name)):
                raise InstallError('安装包中含有异常路径。')
            key = unicodedata.normalize('NFD', name.casefold())
            if key in seen:
                raise InstallError('安装包中含有重复路径。')
            seen.add(key)
            total += info.file_size
            if len(seen) > 100000 or total > 8 * 1024 ** 3:
                raise InstallError('安装包展开后超出大小限制。')
            mode = stat.S_IFMT(info.external_attr >> 16)
            if mode == stat.S_IFLNK:
                if info.file_size > 4096:
                    raise InstallError('安装包中含有异常符号链接。')
                target = package.read(info).decode('utf-8')
                if (not target or target.startswith('/') or '..' in target.split('/')
                        or any(ord(c) < 32 or c in ':\\' for c in target)):
                    raise InstallError('安装包中含有不受支持的符号链接。')
                links.add(key)
            elif mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise InstallError('安装包中含有不支持的文件类型。')
        if any(str(parent) in links for key in seen for parent in Path(key).parents):
            raise InstallError('安装包中的文件路径穿过符号链接。')


def _validate_installed(app, release):
    if app.name != 'Antigravity.app' or app.is_symlink():
        raise InstallError('请指定已安装的 Antigravity.app。')
    version = _info(app)['CFBundleShortVersionString']
    if compare_versions(version, release['version']) == 'ahead':
        raise InstallError('已安装的客户端版本更高，已取消降级。')
    if is_localized(app):
        if not remember_local_profile(app):
            raise InstallError('已汉化客户端缺少可沿用的本机签名配置。')
    else:
        _verify_signature(app, OFFICIAL)
        if not has_local_profile(app):
            raise InstallError('当前用户尚未为这个安装位置配置汉化签名，请先完成首次汉化。')
    return version


def _stamp(app):
    # The official updater can act independently while the download/build is in progress.
    return tuple((p.stat().st_dev, p.stat().st_ino, p.stat().st_mtime_ns)
                 for p in (app, app / PLIST, app / ASAR, app / BINARY))


def stage_update(release, app_path, work_dir, progress=print, *, archive=None):
    """Prepare and verify a candidate in a fresh work directory, without launching it.

    An existing archive may be passed for offline validation. Its size, SHA-512
    and embedded Google signature are checked exactly like a new download.
    """
    app = Path(app_path).absolute()
    work = Path(work_dir).resolve()
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(work.iterdir()):
        raise InstallError('暂存目录必须为空。')
    url = validate_archive_url(release['url'], release['version'], release['architecture'])
    _validate_installed(app, release)
    if archive is None:
        archive = work / 'Antigravity.zip'
        progress('正在下载官方 Antigravity ' + release['version'] + '…')
        run(['/usr/bin/curl', '--fail', '--silent', '--show-error', '--proto', '=https',
             '--max-filesize', str(release['size']), '--connect-timeout', '20', '--max-time', '1200',
             '--retry', '2', '--retry-max-time', '120',
             '--output', archive, url], timeout=1220)
    archive = Path(archive)
    progress('正在校验下载内容和 Google 开发者签名…')
    _validate_package(archive, release)
    _validate_zip(archive)
    run(['/usr/bin/ditto', '-x', '-k', archive, work / 'unpacked'])
    candidate = work / 'unpacked/Antigravity.app'
    if _info(candidate)['CFBundleShortVersionString'] != release['version']:
        raise InstallError('安装包内的版本号与官方清单不符。')
    arch = {'arm64': 'arm64', 'x64': 'x86_64'}[release['architecture']]
    if arch.encode() not in run(['/usr/bin/lipo', '-archs', candidate / BINARY]).split():
        raise InstallError('安装包内的可执行文件架构与清单不符。')
    if arch.encode() not in run(['/usr/bin/lipo', '-archs', app / BINARY]).split():
        raise InstallError('安装包架构与当前客户端不一致。')
    _verify_signature(candidate, OFFICIAL)
    run(['/usr/sbin/spctl', '--assess', '--type', 'execute', candidate])
    permissions = _entitlements(candidate)
    permissions[LIBRARY_VALIDATION] = True
    progress('正在检验新版资源、应用中文翻译并生成本机签名…')
    run([sys.executable, ROOT / 'prepare.py', '--app', candidate, '--output', work / 'build'])
    built = work / 'build/candidate'
    shutil.copyfile(built / 'app.asar', candidate / ASAR)
    shutil.copyfile(built / 'Info.plist', candidate / PLIST)
    plist = work / 'entitlements.plist'
    plist.write_bytes(plistlib.dumps(permissions))
    run(['/usr/bin/codesign', '--force', '--sign', '-', '--options', 'runtime',
         '--entitlements', plist, candidate])
    _verify_signature(candidate)
    if _entitlements(candidate) != permissions or not is_localized(candidate):
        raise InstallError('暂存客户端的中文资源或签名权限验证失败。')
    return candidate


def _pids(app):
    exe = os.fsencode((app / BINARY).resolve())
    matches = []
    for line in run(['/bin/ps', '-axww', '-o', 'pid=,comm=']).splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and fields[1] == exe:
            matches.append(int(fields[0]))
    return matches


def _quit(app):
    pids = _pids(app)
    if not pids:
        return False
    # NSRunningApplication.terminate() is a normal Quit. Never force-quit on timeout.
    for pid in pids:
        script = 'ObjC.import("AppKit"); $.NSRunningApplication.runningApplicationWithProcessIdentifier(' + str(pid) + ').terminate;'
        run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', script])
    deadline = time.monotonic() + 30
    while _pids(app):
        if time.monotonic() >= deadline:
            raise InstallError('客户端尚未退出。请先保存工作、手动退出，再重试。')
        time.sleep(0.3)
    return True


def _launch(app):
    run(['/usr/bin/open', '-a', app], timeout=30)
    deadline = time.monotonic() + 18
    started = None
    while time.monotonic() < deadline:
        live = tuple(_pids(app))
        if not live:
            started = None
        elif started and started[0] == live and time.monotonic() - started[1] >= 4:
            return
        elif not started or started[0] != live:
            started = (live, time.monotonic())
        time.sleep(0.4)
    raise InstallError('新版客户端启动验证失败。')


def _exchange(left, right):
    # macOS swaps both directory entries in a single same-volume transaction.
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    if libc.renamex_np(os.fsencode(left), os.fsencode(right), 0x2):  # RENAME_SWAP
        raise InstallError('macOS 无法交换客户端与备份：' + os.strerror(ctypes.get_errno()))


def _activate(app, reserved, stamp):
    candidate_stamp = _stamp(reserved)
    if _stamp(app) != stamp:
        raise InstallError('安装期间客户端已被其他程序更新。请重新检查更新。')
    running = _quit(app)
    exchanged = False
    try:
        if _stamp(app) != stamp or _pids(app):
            raise InstallError('退出时检测到客户端另有更新或已重启。请重新检查更新。')
        _exchange(app, reserved)
        exchanged = True
        if _stamp(reserved) != stamp:
            raise InstallError('客户端在交换期间发生更新，请重新检查。')
        _launch(app)
        if _stamp(app) != candidate_stamp:
            raise InstallError('客户端在启动期间发生了其他更新。')
    except BaseException as error:
        if exchanged:
            try:
                if _stamp(app) != candidate_stamp:
                    raise InstallError('将保留并行更新的客户端，请重新检查汉化。')
                _quit(app)
                _exchange(app, reserved)
            except (InstallError, OSError) as recovery:
                raise InstallError(f'新版启动失败，自动恢复未完成。完整旧版位于 {reserved}。{recovery}') from error
        if running and not _pids(app):
            try:
                _launch(app)
            except InstallError as restart:
                raise InstallError(f'原有客户端仍在 {app}，请手动打开。{restart}') from error
        if exchanged and isinstance(error, Exception):
            raise InstallError(f'已恢复更新前的客户端。{error}') from error
        raise


def install_update(release, app_path=APP, progress=print):
    """Install a verified localized version; return the full prior application backup."""
    app = Path(app_path).absolute()
    try:
        if not os.access(app.parent, os.W_OK):
            raise InstallError('当前用户没有替换此位置应用的权限，请选择可写的安装位置。')
        before = _stamp(app)
        backup_root = STATE / 'Backups'
        backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if backup_root.stat().st_dev != app.stat().st_dev:
            backup_root = app.parent
        with tempfile.TemporaryDirectory(prefix='antigravity-zh-update-') as temp:
            candidate = stage_update(release, app, Path(temp), progress)
            prefix = 'Antigravity-' if backup_root == STATE / 'Backups' else '.antigravity-'
            holder = Path(tempfile.mkdtemp(prefix=prefix + _info(app)['CFBundleShortVersionString'] + '-', dir=backup_root))
            reserved = holder / 'Antigravity.app'
            # Preserve the old bundle after success, or if recovery itself is interrupted.
            candidate_inode = None
            try:
                run(['/usr/bin/ditto', '--clone', candidate, reserved])
                candidate_inode = reserved.stat().st_ino
                _verify_signature(reserved)
                _validate_installed(app, release)
                progress('即将正常退出客户端，安装汉化版本并重新打开…')
                _activate(app, reserved, before)
                return reserved
            finally:
                # Delete only our staging copy. Preserve the displaced app even after interruption.
                if candidate_inode is None or (reserved.exists() and reserved.stat().st_ino == candidate_inode):
                    shutil.rmtree(holder, ignore_errors=True)
    except InstallError:
        raise
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, plistlib.InvalidFileException) as exc:
        raise InstallError(f'客户端更新未完成：{exc}') from exc
