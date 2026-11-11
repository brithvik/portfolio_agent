"""List the Gemini models your API key can use, then test one.

  python check_models.py                    # list text models
  python check_models.py gemini-x-flash     # also send that model a test message

Model names change over time (old ones get retired), so the agent reads its model
from GEMINI_MODEL in .env instead of hard-coding one.
"""
import os
import sys
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

load_dotenv()
client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    # Fail fast with the real error instead of silently retrying for minutes
    http_options=types.HttpOptions(
        timeout=30_000,  # milliseconds
        retry_options=types.HttpRetryOptions(attempts=1),
    ),
)

if len(sys.argv) == 1:
    print("Text models your key can use:")
    for m in client.models.list():
        if "generateContent" in (m.supported_actions or []):
            print(f"  {m.name.removeprefix('models/'):<40} {m.display_name or ''}")
    raise SystemExit

model = sys.argv[1]
print(f"Testing {model} (30 second limit)...")
started = time.time()
try:
    reply = client.models.generate_content(
        model=model, contents="Reply with exactly: the portfolio agent can reach me.")
    print(f"Reply in {time.time() - started:.1f}s: {reply.text}")
except errors.APIError as e:
    print(f"Error {e.code}: {e.message}")
    if e.code == 429:
        print("Quota or rate limit: this model may have no free-tier quota on your key. Try another.")
    elif e.code == 404:
        print("Not found: this model name isn't callable (possibly retired). Try another.")
except Exception as e:
    print(f"{type(e).__name__}: {e}")