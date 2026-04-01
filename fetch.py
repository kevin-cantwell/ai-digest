#!/usr/bin/env python3
"""
AI Digest — fetches, filters, and renders AI news to index.html
Usage: python fetch.py [--no-claude]
"""

import os
import json
import time
import re
import hashlib
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from html import escape

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests feedparser anthropic", file=sys.stderr)
    sys.exit(1)

try:
    import feedparser
except ImportError:
    print("ERROR: feedparser not installed. Run: pip install requests feedparser anthropic", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TIMEOUT = 10  # seconds for all HTTP requests
NOW = datetime.now(timezone.utc)
YESTERDAY_TS = int((NOW - timedelta(hours=24)).timestamp())

# Keywords for local AI relevance filtering of HN stories
HN_AI_KEYWORDS = [
    "ai", "llm", "gpt", "claude", "gemini", "anthropic", "openai", "deepmind",
    "mistral", "machine learning", "neural", "model", "inference", "fine-tun",
    "embedding", "transformer", "diffusion", "stable diffusion", "midjourney",
    "nvidia", "gpu", "hugging face", "langchain", "rag", "vector", "benchmark",
    "agent", "multimodal",
]

REDDIT_SUBS = [
    "MachineLearning",
    "LocalLLaMA",
    "artificial",
    "ClaudeAI",
    "openai",
    "ChatGPTCoding",
]

RSS_FEEDS = [
    # Anthropic: taobojlen/anthropic-rss-feed dead Feb 2026; www.anthropic.com/rss.xml and
    # feeds.feedburner.com/anthropic-blog both 404 as of Mar 2026. Omitted for now.
    ("OpenAI Blog", "https://openai.com/blog/rss.xml"),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    # Import AI removed — weekly cadence, rarely passes 48h filter
    ("Simon Willison", "https://simonwillison.net/atom/everything/"),
    # The Pragmatic Engineer removed — not AI-focused
    ("Latent Space", "https://www.latent.space/feed"),
    # AI Snake Oil removed — monthly cadence
]

MAJOR_PROVIDER_DOMAINS = {
    "anthropic.com", "openai.com", "deepmind.google", "deepmind.com",
    "ai.meta.com", "blog.google", "research.google", "mistral.ai",
    "cohere.com", "huggingface.co",
}

TECHNICAL_TERMS = [
    "model", "training", "benchmark", "release", "api", "open source",
    "paper", "research", "agent", "multimodal", "inference", "fine-tuning",
    "fine tuning", "dataset", "architecture", "weights", "quantization",
    "rlhf", "rag", "context window", "embedding", "tokenizer", "transformer",
    "diffusion", "reinforcement learning", "evaluation",
    "agentic", "workflow", "orchestration", "sandbox", "mcp", "tool use",
    "claude code", "codex", "copilot", "developer tool", "devtool",
]

# Terms that boost score for Kevin's specific interests
PRIORITY_TERMS = [
    "claude code", "codex", "agentic", "mcp", "model context protocol",
    "ai agent", "agent framework", "workflow automation", "safe ai",
    "reliable ai", "enterprise ai", "docker ai", "sandbox", "guardrails",
    "eval", "evals", "red team", "alignment", "safety",
]

HYPE_PHRASES = [
    "you won't believe", "you will not believe", "shocking", "game changer",
    "game-changer", "revolutionary", "everything you need to know",
    "mind-blowing", "mind blowing", "unbelievable",
]

LISTICLE_PATTERNS = [
    r"\btop\s+\d+\b", r"\bbest\s+\w+\s+for\b", r"\d+\s+ways\b",
    r"\d+\s+things\b", r"\d+\s+reasons\b", r"\d+\s+tips\b",
]

VAGUE_PHRASES = [
    "ai is changing everything", "the future of ai", "ai will change",
    "how ai is transforming", "ai revolution", "ai in", "ai and the future",
]

PRODUCT_NAMES = [
    "claude", "gpt-4", "gpt-5", "gpt4", "gpt5", "gemini", "llama",
    "mistral", "grok", "copilot", "sora", "dall-e", "dalle", "stable diffusion",
    "midjourney", "whisper", "codex", "o1", "o3", "deepseek",
]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class NewsItem:
    __slots__ = (
        "title", "url", "article_url", "post_url", "source", "source_type", "published_ts",
        "score", "upvotes", "num_comments", "summary", "section", "url_hash",
    )

    def __init__(self, title, url, source, source_type, published_ts=None,
                 upvotes=0, num_comments=0, summary=""):
        self.title = title.strip()
        self.url = url.strip()
        self.article_url = ""  # external linked article URL, if different from post URL
        self.post_url = ""     # source thread/post URL (HN thread, Reddit thread)
        self.source = source
        self.source_type = source_type  # "hn" | "reddit" | "rss"
        self.published_ts = published_ts or int(NOW.timestamp())
        self.upvotes = upvotes
        self.num_comments = num_comments
        self.summary = summary
        self.score = 0
        self.section = "Industry News"
        self.url_hash = hashlib.md5(url.encode()).hexdigest()[:8]

    def age_str(self):
        delta = NOW - datetime.fromtimestamp(self.published_ts, tz=timezone.utc)
        hours = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        if hours >= 24:
            return f"{hours // 24}d ago"
        if hours > 0:
            return f"{hours}h ago"
        return f"{minutes}m ago"

# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_hn() -> list[NewsItem]:
    """Fetch AI-relevant HN stories from the last 24h with >=30 points.

    Uses a single broad search_by_date query (no keyword filter) then filters
    locally by AI-relevant keywords. This catches far more stories than
    running 15 narrow keyword queries against the Algolia API.
    """
    items = {}
    headers = {"User-Agent": "ai-digest-bot/1.0"}

    url = (
        f"https://hn.algolia.com/api/v1/search_by_date"
        f"?tags=story"
        f"&numericFilters=points%3E%3D30,created_at_i%3E{YESTERDAY_TS}"
        f"&hitsPerPage=100"
    )
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"  [HN] fetch failed: {exc}", file=sys.stderr)
        return []

    for hit in data.get("hits", []):
        object_id = hit.get("objectID", "")
        hn_thread_url = f"https://news.ycombinator.com/item?id={object_id}" if object_id else ""
        story_url = hit.get("url") or hn_thread_url
        if not story_url or story_url in items:
            continue

        title = hit.get("title", "(no title)")
        # Filter locally: keep only AI-relevant stories.
        # Match against title only (URLs contain too many false "ai" substrings like
        # "air", "tail", "daily"). Use whole-word matching to avoid "ai" matching
        # "Tailscale", "air", "Spain", etc.
        title_lower = title.lower()
        if not any(re.search(r'\b' + re.escape(kw) + r'\b', title_lower) for kw in HN_AI_KEYWORDS):
            continue

        ts = hit.get("created_at_i") or int(NOW.timestamp())
        news_item = NewsItem(
            title=title,
            url=story_url,
            source="Hacker News",
            source_type="hn",
            published_ts=ts,
            upvotes=hit.get("points", 0),
            num_comments=hit.get("num_comments", 0),
        )
        # Store the HN discussion thread URL separately from the linked article
        if hn_thread_url and story_url != hn_thread_url:
            news_item.post_url = hn_thread_url
        items[story_url] = news_item

    print(f"  [HN] fetched {len(items)} AI stories")
    return list(items.values())


