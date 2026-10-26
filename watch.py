#!/usr/bin/env python3
"""
QCinema website watcher.

Checks qcinema.ph pages for changes and sends an alert (Discord webhook,
GitHub issue -> email, and optionally plain SMTP email) when something changes.

qcinema.ph runs on WordPress, so instead of scraping the whole HTML page
(which changes on every load because of nonces, ads, etc.) we ask WordPress
directly for each page's "last modified" time and its main content. That
gives very few false alarms.

Only the Python standard library is used, so nothing needs to be installed.

Settings come from environment variables (GitHub secrets / variables):
  DISCORD_WEBHOOK_URL  Discord channel webhook URL           (optional)
  DISCORD_MENTION      e.g. "@everyone" or "<@123456789>"    (optional)
  GITHUB_TOKEN         set automatically inside GitHub Actions; used to open
                       an issue in your repo, which GitHub emails to you
  ISSUE_ALERTS         "false" to turn the GitHub-issue alerts off
  SMTP_USER, SMTP_PASS send email directly (e.g. Gmail + app password) (optional)
  SMTP_HOST, SMTP_PORT default smtp.gmail.com / 465
  EMAIL_TO             default: SMTP_USER
  TEST_ALERT           "true" to just send a test alert and exit

Run locally:  python watch.py          (normal check)
              python watch.py --test   (send a test alert)
"""

import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from email.message import EmailMessage
from html.parser import HTMLParser

# --------------------------------------------------------------------------
# What to watch. Add more pages by copying a block and changing the slug
# (the slug is the part of the URL after qcinema.ph/, e.g. "schedule").
# "signals" are words you care about: the alert calls it out when one of
# them appears on, or disappears from, the page.
# --------------------------------------------------------------------------
SITE = "https://qcinema.ph"

WATCH = [
    {
        "name": "Festival Schedule",
        "slug": "schedule",
        "url": "https://qcinema.ph/schedule/",
        "signals": ["2026", "2025", "TBA"],
    },
    {
        "name": "Festival Badge",
        "slug": "festival-badge",
        "url": "https://qcinema.ph/festival-badge/",
        "signals": ["2026", "SOLD OUT", "Coming soon", "Buy", "available"],
    },
]

# Also alert when a brand-new page is published anywhere on qcinema.ph
# (e.g. if they make a separate "festival-badge-2026" page instead of
# editing the old one).
WATCH_NEW_PAGES = True

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
FAIL_ALERT_AFTER = 6  # consecutive failed runs before warning you (6 x 10 min = 1 hour)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
MAX_DIFF_LINES = 15


