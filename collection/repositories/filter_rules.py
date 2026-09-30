"""Repository and release filters used in the collection."""

import re
from datetime import datetime
from functools import lru_cache
from statistics import median


def is_true(value):
    return str(value).strip().lower() in {'true', '1', 'yes'}


def as_int(value):
    try:
        return int(float(str(value).strip() or '0'))
    except ValueError:
        return 0


@lru_cache(maxsize=None)
def word_pattern(word):
    return re.compile(r'(?<![a-z0-9])' + re.escape(word.lower()) + r'(?![a-z0-9])')


def keyword_hits(text, words):
    text = text.lower()
    return [word for word in words if word_pattern(word).search(text)]


def preliminary_filter(row, settings):
    """Return the reasons a repository fails the first filter."""
    cutoff = settings['recent_cutoff']
    minimum = settings['min_stars_or_forks']
    checks = {
        'metadata_available': row['metadata_collection_status'] == 'collected',
        'is_nonfork': not is_true(row['fork']),
        'has_nonzero_size': as_int(row['size']) > 0,
        'has_visibility_signal': max(as_int(row['stargazers_count']), as_int(row['forks_count'])) >= minimum,
        'is_python_language': row['language'] == 'Python',
        'is_recently_pushed': bool(row['pushed_at']) and row['pushed_at'][:10] >= cutoff,
    }
    reasons = [name for name, passed in checks.items() if not passed]
    keywords = settings['keywords']
    if keyword_hits(row['topics'].replace(',', ' '), keywords['EXCLUDING_TOPICS']):
        reasons.append('excluded_topic')
    if keyword_hits(row['description'], keywords['EXCLUDING_SUBSTRINGS_FOR_DESCRIPTION']):
        reasons.append('excluded_description')
    if keyword_hits(row['full_name'].split('/', 1)[-1], keywords['EXCLUDING_KEYWORDS_FOR_NAME']):
        reasons.append('excluded_name')
    return reasons


# These expressions keep the original tag rules, including their lenient matches.
SEMVER = re.compile(
    r'^[Vv]?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)'
    r'(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'
    r'(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$')
SEMVER_TOKEN = re.compile(
    r'([Vv]?(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)'
    r'(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)')
LENIENT_FULL = [
    re.compile(r'^[Vv]?\d+\.\d+\.\d+(?:rc|RC|b|a)\d+$'),
    re.compile(r'^\d+_\d+$'),
    re.compile(r'^\d+_\d+_\d+$'),
    re.compile(r'^[Vv]?\d+\.\d+$'),
    re.compile(r'^[Vv]?\d+\.\d+(?:rc|RC|b|a)\d*$', re.IGNORECASE),
    re.compile(r'^(?:[Vv]\.)?\d+\.\d+(?:[-_.](?:[A-Za-z][A-Za-z0-9.-]*))+$'),
]
TWO_PART_TOKEN = re.compile(r'[Vv]?\d+\.\d+(?:[-_.][A-Za-z0-9.-]+)?')


def tag_category(tag):
    tag = tag.strip()
    if SEMVER.fullmatch(tag):
        return 'strict'
    if any(pattern.fullmatch(tag) for pattern in LENIENT_FULL):
        return 'lenient'
    if SEMVER_TOKEN.search(tag) or TWO_PART_TOKEN.search(tag):
        return 'lenient'
    return 'other'


def parse_date(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def release_filter(status, rows, settings):
    """Return failed rules and the stable releases with accepted tags."""
    reasons = []
    if status != 'collected':
        reasons.append('release_metadata_not_collected')
    stable = []
    for row in rows:
        date = parse_date(row['published_at'])
        if not is_true(row['draft']) and not is_true(row['prerelease']) and date:
            stable.append((date, row))
    stable.sort(key=lambda item: item[0])
    count = len(stable)
    if count < 2:
        reasons.append('fewer_than_two_stable_releases')
    intervals = [(new[0] - old[0]).days for old, new in zip(stable, stable[1:])]
    if not intervals or not settings['min_median_days'] <= median(intervals) <= settings['max_median_days']:
        reasons.append('median_release_interval_outside_bounds')

    days = (stable[-1][0] - stable[0][0]).days if stable else 0
    per_year = count / (days / 365) if days else float('inf')
    if not stable or per_year / 365 > 1:
        reasons.append('more_than_one_release_per_day')
    if not stable or per_year < 1:
        reasons.append('fewer_than_one_release_per_year')
    if not stable or stable[-1][0].date().isoformat() < settings['recent_cutoff']:
        reasons.append('last_release_before_recent_cutoff')

    categories = [tag_category(row['tag_name']) for _, row in stable]
    ratio = categories.count('strict') / count + categories.count('lenient') / count if count else 0
    if ratio < settings['min_semver_ratio']:
        reasons.append('strict_plus_lenient_semver_ratio_below_threshold')
    retained = [row for (_, row), category in zip(stable, categories) if category != 'other']
    return reasons, retained if not reasons else []
