"""
LeetCode Daily Auto-Solver
---------------------------
Every POLL_INTERVAL_MINUTES minutes:
  1. Check if you've already submitted anything today
  2. If not → fetch today's Daily Challenge question
  3. Send it to Gemini to solve in your preferred language
  4. Submit Gemini's solution to LeetCode
  5. If it fails (wrong answer etc.) → retry with a fresh Gemini attempt
"""

import os
import re
import json
import time
import logging
import datetime
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("tracker.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
USERNAME        = os.getenv("LEETCODE_USERNAME", "")
LEETCODE_COOKIE = os.getenv("LEETCODE_COOKIE", "")
CSRF_TOKEN      = os.getenv("LEETCODE_CSRF", "")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")
LANGUAGE        = os.getenv("LANGUAGE", "python3")          # leetcode lang slug
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL_MINUTES", "30")) * 60
MAX_RETRIES     = int(os.getenv("MAX_RETRIES", "3"))

GRAPHQL_URL = "https://leetcode.com/graphql"
SUBMIT_URL  = "https://leetcode.com/problems/{slug}/submit/"
CHECK_URL   = "https://leetcode.com/submissions/detail/{id}/check/"
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "deepseek-r1-distill-llama-70b",
    "qwen-qwq-32b",
]
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

HEADERS_BASE = {
    "Content-Type": "application/json",
    "Referer": "https://leetcode.com",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

STATE_FILE = Path("state.json")

LANG_DISPLAY = {
    "python3":    "Python 3",
    "python":     "Python 2",
    "cpp":        "C++",
    "java":       "Java",
    "c":          "C",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "go":         "Go",
    "rust":       "Rust",
    "kotlin":     "Kotlin",
    "swift":      "Swift",
}


# ── State helpers ─────────────────────────────────────────────────────────────

def save_state(data: dict):
    STATE_FILE.write_text(json.dumps(data, indent=2))


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def already_solved_today() -> bool:
    return load_state().get("solved_date") == datetime.date.today().isoformat()


# ── LeetCode API ──────────────────────────────────────────────────────────────

def _gql(query: str, variables: dict = None) -> dict:
    headers = {
        **HEADERS_BASE,
        "Cookie":      LEETCODE_COOKIE,
        "x-csrftoken": CSRF_TOKEN,
    }
    r = requests.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables or {}},
        headers=headers,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


RECENT_SUBMISSIONS_QUERY = """
query recentSubmissions($username: String!, $limit: Int!) {
  recentSubmissionList(username: $username, limit: $limit) {
    id
    title
    titleSlug
    timestamp
    statusDisplay
    lang
  }
}
"""

DAILY_CHALLENGE_QUERY = """
query dailyChallenge {
  activeDailyCodingChallengeQuestion {
    date
    question {
      questionId
      titleSlug
      title
      difficulty
      content
      codeSnippets {
        lang
        langSlug
        code
      }
    }
  }
}
"""


def get_recent_submissions(limit: int = 20) -> list[dict]:
    data = _gql(RECENT_SUBMISSIONS_QUERY, {"username": USERNAME, "limit": limit})
    return data.get("recentSubmissionList") or []


def has_submitted_daily_today(submissions: list[dict], daily_slug: str) -> bool:
    now      = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff   = int(midnight.timestamp())
    for s in submissions:
        if int(s["timestamp"]) >= cutoff and s["titleSlug"] == daily_slug:
            log.info(
                "Found today's daily submission: [%s] %s (%s)",
                s["statusDisplay"], s["title"], s["lang"],
            )
            return True
    return False


def get_daily_question() -> dict:
    data      = _gql(DAILY_CHALLENGE_QUERY)
    challenge = data.get("activeDailyCodingChallengeQuestion", {})
    question  = challenge.get("question", {})
    if not question:
        raise RuntimeError("Could not fetch daily challenge question.")
    log.info(
        "Daily question: #%s  %s  [%s]",
        question["questionId"], question["title"], question["difficulty"],
    )
    return question


