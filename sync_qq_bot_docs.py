#!/usr/bin/env python3
"""Synchronize the QQ Bot documentation with a local repository."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urljoin

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError as exc:  # pragma: no cover - exercised by command-line usage
    raise SystemExit(
        "Playwright is not installed. Run: python -m pip install -r requirements.txt"
    ) from exc


DEFAULT_BASE_URL = "https://bot.q.qq.com/wiki/#"
DEFAULT_ROOT_FOLDER = "QQ机器人开发文档"
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_MIN_FILES = 50
MANIFEST_RELATIVE_PATH = Path(".github/qq-bot-docs-sync.json")
PROTECTED_MARKDOWN = {
    "README.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
}
UPDATE_LABELS = re.compile(r"^(更新|立即更新|点击更新|刷新|刷新页面|重新加载)$")


class SyncError(RuntimeError):
    """Raised when a documentation sync cannot be completed safely."""


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Download QQ Bot Markdown documentation and synchronize a Git repository."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=script_dir,
        help="Repository directory to update (default: directory containing this script).",
    )
    parser.add_argument(
        "--userscript",
        type=Path,
        default=script_dir / "qq-bot-doc-downloader.user.js",
        help="Path to qq-bot-doc-downloader.user.js.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--min-files", type=int, default=DEFAULT_MIN_FILES)
    parser.add_argument(
        "--browser-channel",
        help="Optional installed browser channel, for example chrome or msedge.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window. Headless mode is used by default.",
    )
    parser.add_argument(
        "--no-bootstrap-prune",
        action="store_true",
        help=(
            "When no sync manifest exists, do not treat tracked Markdown files as "
            "previously managed files."
        ),
    )
    return parser.parse_args()


def log(message: str) -> None:
    print(message, flush=True)


def assert_existing_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise SyncError(f"{label} was not found: {resolved}")
    return resolved


def browser_app_version(page) -> str:
    app_src = page.evaluate(
        """
        () => {
          const scripts = Array.from(document.scripts).map((script) => script.src);
          return scripts.find((src) => /\\/assets\\/js\\/app\\.[a-f0-9]+\\.js/.test(src)) || "";
        }
        """
    )
    match = re.search(r"/v(\d+\.\d+\.\d+)/assets/js/app\.", app_src or "")
    return match.group(1) if match else ""


def latest_network_version(base_url: str) -> str:
    wiki_url = urljoin(base_url, "/wiki/")
    request = urllib.request.Request(
        wiki_url,
        headers={
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
            "User-Agent": "qq-bot-docs-sync/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise SyncError(f"Failed to check the latest documentation version: {exc}") from exc

    match = re.search(
        r"/bot-docs/-/v(\d+\.\d+\.\d+)/assets/js/app\.[a-f0-9]+\.js",
        html,
    )
    if not match:
        raise SyncError("The latest documentation HTML did not contain an app.js version.")
    return match.group(1)


def is_bottom_right(page, node) -> bool:
    if not node.is_visible():
        return False
    box = node.bounding_box()
    if not box:
        return False
    viewport = page.viewport_size or {"width": 1440, "height": 900}
    center_x = box["x"] + box["width"] / 2
    center_y = box["y"] + box["height"] / 2
    return center_x >= viewport["width"] * 0.6 and center_y >= viewport["height"] * 0.5


def click_update_if_present(page) -> str | None:
    popup_buttons = page.locator(".sw-update-popup button")
    candidates = [popup_buttons.nth(index) for index in range(popup_buttons.count())]
    text_candidates = page.get_by_text(UPDATE_LABELS)
    candidates.extend(
        text_candidates.nth(index)
        for index in range(min(text_candidates.count(), 30))
    )

    for node in candidates:
        if not node.is_visible():
            continue
        if node.evaluate("(element) => !element.closest('.sw-update-popup')") and not is_bottom_right(page, node):
            continue

        label = (node.inner_text() or "").strip()
        log(f"Detected update control: {label!r}; clicking it.")
        try:
            node.click(timeout=5000)
        except PlaywrightError as exc:
            raise SyncError(f"Failed to click the documentation update control: {exc}") from exc

        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(750)
        return label

    return None


def wait_for_download_button(page) -> None:
    page.wait_for_selector(
        "#qq-bot-doc-downloader .qq-bot-doc-btn",
        state="visible",
        timeout=60000,
    )


def wait_for_download(page, timeout_seconds: int):
    button = page.locator("#qq-bot-doc-downloader .qq-bot-doc-btn")
    status = page.locator("#qq-bot-doc-downloader .qq-bot-doc-status")
    try:
        with page.expect_download(timeout=timeout_seconds * 1000) as download_info:
            button.click()
        return download_info.value
    except PlaywrightTimeoutError as exc:
        detail = status.inner_text().strip() if status.count() else ""
        suffix = f" Current status: {detail}" if detail else ""
        raise SyncError(f"Timed out waiting for the documentation ZIP.{suffix}") from exc


def validate_final_status(page) -> str:
    status = page.locator("#qq-bot-doc-downloader .qq-bot-doc-status")
    current = ""
    for _ in range(40):
        if status.count():
            current = (status.inner_text() or "").strip()
        if current.startswith("完成："):
            break
        if current.startswith("下载失败："):
            raise SyncError(current)
        page.wait_for_timeout(250)

    if not current.startswith("完成："):
        raise SyncError(f"Documentation download did not report completion: {current or 'no status'}")

    failed = re.search(r"失败\s*(\d+)\s*篇", current)
    if failed and int(failed.group(1)) > 0:
        raise SyncError(f"Partial download refused: {current}")
    return current


def extract_markdown(zip_path: Path, staging_dir: Path) -> dict[str, Path]:
    extracted: dict[str, Path] = {}
    staging_root = staging_dir.resolve()

    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue

            parts = PurePosixPath(info.filename.replace("\\", "/")).parts
            if parts and parts[0] == DEFAULT_ROOT_FOLDER:
                parts = parts[1:]
            if not parts or any(part in {"", ".", ".."} for part in parts):
                continue

            relative = PurePosixPath(*parts)
            if relative.suffix.lower() != ".md":
                continue

            destination = staging_root.joinpath(*relative.parts).resolve()
            try:
                destination.relative_to(staging_root)
            except ValueError as exc:
                raise SyncError(f"Unsafe ZIP entry: {info.filename}") from exc

            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(info))
            extracted[relative.as_posix()] = destination

    return extracted


def tracked_markdown(repo_root: Path) -> set[str]:
    if not (repo_root / ".git").is_dir():
        return set()

    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", "*.md"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return set()

    return {
        path
        for path in result.stdout.decode("utf-8", errors="replace").split("\0")
        if path and path not in PROTECTED_MARKDOWN
    }


def load_previous_files(
    repo_root: Path, manifest_path: Path, bootstrap_prune: bool
) -> set[str]:
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SyncError(f"Invalid sync manifest: {manifest_path}: {exc}") from exc
        files = manifest.get("files")
        if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
            raise SyncError(f"Invalid files list in sync manifest: {manifest_path}")
        return {item for item in files if item.endswith(".md")}

    return tracked_markdown(repo_root) if bootstrap_prune else set()


def remove_empty_parents(paths: Iterable[Path], stop: Path) -> None:
    stop = stop.resolve()
    candidates = {parent for path in paths for parent in path.parents}
    for directory in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
        resolved = directory.resolve()
        if resolved == stop or stop not in resolved.parents:
            continue
        try:
            resolved.rmdir()
        except OSError:
            pass


def apply_sync(
    repo_root: Path,
    manifest_path: Path,
    staging_files: dict[str, Path],
    previous_files: set[str],
    source_version: str,
) -> tuple[int, int]:
    new_files = set(staging_files)
    if len(new_files) != len(staging_files):
        raise SyncError("Duplicate output paths were produced by the downloader.")

    stale_files = previous_files - new_files
    removed_paths: list[Path] = []
    for relative in sorted(stale_files):
        if relative in PROTECTED_MARKDOWN:
            continue
        destination = (repo_root / relative).resolve()
        try:
            destination.relative_to(repo_root.resolve())
        except ValueError:
            continue
        if destination.is_file():
            destination.unlink()
            removed_paths.append(destination)

    changed = 0
    for relative, source in sorted(staging_files.items()):
        destination = (repo_root / relative).resolve()
        try:
            destination.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise SyncError(f"Unsafe output path: {relative}") from exc

        data = source.read_bytes()
        if destination.is_file() and destination.read_bytes() == data:
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        changed += 1

    remove_empty_parents(removed_paths, repo_root)

    manifest = {
        "source": DEFAULT_BASE_URL,
        "sourceVersion": source_version or "unknown",
        "files": sorted(new_files),
    }
    encoded_manifest = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8") != encoded_manifest:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(encoded_manifest, encoding="utf-8")

    return changed, len(removed_paths)


def run_sync(args: argparse.Namespace) -> None:
    repo_root = args.repo_root.expanduser().resolve()
    userscript = assert_existing_file(args.userscript, "Userscript")
    repo_root.mkdir(parents=True, exist_ok=True)
    manifest_path = repo_root / MANIFEST_RELATIVE_PATH

    if args.timeout_seconds < 60:
        raise SyncError("--timeout-seconds must be at least 60.")
    if args.min_files < 1:
        raise SyncError("--min-files must be positive.")

    userscript_source = userscript.read_text(encoding="utf-8")
    expected_version = latest_network_version(args.base_url)
    log(f"Latest public documentation version: {expected_version}")

    launch_options = {
        "headless": not args.headed,
        "args": [
            "--disable-application-cache",
            "--disable-http-cache",
            "--disk-cache-size=1",
            "--media-cache-size=1",
        ],
    }
    if args.browser_channel:
        launch_options["channel"] = args.browser_channel

    previous_files = load_previous_files(
        repo_root,
        manifest_path,
        bootstrap_prune=not args.no_bootstrap_prune,
    )

    with tempfile.TemporaryDirectory(prefix="qq-bot-docs-") as temp_name:
        temp_dir = Path(temp_name)
        staging_dir = temp_dir / "staging"
        zip_path = temp_dir / "qq-bot-docs.zip"
        staging_dir.mkdir()

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**launch_options)
            context = browser.new_context(
                accept_downloads=True,
                locale="zh-CN",
                service_workers="block",
                viewport={"width": 1440, "height": 900},
            )
            context.add_init_script(
                """
                window.GM_addStyle = function (css) {
                  const applyStyle = function () {
                    const style = document.createElement('style');
                    style.textContent = css;
                    (document.head || document.documentElement).appendChild(style);
                  };
                  if (document.head || document.documentElement) {
                    applyStyle();
                  } else {
                    document.addEventListener('DOMContentLoaded', applyStyle, { once: true });
                  }
                };
                """
            )
            context.add_init_script(userscript_source)

            page = context.new_page()
            cdp = context.new_cdp_session(page)
            cdp.send("Network.enable")
            cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})

            log(f"Opening a clean browser context: {args.base_url}")
            page.goto(args.base_url, wait_until="domcontentloaded", timeout=60000)
            wait_for_download_button(page)

            update_label = click_update_if_present(page)
            if update_label:
                wait_for_download_button(page)

            actual_version = browser_app_version(page)
            if actual_version != expected_version:
                log(
                    "Browser still reported version "
                    f"{actual_version or 'unknown'} after loading; reloading once."
                )
                page.reload(wait_until="domcontentloaded", timeout=60000)
                wait_for_download_button(page)
                actual_version = browser_app_version(page)

            if actual_version != expected_version:
                raise SyncError(
                    "Cached documentation refused: browser version "
                    f"{actual_version or 'unknown'} != public version {expected_version}."
                )
            log(f"Browser is using the current documentation version: {actual_version}")

            log("Starting the userscript documentation download.")
            download = wait_for_download(page, args.timeout_seconds)
            download.save_as(zip_path)
            final_status = validate_final_status(page)
            log(final_status)
            browser.close()

        staging_files = extract_markdown(zip_path, staging_dir)
        if len(staging_files) < args.min_files:
            raise SyncError(
                f"Only {len(staging_files)} Markdown files were produced; "
                f"refusing to replace the repository (minimum {args.min_files})."
            )

        changed, removed = apply_sync(
            repo_root=repo_root,
            manifest_path=manifest_path,
            staging_files=staging_files,
            previous_files=previous_files,
            source_version=actual_version,
        )

    log(
        f"Sync complete: {len(staging_files)} documents checked, "
        f"{changed} files changed, {removed} stale files removed."
    )


def main() -> int:
    args = parse_args()
    try:
        run_sync(args)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
