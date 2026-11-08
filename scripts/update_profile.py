#!/usr/bin/env python3
"""Refresh the generated section of a GitHub profile using public repository data."""

import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
START = "<!-- PROFILE:START -->"
END = "<!-- PROFILE:END -->"


def validate_username(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", value):
        raise ValueError("Invalid GitHub username")
    return value


def validate_repo_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) or value in (".", ".."):
        raise ValueError("Invalid repository name")
    return value


def text(value):
    value = " ".join(value.split())[:300]
    value = html.escape(value, quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+.!|~>\-])", r"\\\1", value)


def repo_link(username, name):
    validate_repo_name(name)
    return f"[{text(name)}](https://github.com/{quote(username)}/{quote(name)})"


def fetch_repositories(username):
    validate_username(username)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "oishijie-profile-sync", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    repos = []
    page = 1
    while True:
        url = f"https://api.github.com/users/{quote(username)}/repos?type=owner&per_page=100&sort=pushed&page={page}"
        try:
            with urlopen(Request(url, headers=headers), timeout=25) as response:
                batch = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"GitHub API returned HTTP {error.code}; README left unchanged") from None
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"GitHub API unavailable ({type(error).__name__}); README left unchanged") from None
        if not isinstance(batch, list):
            raise ValueError("GitHub API did not return a repository list")
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    if not repos:
        raise ValueError("Empty repository response; refusing to clear profile")
    return repos


def public_repositories(username, repos):
    result = []
    seen = set()
    for repo in repos:
        if not isinstance(repo, dict):
            raise ValueError("Invalid repository record")
        owner = repo.get("owner")
        if not isinstance(owner, dict) or not isinstance(owner.get("login"), str):
            raise ValueError("Invalid repository owner")
        for key in ("private", "fork", "archived", "disabled"):
            if not isinstance(repo.get(key), bool):
                raise ValueError(f"Invalid repository field: {key}")
        name = validate_repo_name(repo.get("name"))
        if repo["private"] or owner["login"].lower() != username.lower() or name.lower() == username.lower():
            continue
        for key in ("description", "language", "pushed_at"):
            if repo.get(key) is not None and not isinstance(repo[key], str):
                raise ValueError(f"Invalid repository field: {key}")
        pushed = repo.get("pushed_at")
        if pushed:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", pushed):
                raise ValueError("Invalid push timestamp")
            datetime.strptime(pushed, "%Y-%m-%dT%H:%M:%SZ")
        stars = repo.get("stargazers_count")
        if type(stars) is not int or stars < 0:
            raise ValueError("Invalid star count")
        if name.lower() in seen:
            raise ValueError("Duplicate repository in API response")
        seen.add(name.lower())
        result.append(repo)
    if not result:
        raise ValueError("No public projects found; refusing to clear profile")
    return result


def render_profile(config, repos):
    username = validate_username(config.get("username"))
    featured = config.get("featured")
    if not isinstance(featured, list) or not featured:
        raise ValueError("featured must be a non-empty list")
    limit = config.get("recent_limit")
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("recent_limit must be between 1 and 10")
    repos = public_repositories(username, repos)
    by_name = {repo["name"].lower(): repo for repo in repos}
    lines = ["## 代表项目", "", "| 项目 | 说明 | 语言 |", "| :--- | :--- | :--- |"]
    featured_count = 0
    selected = set()
    for entry in featured:
        if not isinstance(entry, dict):
            raise ValueError("Invalid featured project")
        name = validate_repo_name(entry.get("name"))
        fallback = entry.get("fallback_description", "")
        if not isinstance(fallback, str) or name.lower() in selected:
            raise ValueError("Invalid or duplicate featured project")
        selected.add(name.lower())
        repo = by_name.get(name.lower())
        if repo is None or repo["disabled"]:
            continue
        label = repo_link(username, repo["name"])
        if repo["fork"]:
            label += " · Fork"
        if repo["archived"]:
            label += " · 已归档"
        description = text(repo.get("description") or fallback or "暂无说明")
        language = text(repo.get("language") or "—")
        lines.append(f"| {label} | {description} | {language} |")
        featured_count += 1
    if not featured_count:
        raise ValueError("No configured featured projects are public; README left unchanged")
    lines += ["", "## 最近在做", "", "> 按最后推送时间排序，不代表开发进度。", ""]
    recent = sorted((repo for repo in repos if not repo["archived"] and not repo["disabled"] and repo.get("pushed_at")), key=lambda repo: (repo["pushed_at"], repo["name"].lower()), reverse=True)[:limit]
    for repo in recent:
        suffix = " · Fork" if repo["fork"] else ""
        lines.append(f"- {repo_link(username, repo['name'])} · {repo['pushed_at'][:10]}{suffix}")
    if not recent:
        lines.append("暂无公开推送记录。")
    languages = sorted({repo["language"] for repo in repos if repo.get("language")})
    lines += ["", "## 使用中的语言", "", " · ".join(text(language) for language in languages) or "暂无公开语言数据。", "", "---", ""]
    latest = max((repo["pushed_at"] for repo in repos if repo.get("pushed_at")), default=None)
    summary = f"公开项目 **{len(repos)}** · 累计 Stars **{sum(repo['stargazers_count'] for repo in repos)}**"
    if latest:
        summary += f" · 最近推送 **{latest[:10]}**"
    lines += [summary, "", "<sub>不含主页仓库；日期为 UTC。语言来自各仓库主语言，不代表熟练程度。</sub>"]
    return "\n".join(lines)


def update_readme(path, content):
    original = path.read_text(encoding="utf-8")
    if original.count(START) != 1 or original.count(END) != 1:
        raise ValueError("README must contain exactly one pair of PROFILE markers")
    start = original.index(START) + len(START)
    end = original.index(END)
    if start > end:
        raise ValueError("README PROFILE markers are out of order")
    if START in content or END in content:
        raise ValueError("Generated content contains a reserved marker")
    updated = original[:start] + "\n\n" + content + "\n\n" + original[end:]
    if updated == original:
        return False
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".profile-", delete=False) as file:
            temp_path = Path(file.name)
            file.write(updated)
        temp_path.chmod(path.stat().st_mode & 0o777)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    return True


def main():
    try:
        config = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("profile.json must contain an object")
        username = validate_username(config.get("username"))
        content = render_profile(config, fetch_repositories(username))
        changed = update_readme(ROOT / "README.md", content)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Profile update failed: {error}", file=sys.stderr)
        return 1
    print("Profile updated." if changed else "Profile unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
