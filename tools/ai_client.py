from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()

PROVIDERS = [
    {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": os.getenv("GROQ_API_KEY"),
        "model": "llama-3.3-70b-versatile",  # 1K/day — best quality
        "name": "Groq (70B)",
    },
    {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": os.getenv("GROQ_API_KEY"),
        "model": "llama-3.1-8b-instant",  # 14.4K/day — fallback if 70B quota hit
        "name": "Groq (8B)",
    },
    {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key": os.getenv("GOOGLE_API_KEY"),
        "model": "gemini-2.0-flash",
        "name": "Google",
    },
]

SYSTEM_PROMPT = """You are a quantitative trading analyst specializing in options market structure. When analyzing data:
1. Identify key gamma levels (call wall, put support, gamma flip point)
2. State directional bias clearly (bullish/bearish/neutral)
3. List specific price levels to watch
4. Note any unusual options activity or flow
5. Be concise — bullet points preferred over paragraphs
"""


def _get_active_providers() -> list:
    return [p for p in PROVIDERS if p["api_key"]]


def chat(messages: list) -> tuple[str, str]:
    """Multi-turn chat. Returns (reply, provider_name)."""
    errors = []
    for provider in _get_active_providers():
        try:
            client = OpenAI(base_url=provider["base_url"], api_key=provider["api_key"])
            response = client.chat.completions.create(
                model=provider["model"],
                messages=messages,
                max_tokens=800,
            )
            return response.choices[0].message.content, provider["name"]
        except Exception as e:
            msg = str(e)
            print(f"Provider {provider['name']} failed: {msg}")
            errors.append(f"**{provider['name']}**: {msg[:200]}")
    if not _get_active_providers():
        return "No API keys configured. Add `GROQ_API_KEY` or `GOOGLE_API_KEY` to `.env`.", "none"
    return "All providers failed:\n\n" + "\n\n".join(errors), "none"
