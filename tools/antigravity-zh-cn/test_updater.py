"""Offline updater workflow tests; never open dialogs, launchd, or a real app."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import plistlib
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import update_install
import update_service as service
import updater


def release(version="2.32.2"):
    return {"version": version, "architecture": "arm64", "size": 216300592,
            "url": "https://storage.googleapis.com/unused-in-offline-tests.zip",
            "sha512": "unused-in-offline-tests"}


class UpdateWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.support = self.root / "Application Support/Antigravity 中文更新"
        self.app = self.root / "Antigravity.app"
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.stack.enter_context(redirect_stdout(self.stdout))
        self.stack.enter_context(redirect_stderr(self.stderr))
        self.stack.enter_context(patch.object(updater, "SUPPORT", self.support))
        self.stack.enter_context(patch.object(updater, "APP", self.app))
        self.stack.enter_context(patch.object(updater, "configure_path"))
        # These fail closed if a workflow accidentally bypasses the mocks below.
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("Real process execution is forbidden")))
        self.stack.enter_context(patch.object(subprocess, "check_output", side_effect=AssertionError("Real process execution is forbidden")))
        self.client = self.stack.enter_context(patch.object(updater, "client_info", return_value=("2.29.0", "arm64", True)))
        self.known_profile = self.stack.enter_context(patch.object(updater, 'has_local_profile', return_value=True))
        self.latest = self.stack.enter_context(patch.object(updater, "get_latest", return_value=release()))
        self.desktop = self.stack.enter_context(patch.object(updater, "desktop"))
        self.install = self.stack.enter_context(patch.object(updater, "install_update", return_value=self.root / "old-client-backup"))
        self.clock = self.stack.enter_context(patch.object(updater.time, "time", return_value=1_800_000_000.0))
        self.answer("later")

    def answer(self, value):
        self.desktop.side_effect = lambda script, *args: value if script == updater.ASK else ""

    def run_command(self, command):
        with patch.object(sys, "argv", ["updater.py", command]):
            return updater.main()

    def ask_calls(self):
        return [call for call in self.desktop.call_args_list if call.args[0] == updater.ASK]

    def test_background_is_silent_when_current_or_ahead(self):
        for installed in ("2.32.2", "2.33.0"):
            with self.subTest(installed=installed):
                self.client.return_value = (installed, "arm64", True)
                self.assertEqual(self.run_command("background"), 0)
                self.desktop.assert_not_called()
                self.install.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertFalse((self.support / "reminder.json").exists())

    def test_discovered_update_installs_only_after_explicit_consent(self):
        events = []

        def desktop(script, *args):
            if script == updater.ASK:
                self.install.assert_not_called()
                events.append("consent")
                return "install"
            return ""

        def install(selected_release, *, app_path, progress):
            self.assertEqual(events, ["consent"])
            self.assertEqual(selected_release, release())
            self.assertEqual(app_path, self.app)
            self.assertTrue(callable(progress))
            events.append("install")
            return self.root / "old-client-backup"

        self.desktop.side_effect = desktop
        self.install.side_effect = install
        self.assertEqual(self.run_command("background"), 0)
        self.assertEqual(events, ["consent", "install"])
        self.assertEqual(len(self.ask_calls()), 1)
        prompt = self.ask_calls()[0].args
        self.assertIn("2.29.0 → 2.32.2", prompt[1])
        self.assertEqual(prompt[2], "更新并重启")
        self.latest.assert_called_once_with("arm64", installed_version="2.29.0")
        self.assertFalse((self.support / "reminder.json").exists())

    def test_later_suppresses_background_for_exactly_24_hours(self):
        start = self.clock.return_value
        self.assertEqual(self.run_command("background"), 0)
        reminder = json.loads((self.support / "reminder.json").read_text())
        self.assertEqual(reminder["until"], start + 24 * 3600)
        self.install.assert_not_called()

        self.desktop.reset_mock()
        self.clock.return_value = start + 24 * 3600 - 1
        self.assertEqual(self.run_command("background"), 0)
        self.desktop.assert_not_called()
        self.clock.return_value = start + 24 * 3600
        self.assertEqual(self.run_command("background"), 0)
        self.assertEqual(len(self.ask_calls()), 1)
        self.install.assert_not_called()

    def test_manual_check_bypasses_snooze_and_can_install(self):
        self.run_command("background")
        self.desktop.reset_mock()
        self.answer("install")
        self.assertEqual(self.run_command("check"), 0)
        self.assertEqual(len(self.ask_calls()), 1)
        self.install.assert_called_once()
        self.assertFalse((self.support / "reminder.json").exists())

    def test_new_release_is_not_hidden_by_previous_release_snooze(self):
        self.run_command("background")
        self.desktop.reset_mock()
        self.latest.return_value = release("2.33.0")
        self.assertEqual(self.run_command("background"), 0)
        self.assertEqual(len(self.ask_calls()), 1)
        self.assertIn("2.33.0", self.ask_calls()[0].args[1])
        self.install.assert_not_called()

    def test_same_version_without_patch_offers_and_performs_repair(self):
        self.client.return_value = ("2.32.2", "arm64", False)
        self.answer("install")
        self.assertEqual(self.run_command("check"), 0)
        self.assertEqual(self.ask_calls()[0].args[2], "恢复中文并重启")
        self.install.assert_called_once()
        self.assertEqual(self.install.call_args.args[0]["version"], "2.32.2")

    def test_unconfigured_official_client_does_not_offer_an_unusable_action(self):
        self.client.return_value = ("2.32.2", "arm64", False)
        self.known_profile.return_value = False
        self.run_command("background")
        self.desktop.assert_not_called()
        self.run_command("check")
        self.assertIn("首次汉化", self.desktop.call_args.args[1])
        self.assertFalse(self.ask_calls())
        self.install.assert_not_called()

    def test_staged_rollout_waits_for_official_eligibility(self):
        self.latest.side_effect = updater.CatalogError("staged_rollout", "not eligible yet")
        self.assertEqual(self.run_command("background"), 0)
        self.desktop.assert_not_called()
        self.run_command("check")
        self.assertIn("分批", self.desktop.call_args.args[1])
        self.install.assert_not_called()

    def test_timeout_does_not_install_and_returns_after_one_hour(self):
        self.answer("timeout")
        self.assertEqual(self.run_command("background"), 0)
        reminder = json.loads((self.support / "reminder.json").read_text())
        self.assertEqual(reminder["until"], self.clock.return_value + 3600)
        self.install.assert_not_called()

    def test_network_failure_is_silent_in_background_but_visible_manually(self):
        self.latest.side_effect = updater.CatalogError("network", "offline-test")
        self.assertEqual(self.run_command("background"), 1)
        self.desktop.assert_not_called()
        self.assertIn("未完成", self.stderr.getvalue())
        self.assertIn("网络", self.stderr.getvalue())
        self.assertEqual(self.run_command("check"), 1)
        self.desktop.assert_called_once()
        self.assertEqual(self.desktop.call_args.args[0], updater.MESSAGE)
        self.assertIn("网络", self.desktop.call_args.args[1])
        self.assertNotIn("已是官方更新通道的最新版本", self.desktop.call_args.args[1])
        self.install.assert_not_called()
        self.assertFalse((self.support / "reminder.json").exists())

    def test_concurrent_check_does_not_fetch_or_install(self):
        with patch.object(updater.fcntl, "flock", side_effect=BlockingIOError):
            self.assertEqual(self.run_command("background"), 0)
            self.desktop.assert_not_called()
            self.assertEqual(self.run_command("check"), 0)
        self.latest.assert_not_called()
        self.client.assert_not_called()
        self.install.assert_not_called()
        self.desktop.assert_called_once()
        self.assertIn("已有更新检查正在进行", self.desktop.call_args.args[1])

    def test_install_failure_keeps_snooze_and_does_not_claim_success(self):
        self.answer("install")
        self.install.side_effect = updater.InstallError("verification-test-failed")
        with self.assertRaises(SystemExit) as raised:
            self.run_command("check")
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("verification-test-failed", self.desktop.call_args.args[1])
        self.assertNotIn("中文版已更新", self.stdout.getvalue())
        reminder = json.loads((self.support / "reminder.json").read_text())
        self.assertEqual(reminder["until"], self.clock.return_value + 24 * 3600)
        # A failed install also releases the check lock, so a later manual retry works.
        self.install.side_effect = None
        self.desktop.reset_mock()
        self.assertEqual(self.run_command("check"), 0)
        self.assertEqual(len(self.ask_calls()), 1)


class UpdateServiceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.support = self.root / "Application Support/Antigravity 中文更新"
        self.plist = self.root / "LaunchAgents" / (service.LABEL + ".plist")
        self.app = self.root / "Antigravity.app"
        self.app.mkdir()
        self.stack.enter_context(patch.object(service, "SUPPORT", self.support))
        self.stack.enter_context(patch.object(service, "PLIST", self.plist))
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("Real launchd/process access is forbidden")))
        self.stack.enter_context(patch.object(subprocess, "check_output", side_effect=AssertionError("Real process execution is forbidden")))
        self.node = self.stack.enter_context(patch.object(service.shutil, "which", return_value="/opt/homebrew/bin/node"))
        self.profile = self.stack.enter_context(patch.object(update_install, "remember_local_profile", return_value=True))
        self.launchctl = self.stack.enter_context(patch.object(service, "launchctl"))
        self.launchctl.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess(args, 1 if args[0] == "print" else 0)

    def test_enable_deploys_fixed_launcher_and_launchd_arguments(self):
        service.enable(self.app)
        self.profile.assert_called_once_with(self.app)
        launcher = self.support / "check-updates"
        agent = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(agent["ProgramArguments"], [str(launcher), "background"])
        self.assertTrue(agent["RunAtLoad"])
        self.assertEqual(agent["StartInterval"], 6 * 3600)
        self.assertEqual(stat.S_IMODE(launcher.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.plist.stat().st_mode), 0o600)
        lines = launcher.read_text().splitlines()
        self.assertEqual(lines[0], "#!/bin/sh")
        command = shlex.split(next(line for line in lines if line.startswith("exec ")))
        self.assertEqual(command[0:2], ["exec", sys.executable])
        self.assertEqual(command[3:], ["${1:-check}"])
        script = Path(command[2])
        self.assertTrue(script.is_file())
        self.assertEqual(script.name, "updater.py")
        self.assertEqual(script.parent.parent, self.support / "versions")
        self.assertEqual(script.read_bytes(), (service.ROOT / "updater.py").read_bytes())
        self.assertTrue((script.parent / "source/zh-CN.json").is_file())
        self.launchctl.assert_any_call("bootstrap", service.DOMAIN, str(self.plist))

    def test_enabling_again_keeps_running_service_and_same_snapshot(self):
        service.enable(self.app)
        before = self.plist.read_bytes()
        snapshots = list((self.support / "versions").iterdir())
        self.launchctl.reset_mock()
        self.launchctl.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)
        service.enable(self.app)
        self.assertEqual(self.plist.read_bytes(), before)
        self.assertEqual(list((self.support / "versions").iterdir()), snapshots)
        self.assertEqual([call.args[0] for call in self.launchctl.call_args_list], ["enable", "print"])

    def test_missing_node_fails_before_deployment_or_launchd(self):
        self.node.return_value = None
        with self.assertRaisesRegex(ValueError, "Node.js"):
            service.enable(self.app)
        self.profile.assert_not_called()
        self.launchctl.assert_not_called()
        self.assertFalse(self.support.exists())
        self.assertFalse(self.plist.exists())

    def test_disable_removes_only_its_agent_and_launcher(self):
        service.enable(self.app)
        keep = self.support / "keep-state.json"
        keep.write_text("{}")
        self.launchctl.reset_mock()
        self.launchctl.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)
        service.disable()
        self.assertFalse(self.plist.exists())
        self.assertFalse((self.support / "check-updates").exists())
        self.assertTrue(keep.exists())
        self.assertTrue((self.support / "versions").is_dir())
        self.launchctl.assert_any_call("bootout", service.DOMAIN + "/" + service.LABEL)


if __name__ == "__main__":
    unittest.main()
