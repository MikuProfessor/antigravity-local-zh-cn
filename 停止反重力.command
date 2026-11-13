#!/bin/zsh
set -eu
task_label="net.colux.antigravity-local"
task_domain="gui/$(id -u)"
if launchctl print "$task_domain/$task_label" >/dev/null 2>&1; then
  launchctl bootout "$task_domain/$task_label"
fi
echo "本机 Antigravity 接口已停止。"
echo "双击“启动反重力.command”可以重新启动。"
echo "下次登录 Mac 时，服务仍会自动启动。"