def fetch_reddit() -> list[NewsItem]:
    """Fetch top posts from AI subreddits via RSS (avoids JSON API auth requirements)."""
    items = []
    # Reddit's RSS endpoint works without OAuth; JSON API returns 403 from CI IPs
    headers = {"User-Agent": "feedreader:ai-digest:v1.0"}

    for sub in REDDIT_SUBS:
        url = f"https://www.reddit.com/r/{sub}/top.rss?t=day&limit=25"
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as exc:
            print(f"  [Reddit] fetch failed for r/{sub}: {exc}", file=sys.stderr)
            continue

        entries_added = 0
        for entry in feed.entries[:25]:
            link = entry.get("link", "")
            title = entry.get("title", "")
            if not link or not title:
                continue

            ts = None
            for time_field in ("published_parsed", "updated_parsed"):
                t = entry.get(time_field)
                if t:
                    try:
                        ts = int(time.mktime(t))
                    except Exception:
                        pass
                    break
            if ts is None:
                ts = int(NOW.timestamp())

            if ts < YESTERDAY_TS:
                continue

            item = NewsItem(
                title=title,
                url=link,
                source=f"r/{sub}",
                source_type="reddit",
                published_ts=ts,
            )
            # Reddit thread URL is always the post URL
            item.post_url = link

            # Extract the external article URL from the post content (link posts embed it as [link])
            content_html = ""
            for field in ("content", "summary"):
                val = entry.get(field)
                if isinstance(val, list) and val:
                    content_html = val[0].get("value", "")
                elif isinstance(val, str):
                    content_html = val
                if content_html:
                    break
            if content_html:
                m = re.search(r'href="(https?://(?!(?:www\.)?reddit\.com)[^"]+)"[^>]*>\[link\]', content_html)
                if m:
                    item.article_url = m.group(1)

            items.append(item)
            entries_added += 1

        print(f"  [Reddit] r/{sub}: {entries_added} posts")

    return items


def fetch_rss() -> list[NewsItem]:
    """Fetch from RSS/Atom feeds."""
    items = []
    headers = {
        "User-Agent": "ai-digest-bot/1.0",
        "Accept": "application/rss+xml, application/atom+xml, text/xml, */*",
    }

    for feed_name, feed_url in RSS_FEEDS:
        try:
            resp = requests.get(feed_url, headers=headers, timeout=TIMEOUT)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as exc:
            print(f"  [RSS] fetch failed for {feed_name}: {exc}", file=sys.stderr)
            continue

        entries_added = 0
        for entry in feed.entries[:20]:
            link = entry.get("link", "")
            title = entry.get("title", "")
            if not link or not title:
                continue

            # Parse published time
            ts = None
            for time_field in ("published_parsed", "updated_parsed"):
                t = entry.get(time_field)
                if t:
                    try:
                        ts = int(time.mktime(t))
                    except Exception:
                        pass
                    break
            if ts is None:
                ts = int(NOW.timestamp())

            # Only last 48h from RSS (feeds may be stale)
            if ts < int((NOW - timedelta(hours=48)).timestamp()):
                continue

            summary = ""
            for field in ("summary", "description"):
                raw = entry.get(field, "")
                if raw:
                    # Strip HTML tags
                    summary = re.sub(r"<[^>]+>", "", raw).strip()
                    summary = re.sub(r"\s+", " ", summary)[:200]
                    break

            items.append(NewsItem(
                title=title,
                url=link,
                source=feed_name,
                source_type="rss",
                published_ts=ts,
                summary=summary,
            ))
            entries_added += 1

        print(f"  [RSS] {feed_name}: {entries_added} entries")

    return items


