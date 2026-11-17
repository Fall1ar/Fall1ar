#!/usr/bin/env python3
"""Refresh the profile's telemetry, Omni metadata, and 60-day commit waveform."""

import datetime
import html
import json
import os
import re
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
API = "https://api.github.com"
PAGE_SIZE = 100
OMNI_DESCRIPTION = (
    "Local desktop plugin manager built with Tauri v2, Rust, vanilla "
    "HTML/CSS/JavaScript, and Python worker plugins."
)


class GitHubAPIError(RuntimeError):
    def __init__(self, status_code, path, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(
            "GitHub API returned HTTP {} for {}: {}".format(
                status_code, path, detail
            )
        )


def api_json(path, token):
    request = urllib.request.Request(
        API + path,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "User-Agent": "profile-svg-updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise GitHubAPIError(error.code, path, detail) from error


def github_date(value):
    value = re.sub(r"\.\d+", "", value)
    if value.endswith("Z"):
        value = value[:-1] + "+0000"
    value = re.sub(
        r"([+-]\d{2}):(\d{2})$", r"\1\2", value
    )
    return datetime.datetime.strptime(
        value, "%Y-%m-%dT%H:%M:%S%z"
    ).astimezone(datetime.timezone.utc)


def public_repositories(username, token):
    repositories = []
    page_number = 1
    while True:
        query = urllib.parse.urlencode(
            {
                "type": "owner",
                "sort": "updated",
                "per_page": PAGE_SIZE,
                "page": page_number,
            }
        )
        path = "/users/{}/repos?{}".format(
            urllib.parse.quote(username, safe=""), query
        )
        page = api_json(path, token)
        if not page:
            return repositories
        repositories.extend(page)
        if len(page) < PAGE_SIZE:
            return repositories
        page_number += 1


def public_commit_dates(username, repositories, since, until, token):
    dates = []
    since_value = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_value = until.strftime("%Y-%m-%dT%H:%M:%SZ")

    for repository in repositories:
        full_name = repository["full_name"]
        page_number = 1
        while True:
            query = urllib.parse.urlencode(
                {
                    "author": username,
                    "since": since_value,
                    "until": until_value,
                    "per_page": PAGE_SIZE,
                    "page": page_number,
                }
            )
            path = "/repos/{}/commits?{}".format(
                urllib.parse.quote(full_name, safe="/"), query
            )
            try:
                page = api_json(path, token)
            except GitHubAPIError as error:
                if (
                    error.status_code == 409
                    and "git repository is empty" in error.detail.lower()
                ):
                    print("Skipping empty public repository {}.".format(full_name))
                    break
                raise
            for item in page:
                commit = item.get("commit", {})
                author = commit.get("author") or {}
                committer = commit.get("committer") or {}
                date_value = author.get("date") or committer.get("date")
                if date_value:
                    dates.append(github_date(date_value))
            if len(page) < PAGE_SIZE:
                break
            page_number += 1
    return dates


def safe_text(value):
    value = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    return html.escape(value, quote=True)


def replace_svg_text(svg, element_id, value):
    pattern = re.compile(
        r'(<text\b(?=[^>]*\bid="{}")[^>]*>).*?(</text>)'.format(
            re.escape(element_id)
        ),
        re.DOTALL,
    )
    updated, count = pattern.subn(
        lambda match: match.group(1) + safe_text(value) + match.group(2),
        svg,
        count=1,
    )
    if count != 1:
        raise ValueError("SVG text element not found: {}".format(element_id))
    return updated


def replace_svg_lines(svg, element_id, value, width, max_lines=1):
    lines = textwrap.wrap(
        re.sub(r"\s+", " ", str(value)).strip(),
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if not lines:
        lines = ["N/A"]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .") + "..."
    pattern = re.compile(
        r'(<text\b(?=[^>]*\bid="{}")[^>]*>).*?(</text>)'.format(
            re.escape(element_id)
        ),
        re.DOTALL,
    )

    def replacement(match):
        opening = match.group(1)
        closing = match.group(2)
        if len(lines) == 1:
            content = safe_text(lines[0])
        else:
            x_match = re.search(r'\bx="([^"]+)"', opening)
            x_value = x_match.group(1) if x_match else "58"
            content = "".join(
                '<tspan x="{}" dy="{}">{}</tspan>'.format(
                    x_value, "0" if index == 0 else "14", safe_text(line)
                )
                for index, line in enumerate(lines)
            )
        return opening + content + closing

    updated, count = pattern.subn(replacement, svg, count=1)
    if count != 1:
        raise ValueError("SVG text element not found: {}".format(element_id))
    return updated


def write_telemetry(account_age, repository_count, commits_year):
    path = ASSETS / "telemetry.svg"
    svg = path.read_text(encoding="utf-8")
    svg = replace_svg_text(svg, "uptime", "{:,}".format(account_age))
    svg = replace_svg_text(
        svg, "repositories", "{:,}".format(repository_count)
    )
    svg = replace_svg_text(svg, "commits", "{:,}".format(commits_year))
    with path.open("w", encoding="utf-8") as output:
        output.write(svg)


def write_project(repo, latest_commit):
    path = ASSETS / "project-omni.svg"
    svg = path.read_text(encoding="utf-8")
    description = repo.get("description") or OMNI_DESCRIPTION
    branch = repo.get("default_branch") or "UNKNOWN"
    language = repo.get("language") or "N/A"

    if latest_commit:
        commit_data = latest_commit.get("commit", {})
        author = commit_data.get("author") or {}
        committer = commit_data.get("committer") or {}
        date_value = author.get("date") or committer.get("date")
        commit_date = (
            github_date(date_value).strftime("%Y-%m-%d UTC")
            if date_value
            else "DATE UNAVAILABLE"
        )
        message = (commit_data.get("message") or "NO COMMIT MESSAGE").splitlines()[0]
    else:
        commit_date = "NO COMMITS FOUND"
        message = "Repository has no commits on its default branch."

    svg = replace_svg_lines(svg, "description", description, 145)
    svg = replace_svg_text(svg, "language", language)
    svg = replace_svg_text(svg, "branch", branch)
    svg = replace_svg_text(svg, "commit-date", commit_date)
    svg = replace_svg_lines(svg, "commit-message", message, 145)
    with path.open("w", encoding="utf-8") as output:
        output.write(svg)


def waveform_path(counts):
    left = 80
    right = 1120
    baseline = 250
    max_height = 142
    peak = max(counts) if counts else 0
    peak = max(peak, 1)
    points = []
    for index, count in enumerate(counts):
        x_value = left + (right - left) * index / max(len(counts) - 1, 1)
        y_value = baseline - max_height * count / peak
        points.append("L{:.1f} {:.1f}".format(x_value, y_value))
    return "M{:.1f} {:.1f}{}".format(left, baseline, "".join(points))


def write_pulse(commit_dates, today):
    path = ASSETS / "pulse.svg"
    svg = path.read_text(encoding="utf-8")
    start_date = today - datetime.timedelta(days=59)
    counts_by_day = {}
    for commit_date in commit_dates:
        day = commit_date.date()
        if start_date <= day <= today:
            counts_by_day[day] = counts_by_day.get(day, 0) + 1
    counts = [
        counts_by_day.get(start_date + datetime.timedelta(days=offset), 0)
        for offset in range(60)
    ]

    waveform = waveform_path(counts)
    path_pattern = re.compile(
        r'(<path id="waveform" class="trace" d=")[^"]*(")'
    )
    svg, path_count = path_pattern.subn(
        lambda match: match.group(1) + waveform + match.group(2), svg, count=1
    )
    if path_count != 1:
        raise ValueError("Pulse waveform path not found")

    markers = []
    for index, count in enumerate(counts):
        if count:
            x_value = 80 + 1040 * index / 59
            y_value = 250 - 142 * count / max(max(counts), 1)
            markers.append(
                '<circle class="peak" cx="{:.1f}" cy="{:.1f}" r="{}" '
                'fill="#FFFFFF"/>'.format(
                    x_value, y_value, min(6, 2 + count)
                )
            )
    marker_pattern = re.compile(
        r'(<g id="peak-markers">).*?(</g>)', re.DOTALL
    )
    svg, marker_count = marker_pattern.subn(
        lambda match: match.group(1) + "\n  ".join(markers) + match.group(2),
        svg,
        count=1,
    )
    if marker_count != 1:
        raise ValueError("Pulse peak marker group not found")
    svg = replace_svg_text(svg, "total", "COMMITS / {}".format(sum(counts)))
    svg = replace_svg_text(svg, "start-date", start_date.strftime("%d %b %Y"))
    svg = replace_svg_text(svg, "end-date", today.strftime("%d %b %Y"))
    with path.open("w", encoding="utf-8") as output:
        output.write(svg)


def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required to update profile assets")

    username = os.environ.get("PROFILE_USERNAME", "inuisaat-crypto")
    omni_repository = os.environ.get("OMNI_REPOSITORY", "Fall1ar/Omni")
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date()

    account = api_json(
        "/users/{}".format(urllib.parse.quote(username, safe="")), token
    )
    repositories = public_repositories(username, token)
    commit_dates = public_commit_dates(
        username,
        repositories,
        now - datetime.timedelta(days=365),
        now,
        token,
    )
    created_at = github_date(account["created_at"])
    account_age = (today - created_at.date()).days

    omni_path = "/repos/{}".format(
        urllib.parse.quote(omni_repository, safe="/")
    )
    omni = api_json(omni_path, token)
    branch = omni.get("default_branch") or "main"
    latest = api_json(
        "{}/commits?{}".format(
            omni_path,
            urllib.parse.urlencode({"sha": branch, "per_page": 1}),
        ),
        token,
    )
    latest_commit = latest[0] if latest else None

    ASSETS.mkdir(parents=True, exist_ok=True)
    write_telemetry(account_age, account.get("public_repos", 0), len(commit_dates))
    write_project(omni, latest_commit)
    write_pulse(commit_dates, today)

    print(
        "Updated telemetry for {}: {} public repositories, {} commits in the "
        "last year; Omni metadata refreshed; pulse covers 60 days.".format(
            username, account.get("public_repos", 0), len(commit_dates)
        )
    )


if __name__ == "__main__":
    main()
