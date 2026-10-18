"""Fetch the quiz, answer every round without an answer file, validate, commit and push.

Usage:
  python run.py                      # answer all missing rounds, commit, push
  python run.py --no-push            # same, but don't commit/push
  python run.py --set 74 q5=C q9=A   # override answers for a flagged round
                                     # (refused if the round is 3+ hours old)

Questions the solver can't answer are filled with a best guess so the round is
submitted on time, listed in flagged.json, and reported on stdout. A Claude
session then works each one out in code and corrects it with --set.
"""
import csv
import io
import json
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from solver import LETTERS, parse_option, solve

ROOT = Path(__file__).resolve().parent
ANSWERS = ROOT / "answers"
FLAGGED = ROOT / "flagged.json"
QUESTIONS_URL = "https://raw.githubusercontent.com/FaisalXL/dso429-hourly-quiz/main/questions.csv"
# Stop editing a round a bit before the 3-hour mark so a slow push still lands in time.
EDIT_WINDOW = timedelta(hours=2, minutes=50)


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True)


def fetch(url):
    """curl first (system CA store), urllib as a fallback; retries on failure."""
    url = f"{url}?t={int(time.time())}"  # bust CDN cache
    for attempt in range(5):
        res = subprocess.run(["curl", "-sSfL", "--max-time", "30", url], capture_output=True, text=True)
        if res.returncode == 0:
            return res.stdout
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            print(f"fetch failed ({res.stderr.strip()} / {e}); retrying", file=sys.stderr)
            time.sleep(10 * (attempt + 1))
    sys.exit(f"could not fetch {url}")


def fetch_rounds():
    text = fetch(QUESTIONS_URL)
    rounds = defaultdict(list)
    for row in csv.DictReader(io.StringIO(text)):
        rounds[int(row["round_id"])].append(row)
    return rounds


def released_at(rows):
    return datetime.fromisoformat(rows[0]["released_at"].replace("Z", "+00:00"))


def path_for(rid):
    return ANSWERS / f"round-{rid:04d}.json"


def best_guess(row):
    """Fallback for flagged questions: nearest numeric option, else A."""
    from solver import compute
    value = compute(row["question_text"])
    opts = [parse_option(row["option_" + l.lower()]) for l in LETTERS]
    if isinstance(value, float):
        nums = [(abs(o - value), i) for i, o in enumerate(opts) if isinstance(o, float)]
        if nums:
            return LETTERS[min(nums)[1]]
    return "A"


def validate(path, rid):
    data = json.loads(path.read_text())
    assert data["round_id"] == rid, f"{path.name}: round_id mismatch"
    ans = data["answers"]
    assert set(ans) == {f"q{i}" for i in range(1, 26)}, f"{path.name}: need q1..q25"
    assert all(v in LETTERS for v in ans.values()), f"{path.name}: values must be A-D"


def write_round(rid, answers):
    path = path_for(rid)
    ordered = {f"q{i}": answers[f"q{i}"] for i in range(1, 26)}
    path.write_text(json.dumps({"round_id": rid, "answers": ordered}) + "\n")
    validate(path, rid)
    return path


def commit_and_push(paths, message):
    git("add", *[str(p) for p in paths])
    if not git("diff", "--cached", "--quiet", check=False).returncode:
        print("nothing to commit")
        return
    git("commit", "-m", message)
    for attempt in range(5):
        res = git("push", "origin", "HEAD:main", check=False)
        if res.returncode == 0:
            print("pushed:", message)
            return
        print(f"push failed: {res.stderr.strip()}; pull --rebase and retry", file=sys.stderr)
        git("pull", "--rebase", "origin", "main", check=False)
        time.sleep(5 * (attempt + 1))
    sys.exit("push failed after retries")


def answer_missing(rounds, push):
    ANSWERS.mkdir(exist_ok=True)
    written, flagged = [], {}
    # Newest first, so the rounds with full credit still available go out first.
    for rid in sorted(rounds, reverse=True):
        rows = rounds[rid]
        if path_for(rid).exists():
            continue  # never overwrite an existing round here
        if len(rows) != 25:
            print(f"round {rid}: only {len(rows)} questions so far, skipping")
            continue
        answers = {}
        for row in rows:
            q = "q" + row["q_index"]
            letter, info = solve(row)
            if letter is None:
                letter = best_guess(row)
                flagged.setdefault(str(rid), []).append({
                    "q": q, "guess": letter, "reason": info,
                    "question_text": row["question_text"],
                    "options": {l: row["option_" + l.lower()] for l in LETTERS},
                })
            answers[q] = letter
        written.append(write_round(rid, answers))
        print(f"round {rid}: written" + (f" ({len(flagged.get(str(rid), []))} flagged)" if str(rid) in flagged else ""))
    if flagged:
        FLAGGED.write_text(json.dumps(flagged, indent=2) + "\n")
        print("FLAGGED QUESTIONS (best guess submitted; fix with --set):")
        print(json.dumps(flagged, indent=2))
    if written and push:
        ids = sorted(int(p.stem.split("-")[1]) for p in written)
        commit_and_push(written, f"Answer rounds {', '.join(map(str, ids))}")
    if not written:
        print("no new rounds to answer")


def set_answers(rounds, rid, pairs, push):
    path = path_for(rid)
    if rid not in rounds:
        sys.exit(f"round {rid} not released")
    age = datetime.now(timezone.utc) - released_at(rounds[rid])
    if path.exists() and age > EDIT_WINDOW:
        sys.exit(f"round {rid} is {age} old; refusing to overwrite (locked after 3h)")
    answers = json.loads(path.read_text())["answers"] if path.exists() else {}
    for pair in pairs:
        q, letter = pair.split("=")
        assert q in {f"q{i}" for i in range(1, 26)} and letter in LETTERS, pair
        answers[q] = letter
    write_round(rid, answers)
    if FLAGGED.exists():
        flagged = json.loads(FLAGGED.read_text())
        done = {p.split("=")[0] for p in pairs}
        flagged[str(rid)] = [f for f in flagged.get(str(rid), []) if f["q"] not in done]
        flagged = {k: v for k, v in flagged.items() if v}
        FLAGGED.write_text(json.dumps(flagged, indent=2) + "\n") if flagged else FLAGGED.unlink()
    if push:
        commit_and_push([path], f"Correct flagged answers in round {rid}")


def main():
    args = sys.argv[1:]
    push = "--no-push" not in args
    args = [a for a in args if a != "--no-push"]
    if push:
        git("pull", "--rebase", "origin", "main", check=False)
    rounds = fetch_rounds()
    if args and args[0] == "--set":
        set_answers(rounds, int(args[1]), args[2:], push)
    else:
        answer_missing(rounds, push)
    for p in sorted(ANSWERS.glob("round-*.json")):
        validate(p, int(p.stem.split("-")[1]))
    print("all answer files valid")


if __name__ == "__main__":
    main()