def get_code_snippet(question: dict, lang_slug: str) -> str:
    for snippet in question.get("codeSnippets", []):
        if snippet["langSlug"] == lang_slug:
            return snippet["code"]
    snippets = question.get("codeSnippets", [])
    if snippets:
        log.warning("'%s' snippet not found, using '%s'", lang_slug, snippets[0]["langSlug"])
        return snippets[0]["code"]
    return ""


# ── Gemini solver ─────────────────────────────────────────────────────────────

def clean_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;",   "<", text)
    text = re.sub(r"&gt;",   ">", text)
    text = re.sub(r"&amp;",  "&", text)
    text = re.sub(r"\s+",    " ", text)
    return text.strip()


def ask_groq(question: dict, lang_slug: str, attempt: int = 1) -> str:
    lang_name   = LANG_DISPLAY.get(lang_slug, lang_slug)
    starter     = get_code_snippet(question, lang_slug)
    description = clean_html(question.get("content", ""))
    title       = question["title"]
    difficulty  = question["difficulty"]

    retry_note = ""
    if attempt > 1:
        retry_note = (
            f"\nNOTE: This is attempt {attempt}. "
            "A previous solution was rejected. "
            "Use a completely different algorithm or approach.\n"
        )

    prompt = f"""You are an expert competitive programmer. Solve this LeetCode problem.
{retry_note}
Problem: {title} (Difficulty: {difficulty})

{description}

Starter code ({lang_name}):
{starter}

Instructions:
- Return ONLY the raw solution code. No explanation, no markdown fences, no backticks.
- Fill in the starter code above. Do not rewrite the class/function signature unnecessarily.
- Handle all edge cases. The solution must pass all LeetCode test cases.
- Do not add print statements or debug output.
"""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json",
    }

    # Try each model in order, fall through on rate limit or error
    for i, model in enumerate(GROQ_MODELS):
        payload = {
            "model":    model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1 if attempt == 1 else 0.6,
        }
        log.info("Asking Groq model=%s (attempt %d, lang=%s) ...", model, attempt, lang_slug)
        try:
            r = requests.post(GROQ_URL, json=payload, headers=headers, timeout=60)
            if r.status_code == 429:
                wait = 30 * (i + 1)
                log.warning("Rate limited on %s - waiting %ds ...", model, wait)
                time.sleep(wait)
                continue
            if r.status_code == 404:
                log.warning("Model %s not found - skipping ...", model)
                continue
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            log.warning("HTTP error on %s: %s - skipping ...", model, e)
            continue

        resp = r.json()
        try:
            raw = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected Groq response: {e}  full={resp}")

        # Strip accidental markdown fences
        raw = re.sub(r"^```[\w]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$",       "", raw.strip())
        log.info("Groq (%s) returned %d chars.", model, len(raw))
        return raw.strip()

    raise RuntimeError("All Groq models rate-limited. Will retry next poll cycle.")

# ── LeetCode submission ───────────────────────────────────────────────────────

