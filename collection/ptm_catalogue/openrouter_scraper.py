#!/usr/bin/env python3
"""Notebook helpers for OpenRouter Wayback model usage research."""

from __future__ import annotations

import csv
import html as ihtml
import json
import math
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from playwright.sync_api import Frame, Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import TimeoutError as AsyncPlaywrightTimeoutError


MODELS_URL = "https://openrouter.ai/models"
CDX_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_PAGE_URL = "https://web.archive.org/web/{timestamp}/{url}"
WAYBACK_ID_URL = "https://web.archive.org/web/{timestamp}id_/{url}"
LIVE_BASE_URL = "https://openrouter.ai"
DEFAULT_END_UTC = datetime(2026, 5, 5, 6, 0, 0, tzinfo=timezone.utc)


MODEL_ROWS_JS = r"""
() => {
  const clean = (value) => (value || "").replace(/\s+/g, " ").trim();
  const text = (node) => clean(node?.innerText || node?.textContent || "");
  const modelPath = (href) => {
    try {
      const url = new URL(href, location.href);
      const path = url.pathname
        .replace(/^\/web\/\d+(?:[a-z_]+)?\/https?:\/\/openrouter\.ai/i, "")
        .replace(/\/$/, "");
      const first = path.split("/").filter(Boolean)[0] || "";
      const excluded = new Set([
        "apps", "chat", "compare", "credits", "docs", "enterprise", "labs",
        "models", "pricing", "providers", "rankings", "settings", "privacy",
        "terms", "support", "announcements", "careers",
      ]);
      if (excluded.has(first.replace(/^~/, ""))) return "";
      if (/^\/(?:~?[a-z0-9_.-]+)\/[a-z0-9_.:~-]+$/i.test(path)) return path;
      return "";
    } catch {
      return "";
    }
  };
  const normalizedPath = (href) => {
    try {
      const url = new URL(href, location.href);
      return url.pathname
        .replace(/^\/web\/\d+(?:[a-z_]+)?\/https?:\/\/openrouter\.ai/i, "")
        .replace(/\/$/, "");
    } catch {
      return "";
    }
  };
  const bestLinkLabel = (a) => {
    const spanTexts = [...a.querySelectorAll("span")]
      .map((span) => text(span))
      .filter(Boolean);
    const preferred = spanTexts.find((value) => value.includes(":"));
    if (preferred) return preferred;
    if (spanTexts.length) return spanTexts.sort((a, b) => b.length - a.length)[0];
    return text(a);
  };
  const parseUsage = (rowText, title) => {
    const escapedTitle = title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const afterTitle = rowText.replace(new RegExp("^.*?" + escapedTitle), "");
    const candidates = [...afterTitle.matchAll(/(?:^|[^$\/\w])(\d+(?:\.\d+)?\s*[KMBT]?)\s+tokens\b/gi)];
    for (const match of candidates) {
      const start = Math.max(0, match.index - 18);
      const end = Math.min(afterTitle.length, match.index + match[0].length + 18);
      const context = afterTitle.slice(start, end).toLowerCase();
      if (context.includes("/m input") || context.includes("/m output")) continue;
      if (context.includes("input tokens") || context.includes("output tokens")) continue;
      if (context.includes("prompt tokens") || context.includes("completion tokens")) continue;
      return clean(match[1] + " tokens");
    }
    return "";
  };
  const rows = [];
  const seen = new Set();
  const datePattern = "(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\s+\\d{1,2},\\s+\\d{4}";

  // Older OpenRouter snapshots render rows as virtualized list items.
  for (const item of document.querySelectorAll('[data-testid="model-list-item"]')) {
    const rowText = text(item);
    const link = [...item.querySelectorAll("a[href]")]
      .map((a) => ({ node: a, path: modelPath(a.href), label: bestLinkLabel(a) }))
      .find((candidate) => candidate.path && candidate.label && !candidate.label.includes("OpenRouter"));
    if (!link || seen.has(link.path)) continue;
    const usageNode = item.querySelector('[title="Tokens this week"]');
    const usage = clean(usageNode ? text(usageNode) : parseUsage(rowText, link.label));
    const authorLink = [...item.querySelectorAll("a[href]")]
      .map((a) => ({ path: normalizedPath(a.href), label: text(a).toLowerCase() }))
      .find((candidate) => /^\/[a-z0-9_.-]+$/i.test(candidate.path) && candidate.label);
    const author = authorLink ? authorLink.label : "";
    const releasedMatch = rowText.match(new RegExp("\\b" + datePattern + "\\b"));
    if (!author) continue;
    seen.add(link.path);
    rows.push({
      model_name: link.label,
      model_path: link.path,
      model_url: new URL(link.path, "https://openrouter.ai").href,
      author,
      released: releasedMatch ? releasedMatch[0] : "",
      weekly_tokens_text: usage,
      row_text: rowText,
    });
  }

  // Some Wayback captures render rows as absolute-positioned <li> elements.
  for (const item of document.querySelectorAll("li.group.absolute")) {
    const rowText = text(item);
    const anchors = [...item.querySelectorAll("a[href]")].map((a) => ({
      node: a,
      path: modelPath(a.href),
      normalized: normalizedPath(a.href),
      label: bestLinkLabel(a),
      text: text(a).toLowerCase(),
    }));
    const link = anchors.find((candidate) => candidate.path && candidate.label);
    if (!link || seen.has(link.path)) continue;
    const usageNode = item.querySelector('[title="Tokens this week"]');
    const usage = clean(usageNode ? text(usageNode) : parseUsage(rowText, link.label));
    const authorLink = anchors.find((candidate) => {
      const p = (candidate.normalized || "").trim();
      return /^\/[a-z0-9_.-]+$/i.test(p);
    });
    const author = authorLink ? authorLink.normalized.replace(/^\//, "").toLowerCase() : "";
    const releasedMatch = rowText.match(new RegExp("\\b" + datePattern + "\\b"));
    if (!author) continue;
    seen.add(link.path);
    rows.push({
      model_name: link.label,
      model_path: link.path,
      model_url: new URL(link.path, "https://openrouter.ai").href,
      author,
      released: releasedMatch ? releasedMatch[0] : "",
      weekly_tokens_text: usage,
      row_text: rowText,
    });
  }

  for (const a of document.querySelectorAll("a[href]")) {
    const path = modelPath(a.href);
    if (!path || seen.has(path)) continue;
    const title = text(a);
    if (!title || title.length > 140) continue;
    let node = a;
    let card = null;
    for (let i = 0; i < 10 && node.parentElement; i += 1) {
      const parent = node.parentElement;
      const parentText = text(parent);
      const authorDate = new RegExp("\\bby\\s+[a-z0-9_.-]+\\s+" + datePattern, "i");
      if (
        parentText.includes(title) &&
        authorDate.test(parentText) &&
        /\bcontext\b/i.test(parentText) &&
        parentText.length < 3500
      ) {
        card = parent;
        break;
      }
      node = parent;
    }
    if (!card) continue;
    const rowText = text(card);
    const authorMatch = rowText.match(new RegExp("\\bby\\s+([a-z0-9_.-]+)\\s+(" + datePattern + ")", "i"));
    if (!authorMatch) continue;
    const releasedMatch = rowText.match(new RegExp("\\b" + datePattern + "\\b"));
    const usage = parseUsage(rowText, title);
    seen.add(path);
    rows.push({
      model_name: title,
      model_path: path,
      model_url: new URL(path, "https://openrouter.ai").href,
      author: authorMatch[1],
      released: releasedMatch ? releasedMatch[0] : "",
      weekly_tokens_text: usage,
      row_text: rowText,
    });
  }
  return rows;
}
"""


