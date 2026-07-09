"""
Regenerate the profile SVG (dark/light) with live data from GitHub.

This is the YOLIANI variant of Andrew Grant's `today.py` from
https://github.com/Andrew6rant/Andrew6rant — adapted to:
  * use the public REST API (no GraphQL / no PAT required)
  * skip lines-of-code (slow, requires PAT with broad scopes)
  * preserve the original SVG layout byte-for-byte (string replace instead
    of full XML round-trip, so namespace prefixes stay clean)
  * use `python-dateutil` when available for age math (else stdlib fallback)

Run via GitHub Actions on a schedule, see .github/workflows/generate.yml.
"""
import datetime
import os
import re
import time
import requests

USER_NAME = os.environ.get('USER_NAME', 'Yoliani')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')

HEADERS = {'Accept': 'application/vnd.github+json'}
if GITHUB_TOKEN:
    HEADERS['Authorization'] = f'Bearer {GITHUB_TOKEN}'

QUERY_COUNT = {
    'user': 0, 'followers': 0, 'repos': 0, 'stars': 0, 'contributed': 0, 'commits': 0,
}


# ---------------------------------------------------------------------------
# Age math
# ---------------------------------------------------------------------------

def daily_readme(birthday):
    diff = _rd(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), birthday)
    return '{} {}, {} {}, {} {}'.format(
        diff.years, 'year' + ('s' if diff.years != 1 else ''),
        diff.months, 'month' + ('s' if diff.months != 1 else ''),
        diff.days, 'day' + ('s' if diff.days != 1 else ''),
    )


def _rd(dt1, dt2):
    try:
        from dateutil.relativedelta import relativedelta
        return relativedelta(dt1, dt2)
    except ImportError:
        years = dt1.year - dt2.year
        try:
            anniv = dt2.replace(year=dt2.year + years)
        except ValueError:
            anniv = dt2.replace(year=dt2.year + years, day=28)
        if dt1 < anniv:
            years -= 1
            try:
                anniv = dt2.replace(year=dt2.year + years)
            except ValueError:
                anniv = dt2.replace(year=dt2.year + years, day=28)
        class _R:
            pass
        r = _R()
        r.years = max(0, years)
        # months
        months_total = (dt1.year - anniv.year) * 12 + (dt1.month - anniv.month)
        if dt1.day < anniv.day:
            months_total -= 1
        r.months = max(0, months_total % 12)
        r.days = max(0, (dt1 - anniv).days)
        return r


# ---------------------------------------------------------------------------
# GitHub REST API
# ---------------------------------------------------------------------------

def _get_json(url, params=None, extra_headers=None):
    h = dict(HEADERS)
    if extra_headers:
        h.update(extra_headers)
    r = requests.get(url, headers=h, params=params, timeout=30)
    if r.status_code == 200:
        return r.json()
    print(f'  ! GET {url} -> {r.status_code}: {r.text[:200]}')
    return None


def user_stats():
    QUERY_COUNT['user'] += 1
    data = _get_json(f'https://api.github.com/users/{USER_NAME}')
    return int(data['id']), data.get('created_at', ''), int(data.get('followers', 0))


def repos_owned_count():
    QUERY_COUNT['repos'] += 1
    data = _get_json('https://api.github.com/search/repositories',
                     params={'q': f'user:{USER_NAME}', 'per_page': 1})
    return int(data.get('total_count', 0)) if data else 0


def contributed_pr_count():
    """Public PRs authored by the user (used as a 'I contribute elsewhere' signal)."""
    QUERY_COUNT['contributed'] += 1
    data = _get_json('https://api.github.com/search/issues',
                     params={'q': f'type:pr author:{USER_NAME} is:public', 'per_page': 1})
    return int(data.get('total_count', 0)) if data else 0


def total_stars():
    QUERY_COUNT['stars'] += 1
    total = 0
    for page in range(1, 3):  # cap at 200 repos
        data = _get_json(f'https://api.github.com/users/{USER_NAME}/repos', params={
            'per_page': 100, 'page': page, 'type': 'owner', 'sort': 'full_name',
        })
        if not data:
            break
        for repo in data:
            total += int(repo.get('stargazers_count', 0))
        if len(data) < 100:
            break
    return total


def total_commits():
    """Total commits authored by the user (uses the search/commits preview API)."""
    QUERY_COUNT['commits'] += 1
    data = _get_json('https://api.github.com/search/commits',
                     params={'q': f'author:{USER_NAME}', 'per_page': 1},
                     extra_headers={'Accept': 'application/vnd.github.cloak-preview+json'})
    return int(data.get('total_count', 0)) if data else 0