def submit_solution(question: dict, code: str, lang_slug: str) -> dict:
    slug  = question["titleSlug"]
    q_id  = question["questionId"]
    url   = SUBMIT_URL.format(slug=slug)
    headers = {
        **HEADERS_BASE,
        "Cookie":      LEETCODE_COOKIE,
        "x-csrftoken": CSRF_TOKEN,
        "Referer":     f"https://leetcode.com/problems/{slug}/",
    }

    log.info("Submitting '%s' in %s …", question["title"], lang_slug)
    r = requests.post(
        url,
        json={"lang": lang_slug, "question_id": q_id, "typed_code": code},
        headers=headers,
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Submit failed: HTTP {r.status_code}  {r.text[:300]}")

    submission_id = r.json().get("submission_id")
    if not submission_id:
        raise RuntimeError(f"No submission_id in response: {r.text[:300]}")

    log.info("Queued (id=%s). Polling for result …", submission_id)
    return poll_result(submission_id, headers)


def poll_result(submission_id: int, headers: dict, max_wait: int = 30) -> dict:
    check_url = CHECK_URL.format(id=submission_id)
    for i in range(max_wait):
        time.sleep(2)
        r = requests.get(check_url, headers=headers, timeout=15)
        if r.status_code != 200:
            log.warning("Poll %d: HTTP %d", i + 1, r.status_code)
            continue
        data  = r.json()
        state = data.get("state", "")
        if state == "SUCCESS":
            status = data.get("status_msg", "Unknown")
            log.info(
                "Verdict: %s  |  runtime: %s  |  memory: %s",
                status,
                data.get("status_runtime", "?"),
                data.get("status_memory", "?"),
            )
            return data
        log.info("Poll %d: state=%s …", i + 1, state)
    raise RuntimeError(f"Timed out waiting for result (id={submission_id})")


# ── Main flow ─────────────────────────────────────────────────────────────────

def solve_and_submit(question: dict):
    lang_slug = LANGUAGE

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            code   = ask_groq(question, lang_slug, attempt=attempt)
            result = submit_solution(question, code, lang_slug)
        except Exception as e:
            log.error("Attempt %d error: %s", attempt, e)
            time.sleep(5)
            continue

        status = result.get("status_msg", "")

        if status == "Accepted":
            log.info("✓  ACCEPTED on attempt %d  —  %s", attempt, question["title"])
            save_state({
                "solved_date":    datetime.date.today().isoformat(),
                "question_title": question["title"],
                "language":       lang_slug,
                "attempts":       attempt,
                "status":         "Accepted",
            })
            return True

        log.warning("✗  Attempt %d: %s", attempt, status)
        if attempt < MAX_RETRIES:
            log.info("Retrying with a different approach …")
            time.sleep(3)

    # Even if all attempts failed, mark today so we don't spam LeetCode
    save_state({
        "solved_date":    datetime.date.today().isoformat(),
        "question_title": question["title"],
        "language":       lang_slug,
        "attempts":       MAX_RETRIES,
        "status":         "Failed",
    })
    log.error("All %d attempts failed for '%s'.", MAX_RETRIES, question["title"])
    return False


def check_and_act():
    now = datetime.datetime.now()
    log.info("──── Checking at %s ────", now.strftime("%H:%M"))

    # Fetch daily question first so we can check against its slug specifically
    try:
        question = get_daily_question()
    except Exception as e:
        log.error("Failed to fetch daily question: %s", e)
        return

    try:
        submissions = get_recent_submissions(limit=20)
    except Exception as e:
        log.error("Failed to fetch submissions: %s", e)
        return

    if has_submitted_daily_today(submissions, question["titleSlug"]):
        log.info("✓  Daily question already submitted today. Nothing to do.")
        return

    if already_solved_today():
        log.info("Auto-solve already attempted today (see tracker.log).")
        return

    log.warning(
        "✗  Daily question '%s' not submitted yet — starting Gemini solver …",
        question["title"],
    )
    try:
        solve_and_submit(question)
    except Exception as e:
        log.exception("Pipeline error: %s", e)


def run():
    missing = [
        v for v, val in [
            ("LEETCODE_USERNAME",  USERNAME),
            ("LEETCODE_COOKIE",    LEETCODE_COOKIE),
            ("LEETCODE_CSRF",      CSRF_TOKEN),
            ("GROQ_API_KEY",      GROQ_API_KEY),
        ] if not val
    ]
    if missing:
        raise SystemExit(f"ERROR: Missing in .env: {', '.join(missing)}")

    log.info(
        "LeetCode Auto-Solver started  |  user=%s  lang=%s  poll=%dm  retries=%d",
        USERNAME, LANGUAGE, POLL_INTERVAL // 60, MAX_RETRIES,
    )

    while True:
        try:
            check_and_act()
        except Exception as e:
            log.exception("Unexpected error: %s", e)
        log.info("Sleeping %d minutes …\n", POLL_INTERVAL // 60)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()