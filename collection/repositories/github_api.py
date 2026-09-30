"""GitHub requests with rate-limit waits and a local response cache."""

import gzip
import json
import sqlite3
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        # Never forward the token to a different host.
        if urlsplit(url).scheme != 'https' or urlsplit(url).netloc != 'api.github.com':
            raise ValueError('Unexpected GitHub API redirect')
        return super().redirect_request(request, response, code, message, headers, url)


def wait(seconds):
    if seconds >= 30:
        print(f'Waiting {round(seconds)} seconds for GitHub.', flush=True)
    time.sleep(max(0, seconds))


class GitHub:
    def __init__(self, token, cache):
        self.token = token
        self.opener = build_opener(GitHubRedirects())
        self.db = sqlite3.connect(cache)
        self.db.execute('CREATE TABLE IF NOT EXISTS responses '
                        '(url TEXT PRIMARY KEY, status INTEGER, checked_at TEXT, body BLOB)')
        self.last_request = 0
        self.last_search = 0

    def close(self):
        self.db.close()

    def get(self, path, params=None):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Expected a GitHub API path')
        url = 'https://api.github.com' + path
        if params:
            url += '?' + urlencode(params)
        cached = self.db.execute('SELECT status, checked_at, body FROM responses WHERE url=?',
                                 (url,)).fetchone()
        if cached:
            status, checked_at, body = cached
            return json.loads(gzip.decompress(body)), checked_at, status

        search = path == '/search/code'
        headers = {'Authorization': f'Bearer {self.token}',
                   'Accept': 'application/vnd.github.text-match+json' if search else 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'MIST-replication-collector'}
        for attempt in range(6):
            delay = self.last_request + 1.1 - time.monotonic()
            if search:
                delay = max(delay, self.last_search + 6.1 - time.monotonic())
            if delay > 0:
                wait(delay)
            self.last_request = time.monotonic()
            if search:
                self.last_search = self.last_request
            try:
                with self.opener.open(Request(url, headers=headers), timeout=90) as response:
                    data = json.load(response)
                    status = response.status
            except HTTPError as error:
                if error.code == 404 and not search:
                    data, status = None, 404
                elif error.code in {403, 429, 500, 502, 503, 504} and attempt < 5:
                    retry_after = error.headers.get('Retry-After')
                    remaining = error.headers.get('X-RateLimit-Remaining')
                    reset = error.headers.get('X-RateLimit-Reset', '0')
                    if retry_after:
                        delay = float(retry_after)
                    elif remaining == '0':
                        delay = max(1, float(reset) - time.time() + 2)
                    else:
                        delay = 60 * (attempt + 1)
                    wait(delay)
                    continue
                else:
                    # Avoid logging request headers or response bodies containing credentials.
                    raise RuntimeError(f'GitHub HTTP {error.code}: {path}. Resume after resolving it.') from None
            except (URLError, TimeoutError, OSError):
                if attempt == 5:
                    raise RuntimeError(f'GitHub request failed: {path}. Resume to retry.') from None
                wait(2 ** (attempt + 1))
                continue
            checked_at = datetime.now(timezone.utc).isoformat()
            body = gzip.compress(json.dumps(data).encode('utf-8'), mtime=0)
            self.db.execute('INSERT INTO responses VALUES (?, ?, ?, ?)',
                            (url, status, checked_at, body))
            self.db.commit()
            return data, checked_at, status