WEIGHTS_LINK_JS = r"""
() => {
  const links = [...document.querySelectorAll("a[href]")].map((a) => ({
    href: a.href,
    text: (a.innerText || a.textContent || "").replace(/\s+/g, " ").trim(),
  }));
  const weights = links.filter((link) =>
    /model weights/i.test(link.text) || /huggingface\.co/i.test(link.href)
  );
  return {
    has_model_weights_link: weights.length > 0,
    weights_links: weights,
    title: document.title,
    body_text: (document.body?.innerText || "").replace(/\s+/g, " ").trim().slice(0, 3000),
  };
}
"""


@dataclass(frozen=True)
class SnapshotMatch:
    target_utc: datetime
    timestamp: str | None
    actual_utc: datetime | None
    distance_hours: float | None
    status: str
    url: str | None


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_output_dir(base: Path = Path("."), now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    return base / f"valid_outputs_{now.strftime('%Y%m%d_%H%M%S')}"


def wayback_timestamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def parse_wayback_timestamp(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def weekly_targets(end_utc: datetime = DEFAULT_END_UTC, weeks: int = 26) -> list[datetime]:
    return [end_utc - timedelta(days=7 * offset) for offset in range(weeks)]


def http_json(url: str, timeout: int = 60, retries: int = 3) -> Any:
    request = Request(url, headers={"User-Agent": "openrouter-wayback-research/1.0"})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, ConnectionError, TimeoutError, socket.timeout) as exc:
            last_error = exc
            if attempt == retries - 1:
                break
            time.sleep(1.5 * (attempt + 1))
    raise last_error or RuntimeError("request failed")


def fetch_cdx_captures(
    url: str = MODELS_URL,
    start: datetime | None = None,
    end: datetime | None = None,
    timeout: int = 60,
) -> list[dict[str, str]]:
    start = start or (DEFAULT_END_UTC - timedelta(days=190))
    end = end or DEFAULT_END_UTC
    query = (
        f"{CDX_URL}?url={quote(url, safe='')}"
        f"&from={start.strftime('%Y%m%d%H%M%S')}"
        f"&to={end.strftime('%Y%m%d%H%M%S')}"
        "&output=json&fl=timestamp,original,statuscode,mimetype,digest"
        "&filter=statuscode:200"
        "&filter=mimetype:text/html"
    )
    data = http_json(query, timeout=timeout)
    if not data or len(data) < 2:
        return []
    headers = data[0]
    return [dict(zip(headers, row)) for row in data[1:]]


def nearest_snapshots(
    targets: list[datetime],
    captures: list[dict[str, str]],
    max_distance: timedelta = timedelta(days=3),
) -> list[SnapshotMatch]:
    parsed = [
        (capture["timestamp"], parse_wayback_timestamp(capture["timestamp"]))
        for capture in captures
        if capture.get("timestamp")
    ]
    matches: list[SnapshotMatch] = []
    for target in targets:
        if not parsed:
            matches.append(SnapshotMatch(target, None, None, None, "missing", None))
            continue
        timestamp, actual = min(parsed, key=lambda item: abs(item[1] - target))
        distance = abs(actual - target)
        if distance <= max_distance:
            matches.append(
                SnapshotMatch(
                    target_utc=target,
                    timestamp=timestamp,
                    actual_utc=actual,
                    distance_hours=distance.total_seconds() / 3600,
                    status="matched",
                    url=WAYBACK_PAGE_URL.format(timestamp=timestamp, url=MODELS_URL),
                )
            )
        else:
            matches.append(
                SnapshotMatch(
                    target_utc=target,
                    timestamp=timestamp,
                    actual_utc=actual,
                    distance_hours=distance.total_seconds() / 3600,
                    status="outside_window",
                    url=WAYBACK_PAGE_URL.format(timestamp=timestamp, url=MODELS_URL),
                )
            )
    return matches


def candidate_snapshots_for_target(
    target: datetime,
    captures: list[dict[str, str]],
    max_distance: timedelta = timedelta(days=3),
) -> list[SnapshotMatch]:
    """Return nearby HTML captures sorted by distance from the weekly target."""
    candidates: list[SnapshotMatch] = []
    seen: set[str] = set()
    for capture in captures:
        timestamp = capture.get("timestamp")
        if not timestamp or timestamp in seen:
            continue
        actual = parse_wayback_timestamp(timestamp)
        distance = abs(actual - target)
        if distance > max_distance:
            continue
        seen.add(timestamp)
        candidates.append(
            SnapshotMatch(
                target_utc=target,
                timestamp=timestamp,
                actual_utc=actual,
                distance_hours=distance.total_seconds() / 3600,
                status="matched",
                url=WAYBACK_PAGE_URL.format(timestamp=timestamp, url=MODELS_URL),
            )
        )
    candidates.sort(key=lambda match: (match.distance_hours or math.inf, match.timestamp or ""))
    return candidates


def token_text_to_number(value: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*([KMBT]?)\s*tokens", value or "", re.I)
    if not match:
        return None
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "T": 1_000_000_000_000}
    return float(match.group(1)) * multiplier[match.group(2).upper()]


def safe_slug(value: str) -> str:
    value = value.strip("/").replace("/", "__")
    return re.sub(r"[^a-zA-Z0-9_.~-]+", "_", value)[:180] or "unknown"


def model_path_from_wayback_href(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return ""
    m = re.search(r"/https?://openrouter\.ai(/[^\"'?#\s]+)", href, re.I)
    if m:
        path = m.group(1).rstrip("/")
    else:
        parsed = urlparse(href)
        path = parsed.path.rstrip("/")
    if re.match(r"^/[a-z0-9_.-]+/[a-z0-9_.:~-]+$", path, re.I):
        return path
    return ""


def strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = ihtml.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def fallback_extract_models_from_html(html: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in re.findall(r'<li class="group absolute[^>]*>.*?</li>', html or "", flags=re.S | re.I):
        model_match = re.search(
            r'href="([^"]*/https?://openrouter\.ai/[^"/]+/[^"/]+)"[^>]*>\s*(?:<span[^>]*>)?([^<]+)',
            block,
            flags=re.I,
        )
        if not model_match:
            continue
        model_href = model_match.group(1)
        model_name = strip_tags(model_match.group(2))
        model_path = model_path_from_wayback_href(model_href)
        if not model_path or model_path in seen:
            continue
        author_match = re.search(r"by\s*<a[^>]*>([^<]+)</a>", block, flags=re.I)
        author = strip_tags(author_match.group(1)).lower() if author_match else ""
        if not author:
            continue
        usage_match = re.search(r'title="Tokens this week".{0,500}?(\d+(?:\.\d+)?\s*[KMBT]?\s*tokens)', block, flags=re.I | re.S)
        usage = strip_tags(usage_match.group(1)) if usage_match else ""
        row_text = strip_tags(block)
        seen.add(model_path)
        rows.append(
            {
                "model_name": model_name,
                "model_path": model_path,
                "model_url": urljoin(LIVE_BASE_URL, model_path),
                "author": author,
                "released": "",
                "weekly_tokens_text": usage,
                "row_text": row_text,
                "weekly_tokens": token_text_to_number(usage),
            }
        )
    # Additional pass keyed by "Tokens this week" sections for captures where li slicing is incomplete.
    if html:
        for match in re.finditer(r'title="Tokens this week".{0,500}?(\d+(?:\.\d+)?\s*[KMBT]?\s*tokens)', html, flags=re.I | re.S):
            usage = strip_tags(match.group(1))
            start = max(0, match.start() - 12000)
            end = min(len(html), match.end() + 4000)
            chunk = html[start:end]
            hrefs = re.findall(r'href="([^"]*/https?://openrouter\.ai/[^"/]+/[^"/]+)"', chunk, flags=re.I)
            if not hrefs:
                continue
            model_href = hrefs[-1]
            model_path = model_path_from_wayback_href(model_href)
            if not model_path or model_path in seen:
                continue
            anchor_match = re.search(
                rf'href="{re.escape(model_href)}"[^>]*>(.*?)</a>',
                chunk,
                flags=re.I | re.S,
            )
            model_name = strip_tags(anchor_match.group(1)) if anchor_match else model_path.split("/")[-1]
            author_match = re.search(r"by\s*<a[^>]*>([^<]+)</a>", chunk, flags=re.I)
            author = strip_tags(author_match.group(1)).lower() if author_match else ""
            if not author:
                continue
            row_text = strip_tags(chunk)
            seen.add(model_path)
            rows.append(
                {
                    "model_name": model_name,
                    "model_path": model_path,
                    "model_url": urljoin(LIVE_BASE_URL, model_path),
                    "author": author,
                    "released": "",
                    "weekly_tokens_text": usage,
                    "row_text": row_text,
                    "weekly_tokens": token_text_to_number(usage),
                }
            )
    return rows


def save_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, rows: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def collect_models_while_scrolling(
    page: Page,
    max_scrolls: int = 240,
    stable_rounds: int = 24,
) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    stable = 0
    for _ in range(max_scrolls):
        rows = extract_models_from_page(page)
        before = len(seen)
        for row in rows:
            key = row.get("model_url") or row.get("model_path")
            if key:
                seen[key] = row
        if len(seen) == before:
            stable += 1
        else:
            stable = 0
        if stable >= stable_rounds:
            break
        # Some Wayback replays keep the list inside an internal scroll container.
        # Also trigger keyboard paging to advance virtualized lists that ignore wheel events.
        try:
            active_context(page).evaluate(
                """
                () => {
                  const nodes = [...document.querySelectorAll('*')];
                  let best = null;
                  let bestDelta = 0;
                  for (const node of nodes) {
                    const style = getComputedStyle(node);
                    if (!/(auto|scroll)/.test(style.overflowY || '')) continue;
                    const delta = (node.scrollHeight || 0) - (node.clientHeight || 0);
                    if (delta > bestDelta) {
                      bestDelta = delta;
                      best = node;
                    }
                  }
                  if (best && bestDelta > 0) {
                    best.scrollBy(0, 1600);
                    return true;
                  }
                  window.scrollBy(0, 1600);
                  return false;
                }
                """
            )
        except Exception:  # noqa: BLE001
            pass
        page.mouse.wheel(0, 1600)
        try:
            page.keyboard.press("PageDown")
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(600)
    return list(seen.values())


def scroll_until_stable(page: Page, max_scrolls: int = 80, stable_rounds: int = 5) -> None:
    collect_models_while_scrolling(page, max_scrolls=max_scrolls, stable_rounds=stable_rounds)


def active_context(page: Page) -> Any:
    frame = page.frame(name="playback")
    return frame or page


def rendered_content(page: Page) -> str:
    return active_context(page).content()


def render_page(page: Page, url: str, timeout_ms: int = 60_000) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15_000))
    except PlaywrightTimeoutError:
        pass
    frame = page.frame(name="playback")
    if frame:
        try:
            frame.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 15_000))
            frame.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15_000))
        except PlaywrightTimeoutError:
            pass
    page.wait_for_timeout(2_000)
    return rendered_content(page)