# ---------------------------------------------------------------------------
# Heuristic scoring
# ---------------------------------------------------------------------------

def heuristic_score(item: NewsItem) -> int:
    score = 50  # baseline
    title_lower = item.title.lower()
    url_lower = item.url.lower()

    # --- Positive signals ---

    # Technical terms (max +30)
    tech_hits = sum(1 for t in TECHNICAL_TERMS if t in title_lower)
    score += min(tech_hits * 15, 30)

    # HN points bonus
    if item.source_type == "hn":
        if item.upvotes > 100:
            score += 20
        elif item.upvotes > 50:
            score += 10

    # Major provider blog
    parsed = urlparse(item.url)
    domain = parsed.netloc.lower().lstrip("www.")
    if any(domain == d or domain.endswith("." + d) for d in MAJOR_PROVIDER_DOMAINS):
        score += 25

    # Specific product mentions
    if any(p in title_lower for p in PRODUCT_NAMES):
        score += 10

    # arxiv or github link
    if "arxiv.org" in url_lower or "github.com" in url_lower:
        score += 15

    # Priority topics: dev tools, agentic AI, safety
    if any(p in title_lower for p in PRIORITY_TERMS):
        score += 20

    # --- Negative signals ---

    # All-caps title
    words = item.title.split()
    upper_words = sum(1 for w in words if len(w) > 3 and w.isupper())
    if upper_words > 3 or item.title.isupper():
        score -= 30

    # Excessive exclamation marks
    if item.title.count("!") > 3:
        score -= 30

    # Hype phrases
    if any(p in title_lower for p in HYPE_PHRASES):
        score -= 25

    # Listicle patterns
    if any(re.search(p, title_lower) for p in LISTICLE_PATTERNS):
        score -= 15


    # Vague titles
    if any(p in title_lower for p in VAGUE_PHRASES):
        score -= 20

    # Short / uninformative title
    if len(item.title.split()) < 4:
        score -= 15

    return max(0, min(100, score))


def assign_section(item: NewsItem) -> str:
    title_lower = item.title.lower()
    url_lower = item.url.lower()

    # Dev tools & agentic AI gets its own section (check first)
    if any(kw in title_lower for kw in (
            "claude code", "codex", "agentic", "mcp", "model context protocol",
            "ai agent", "agent framework", "workflow", "orchestrat",
            "sandbox", "guardrails", "evals", "eval framework",
            "developer tool", "devtool", "copilot", "coding assistant",
            "docker ai", "ai infrastructure", "ai platform")):
        return "Developer Tools & Agentic AI"

    if any(kw in title_lower for kw in ("release", "launch", "announce", "debut",
                                         "available", "ship", "gpt-", "claude ",
                                         "gemini ", "llama ", "new model")):
        return "Major Releases & Announcements"
    if "arxiv.org" in url_lower or any(kw in title_lower for kw in (
            "paper", "research", "study", "survey", "benchmark",
            "evaluation", "training", "dataset", "architecture")):
        return "Research & Papers"
    if "github.com" in url_lower or any(kw in title_lower for kw in (
            "open source", "open-source", "library", "framework",
            "tool", "sdk", "api", "plugin", "extension", "demo")):
        return "Tools & Open Source"
    return "Industry News"


# ---------------------------------------------------------------------------
# Claude re-scoring (optional)
# ---------------------------------------------------------------------------