# ------------------------------------------------------------------ helpers
def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def http_get(url, retries=3):
    """GET a URL and return the body as text. Retries on network hiccups."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                    "Cache-Control": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_err = e
            if 400 <= e.code < 500 and e.code != 429:
                break  # retrying won't help (404, 403, ...)
            time.sleep(3 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}: {last_err}")


def http_post_json(url, payload, headers=None, method="POST"):
    """Send JSON and return the JSON answer ({} if there is none)."""
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": "QCinemaWatcher/1.0"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        answer = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(answer) if answer.strip() else {}
    except ValueError:
        return {}


class _TextExtractor(HTMLParser):
    """Turns HTML into readable lines of text (one block/row per line)."""

    SKIP = {"script", "style", "noscript", "svg", "template", "head", "iframe"}
    BLOCK = {
        "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
        "tr", "table", "section", "article", "header", "footer", "figure",
        "figcaption", "blockquote", "hr", "dt", "dd", "main", "nav", "aside",
    }
    CELL = {"td", "th"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in self.CELL:
            self.parts.append(" | ")

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_lines(html):
    p = _TextExtractor()
    p.feed(html or "")
    p.close()
    lines = []
    for raw in "".join(p.parts).split("\n"):
        line = re.sub(r"\s+", " ", raw).strip(" |")
        line = re.sub(r"(\s*\|\s*)+", " | ", line).strip()
        if line:
            lines.append(line)
    return lines


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------- fetching
def fetch_page(page, prev_source=None):
    """Return dict(source, modified, lines, missing) for one watched page.

    Normally uses the WordPress API. If the API has never worked for this page,
    falls back to reading the public HTML page. If the API *used* to work, a
    failure is treated as a temporary error (so we don't flip between the two
    and miss a change), and an empty answer means the page was unpublished.
    """
    q = urllib.parse.urlencode(
        {
            "slug": page["slug"],
            "_fields": "id,slug,modified_gmt,title,content,link",
            "_cb": int(time.time()),  # cache-buster
        }
    )
    api_url = f"{SITE}/wp-json/wp/v2/pages?{q}"
    data = None
    try:
        data = json.loads(http_get(api_url))
    except (RuntimeError, ValueError) as e:
        if prev_source == "api":
            raise
        log(f"  WordPress API failed ({e}), using the HTML page")

    if isinstance(data, list) and data:
        item = data[0]
        return {
            "source": "api",
            "modified": item.get("modified_gmt", ""),
            "lines": html_to_lines(item.get("content", {}).get("rendered", "")),
            "missing": False,
        }
    if isinstance(data, list) and prev_source == "api":
        # The API answered, but the page isn't published any more.
        return {"source": "api", "modified": "", "lines": [], "missing": True}
    if data is not None and prev_source == "api":
        raise RuntimeError("unexpected answer from the WordPress API")
    if isinstance(data, list):
        log(f"  WordPress API has no page '{page['slug']}', using the HTML page")

    # Fallback: read the public page itself.
    html = http_get(page["url"] + ("&" if "?" in page["url"] else "?") + f"_cb={int(time.time())}")
    return {"source": "html", "modified": "", "lines": html_to_lines(html), "missing": False}


def fetch_recent_pages():
    q = urllib.parse.urlencode(
        {
            "orderby": "date",
            "order": "desc",
            "per_page": 15,
            "context": "embed",  # short answer without each page's full content
            "_fields": "id,slug,title,link,date,date_gmt",
            "_cb": int(time.time()),
        }
    )
    data = json.loads(http_get(f"{SITE}/wp-json/wp/v2/pages?{q}"))
    if not isinstance(data, list):
        raise RuntimeError("unexpected response for page list")
    return data


# ------------------------------------------------------------- comparing
def describe_change(page, old, new):
    """Build a human-readable summary of what changed. None = no change."""
    if new.get("missing") and not old.get("missing"):
        return (
            "**The page is no longer published** (it was unpublished, renamed or moved). "
            "Sites often do this right before posting an update. You'll get another alert "
            "when it's back."
        )

    text_changed = old["hash"] != new["hash"]
    modified_changed = bool(new["modified"]) and old.get("modified") != new["modified"]
    if not text_changed and not modified_changed:
        return None

    parts = []
    if old.get("missing"):
        parts.append("**The page is back online!**")

    # Signal words that appeared / disappeared
    old_text = "\n".join(old.get("lines", [])).lower()
    new_text = "\n".join(new["lines"]).lower()
    for word in page.get("signals", []) if not old.get("missing") else []:
        w = word.lower()
        was, now = w in old_text, w in new_text
        if now and not was:
            parts.append(f"**Now mentions \"{word}\"**")
        elif was and not now:
            parts.append(f"**No longer says \"{word}\"**")

    if new["modified"]:
        was = f" (was {fmt_time(old['modified'])})" if old.get("modified") not in ("", None, new["modified"]) else ""
        parts.append(f"Last edited on WordPress: {fmt_time(new['modified'])}{was}")

    if text_changed:
        added = only_in(new["lines"], old.get("lines", []))
        removed = only_in(old.get("lines", []), new["lines"])
        if not added and not removed:
            parts.append("Some lines were moved around (same text, different order).")
        diff_lines = [f"+ {l}" for l in added[:MAX_DIFF_LINES]]
        if len(added) > MAX_DIFF_LINES:
            diff_lines.append(f"+ ... and {len(added) - MAX_DIFF_LINES} more new lines")
        diff_lines += [f"- {l}" for l in removed[:MAX_DIFF_LINES]]
        if len(removed) > MAX_DIFF_LINES:
            diff_lines.append(f"- ... and {len(removed) - MAX_DIFF_LINES} more removed lines")
        if diff_lines:
            parts.append(f"{len(added)} line(s) added, {len(removed)} removed:")
            parts.append("```diff\n" + "\n".join(shorten(l, 180) for l in diff_lines) + "\n```")
    else:
        parts.append("The page was re-saved but the text didn't change (maybe an image, link or layout edit).")

    return "\n".join(parts)


def only_in(lines, other):
    """Lines in `lines` that aren't in `other` (counting repeats), in page order."""
    left = Counter(other)
    out = []
    for line in lines:
        if left[line] > 0:
            left[line] -= 1
        else:
            out.append(line)
    return out