def extract_models_from_page(page: Page) -> list[dict[str, Any]]:
    contexts: list[Any] = [page]
    frame = page.frame(name="playback")
    if frame:
        contexts.append(frame)
    best_rows: list[dict[str, Any]] = []
    for context in contexts:
        try:
            rows = context.evaluate(MODEL_ROWS_JS)
        except Exception:  # noqa: BLE001
            continue
        if len(rows) > len(best_rows):
            best_rows = rows
    for row in best_rows:
        row["weekly_tokens"] = token_text_to_number(row.get("weekly_tokens_text", ""))
    return best_rows


def scrape_models_snapshot(
    page: Page,
    match: SnapshotMatch,
    output_dir: Path,
    timeout_ms: int = 60_000,
) -> list[dict[str, Any]]:
    if not match.url or not match.timestamp:
        return []
    html = render_page(page, match.url, timeout_ms=timeout_ms)
    rows = collect_models_while_scrolling(page)
    html = rendered_content(page)
    if not rows:
        rows = fallback_extract_models_from_html(html)
    target_label = wayback_timestamp(match.target_utc)
    evidence_path = output_dir / "evidence" / "models_pages" / f"{target_label}__{match.timestamp}.html"
    save_text(evidence_path, html)
    for row in rows:
        row.update(
            {
                "target_utc": match.target_utc.isoformat(),
                "wayback_timestamp": match.timestamp,
                "snapshot_utc": match.actual_utc.isoformat() if match.actual_utc else "",
                "snapshot_distance_hours": match.distance_hours,
                "models_page_evidence": str(evidence_path),
            }
        )
    return rows