def claude_rescore(items: list[NewsItem]) -> list[NewsItem]:
    """Re-score top 30 items using Claude Haiku for nuanced judgment."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return items

    try:
        import anthropic
    except ImportError:
        print("  [Claude] anthropic package not installed, skipping", file=sys.stderr)
        return items

    print("  [Claude] Re-scoring with claude-haiku-4-5-20251001...")
    client = anthropic.Anthropic(api_key=api_key)

    candidates = sorted(items, key=lambda x: x.score, reverse=True)[:30]
    payload = [
        {"index": i, "title": item.title, "url": item.url, "source": item.source}
        for i, item in enumerate(candidates)
    ]

    prompt = (
        "You are a curator of a high-quality AI news digest. Rate each of the following "
        "AI news items for credibility and genuine newsworthiness.\n\n"
        "Scoring:\n"
        "10 = significant technical development, major product release, or important research\n"
        "7-9 = notable news, real substance\n"
        "4-6 = mildly interesting but not essential\n"
        "1-3 = hype, slop, opinion piece, self-promotion, or low-value content\n"
        "0 = must not appear in the digest (see below)\n\n"
        "Score 0 for any item that:\n"
        "- Contains NSFW, sexual, or explicit content\n"
        "- Is a personal anecdote or embarrassing story (e.g. 'my son did X', 'I got banned for')\n"
        "- Is not substantively about AI technology (merely mentions an AI product incidentally)\n"
        "- Is political news, geopolitics, or general world events with no real AI angle\n\n"
        "Return ONLY a JSON array (no markdown, no explanation) of objects with this shape:\n"
        '[{"index": 0, "score": 8, "summary": "One sentence describing the news."}]\n\n'
        "Items to rate:\n"
        + json.dumps(payload, indent=2)
    )

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        ratings = json.loads(raw)
    except Exception as exc:
        print(f"  [Claude] scoring failed: {exc}", file=sys.stderr)
        return items

    idx_map = {r["index"]: r for r in ratings}
    for i, item in enumerate(candidates):
        if i in idx_map:
            r = idx_map[i]
            claude_score = int(r["score"])
            # Score 0 = Claude flagged this as inappropriate/off-topic — drop it entirely
            if claude_score == 0:
                item.score = -1
                continue
            # Blend heuristic (40%) and Claude (60%), both on 0-100 scale
            item.score = int(item.score * 0.4 + claude_score * 10 * 0.6)
            if r.get("summary"):
                item.summary = r["summary"].strip()

    print(f"  [Claude] scored {len(idx_map)} items")
    return items


def generate_tldr(items: list[NewsItem]) -> str:
    """Generate a 2-sentence TL;DR using Claude if available."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        # Fallback: construct from top items
        top = items[:3]
        if not top:
            return "No significant AI news found in the last 24 hours."
        titles = "; ".join(t.title for t in top)
        return f"Today's AI digest covers {len(items)} stories. Top stories include: {titles[:200]}."

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        top_titles = [f"- {it.title} ({it.source})" for it in items[:10]]
        prompt = (
            "Based on these top AI news stories from the past 24 hours, write exactly 2 sentences "
            "summarizing the most important developments. Be specific, factual, and concise. "
            "Do not use hype language.\n\n"
            + "\n".join(top_titles)
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:
        print(f"  [Claude] TL;DR failed: {exc}", file=sys.stderr)
        top = items[:3]
        titles = "; ".join(t.title for t in top)
        return f"Today's AI digest covers {len(items)} stories. Top stories: {titles[:200]}."


# ---------------------------------------------------------------------------
# Article freshness filter
# ---------------------------------------------------------------------------

# Domains whose URLs are inherently not "articles" with a publication date
_STALENESS_SKIP_DOMAINS = {
    "reddit.com", "news.ycombinator.com", "github.com",
    "twitter.com", "x.com", "youtube.com", "linkedin.com",
}


def _should_check_article_freshness(url: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower().lstrip("www.")
        return bool(domain) and not any(
            domain == d or domain.endswith("." + d)
            for d in _STALENESS_SKIP_DOMAINS
        )
    except Exception:
        return False


def _date_from_url(url: str) -> "datetime | None":
    """
    Try to extract a publication date from the URL path without any HTTP request.
    Matches patterns like /2025/01/, /2025-01-15/, ?date=2025-01-15, etc.
    """
    path = urlparse(url).path + "?" + urlparse(url).query
    m = re.search(r'[/\-_=](\d{4})[/\-_](\d{2})(?:[/\-_](\d{2}))?', path)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        day = int(m.group(3)) if m.group(3) else 1
        if 2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
            try:
                return datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def _get_article_published_date(url: str) -> "datetime | None":
    """Fetch up to 50 KB of a URL and extract its publication date from meta tags."""
    try:
        resp = requests.get(
            url, timeout=5,
            headers={"User-Agent": "ai-digest-bot/1.0"},
            allow_redirects=True,
            stream=True,
        )
        resp.raise_for_status()
        chunks = []
        size = 0
        for chunk in resp.iter_content(8192):
            chunks.append(chunk)
            size += len(chunk)
            if size > 51200:
                break
        text = b"".join(chunks).decode("utf-8", errors="ignore")
    except Exception:
        return None

    # Try common publication date meta tags (property= and name= variants)
    patterns = [
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([\d\-T:+Z]+)',
        r'<meta[^>]+content=["\']([\d\-T:+Z]+)[^>]+property=["\']article:published_time["\']',
        r'<meta[^>]+name=["\']pubdate["\'][^>]+content=["\']([\d\-T:+Z]+)',
        r'<meta[^>]+content=["\']([\d\-T:+Z]+)[^>]+name=["\']pubdate["\']',
        r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([\d\-T:+Z]+)',
        r'<meta[^>]+content=["\']([\d\-T:+Z]+)[^>]+name=["\']date["\']',
        r'<time[^>]+datetime=["\']([\d\-T:+Z]+)',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                continue
    return None


def filter_stale_articles(items: list[NewsItem]) -> list[NewsItem]:
    """
    Drop items whose linked article was published more than 48h ago.
    For HN items the story URL is the article; for Reddit we use the extracted article_url.
    If the article date cannot be determined, the item is kept.
    HTTP requests are parallelized for speed.
    """
    cutoff = NOW - timedelta(hours=48)

    def check_item(item: NewsItem) -> "tuple[NewsItem, datetime | None]":
        if item.source_type == "hn":
            url_to_check = item.url if not item.url.startswith("https://news.ycombinator.com") else ""
        elif item.source_type == "reddit":
            url_to_check = item.article_url
        else:
            url_to_check = ""

        if url_to_check and _should_check_article_freshness(url_to_check):
            pub_date = _date_from_url(url_to_check) or _get_article_published_date(url_to_check)
            return (item, pub_date)
        return (item, None)

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(check_item, item): item for item in items}
        for future in as_completed(futures):
            try:
                item, pub_date = future.result()
                results.append((item, pub_date))
            except Exception as exc:
                item = futures[future]
                print(f"  [stale] error checking {item.title[:40]}: {exc}", file=sys.stderr)
                results.append((item, None))

    result = []
    dropped = 0
    # Restore original ordering
    item_order = {id(item): i for i, item in enumerate(items)}
    results.sort(key=lambda t: item_order.get(id(t[0]), 0))

    for item, pub_date in results:
        if pub_date and pub_date < cutoff:
            print(f"  [stale] dropped ({pub_date.date()}): {item.title[:60]}")
            dropped += 1
            continue
        result.append(item)

    print(f"  [stale] kept {len(result)}, dropped {dropped} stale article(s)")
    return result


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    seen_urls: set[str] = set()
    seen_titles: dict[str, NewsItem] = {}
    out = []

    for item in items:
        if item.url in seen_urls:
            continue

        # Cross-source dedup: HN stores the article URL in item.url; Reddit stores
        # the reddit post URL in item.url and the actual article in item.article_url.
        # Normalize to the article URL so HN and Reddit stories about the same
        # article are treated as duplicates.
        article_url = item.article_url if item.article_url else item.url
        if article_url and article_url in seen_urls:
            continue

        # Fuzzy title dedup: normalize and compare
        norm_title = re.sub(r"[^a-z0-9]", "", item.title.lower())[:60]
        if norm_title in seen_titles:
            # Keep the one with higher score (will be recalculated, so keep first)
            continue

        seen_urls.add(item.url)
        if article_url:
            seen_urls.add(article_url)
        seen_titles[norm_title] = item
        out.append(item)

    return out


# ---------------------------------------------------------------------------
# RSS feed generation
# ---------------------------------------------------------------------------

FEED_URL = "https://kevin-cantwell.github.io/ai-digest/feed.xml"
DIGEST_URL = "https://kevin-cantwell.github.io/ai-digest/"


def _rfc822(ts: int) -> str:
    """Convert a Unix timestamp to RFC 822 date format for RSS."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def generate_feed(items: list[NewsItem]) -> None:
    """
    Generate feed.xml from the scored+filtered items list.

    Deduplicates by canonical article URL (GUID), merging sources if the same
    article appears from multiple origins (e.g. HN + Reddit). Items are sorted
    by score descending.
    """
    # --- Deduplicate by canonical article URL ---
    merged: dict[str, dict] = {}  # canonical_url -> merged record

    for item in items:
        # Canonical URL: article_url if available, otherwise item.url
        canonical = item.article_url if item.article_url else item.url
        canonical = canonical.strip()
        if not canonical:
            continue

        if canonical not in merged:
            merged[canonical] = {
                "title": item.title,
                "canonical_url": canonical,
                "score": item.score,
                "published_ts": item.published_ts,
                "summary": item.summary,
                "section": item.section,
                "sources": [item.source],
            }
        else:
            rec = merged[canonical]
            # Keep highest score; merge source names
            if item.score > rec["score"]:
                rec["score"] = item.score
                rec["title"] = item.title
            if item.source not in rec["sources"]:
                rec["sources"].append(item.source)
            # Use earliest timestamp
            if item.published_ts < rec["published_ts"]:
                rec["published_ts"] = item.published_ts
            # Prefer non-empty summary
            if not rec["summary"] and item.summary:
                rec["summary"] = item.summary

    # Sort by score descending
    records = sorted(merged.values(), key=lambda r: r["score"], reverse=True)

    def xml_escape(s: str) -> str:
        return (
            s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;")
        )

    def make_description(rec: dict) -> str:
        sources_str = ", ".join(rec["sources"])
        score = rec["score"]
        section = rec["section"]
        summary = rec.get("summary", "")

        lines = []
        lines.append(f"<b>Category:</b> {xml_escape(section)}")
        lines.append(f"<b>Score:</b> {score}/100")
        lines.append(f"<b>Source:</b> {xml_escape(sources_str)}")
        if summary:
            lines.append(f"<br/>{xml_escape(summary)}")
        lines.append(
            f'<br/><a href="{DIGEST_URL}">View full digest →</a>'
        )
        return "<![CDATA[" + "<br/>".join(lines) + "]]>"

    now_rfc822 = _rfc822(int(NOW.timestamp()))

    items_xml = ""
    for rec in records:
        canonical_url = rec["canonical_url"]
        title = xml_escape(rec["title"])
        pub_date = _rfc822(rec["published_ts"])
        section = xml_escape(rec["section"])
        description = make_description(rec)

        items_xml += f"""
    <item>
      <title>{title}</title>
      <link>{xml_escape(canonical_url)}</link>
      <guid isPermaLink="true">{xml_escape(canonical_url)}</guid>
      <pubDate>{pub_date}</pubDate>
      <category>{section}</category>
      <description>{description}</description>
    </item>"""

    feed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>AI Digest</title>
    <link>{DIGEST_URL}</link>
    <description>Curated AI news, updated hourly</description>
    <language>en-us</language>
    <lastBuildDate>{now_rfc822}</lastBuildDate>
    <atom:link href="{FEED_URL}" rel="self" type="application/rss+xml"/>{items_xml}
  </channel>
</rss>
"""

    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed.xml")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(feed_xml)

    print(f"Wrote {len(feed_xml):,} bytes to {output_path} ({len(records)} items)")


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

SECTION_ORDER = [
    "Developer Tools & Agentic AI",
    "Major Releases & Announcements",
    "Research & Papers",
    "Tools & Open Source",
    "Industry News",
]

SECTION_COLORS = {
    "Developer Tools & Agentic AI":   "#4ade80",
    "Major Releases & Announcements": "#6c8ebf",
    "Research & Papers":              "#82b366",
    "Tools & Open Source":            "#d6a24a",
    "Industry News":                  "#9c6fbd",
}

SECTION_ICONS = {
    "Developer Tools & Agentic AI":   "⚡",
    "Major Releases & Announcements": "🚀",
    "Research & Papers":              "📄",
    "Tools & Open Source":            "🔧",
    "Industry News":                  "📰",
}


def render_html(items: list[NewsItem], tldr: str) -> str:
    now_str = NOW.strftime("%A, %B %-d, %Y — %H:%M UTC")
    now_iso = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Group items by section
    sections: dict[str, list[NewsItem]] = {s: [] for s in SECTION_ORDER}
    for item in items:
        sections.setdefault(item.section, []).append(item)

    def score_bar(score: int) -> str:
        filled = round(score / 10)
        empty = 10 - filled
        return (
            f'<span class="score-bar" title="Quality score: {score}/100">'
            + '<span class="bar-filled">' + "█" * filled + "</span>"
            + '<span class="bar-empty">' + "░" * empty + "</span>"
            + f' <span class="score-num">{score}</span>'
            + "</span>"
        )

    def render_section(name: str, sec_items: list[NewsItem]) -> str:
        if not sec_items:
            return ""
        color = SECTION_COLORS.get(name, "#888")
        icon = SECTION_ICONS.get(name, "•")
        cards = ""
        for item in sec_items:
            summary_html = (
                f'<p class="item-summary">{escape(item.summary)}</p>'
                if item.summary else ""
            )
            upvotes_html = ""
            if item.source_type == "hn" and item.upvotes > 0:
                comments_part = f'  💬 {item.num_comments}' if item.num_comments > 0 else ''
                upvotes_html = f'<span class="upvotes">▲ {item.upvotes}{escape(comments_part)}</span>'
            elif item.source_type == "reddit" and item.upvotes > 0:
                upvotes_html = f'<span class="upvotes">▲ {item.upvotes}</span>'

            # Title links to the article; source label links to the source thread/post
            title_href = escape(item.article_url if item.article_url else item.url)
            source_href = escape(item.post_url if item.post_url else item.url)

            source_html = (
                f'<a class="source" href="{source_href}" target="_blank" rel="noopener noreferrer">{escape(item.source)}</a>'
                if source_href else
                f'<span class="source">{escape(item.source)}</span>'
            )

            cards += f"""
      <div class="card">
        <div class="card-header">
          <a class="card-title" href="{title_href}" target="_blank" rel="noopener noreferrer">{escape(item.title)}</a>
        </div>
        {summary_html}
        <div class="card-meta">
          {source_html}
          {upvotes_html}
          <span class="age">{item.age_str()}</span>
          {score_bar(item.score)}
        </div>
      </div>"""

        return f"""
    <section class="digest-section">
      <h2 class="section-title" style="color: {color};">{icon} {escape(name)}</h2>
      <div class="cards">{cards}
      </div>
    </section>"""

    sections_html = "".join(
        render_section(name, sections.get(name, []))
        for name in SECTION_ORDER
    )

    total_items = len(items)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="300">
  <title>AI Digest — {NOW.strftime('%Y-%m-%d')}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    :root {{
      --bg:          #0f1117;
      --bg-card:     #161b27;
      --bg-card-hover: #1c2333;
      --border:      #2a3040;
      --border-light:#3a4555;
      --text:        #e2e8f0;
      --text-muted:  #8896a8;
      --text-dim:    #566478;
      --accent:      #5b8dee;
      --tldr-bg:     #131a2e;
      --tldr-border: #2d4270;
    }}

    html {{ scroll-behavior: smooth; }}

    body {{
      background: var(--bg);
      color: var(--text);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      font-size: 15px;
      line-height: 1.6;
      min-height: 100vh;
    }}

    /* ---- Header ---- */
    header {{
      border-bottom: 1px solid var(--border);
      padding: 24px 0 20px;
      background: linear-gradient(180deg, #0d1220 0%, var(--bg) 100%);
    }}
    .header-inner {{
      max-width: 860px;
      margin: 0 auto;
      padding: 0 24px;
      display: flex;
      align-items: baseline;
      gap: 16px;
      flex-wrap: wrap;
    }}
    .logo {{
      font-size: 28px;
      font-weight: 700;
      letter-spacing: -0.5px;
      background: linear-gradient(135deg, #5b8dee 0%, #a78bfa 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }}
    .logo-dot {{
      -webkit-text-fill-color: #5b8dee;
      color: #5b8dee;
    }}
    .header-meta {{
      color: var(--text-muted);
      font-size: 13px;
      display: flex;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .header-date {{
      font-weight: 500;
      color: var(--text);
    }}
    .header-updated {{
      color: var(--text-dim);
    }}
    .header-count {{
      background: rgba(91,141,238,0.15);
      color: #5b8dee;
      padding: 2px 8px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 600;
    }}

    /* ---- Main layout ---- */
    main {{
      max-width: 860px;
      margin: 0 auto;
      padding: 32px 24px 80px;
    }}

    /* ---- TL;DR ---- */
    .tldr-box {{
      background: var(--tldr-bg);
      border: 1px solid var(--tldr-border);
      border-radius: 10px;
      padding: 20px 24px;
      margin-bottom: 36px;
      position: relative;
    }}
    .tldr-box::before {{
      content: "TL;DR";
      position: absolute;
      top: -10px;
      left: 20px;
      background: var(--tldr-bg);
      padding: 0 8px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 1.5px;
      color: var(--accent);
      border: 1px solid var(--tldr-border);
      border-radius: 4px;
    }}
    .tldr-text {{
      color: var(--text);
      font-size: 15px;
      line-height: 1.7;
      font-weight: 400;
    }}

    /* ---- Sections ---- */
    .digest-section {{
      margin-bottom: 40px;
    }}
    .section-title {{
      font-size: 16px;
      font-weight: 600;
      letter-spacing: 0.3px;
      margin-bottom: 14px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 8px;
    }}

    /* ---- Cards ---- */
    .cards {{
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 16px;
      transition: background 0.15s ease, border-color 0.15s ease;
    }}
    .card:hover {{
      background: var(--bg-card-hover);
      border-color: var(--border-light);
    }}
    .card-header {{
      margin-bottom: 6px;
    }}
    .card-title {{
      color: var(--text);
      text-decoration: none;
      font-size: 14.5px;
      font-weight: 500;
      line-height: 1.45;
      transition: color 0.15s ease;
    }}
    .card-title:hover {{
      color: var(--accent);
    }}
    a:visited {{ color: #7a8a9a; }}
    .item-summary {{
      font-size: 13px;
      color: var(--text-muted);
      line-height: 1.5;
      margin-bottom: 8px;
    }}
    .card-meta {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 8px;
    }}
    .source {{
      font-size: 12px;
      font-weight: 500;
      color: var(--text-dim);
      background: rgba(255,255,255,0.04);
      padding: 2px 7px;
      border-radius: 4px;
      border: 1px solid var(--border);
      text-decoration: none;
    }}
    a.source:hover {{
      color: var(--accent);
      border-color: var(--accent);
    }}
    .upvotes {{
      font-size: 12px;
      color: #d6a24a;
      font-weight: 500;
    }}
    .age {{
      font-size: 12px;
      color: var(--text-dim);
    }}
    .score-bar {{
      font-family: 'JetBrains Mono', monospace;
      font-size: 10px;
      letter-spacing: 0px;
      margin-left: auto;
    }}
    .bar-filled {{ color: #5b8dee; }}
    .bar-empty  {{ color: var(--text-dim); }}
    .score-num  {{ color: var(--text-dim); margin-left: 4px; font-size: 10px; }}

    /* ---- Footer ---- */
    footer {{
      border-top: 1px solid var(--border);
      padding: 20px 24px;
      text-align: center;
      color: var(--text-dim);
      font-size: 12px;
    }}
    footer a {{ color: var(--text-muted); text-decoration: none; }}
    footer a:hover {{ color: var(--text); }}

    /* ---- Responsive ---- */
    @media (max-width: 600px) {{
      .header-inner {{ gap: 8px; }}
      .logo {{ font-size: 22px; }}
      main {{ padding: 20px 16px 60px; }}
      .card {{ padding: 12px 14px; }}
      .score-bar {{ display: none; }}
    }}
  </style>
</head>
<body>

<header>
  <div class="header-inner">
    <div class="logo">AI Digest<span class="logo-dot">.</span></div>
    <div class="header-meta">
      <span class="header-date" id="header-date" data-utc="{now_iso}">{escape(now_str)}</span>
      <script>
        (function() {{
          var el = document.getElementById('header-date');
          var d = new Date(el.dataset.utc);
          if (!isNaN(d)) {{
            el.textContent = d.toLocaleDateString(undefined, {{weekday:'long',year:'numeric',month:'long',day:'numeric'}}) + ' — ' + d.toLocaleTimeString(undefined, {{hour:'2-digit',minute:'2-digit'}});
          }}
        }})();
      </script>
      <span class="header-updated" id="header-updated">Updated just now</span>
      <script>
        (function() {{
          var builtAt = new Date("{now_iso}");
          function updateAge() {{
            var mins = Math.floor((Date.now() - builtAt.getTime()) / 60000);
            var el = document.getElementById('header-updated');
            if (!el) return;
            if (mins < 2) {{ el.textContent = 'Updated just now'; }}
            else if (mins < 60) {{ el.textContent = 'Updated ' + mins + ' minutes ago'; }}
            else {{
              var hrs = Math.floor(mins / 60);
              el.textContent = 'Updated ' + hrs + (hrs === 1 ? ' hour ago' : ' hours ago');
            }}
          }}
          updateAge();
          setInterval(updateAge, 60000);
        }})();
      </script>
      <span class="header-count">{total_items} stories</span>
    </div>
  </div>
</header>

<main>
  <div class="tldr-box">
    <p class="tldr-text">{escape(tldr)}</p>
  </div>

  {sections_html}
</main>

<footer>
  <p>Fetches every hour &nbsp;•&nbsp; <a href="https://github.com/kevin-cantwell/ai-digest" rel="noopener">Source</a></p>
</footer>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main():
    use_claude = "--no-claude" not in sys.argv
    print(f"AI Digest fetch — {NOW.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Claude API: {'enabled if ANTHROPIC_API_KEY set' if use_claude else 'disabled'}")
    print()

    # 1. Fetch from all sources
    all_items: list[NewsItem] = []

    print("Fetching Hacker News...")
    try:
        all_items.extend(fetch_hn())
    except Exception:
        print("  [HN] unexpected error:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    print("Fetching Reddit...")
    try:
        all_items.extend(fetch_reddit())
    except Exception:
        print("  [Reddit] unexpected error:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    print("Fetching RSS feeds...")
    try:
        all_items.extend(fetch_rss())
    except Exception:
        print("  [RSS] unexpected error:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    print(f"\nTotal raw items: {len(all_items)}")

    # 2. Deduplicate
    all_items = deduplicate(all_items)
    print(f"After dedup: {len(all_items)}")

    # 3. Heuristic scoring
    for item in all_items:
        item.score = heuristic_score(item)

    # 4. Drop low-quality items
    all_items = [it for it in all_items if it.score >= 40]
    print(f"After heuristic filter (score>=40): {len(all_items)}")

    # 5. Drop items linking to stale articles (older than 48h)
    print("\nChecking article freshness...")
    try:
        all_items = filter_stale_articles(all_items)
    except Exception:
        print("  [stale] unexpected error:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    print(f"After staleness filter: {len(all_items)}")

    # 7. Claude re-scoring (optional)
    if use_claude:
        print("\nClaude re-scoring...")
        try:
            all_items = claude_rescore(all_items)
        except Exception:
            print("  [Claude] unexpected error:", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

    # 8. Drop items flagged by Claude (score == -1), then sort and take top 30
    flagged = [it for it in all_items if it.score == -1]
    if flagged:
        print(f"  [Claude] dropped {len(flagged)} flagged item(s): {[it.title[:50] for it in flagged]}")
    all_items = [it for it in all_items if it.score >= 0]
    all_items.sort(key=lambda x: x.score, reverse=True)
    all_items = all_items[:30]

    # 9. Assign sections
    for item in all_items:
        item.section = assign_section(item)

    # 9a. Per-section soft cap: max 6 items per section, up to 20 total.
    # Demote lowest-scoring items from over-represented sections and use
    # them to backfill sections with room.
    SECTION_CAP = 6
    TOTAL_CAP = 20
    selected: list[NewsItem] = []
    runnerups: list[NewsItem] = []
    section_counts: dict[str, int] = {}
    for item in all_items:  # already sorted by score descending
        sec = item.section
        if section_counts.get(sec, 0) < SECTION_CAP:
            selected.append(item)
            section_counts[sec] = section_counts.get(sec, 0) + 1
        else:
            runnerups.append(item)
    # Backfill from runner-ups (already score-sorted) if we haven't hit TOTAL_CAP
    for item in runnerups:
        if len(selected) >= TOTAL_CAP:
            break
        if section_counts.get(item.section, 0) < SECTION_CAP:
            selected.append(item)
            section_counts[item.section] = section_counts.get(item.section, 0) + 1
    # Final sort by score and enforce overall cap
    selected.sort(key=lambda x: x.score, reverse=True)
    all_items = selected[:TOTAL_CAP]

    # 10. Generate TL;DR
    print("\nGenerating TL;DR...")
    tldr = generate_tldr(all_items)
    print(f"  TL;DR: {tldr[:80]}...")

    # 11. Render HTML
    html = render_html(all_items, tldr)

    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nWrote {len(html):,} bytes to {output_path}")

    # 12. Generate RSS feed
    print("\nGenerating RSS feed...")
    try:
        generate_feed(all_items)
    except Exception:
        print("  [feed] unexpected error:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    print(f"Final stories: {len(all_items)}")
    for item in all_items:
        print(f"  [{item.score:3d}] [{item.section[:20]:<20}] {item.title[:70]}")


if __name__ == "__main__":
    main()
