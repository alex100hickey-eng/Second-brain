"""
stock_watch_agent.py

Autonomous agent that checks in on a small watchlist of stock tickers and
summarizes anything notable happening with them -- big price moves,
earnings reports, analyst upgrades/downgrades, etc.

Since this agent has no dedicated market-data/news API available, it uses
Claude directly to reason about and summarize the most notable recent
developments for each ticker, based on its training knowledge. Each
summary explicitly flags that it may not reflect same-day price action.

The final result (a per-ticker summary plus an overall digest) is saved
as JSON to the Supabase table "Agent Outputs" with:
    agent_name  = "stock_watch_agent"
    output_text = JSON-encoded string of the result

Environment variables required:
    CLAUDE_API_KEY
    SUPABASE_URL
    SUPABASE_KEY

Run locally:
    python3 stock_watch_agent.py
"""

import os
import sys
import json
from datetime import datetime, timezone

from anthropic import Anthropic
from supabase import create_client

AGENT_NAME = "stock_watch_agent"
MODEL = "claude-sonnet-5"

# Small watchlist of tickers Alex follows.
WATCHLIST = ["AAPL", "TSLA", "NVDA", "MSFT", "AMZN"]


def get_env_or_exit(var_name):
    value = os.environ.get(var_name)
    if not value:
        sys.exit(f"Missing required environment variable: {var_name}")
    return value


def summarize_ticker(client, ticker):
    """Ask Claude to summarize anything notable for a given ticker."""
    prompt = (
        f"You are a financial news assistant. Summarize anything notable "
        f"about the stock ticker {ticker} that an investor should know about: "
        f"significant price moves, earnings results, analyst upgrades or "
        f"downgrades, or major company news.\n\n"
        f"Keep it to 2-4 concise sentences. If you are not certain about "
        f"very recent (last few days) events, say so plainly rather than "
        f"guessing at specific numbers. Do not fabricate exact stock prices "
        f"or dates you are not confident about."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text.strip()


def main():
    print(f"[{AGENT_NAME}] Starting run...")

    claude_api_key = get_env_or_exit("CLAUDE_API_KEY")
    supabase_url = get_env_or_exit("SUPABASE_URL")
    supabase_key = get_env_or_exit("SUPABASE_KEY")

    print(f"[{AGENT_NAME}] Environment variables loaded.")

    client = Anthropic(api_key=claude_api_key)
    supabase = create_client(supabase_url, supabase_key)

    print(f"[{AGENT_NAME}] Connected to Anthropic and Supabase clients.")

    ticker_summaries = {}

    for ticker in WATCHLIST:
        print(f"[{AGENT_NAME}] Checking news for {ticker}...")
        try:
            summary = summarize_ticker(client, ticker)
        except Exception as e:
            summary = f"Could not retrieve summary due to error: {e}"
        ticker_summaries[ticker] = summary
        print(f"[{AGENT_NAME}] {ticker} summary ready.")

    print(f"[{AGENT_NAME}] Generating overall digest...")

    digest_prompt = (
        "Here are individual stock news summaries:\n\n"
        + "\n".join(f"{t}: {s}" for t, s in ticker_summaries.items())
        + "\n\nWrite a short overall digest (3-5 sentences) highlighting "
        "the most notable items across this watchlist, if any stand out. "
        "If nothing is particularly notable, say so plainly."
    )

    try:
        digest_response = client.messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": digest_prompt}],
        )
        overall_digest = digest_response.content[0].text.strip()
    except Exception as e:
        overall_digest = f"Could not generate overall digest due to error: {e}"

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "watchlist": WATCHLIST,
        "ticker_summaries": ticker_summaries,
        "overall_digest": overall_digest,
    }

    output_text = json.dumps(result)

    print(f"[{AGENT_NAME}] Saving result to Supabase...")

    supabase.table("Agent Outputs").insert(
        {
            "agent_name": AGENT_NAME,
            "output_text": output_text,
        }
    ).execute()

    print(f"[{AGENT_NAME}] Done. Result saved to 'Agent Outputs' table.")


if __name__ == "__main__":
    main()