def archived_detail_url(model_url: str, timestamp: str) -> str:
    return WAYBACK_PAGE_URL.format(timestamp=timestamp, url=model_url)


def huggingface_repo_has_public_weights(author: str, model_slug: str, timeout: int = 20) -> tuple[bool, str]:
    author = author.strip().lstrip("~")
    model_slug = model_slug.strip().split("/")[-1]
    if not author or not model_slug:
        return False, ""
    url = f"https://huggingface.co/api/models/{quote(author + '/' + model_slug, safe='/')}"
    request = Request(url, headers={"User-Agent": "openrouter-wayback-research/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
            siblings = data.get("siblings") or []
            has_weights = any(
                re.search(r"\.(safetensors|bin|gguf|pt|pth)$", str(item.get("rfilename", "")), re.I)
                for item in siblings
            )
            return bool(has_weights), url
    except HTTPError as exc:
        return False, url
    except URLError:
        return False, url


def classify_model_detail(
    page: Page,
    row: dict[str, Any],
    output_dir: Path,
    timeout_ms: int = 60_000,
    hf_fallback: bool = True,
) -> dict[str, Any]:
    target_label = row["target_utc"].replace(":", "").replace("+00:00", "Z")
    model_url = row["model_url"]
    evidence_path = (
        output_dir
        / "evidence"
        / "model_details"
        / target_label
        / f"{safe_slug(row.get('model_path', model_url))}.html"
    )
    result = {
        "model_url": model_url,
        "detail_url": model_url,
        "classification": "unknown",
        "classification_source": "",
        "has_model_weights_link": False,
        "weights_links": "",
        "hf_exact_url": "",
        "hf_exact_found": False,
        "detail_evidence": str(evidence_path),
        "classification_error": "",
    }
    try:
        html = render_page(page, model_url, timeout_ms=timeout_ms)
        if "Invalid credentials" in html or len(html.strip()) < 1000:
            raise RuntimeError("live detail page was not usable")
        save_text(evidence_path, html)
        detail = active_context(page).evaluate(WEIGHTS_LINK_JS)
        weights_links = detail.get("weights_links") or []
        has_weights = bool(detail.get("has_model_weights_link"))
        result.update(
            {
                "classification": "open_weight" if has_weights else "closed",
                "classification_source": "live_detail",
                "has_model_weights_link": has_weights,
                "weights_links": json.dumps(weights_links, ensure_ascii=False),
            }
        )
        return result
    except Exception as exc:  # noqa: BLE001 - notebook research should keep going.
        result["classification_error"] = str(exc)
    if hf_fallback:
        model_slug = str(row.get("model_path") or model_url).strip("/").split("/")[-1]
        found, hf_url = huggingface_repo_has_public_weights(str(row.get("author") or ""), model_slug)
        result["hf_exact_url"] = hf_url
        result["hf_exact_found"] = found
        if found:
            result["classification"] = "open_weight_hf_exact"
            result["classification_source"] = "hf_exact_public_weights_fallback"
    return result


def aggregate_author_rankings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    weekly_totals: dict[tuple[str, str], float] = {}
    observed_weeks: set[str] = set()
    for row in rows:
        if row.get("classification") != "closed":
            continue
        tokens = row.get("weekly_tokens")
        if tokens is None or (isinstance(tokens, float) and math.isnan(tokens)):
            continue
        week = row["target_utc"]
        author = row["author"]
        observed_weeks.add(week)
        weekly_totals[(author, week)] = weekly_totals.get((author, week), 0.0) + float(tokens)

    by_author: dict[str, list[float]] = {}
    for (author, _week), total in weekly_totals.items():
        by_author.setdefault(author, []).append(total)

    rankings = []
    for author, totals in by_author.items():
        rankings.append(
            {
                "author": author,
                "observed_weeks": len(totals),
                "mean_weekly_tokens": sum(totals) / len(totals),
                "total_tokens_observed": sum(totals),
            }
        )
    rankings.sort(key=lambda row: row["mean_weekly_tokens"], reverse=True)
    for index, row in enumerate(rankings, start=1):
        row["rank"] = index
    return rankings


async def async_active_context(page: Any) -> Any:
    frame = page.frame(name="playback")
    return frame or page


async def async_rendered_content(page: Any) -> str:
    context = await async_active_context(page)
    return await context.content()


async def async_render_page(page: Any, url: str, timeout_ms: int = 60_000) -> str:
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15_000))
    except AsyncPlaywrightTimeoutError:
        pass
    frame = page.frame(name="playback")
    if frame:
        try:
            await frame.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 15_000))
            await frame.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15_000))
        except AsyncPlaywrightTimeoutError:
            pass
    await page.wait_for_timeout(2_000)
    return await async_rendered_content(page)


