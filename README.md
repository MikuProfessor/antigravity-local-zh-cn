# Antigravity 本机工具集（汉化）

保存 macOS 上的 Antigravity 本机接口启停脚本、SillyTavern 连接验证工具，以及独立客户端的简体中文补丁和更新提醒。

仓库只包含源码、示例配置和文档。真实账号凭据、API key、聊天记录、日志、客户端安装包和完整应用备份均留在本机，不上传 GitHub。

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `启动反重力.command` / `停止反重力.command` | 通过 macOS LaunchAgent 启停本机接口 |
| `verify_local.py` | 用合成消息验证 SillyTavern → CLIProxyAPI 的普通与流式调用 |
| `config.example.yaml` | CLIProxyAPI v8 配置示例，不含有效密钥 |
| `examples/` | LaunchAgent 和 SillyTavern 集成配置示例 |
| `开启汉化更新提醒.command` / `检查汉化更新.command` | 开启登录后的更新提示，或立即手动检查 |
| `tools/antigravity-zh-cn/` | macOS 独立客户端的中文补丁构建器、更新器和上游词典 |
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

## macOS 中文界面更新

**已经汉化的 Mac**：双击 `开启汉化更新提醒.command` 一次。以后每次登录和每隔 6 小时，更新器都会检查 Google 为当前客户端架构推送的版本。Mac 需要 Python 3.9 或更高版本、Node.js，以及可由当前用户替换的 `/Applications/Antigravity.app`。

发现新版时，提示框会显示当前版本、可更新版本和下载大小。先保存工作，点击 **更新并重启**，更新器会下载官方 ZIP，核验大小、SHA-512、Google 开发者签名和系统安全评估，再构建中文补丁。在暂存副本验证通过后，更新器才会正常退出客户端、保留完整旧版并启动新版。点击 **稍后** 会推迟同一更新 24 小时。

- **现在就检查**。双击 `检查汉化更新.command`。用新版工具构建的客户端在 Antigravity 菜单中也有 **检查汉化更新…**。原有的 **检查更新** 仍由 Google 提供。
- **官方升级后恢复中文**。如果已开启提醒，Google 的更新覆盖了补丁，更新器在已装版本与通道版本相同时提供 **恢复中文并重启**。本机版本高于更新通道时会保留较新版本。
- **关闭提醒**。在本项目运行 `python3 tools/antigravity-zh-cn/updater.py disable`。运行 `python3 tools/antigravity-zh-cn/updater.py status` 可查看当前状态，无需弹窗。
- **工具升级**。从新发行版再运行一次 `开启汉化更新提醒.command`，即可更新后台检查工具。启用后，工具会复制到当前用户的 `~/Library/Application Support/Antigravity 中文更新/`，可以移动下载的源码目录。
- **恢复旧版**。每次成功更新后会显示完整旧版的备份路径。若新版本启动检查失败，更新器会尝试自动回退。手动恢复时，先退出客户端，再用备份的完整 `Antigravity.app` 恢复到原安装位置。

更新器跟随独立客户端的 macOS 自动更新通道（Apple Silicon 或 Intel），推送时间可能晚于官网开放下载的时间。版本号与官方客户端一致。未来版本会在本机进行补丁结构和资源完整性检查；如果 Google 改变相关结构，更新会在替换前停止。主要界面和菜单使用已有中文词典，新加入的文字可能仍显示英文。

本机汉化副本使用本地签名。更新器沿用这台 Mac 上该客户端已有的签名权限（包括该应用已有的动态库校验例外）。首次安装、从未汉化的 Mac 需要先完成本机汉化部署。本地签名与 Google 签名的信任范围不同；账号、聊天数据和系统安全设置不属于更新操作。

### 单独构建中文补丁

```sh
python3 tools/antigravity-zh-cn/prepare.py \
  --app /path/to/official/Antigravity.app
```

构建器已使用官方 2.29.0 和 2.32.2 的 macOS Apple Silicon 客户端验证。默认输出到本项目的 `runtime/antigravity-zh-cn-build/`。可通过 `--output` 指定目录，或通过 `--node` 指定 Node.js。仅修改候选包内的 `dist/preload.js`、`dist/menu.js` 和对应的完整性信息，生成原始资源副本及校验报告。这个命令只生成文件，适合单独验证新版适配情况。源码发布包只分发工具和词典。

词典和翻译引擎来源、固定版本与 MIT 许可证见 [第三方声明](tools/antigravity-zh-cn/THIRD_PARTY_NOTICES.md)。本项目与 Google 无隶属关系。

## 本机文件与 Git

真实 `config.yaml`、编辑器交换文件、`.env`、`auth/`、`runtime/`、`logs/`、`vendor/` 和 `.app` / `.asar` 安装产物都由 `.gitignore` 排除。不要为了上传运行数据而取消这些规则。
