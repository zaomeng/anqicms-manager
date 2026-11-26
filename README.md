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

### 方式一：运行 Windows EXE（推荐，无需安装 Python）

1. 前往 [GitHub Releases](https://github.com/zaomeng/anqicms-manager/releases) 下载最新版（永久直链：[anqicms-manager.zip](https://github.com/zaomeng/anqicms-manager/releases/latest/download/anqicms-manager.zip) / [anqicms-manager.exe](https://github.com/zaomeng/anqicms-manager/releases/latest/download/anqicms-manager.exe)）压缩包 **`anqicms-manager.zip`**（解压后得到 `anqicms-manager.exe`）或直接下载单文件 **`anqicms-manager.exe`**（已避开 AnqiCMS 建站程序 `anqicms.exe` 同名冲突）。
2. 将程序放入你的站点配置目录（即包含 `config.ini` 的文件夹）；如需从零配置，可复制本仓库的 [`config.example.ini`](config.example.ini) 为同目录下的 `config.ini`，或直接双击启动后在 Web 控制台「站点管理与导入」中批量粘贴导入（支持直接混贴 AnqiCMS 后台复制的 `mcpServers` JSON 和 `/api/import/archive?token=...` 地址）。
3. 双击运行，程序会自动启动本地 Web 服务并打开独立控制台窗口。
4. 后续有新版本时，直接点击控制台右上角版本号或进入「系统设置 → 程序版本与 GitHub 自动更新」即可一键检查、校验并自动更新重启。

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

### GitHub Actions 自动发布新版本

仓库已配置中文注释完整的云端构建工作流 [`.github/workflows/release-web.yml`](.github/workflows/release-web.yml)：

1. **升级版本号**：修改 [`core/app_info.py`](core/app_info.py) 中的 `APP_VERSION`（例如改为 `"2.0.1"`）。
2. **提交并推送版本标签**：
   ```bash
   git add .
   git commit -m "release: v2.0.1"
   git push origin main
   git tag v2.0.1
   git push origin v2.0.1
   ```
3. **自动构建上架**：GitHub Actions 会在 `windows-latest` 上自动安装依赖、运行 1,100+ 项核心与 Web 测试、通过 `anqicms_web.spec` 编译单文件 EXE，并自动生成 ZIP 与 `SHA256SUMS-web.txt` 发布到 [GitHub Releases](https://github.com/zaomeng/anqicms-manager/releases)。
4. **手动触发**：也可以直接在仓库的 **Actions → 构建并发布 Web 控制台 EXE → Run workflow** 手动触发云端构建或补发 Release。

## 文档索引

- [Web 站群控制台与 MCP 使用说明](docs/Web站群控制台与MCP使用说明.md)（含远程挂机宝模式、免密跳后台、12 个内置 MCP 工具与 AI 接入配置）
- [站群管理使用说明](docs/站群管理使用说明.md)（AnqiCMS 后台 Token 与开放意图配置说明）
- [站群架构设计文档](docs/plans/2026-11-21-sitegroup-architecture.md)

## 安全与目录说明

- `config.ini`、`设置/`、`文章目录/`、`归档/`、`*.db`、`*.log` 等运行时与敏感凭据文件均已列入 `.gitignore`，请勿提交到公开仓库。
- Web 控制台内置敏感信息脱敏（日志、API 返回与任务进度均自动遮罩 Token 与密码）。
