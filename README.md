# AnqiCMS 站群管理工具（Web 控制台 & MCP 总控）

面向 AnqiCMS 多站点运营的高密度本地 Web 控制台与站群自动化工具，集成友链全流程管理、智能导入、统一灰度升级、免密跳后台、定时文章发布及内置 MCP Server，支持单文件 Windows EXE 运行与 GitHub Releases 一键自动更新。

## 核心功能

- **站群 Web 控制台**：基于 Python 标准库 HTTP/JSON + 纯静态单页前端，零外部前端依赖；支持本地独立应用窗口（`--mode app`）及服务器/挂机宝无头模式（`--no-browser`）。
- **5 合 1 友链中心**：
  - **单站友链**：实时读取与管理单站友链，支持智能粘贴 HTML `<a>` 标签或多行文本一键上链。
  - **统一上下链**：跨多站并发批量上链/下链，支持按主域名自动去重跳过。
  - **站群互链**：内置 5 种拓扑模式（`random_k` 随机 K 度、`group_cross` 跨组互链、`ring` 轮链、`hub` 主从星型、`mesh` 全互链），支持锚文本轮换、防直接互指、台账记录与一键精准回滚。
  - **全站反链巡检**：并发检测对方页面反链存活、HTTP 状态与 `nofollow` 标记，支持域名白名单与一键批量下掉掉链。
  - **全站查链**：按主域名或关键词跨全站检索友链并批量清理。
- **AnqiCMS v3.6.x MCP 适配**：自动探测站点连通性、CMS 版本、可用 MCP 工具与默认栏目，友链与升级流程兼容 MCP 与 HTTP API 双通道。
- **智能粘贴导入**：在「站点管理与导入」中直接混贴 AnqiCMS「一键复制 MCP JSON」与导入接口 URL，按域名自动合并并补全已有站点凭据；支持 `fid=0` 纯站群模式（不开启自动发布）。
- **统一升级**：批量检测站群版本，支持金丝雀先行（先升第 1 站验证健康）、1–5 并发控制、失败即停与中文异常指引。
- **免密代理跳后台**：支持 Ticket 免密跳转、本地反代静默登录桥接、直达后台三级策略，支持批量一键打开多个站点后台。
- **内置站群总控 MCP Server**：同时提供 Streamable HTTP（`http://127.0.0.1:<port>/mcp`）与 Stdio（`mcp_server.py` / `--mcp-stdio`），可在「系统设置 & MCP」一键复制客户端配置接入 Cursor、Claude Desktop 等 AI 工具。
- **一键自动更新**：在「系统设置 & MCP」内置 GitHub Releases（`zaomeng/anqicms-manager`）检查更新、SHA256 校验与 Windows EXE 一键热替换重启。

## 快速开始

### 方式一：运行 Windows EXE（推荐）

1. 前往 [GitHub Releases](https://github.com/zaomeng/anqicms-manager/releases) 下载最新版 `anqicms站群控制台.exe`（或 `anqicms站群控制台.zip`）。
2. 将程序放入独立文件夹；如需从零配置，可复制本仓库的 [`config.example.ini`](config.example.ini) 为同目录下的 `config.ini` 并按需修改，或直接双击启动后在 Web 控制台「站点管理与导入」中批量粘贴导入。
3. 双击 `anqicms站群控制台.exe`，程序会自动启动本地服务并打开控制台窗口。

### 方式二：从源码运行

要求 Python 3.11+（推荐 Python 3.12）：

```bash
pip install -r requirements.txt
copy config.example.ini config.ini
python web_app.py
```

常用启动参数：

```bash
# 指定固定端口与配置文件
python web_app.py --port 18900 --config config.ini

# 无头服务模式（VPS / 挂机宝远程访问）
python web_app.py --host 0.0.0.0 --port 18900 --no-browser

# 以 MCP Stdio Server 模式运行
python web_app.py --mcp-stdio --config config.ini
```

## 打包与发布构建

### 本地打包（Windows）

项目提供了自动化打包脚本 `build-web-release.ps1`（使用 `requirements-build.lock` 锁定依赖并生成 `EXE`、`ZIP` 及 `SHA256SUMS-web.txt`）：

```powershell
.\build-web-release.ps1
```

或直接使用 PyInstaller：

```powershell
pip install -r requirements-build.lock
pyinstaller --noconfirm --clean anqicms_web.spec
```

### GitHub Actions 自动发布

仓库配置了 `.github/workflows/release-web.yml`：
- 推送 `v*` 标签（例如 `git tag v2.0.1 && git push origin v2.0.1`）或在 Actions 页面手动触发 `workflow_dispatch`。
- 工作流会在 `windows-latest` 上安装依赖、运行 `python -m pytest -q`、执行 `pyinstaller --noconfirm --clean anqicms_web.spec` 打包，并自动生成 `anqicms站群控制台.zip` 与 `SHA256SUMS-web.txt` 上传至对应 GitHub Release。

## 安全与目录说明

- `config.ini`、`设置/`、`文章目录/`、`归档/`、`*.db`、`*.log` 等运行时与敏感凭据文件均已列入 `.gitignore`，请勿提交到公开仓库。
- Web 控制台内置敏感信息脱敏（日志、API 返回与任务进度均自动遮罩 Token 与密码）。