# ---------------------------------------------------------------------------
# SVG manipulation (string-based to preserve original byte layout)
# ---------------------------------------------------------------------------

# We tag the dynamic values in the SVG as
#   <tspan class="value" id="commit_data">PLACEHOLDER</tspan>
# and the corresponding dot-spacers as
#   <tspan class="cc" id="commit_data_dots"> ............ </tspan>

def _update_tspan(text, element_id, new_value, target_width=None):
    """Replace the text inside <tspan ... id="ELEMENT_ID">...</tspan>."""
    new_value = str(new_value)
    # Match the tspan that contains the id and a placeholder text
    pattern = re.compile(
        r'(<tspan[^>]*\sid="' + re.escape(element_id) + r'"[^>]*>)([^<]*)(</tspan>)',
        re.DOTALL,
    )
    text, n = pattern.subn(lambda m: m.group(1) + new_value + m.group(3), text, count=1)
    if n != 1:
        print(f'  WARN: could not find tspan id="{element_id}"')
    if target_width is not None:
        # Adjust the corresponding _dots tspan
        just_len = max(0, target_width - len(new_value))
        if just_len <= 2:
            dot_string = {0: '', 1: ' ', 2: '. '}[just_len]
        else:
            dot_string = ' ' + ('.' * just_len) + ' '
        dots_pattern = re.compile(
            r'(<tspan[^>]*\sid="' + re.escape(element_id) + r'_dots"[^>]*>)([^<]*)(</tspan>)',
            re.DOTALL,
        )
        text, n2 = dots_pattern.subn(lambda m: m.group(1) + dot_string + m.group(3), text, count=1)
        if n2 != 1:
            print(f'  WARN: could not find tspan id="{element_id}_dots"')
    return text


def _update_age_dots(text, age_value):
    """Age dots: the value is long, give it a small but consistent pad."""
    target = 28
    pad = max(2, target - len(age_value))
    dot_string = ' ' + ('.' * pad) + ' '
    pattern = re.compile(
        r'(<tspan[^>]*\sid="age_data_dots"[^>]*>)([^<]*)(</tspan>)',
        re.DOTALL,
    )
    text, _ = pattern.subn(lambda m: m.group(1) + dot_string + m.group(3), text, count=1)
    return text


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data):
    with open(filename, 'r', encoding='utf-8') as f:
        text = f.read()

    text = _update_tspan(text, 'age_data', age_data)
    text = _update_age_dots(text, age_data)
    text = _update_tspan(text, 'commit_data',  f'{commit_data:,}', target_width=22)
    text = _update_tspan(text, 'star_data',    f'{star_data:,}',   target_width=14)
    text = _update_tspan(text, 'repo_data',    f'{repo_data:,}',   target_width=6)
    text = _update_tspan(text, 'contrib_data', f'{contrib_data:,}')
    text = _update_tspan(text, 'follower_data', f'{follower_data:,}', target_width=10)

    with open(filename, 'w', encoding='utf-8') as f:
        f.write(text)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def perf(funct, *args):
    start = time.perf_counter()
    return funct(*args), time.perf_counter() - start


def fmt(name, dt):
    unit = 's' if dt >= 1 else 'ms'
    val = dt if dt >= 1 else dt * 1000
    print(f'   {name:<20} {val:>9.2f} {unit}')


if __name__ == '__main__':
    print('Calculation times:')

    user_data, t = perf(user_stats)
    fmt('user', t)
    _, _, follower_data = user_data

    birthday_str = os.environ.get('BIRTHDAY', '2002-03-08')
    y, m, d = [int(x) for x in birthday_str.split('-')]
    age_data, t = perf(daily_readme, datetime.datetime(y, m, d))
    fmt('age', t)

    repo_data, t = perf(repos_owned_count)
    fmt('repos (owned)', t)

    contrib_data, t = perf(contributed_pr_count)
    fmt('contrib (PRs)', t)

    star_data, t = perf(total_stars)
    fmt('stars', t)

    commit_data, t = perf(total_commits)
    fmt('commits', t)

    svg_overwrite('dark_mode.svg',  age_data, commit_data, star_data, repo_data, contrib_data, follower_data)
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data)

    print('\nTotal GitHub API requests:', sum(QUERY_COUNT.values()))
    for k, v in QUERY_COUNT.items():
        print(f'   {k:<20} {v:>4}')

    print('\nResults:')
    print(f'  age:        {age_data}')
    print(f'  repos:      {repo_data}')
    print(f'  contribs:   {contrib_data}')
    print(f'  stars:      {star_data:,}')
    print(f'  commits:    {commit_data:,}')
    print(f'  followers:  {follower_data:,}')