async def async_extract_models_from_page(page: Any) -> list[dict[str, Any]]:
    contexts: list[Any] = [page]
    frame = page.frame(name="playback")
    if frame:
        contexts.append(frame)
    best_rows: list[dict[str, Any]] = []
    for context in contexts:
        try:
            rows = await context.evaluate(MODEL_ROWS_JS)
        except Exception:  # noqa: BLE001
            continue
        if len(rows) > len(best_rows):
            best_rows = rows
    for row in best_rows:
        row["weekly_tokens"] = token_text_to_number(row.get("weekly_tokens_text", ""))
    return best_rows


async def async_collect_models_while_scrolling(
    page: Any,
    max_scrolls: int = 240,
    stable_rounds: int = 24,
) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    stable = 0
    for _ in range(max_scrolls):
        rows = await async_extract_models_from_page(page)
        before = len(seen)
        for row in rows:
            key = row.get("model_url") or row.get("model_path")
            if key:
                seen[key] = row
        if len(seen) == before:
            stable += 1
        else:
            stable = 0
        if stable >= stable_rounds:
            break
        # Some Wayback replays keep the list inside an internal scroll container.
        # Also trigger keyboard paging to advance virtualized lists that ignore wheel events.
        try:
            context = await async_active_context(page)
            await context.evaluate(
                """
                () => {
                  const nodes = [...document.querySelectorAll('*')];
                  let best = null;
                  let bestDelta = 0;
                  for (const node of nodes) {
                    const style = getComputedStyle(node);
                    if (!/(auto|scroll)/.test(style.overflowY || '')) continue;
                    const delta = (node.scrollHeight || 0) - (node.clientHeight || 0);
                    if (delta > bestDelta) {
                      bestDelta = delta;
                      best = node;
                    }
                  }
                  if (best && bestDelta > 0) {
                    best.scrollBy(0, 1600);
                    return true;
                  }
                  window.scrollBy(0, 1600);
                  return false;
                }
                """
            )
        except Exception:  # noqa: BLE001
            pass
        await page.mouse.wheel(0, 1600)
        try:
            await page.keyboard.press("PageDown")
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(600)
    return list(seen.values())


