"""Install the current user's launchd update reminder on macOS."""
import hashlib
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
SUPPORT = Path.home() / 'Library/Application Support/Antigravity 中文更新'
LABEL = 'io.github.mikuprofessor.antigravity-zh-cn.updater'
DOMAIN = f'gui/{os.getuid()}'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')


def configure_path():
    parts = os.environ.get('PATH', '').split(os.pathsep)
    for folder in ('/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin'):
        if folder not in parts:
            parts.append(folder)
    os.environ['PATH'] = os.pathsep.join(p for p in parts if p)


def deploy_files():
    # Use a complete snapshot so moving the downloaded project won't break login checks.
    files = [ROOT / name for name in (
        'updater.py', 'update_catalog.py', 'update_install.py', 'update_service.py',
        'prepare.py', 'THIRD_PARTY_NOTICES.md')]
    files += sorted(p for p in (ROOT / 'source').iterdir() if p.is_file())
    fingerprint = hashlib.sha256()
    for path in files:
        fingerprint.update(path.relative_to(ROOT).as_posix().encode() + b'\x00')
        fingerprint.update(hashlib.sha256(path.read_bytes()).digest())
    versions = SUPPORT / 'versions'
    versions.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = versions / fingerprint.hexdigest()
    if not destination.is_dir():
        with tempfile.TemporaryDirectory(dir=versions) as temp:
            stage = Path(temp) / 'source-tree'
            for path in files:
                target = stage / path.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            if not destination.exists():
                stage.rename(destination)
    return destination / 'updater.py'


def atomic_write(path, data, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        f.write(data)
        os.fchmod(f.fileno(), mode)
    os.replace(f.name, path)


def launchctl(*args, check=True):
    return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True, check=check)


def enable(app):
    from update_install import has_local_profile, remember_local_profile
    if not app.is_dir():
        raise ValueError(f'未找到客户端 {app}')
    if not shutil.which('node'):
        raise ValueError('汉化需要 Node.js，请先安装 Node.js 再开启更新提醒。')
    SUPPORT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not (remember_local_profile(app) or has_local_profile(app)):
        raise ValueError('此位置还没有本机汉化签名配置。请先完成首次汉化，再开启更新提醒。')
    script = deploy_files()
    launcher = SUPPORT / 'check-updates'
    # The native menu starts this fixed launcher without arguments.
    node_dir = str(Path(shutil.which('node')).parent)
    shell = ('#!/bin/sh\nexport PATH=' + shlex.quote(node_dir + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin') +
             '\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(script)) + ' "${1:-check}"\n')
    atomic_write(launcher, shell.encode(), 0o700)
    label_path = DOMAIN + '/' + LABEL
    agent = {'Label': LABEL, 'ProgramArguments': [str(launcher), 'background'],
             'RunAtLoad': True, 'StartInterval': 6 * 3600, 'LimitLoadToSessionType': 'Aqua',
             'StandardErrorPath': str(SUPPORT / 'update-errors.log')}
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    data = plistlib.dumps(agent)
    same = PLIST.is_file() and PLIST.read_bytes() == data
    launchctl('enable', label_path)
    running = launchctl('print', label_path, check=False).returncode == 0
    if same and running:
        return
    if running:
        launchctl('bootout', label_path)
    atomic_write(PLIST, data, 0o600)
    launchctl('bootstrap', DOMAIN, str(PLIST))


def disable():
    label_path = DOMAIN + '/' + LABEL
    if launchctl('print', label_path, check=False).returncode == 0:
        launchctl('bootout', label_path)
    PLIST.unlink(missing_ok=True)
    (SUPPORT / 'check-updates').unlink(missing_ok=True)
