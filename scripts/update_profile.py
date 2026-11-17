#!/usr/bin/env python3
"""Refresh profile telemetry, Omni metadata, and the 60-day contribution waveform."""

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
CONTRIBUTIONS_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    createdAt
    contributionsCollection(from: $from, to: $to) {
      restrictedContributionsCount
      contributionCalendar {
        weeks {
          contributionDays {
            date
            contributionCount
          }
        }
      }
    }
  }
}
"""
OMNI_DESCRIPTION = "No repository description set."
ANIMATION_REQUIREMENTS = {
    "header.svg": (
        'class="frame"',
        'class="radar"',
        "@keyframes framePulse",
        "@keyframes orbit",
    ),
    "pulse.svg": (
        'class="frame"',
        'class="trace"',
        'class="scanner"',
        "@keyframes framePulse",
        "@keyframes trace",
        "@keyframes scan",
    ),
    "telemetry.svg": (
        'class="frame"',
        "@keyframes framePulse",
        "@keyframes online",
    ),
    "project-omni.svg": (
        'class="frame"',
        "@keyframes framePulse",
        "@keyframes activity",
    ),
}


class GitHubAPIError(RuntimeError):
    def __init__(self, status_code, path, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(
            "GitHub API returned HTTP {} for {}: {}".format(
                status_code, path, detail
            )
        )


def validate_animation_styles(svg, filename):
    missing = [
        requirement
        for requirement in ANIMATION_REQUIREMENTS[filename]
        if requirement not in svg
    ]
    if missing:
        raise ValueError(
            "{} is missing required animation CSS: {}".format(
                filename, ", ".join(missing)
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


def graphql_json(query, variables, token):
    request = urllib.request.Request(
        API + "/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(
            "utf-8"
        ),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "profile-svg-updater",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise GitHubAPIError(error.code, "/graphql", detail) from error

    if result.get("errors"):
        raise RuntimeError(
            "GitHub GraphQL query failed: {}".format(
                json.dumps(result["errors"], ensure_ascii=True)
            )
        )
    return result["data"]


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


def contribution_data(username, start, end, token):
    data = graphql_json(
        CONTRIBUTIONS_QUERY,
        {
            "login": username,
            "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        token,
    )
    user = data.get("user")
    if user is None:
        raise RuntimeError(
            "GitHub GraphQL did not return the requested user: {}".format(
                username
            )
        )

    collection = user["contributionsCollection"]
    daily_counts = {}
    for week in collection["contributionCalendar"]["weeks"]:
        for day in week["contributionDays"]:
            day_date = datetime.date.fromisoformat(day["date"])
            daily_counts[day_date] = day["contributionCount"]
    return user["createdAt"], daily_counts, collection[
        "restrictedContributionsCount"
    ]


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


def write_telemetry(account_age, repository_count, contributions_year):
    path = ASSETS / "telemetry.svg"
    svg = path.read_text(encoding="utf-8")
    svg = replace_svg_text(svg, "uptime", "{:,}".format(account_age))
    svg = replace_svg_text(
        svg, "repositories", "{:,}".format(repository_count)
    )
    svg = replace_svg_text(
        svg, "contributions", "{:,}".format(contributions_year)
    )
    validate_animation_styles(svg, path.name)
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
    validate_animation_styles(svg, path.name)
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


def write_pulse(daily_contributions, today):
    path = ASSETS / "pulse.svg"
    svg = path.read_text(encoding="utf-8")
    start_date = today - datetime.timedelta(days=59)
    counts = [
        daily_contributions.get(
            start_date + datetime.timedelta(days=offset), 0
        )
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
    svg = replace_svg_text(
        svg, "total", "CONTRIBUTIONS / {}".format(sum(counts))
    )
    svg = replace_svg_text(svg, "start-date", start_date.strftime("%d %b %Y"))
    svg = replace_svg_text(svg, "end-date", today.strftime("%d %b %Y"))
    validate_animation_styles(svg, path.name)
    with path.open("w", encoding="utf-8") as output:
        output.write(svg)


def main():
    header = (ASSETS / "header.svg").read_text(encoding="utf-8")
    validate_animation_styles(header, "header.svg")

    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GH_PAT or GITHUB_TOKEN is required to update profile assets")

    if not os.environ.get("GH_PAT"):
        print(
            "GH_PAT is not set; GITHUB_TOKEN is being used. "
            "Private contribution data may be incomplete."
        )

    username = os.environ.get("PROFILE_USERNAME", "Fall1ar")
    omni_repository = os.environ.get("OMNI_REPOSITORY", "Fall1ar/Omni")
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.date()

    created_at_value, daily_contributions, restricted_contributions = (
        contribution_data(
            username,
            now - datetime.timedelta(days=365),
            now,
            token,
        )
    )
    account = api_json(
        "/users/{}".format(urllib.parse.quote(username, safe="")), token
    )
    created_at = github_date(created_at_value)
    account_age = (today - created_at.date()).days
    year_start = today - datetime.timedelta(days=364)
    contributions_year = sum(
        count
        for day, count in daily_contributions.items()
        if year_start <= day <= today
    )

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
    write_telemetry(account_age, account["public_repos"], contributions_year)
    write_project(omni, latest_commit)
    write_pulse(daily_contributions, today)

    print(
        "Updated telemetry for {}: {} public repositories, {} contributions "
        "in the last year ({} restricted/private); Omni metadata refreshed; "
        "pulse covers 60 days.".format(
            username,
            account["public_repos"],
            contributions_year,
            restricted_contributions,
        )
    )


if __name__ == "__main__":
    main()
