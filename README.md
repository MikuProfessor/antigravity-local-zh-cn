# Antigravity 本机工具集

保存 macOS 上的 Antigravity 本机接口启停脚本、SillyTavern 连接验证工具，以及独立客户端的简体中文界面补丁构建源码。

仓库只包含源码、示例配置和文档。真实账号凭据、API key、聊天记录、日志、客户端安装包和完整应用备份均留在本机，不上传 GitHub。

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `启动反重力.command` / `停止反重力.command` | 通过 macOS LaunchAgent 启停本机接口 |
| `verify_local.py` | 用合成消息验证 SillyTavern → CLIProxyAPI 的普通与流式调用 |
| `config.example.yaml` | CLIProxyAPI v8 配置示例，不含有效密钥 |
| `examples/` | LaunchAgent 和 SillyTavern 集成配置示例 |
| `tools/antigravity-zh-cn/` | Antigravity App 2.29.0 中文界面补丁构建器及上游词典 |
| `auth/`、`runtime/`、`logs/`、`vendor/` | 本机运行目录，全部由 Git 忽略 |

## 本机接口

调用链：SillyTavern → CLIProxyAPI → 用户自己的 Antigravity 账号。

接口默认监听 `http://127.0.0.1:8317/v1`，客户端请求需要本地 API key。关闭终端不会停止 LaunchAgent 服务；Mac 关机或休眠时服务不可用。

本机部署采用 [CLIProxyAPI v8.0.69](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.69)。二进制与 OAuth 凭据不包含在仓库内，克隆源码不会自动安装服务或登录账号。

### 新机器配置

1. 从上游发布页获取适合本机架构的 CLIProxyAPI，核对发布文件校验和，将可执行文件放到 `vendor/cli-proxy-api`。
2. 创建本机目录，复制配置示例：

   ```sh
   mkdir -p auth runtime logs vendor
   chmod 700 auth runtime logs
   cp config.example.yaml config.yaml
   chmod 600 config.yaml
   ```

3. 编辑 `config.yaml`：将 API key 占位符换成随机密钥，并将 `oauth.auth-dir` 改为本项目 `auth/` 的绝对路径。可用 `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` 生成密钥。保留 `127.0.0.1` 监听地址。
4. 按 CLIProxyAPI 的说明完成自己的 Antigravity OAuth 登录，将凭据保存在本机 `auth/` 中。
5. 将 `examples/net.colux.antigravity-local.plist` 复制到 `~/Library/LaunchAgents/`，把其中所有 `/ABSOLUTE/PATH/TO/PROJECT` 替换为本项目的绝对路径。已有同名服务时先检查现有配置，避免覆盖正在使用的部署。
6. 双击 `启动反重力.command`。在 SillyTavern 的 Custom 连接中填入接口地址与本地 API key，并选择该账号实际可用的模型。

`停止反重力.command` 只停止当前登录会话的服务；下次登录 Mac 时仍会自动启动。

### 连接验证

验证脚本默认连接本机 SillyTavern `http://127.0.0.1:8000`。先复制配置示例，并填写由 SillyTavern 保存的 Custom API 密钥对应的 **secret ID**（不是 API key 本身）：

```sh
cp examples/sillytavern-integration.example.json runtime/sillytavern-integration.json
chmod 600 runtime/sillytavern-integration.json
python3 verify_local.py --model YOUR_AVAILABLE_MODEL
python3 verify_local.py --model YOUR_AVAILABLE_MODEL --stream
```

这些命令会产生少量真实模型调用，只发送脚本内置合成消息，不读取角色卡或现有聊天记录。退出码为 0 且结果中的 `passed` 为 `true` 才表示测试通过；流式测试还检查 SSE `[DONE]` 和正常结束标记。结果保存在本机 `runtime/verify-*.json`。

CLIProxyAPI 是第三方兼容接口，不是官方 `agy` CLI。模型与额度取决于实际账号。已使用的版本会移除非 Claude 路由中的 `maxOutputTokens`；对话中途的 system 消息也不完全等价于上游原生角色语义。

## macOS 中文界面补丁

`tools/antigravity-zh-cn/prepare.py` 针对 **Antigravity App 2.29.0 独立客户端**，不是 VS Code 系 IDE 语言包。构建器使用 Python 3 标准库；JavaScript 语法检查需要 PATH 中可用的 Node.js。

```sh
python3 tools/antigravity-zh-cn/prepare.py \
  --app /path/to/official/Antigravity.app
```

必须提供未打补丁的官方 2.29.0 应用。默认生成到已被 Git 忽略的 `runtime/antigravity-zh-cn-build/`，也可通过 `--output` 指定目录、`--node` 指定 Node.js。构建器保留原始资源备份，生成候选 ASAR、plist 和校验报告；只改动候选包中的 `dist/preload.js` 与 `dist/menu.js`，并修复翻译后的更新菜单分发。

**该命令仅构建候选文件，不替换、签名或启动已安装客户端。** macOS 部署前需完整备份官方 `.app`。本机实际部署使用了本地重新签名，并经确认仅为该应用放宽动态库校验和进行 Gatekeeper 放行；这是额外的安全取舍，构建器不会自动执行这些操作。恢复官方签名应恢复完整原版 `.app`，不能只恢复 `app.asar`。

主要菜单、侧边栏和设置页面已在本机验证显示中文。部分动态文字仍为英文；官方升级可能覆盖汉化。构建产物与官方应用文件不随仓库分发。

词典和翻译引擎来源、固定版本与 MIT 许可证见 [第三方声明](tools/antigravity-zh-cn/THIRD_PARTY_NOTICES.md)。本项目与 Google 无隶属关系。

## 本机文件与 Git

真实 `config.yaml`、编辑器交换文件、`.env`、`auth/`、`runtime/`、`logs/`、`vendor/` 和 `.app` / `.asar` 安装产物都由 `.gitignore` 排除。不要为了上传运行数据而取消这些规则。