async def async_scrape_models_snapshot(
    page: Any,
    match: SnapshotMatch,
    output_dir: Path,
    timeout_ms: int = 60_000,
) -> list[dict[str, Any]]:
    if not match.url or not match.timestamp:
        return []
    await async_render_page(page, match.url, timeout_ms=timeout_ms)
    rows = await async_collect_models_while_scrolling(page)
    html = await async_rendered_content(page)
    if not rows:
        rows = fallback_extract_models_from_html(html)
    target_label = wayback_timestamp(match.target_utc)
    evidence_path = output_dir / "evidence" / "models_pages" / f"{target_label}__{match.timestamp}.html"
    save_text(evidence_path, html)
    for row in rows:
        row.update(
            {
                "target_utc": match.target_utc.isoformat(),
                "wayback_timestamp": match.timestamp,
                "snapshot_utc": match.actual_utc.isoformat() if match.actual_utc else "",
                "snapshot_distance_hours": match.distance_hours,
                "models_page_evidence": str(evidence_path),
            }
        )
    return rows


async def async_scrape_models_snapshot_with_fallback(
    page: Any,
    match: SnapshotMatch,
    captures: list[dict[str, str]],
    output_dir: Path,
    max_distance: timedelta = timedelta(days=3),
    min_rows: int = 1,
    min_usage_rows: int = 1,
    same_capture_retries: int = 2,
    timeout_ms: int = 60_000,
) -> tuple[list[dict[str, Any]], SnapshotMatch, list[dict[str, Any]]]:
    """Try the nearest capture first, then nearby captures until data is extractable."""
    if match.status != "matched" or not match.timestamp:
        return [], match, []

    candidates = candidate_snapshots_for_target(match.target_utc, captures, max_distance=max_distance)
    ordered: list[SnapshotMatch] = []
    seen: set[str] = set()
    for candidate in [match, *candidates]:
        if not candidate.timestamp or candidate.timestamp in seen:
            continue
        seen.add(candidate.timestamp)
        ordered.append(candidate)

    attempts: list[dict[str, Any]] = []
    last_rows: list[dict[str, Any]] = []
    last_match = match
    for candidate in ordered:
        for retry in range(max(1, same_capture_retries)):
            try:
                rows = await async_scrape_models_snapshot(page, candidate, output_dir, timeout_ms=timeout_ms)
                usage_rows = sum(1 for row in rows if row.get("weekly_tokens") is not None)
                ok = len(rows) >= min_rows and usage_rows >= min_usage_rows
                attempts.append(
                    {
                        "target_utc": candidate.target_utc.isoformat(),
                        "attempt_timestamp": candidate.timestamp,
                        "attempt_retry": retry + 1,
                        "snapshot_utc": candidate.actual_utc.isoformat() if candidate.actual_utc else "",
                        "distance_hours": candidate.distance_hours,
                        "rows": len(rows),
                        "usage_rows": usage_rows,
                        "accepted": ok,
                        "error": "",
                    }
                )
                last_rows = rows
                last_match = candidate
                if ok:
                    return rows, candidate, attempts
            except Exception as exc:  # noqa: BLE001
                attempts.append(
                    {
                        "target_utc": candidate.target_utc.isoformat(),
                        "attempt_timestamp": candidate.timestamp,
                        "attempt_retry": retry + 1,
                        "snapshot_utc": candidate.actual_utc.isoformat() if candidate.actual_utc else "",
                        "distance_hours": candidate.distance_hours,
                        "rows": 0,
                        "usage_rows": 0,
                        "accepted": False,
                        "error": str(exc),
                    }
                )
                last_match = candidate

    return last_rows, last_match, attempts


