"""Check the official macOS channel and offer an interactive localized update."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time

from update_catalog import CatalogError, compare_versions, get_latest, version_tuple
from update_install import InstallError, has_local_profile, install_update, is_localized
from update_service import SUPPORT, configure_path, disable, enable

APP = Path('/Applications/Antigravity.app')
SNOOZE = 24 * 3600
ASK = '''
on run argv
    activate
    set actionLabel to item 2 of argv
    try
        set answer to display dialog (item 1 of argv) with title "Antigravity 中文更新" buttons {"稍后", actionLabel} default button "稍后" cancel button "稍后" with icon note giving up after 300
        if gave up of answer then return "timeout"
        if button returned of answer is actionLabel then return "install"
    on error number -128
        return "later"
    end try
    return "later"
end run'''
MESSAGE = '''
on run argv
    activate
    display dialog (item 1 of argv) with title "Antigravity 中文更新" buttons {"好"} default button 1 with icon note giving up after 180
end run'''
NOTICE = '''
on run argv
    display notification (item 1 of argv) with title "Antigravity 中文更新"
end run'''


def desktop(script, *args):
    result = subprocess.run(['/usr/bin/osascript', '-e', script, *args],
                            capture_output=True, text=True, timeout=330)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or '无法打开 macOS 更新提示')
    return result.stdout.strip()


def message(body):
    print(body, flush=True)
    desktop(MESSAGE, body)


def client_info(app):
    with (app / 'Contents/Info.plist').open('rb') as f:
        info = plistlib.load(f)
    if info.get('CFBundleIdentifier') != 'com.google.antigravity':
        raise ValueError('选定的应用不是 Antigravity 独立客户端')
    version = info['CFBundleShortVersionString']
    version_tuple(version)
    arches = subprocess.check_output(['/usr/bin/lipo', '-archs',
                                      str(app / 'Contents/MacOS/Antigravity')], text=True).split()
    if arches == ['arm64']:
        arch = 'arm64'
    elif arches == ['x86_64']:
        arch = 'x64'
    elif set(arches) == {'x86_64', 'arm64'}:
        arm = subprocess.run(['/usr/sbin/sysctl', '-in', 'hw.optional.arm64'],
                             capture_output=True, text=True)
        arch = 'arm64' if arm.stdout.strip() == '1' else 'x64'
    else:
        raise ValueError('无法识别客户端架构')
    return version, arch, is_localized(app)


def status(app):
    installed, arch, localized = client_info(app)
    try:
        release = get_latest(arch, installed_version=installed)
    except CatalogError as exc:
        if exc.kind == 'staged_rollout':
            return {'installed': installed, 'localized': localized, 'status': 'staged_rollout'}
        raise
    kind = compare_versions(installed, release['version'])
    if kind == 'up_to_date' and not localized:
        kind = 'repair'
    return {'installed': installed, 'localized': localized, 'status': kind, 'release': release}


def read_snooze():
    try:
        data = json.loads((SUPPORT / 'reminder.json').read_text())
        if isinstance(data, dict) and isinstance(data.get('until'), (int, float)):
            return data
    except (FileNotFoundError, ValueError):
        pass
    return {}


def snooze(key, seconds):
    with tempfile.NamedTemporaryFile('w', dir=SUPPORT, delete=False) as f:
        json.dump({'key': key, 'until': time.time() + seconds}, f)
    os.replace(f.name, SUPPORT / 'reminder.json')


@contextmanager
def check_lock():
    SUPPORT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (SUPPORT / 'updater.lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def check(app, background):
    with check_lock() as acquired:
        if not acquired:
            if not background:
                message('已有更新检查正在进行。请查看已打开的更新提示。')
            return
        info = status(app)
        if info['status'] == 'staged_rollout':
            if not background:
                message('Google 正在分批推送，这台 Mac 尚未收到本轮更新。')
            return
        installed, kind, release = info['installed'], info['status'], info['release']
        version = release['version']
        if kind == 'ahead':
            if not background:
                message(f'本机版本 {installed} 高于官方更新通道的 {version}，将保留较新版本。')
            return
        if kind == 'up_to_date':
            if not background:
                message(f'Antigravity {installed} 已是官方更新通道的最新版本，中文补丁已安装。')
            return
        if not info['localized'] and not has_local_profile(app):
            if not background:
                message('这台 Mac 尚未配置该客户端的本机汉化签名。请先完成首次汉化，再运行更新。')
            return
        key = [kind, installed, version]
        reminder = read_snooze()
        if background and reminder.get('key') == key and reminder['until'] > time.time():
            return
        if kind == 'repair':
            intro = f'Antigravity {installed} 已是当前版本，检测到中文补丁尚未安装。'
            action = '恢复中文并重启'
        else:
            intro = f'Antigravity 有更新：{installed} → {version}。'
            action = '更新并重启'
        size = round(release['size'] / (1024 * 1024))
        prompt = (f'{intro}\n\n将下载约 {size} MiB 的官方安装包，校验 Google 签名，'
                  '在本机应用中文补丁并重新签名。请先保存工作。准备完成后会退出并重新打开客户端，保留旧版备份。')
        answer = desktop(ASK, prompt, action)
        if answer != 'install':
            # A missed dialog can return on the next scheduled check.
            snooze(key, SNOOZE if answer == 'later' else 3600)
            return
        print(f'正在准备 Antigravity {version}…', flush=True)
        try:
            desktop(NOTICE, '正在下载并校验官方客户端，准备完成后会自动重启。')
        except (RuntimeError, subprocess.SubprocessError):
            pass
        # Avoid presenting the same failure every time the machine wakes.
        snooze(key, SNOOZE)
        try:
            backup = install_update(release, app_path=app, progress=lambda line: print(line, flush=True))
        except (InstallError, OSError, ValueError, subprocess.SubprocessError) as exc:
            message(f'更新未完成。\n\n{exc}')
            raise SystemExit(1)
        (SUPPORT / 'reminder.json').unlink(missing_ok=True)
        message(f'Antigravity {version} 中文版已更新。\n\n上一版保存在\n{backup}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('status', 'check', 'background', 'enable', 'disable'))
    args = parser.parse_args()
    configure_path()
    try:
        if args.command == 'status':
            print(json.dumps(status(APP), ensure_ascii=False, indent=2))
        elif args.command == 'enable':
            enable(APP)
            print('已开启中文更新提醒。登录和每 6 小时检查官方更新通道。', flush=True)
        elif args.command == 'disable':
            with check_lock() as acquired:
                if not acquired:
                    raise RuntimeError('正在检查或安装更新。请关闭更新提示或等待安装完成，再关闭提醒。')
                disable()
            print('已关闭中文更新提醒。', flush=True)
        else:
            check(APP, args.command == 'background')
    except (CatalogError, InstallError, OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        reason = str(exc)
        if isinstance(exc, CatalogError):
            reason = {
                'network': '暂时连不上官方更新通道，请检查网络后重试。',
                'timeout': '官方更新通道响应超时，请稍后重试。',
                'invalid_metadata': '官方更新清单格式或校验信息有变化，请更新汉化工具后重试。',
            }.get(exc.kind, reason)
        detail = f'Antigravity 中文更新未完成。\n{reason}'
        print(detail, file=sys.stderr, flush=True)
        if args.command == 'check':
            desktop(MESSAGE, detail)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
