# AI Digest

An automated AI news digest that fetches, filters, and renders the best AI stories from the past 24 hours.

**Live site:** https://kevin-cantwell.github.io/ai-digest/

## How it works

1. **Fetches** stories from Hacker News (broad 24h query, locally filtered for AI relevance), Reddit (r/MachineLearning, r/LocalLLaMA, r/artificial, r/ClaudeAI, r/openai, r/ChatGPTCoding), and RSS feeds from OpenAI, Google DeepMind, Ars Technica, TechCrunch AI, Simon Willison, and Latent Space. (Anthropic's RSS feed is currently unavailable.)

2. **Filters** using a heuristic scorer (0–100) that rewards technical substance and penalizes hype and listicles. Items scoring below 40 are dropped.

3. **Optionally re-scores** the top 30 items using Claude Haiku if `ANTHROPIC_API_KEY` is set — blending AI judgment with heuristics for better signal.

4. **Renders** a clean, dark-themed HTML page (`index.html`) grouped into sections: Major Releases & Announcements, Research & Papers, Tools & Open Source, and Industry News.

5. **Publishes** via GitHub Pages, updated every hour by GitHub Actions.

## Running locally

```bash
pip install requests feedparser anthropic
python fetch.py
open index.html
```

Set `ANTHROPIC_API_KEY` in your environment to enable Claude-powered scoring and TL;DR generation.

## GitHub Pages

The repo is configured to serve `index.html` from the `main` branch root via GitHub Pages. The page auto-refreshes every 5 minutes.

