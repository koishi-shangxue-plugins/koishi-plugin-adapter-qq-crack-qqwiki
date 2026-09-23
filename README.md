# QQ 机器人开发文档同步仓库

这个仓库通过 GitHub Actions 每天自动同步 QQ 机器人官方开发文档。

## 文件说明

- `qq-bot-doc-downloader.user.js`：可在 Tampermonkey 等用户脚本管理器中安装的一键下载脚本。
- `sync_qq_bot_docs.py`：启动全新的无缓存 Chromium，调用用户脚本下载文档，并安全覆盖本仓库。
- `requirements.txt`：固定 Python Playwright 版本。
- `.github/qq-bot-docs-sync.json`：记录当前同步版本和由脚本管理的 Markdown 文件。
- `.github/workflows/sync-qq-bot-docs.yml`：每天运行的 GitHub Actions 工作流。

## GitHub Actions

工作流在每天北京时间 08:00 自动运行，也可以在仓库的 `Actions` 页面手动运行。

首次推送到 GitHub 后，请确认仓库允许 Actions 写入：

1. 打开仓库的 `Settings`。
2. 打开 `Actions` -> `General`。
3. 在 `Workflow permissions` 中选择 `Read and write permissions`。
4. 保存设置。

工作流检测到文档变化后，会以 `github-actions[bot]` 身份创建提交并推送。提交信息包含官方文档版本和日期，例如：

```text
docs: sync QQ bot documentation 1.32.0 (2026-09-28)
```

本地只需要执行 `git pull`，就可以通过每个提交的 diff 查看 QQ 官方这一次更新了哪些内容。如果没有变化，则不会产生提交。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe sync_qq_bot_docs.py
```

如果本机已经安装了 Chrome，可以跳过 Playwright 浏览器安装，并指定浏览器通道：

```powershell
.\.venv\Scripts\python.exe sync_qq_bot_docs.py --browser-channel chrome
```

## 缓存处理

同步器每次都会启动新的浏览器进程和独立上下文，关闭 HTTP 缓存并阻止 Service Worker 接管页面。如果官方站点显示新版更新弹窗，脚本会点击其中的 `刷新` 按钮，然后再次确认浏览器加载的版本与公开页面版本一致。

只有全部文档下载成功且产物数量达到安全阈值时，脚本才会更新仓库。之前由脚本管理、但当前目录中已经删除的 Markdown 文件也会从仓库中清理。