def fmt_time(value, local=False):
    """WordPress time -> Philippine time string. local=True: already site time."""
    if not value:
        return "unknown"
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
        if not local:
            dt = datetime.fromtimestamp(dt.replace(tzinfo=timezone.utc).timestamp() + 8 * 3600, timezone.utc)
        return dt.strftime("%b %d, %Y %I:%M %p") + (" PHT" if not local else "")
    except ValueError:
        return value


def shorten(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------- alerting
def notify(title, body, url=None):
    """Send an alert to every channel that's configured. Never crashes the run."""
    sent = []

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if webhook:
        try:
            mention = os.environ.get("DISCORD_MENTION", "").strip()
            desc = body if len(body) <= 4000 else body[:3990] + "\n…```"
            payload = {
                "username": "QCinema Watcher",
                "content": f"{mention} {title}".strip(),
                "embeds": [
                    {
                        "title": shorten(title, 250),
                        "url": url,
                        "description": desc,
                        "color": 0xE4572E,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ],
                "allowed_mentions": {"parse": ["everyone", "users", "roles"]},
            }
            http_post_json(webhook, payload)
            sent.append("Discord")
        except Exception as e:  # noqa: BLE001
            log(f"  ! Discord alert failed: {e}")

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if token and repo and os.environ.get("ISSUE_ALERTS", "true").lower() != "false":
        # The @mention makes GitHub email you. The issue is closed right away
        # so the repo doesn't fill up; old alerts stay under Issues > Closed.
        owner = os.environ.get("GITHUB_REPOSITORY_OWNER", repo.split("/")[0])
        issue = {
            "title": shorten(title, 250),
            "body": f"@{owner}\n\n{body}\n\n{url or ''}",
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        api = f"https://api.github.com/repos/{repo}/issues"
        try:
            created = http_post_json(api, issue, headers)
            sent.append("GitHub issue (emails you)")
            number = created.get("number")
            if number:
                try:
                    http_post_json(
                        f"{api}/{number}",
                        {"state": "closed", "state_reason": "completed"},
                        headers,
                        method="PATCH",
                    )
                except Exception as e:  # noqa: BLE001
                    log(f"  ! couldn't close issue #{number}: {e}")
        except Exception as e:  # noqa: BLE001
            log(f"  ! GitHub issue alert failed: {e}")

    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip()
    if smtp_user and smtp_pass:
        try:
            host = os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
            port = int(os.environ.get("SMTP_PORT", "").strip() or 465)
            msg = EmailMessage()
            msg["Subject"] = title
            msg["From"] = smtp_user
            msg["To"] = os.environ.get("EMAIL_TO", "").strip() or smtp_user
            plain = body.replace("```diff\n", "").replace("```", "").replace("**", "")
            msg.set_content(f"{plain}\n\n{url or ''}")
            ctx = ssl.create_default_context()
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                    s.login(smtp_user, smtp_pass)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=30) as s:
                    s.starttls(context=ctx)
                    s.login(smtp_user, smtp_pass)
                    s.send_message(msg)
            sent.append("email")
        except Exception as e:  # noqa: BLE001
            log(f"  ! Email alert failed: {e}")

    if not sent:
        log("  (no alert channel is configured, or all of them failed; the alert is only in this log)")
    else:
        log(f"  alert sent via: {', '.join(sent)}")
    print(f"\n=== {title} ===\n{body}\n{url or ''}\n", flush=True)


# ------------------------------------------------------------- main
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def run():
    if "--test" in sys.argv or os.environ.get("TEST_ALERT", "").lower() == "true":
        notify(
            "Test alert from QCinema Watcher",
            "If you can read this, alerts are working. You'll get a message like this "
            "when the schedule or festival badge page changes.",
            WATCH[0]["url"],
        )
        return

    state = load_state()
    pages_state = state.setdefault("pages", {})
    first_run = not pages_state
    ok, failed = 0, 0
    baseline_notes = []

    for page in WATCH:
        log(f"Checking {page['name']} ({page['url']})")
        old = pages_state.get(page["slug"])
        try:
            got = fetch_page(page, (old or {}).get("source"))
        except Exception as e:  # noqa: BLE001
            failed += 1
            log(f"  ! failed: {e}")
            continue
        ok += 1
        new = {
            "source": got["source"],
            "modified": got["modified"],
            "hash": sha("\n".join(got["lines"])),
            "lines": got["lines"],
            "missing": got["missing"],
        }
        if old is None or old.get("source") != new["source"]:
            pages_state[page["slug"]] = new
            if new["modified"]:
                baseline_notes.append(f"• {page['name']}: last edited {fmt_time(new['modified'])}")
            else:
                baseline_notes.append(f"• {page['name']}")
            log("  baseline saved (nothing to compare with yet)")
            continue

        change = describe_change(page, old, new)
        if change:
            log("  CHANGED")
            notify(f"QCinema {page['name']} page changed!", change, page["url"])
        else:
            log("  no change")
        pages_state[page["slug"]] = new

    if WATCH_NEW_PAGES:
        log("Checking for newly published pages")
        try:
            recent = fetch_recent_pages()
            ok += 1
            known = set(state.get("known_page_ids", []))
            current = {p["id"] for p in recent if "id" in p}
            if known:
                for p in recent:
                    if p.get("id") not in known:
                        title = " ".join(html_to_lines(p.get("title", {}).get("rendered", ""))) or p.get("slug", "")
                        log(f"  NEW PAGE: {title}")
                        notify(
                            f"New page on qcinema.ph: {title}",
                            f"A new page was published on {fmt_time(p.get('date_gmt')) if p.get('date_gmt') else fmt_time(p.get('date'), local=True)}.",
                            p.get("link"),
                        )
            else:
                log("  baseline saved")
            state["known_page_ids"] = sorted(known | current)
        except Exception as e:  # noqa: BLE001
            failed += 1
            log(f"  ! failed: {e}")

    # Warn once if the site can't be reached for a while.
    if ok == 0 and failed:
        state["fail_streak"] = state.get("fail_streak", 0) + 1
        if state["fail_streak"] == FAIL_ALERT_AFTER:
            notify(
                "QCinema Watcher can't reach qcinema.ph",
                f"The last {FAIL_ALERT_AFTER} checks all failed. The site may be down or blocking "
                "GitHub's servers. The watcher keeps trying and will tell you when it's back.",
                SITE,
            )
    else:
        if state.get("fail_streak", 0) >= FAIL_ALERT_AFTER:
            notify("QCinema Watcher is working again", "qcinema.ph is reachable again.", SITE)
        state["fail_streak"] = 0

    if first_run and baseline_notes:
        notify(
            "QCinema Watcher is now running",
            "Watching these pages; you'll get an alert when they change:\n" + "\n".join(baseline_notes),
            SITE,
        )

    save_state(state)


if __name__ == "__main__":
    run()