async def async_classify_model_detail(
    page: Any,
    row: dict[str, Any],
    output_dir: Path,
    timeout_ms: int = 60_000,
    hf_fallback: bool = True,
) -> dict[str, Any]:
    target_label = row["target_utc"].replace(":", "").replace("+00:00", "Z")
    model_url = row["model_url"]
    evidence_path = (
        output_dir
        / "evidence"
        / "model_details"
        / target_label
        / f"{safe_slug(row.get('model_path', model_url))}.html"
    )
    result = {
        "model_url": model_url,
        "detail_url": model_url,
        "classification": "unknown",
        "classification_source": "",
        "has_model_weights_link": False,
        "weights_links": "",
        "hf_exact_url": "",
        "hf_exact_found": False,
        "detail_evidence": str(evidence_path),
        "classification_error": "",
    }
    try:
        html = await async_render_page(page, model_url, timeout_ms=timeout_ms)
        if "Invalid credentials" in html or len(html.strip()) < 1000:
            raise RuntimeError("live detail page was not usable")
        save_text(evidence_path, html)
        context = await async_active_context(page)
        detail = await context.evaluate(WEIGHTS_LINK_JS)
        weights_links = detail.get("weights_links") or []
        has_weights = bool(detail.get("has_model_weights_link"))
        result.update(
            {
                "classification": "open_weight" if has_weights else "closed",
                "classification_source": "live_detail",
                "has_model_weights_link": has_weights,
                "weights_links": json.dumps(weights_links, ensure_ascii=False),
            }
        )
        return result
    except Exception as exc:  # noqa: BLE001
        result["classification_error"] = str(exc)
    if hf_fallback:
        model_slug = str(row.get("model_path") or model_url).strip("/").split("/")[-1]
        found, hf_url = huggingface_repo_has_public_weights(str(row.get("author") or ""), model_slug)
        result["hf_exact_url"] = hf_url
        result["hf_exact_found"] = found
        if found:
            result["classification"] = "open_weight_hf_exact"
            result["classification_source"] = "hf_exact_public_weights_fallback"
    return result
