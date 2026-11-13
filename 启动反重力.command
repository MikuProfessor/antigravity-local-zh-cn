#!/bin/zsh
set -eu
task_label="net.colux.antigravity-local"
task_domain="gui/$(id -u)"
task_plist="$HOME/Library/LaunchAgents/$task_label.plist"
if ! launchctl print "$task_domain/$task_label" >/dev/null 2>&1; then
  launchctl bootstrap "$task_domain" "$task_plist"
fi
launchctl kickstart "$task_domain/$task_label"
task_ready=0
for task_attempt in {1..30}; do
  task_status=$(curl -s --max-time 2 -o /dev/null -w '%{http_code}' http://127.0.0.1:8317/v1/models || true)
  if [[ "$task_status" == "401" ]]; then
    task_ready=1
    break
  fi
  sleep 0.2
done
if [[ "$task_ready" != "1" ]]; then
  echo "服务尚未就绪，请检查反重力目录中的 logs/ 和 runtime/service.log。"
  exit 1
fi
echo "本机接口：http://127.0.0.1:8317/v1"
echo "酒馆：http://127.0.0.1:8000"
echo "接口由 macOS 管理；关闭这个终端不会停止服务。"
