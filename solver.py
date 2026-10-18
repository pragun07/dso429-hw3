"""Rule-based solver for the DSO 429 hourly quiz.

solve(row) returns (letter, value) for a question row, or (None, reason) when
the question doesn't match a known type and needs a human/Claude to work it out.
"""
import re
from datetime import date, timedelta

LETTERS = ["A", "B", "C", "D"]
MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
NUM = r"\$?(-?[\d,]+(?:\.\d+)?)"


def num(s):
    return float(s.replace(",", "").replace("$", ""))


def parse_option(s):
    """Option text -> float (dates -> date). Returns None if unparseable."""
    s = s.strip()
    m = re.fullmatch(r"(\d{1,2}) ([A-Za-z]+) (\d{4})", s)
    if m and m.group(2) in MONTHS:
        return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    m = re.search(r"-?\$?-?[\d,]+(?:\.\d+)?", s)
    if not m:
        return None
    return float(m.group(0).replace("$", "").replace(",", ""))


def _series_next(vals):
    d = [b - a for a, b in zip(vals, vals[1:])]
    if len(set(d)) == 1:
        return vals[-1] + d[0]
    if all(v != 0 for v in vals):
        r = [b / a for a, b in zip(vals, vals[1:])]
        if max(r) - min(r) < 1e-9:
            return vals[-1] * r[0]
    dd = [b - a for a, b in zip(d, d[1:])]
    if len(set(dd)) == 1:  # quadratic
        return vals[-1] + d[-1] + dd[0]
    if len(vals) >= 3 and all(vals[i] == vals[i - 1] + vals[i - 2] for i in range(2, len(vals))):
        return vals[-1] + vals[-2]
    return None


# Each rule: (regex, function(match) -> computed value)
RULES = [
    (r"An invoice for " + NUM + r" is converted to euros at " + NUM + r" euros per US dollar",
     lambda m: num(m[1]) * num(m[2])),
    (r"sells " + NUM + r" units at " + NUM + r" each\. What is total revenue",
     lambda m: num(m[1]) * num(m[2])),
    (r"listed at " + NUM + r" is discounted by " + NUM + r"%\. What is the sale price",
     lambda m: num(m[1]) * (1 - num(m[2]) / 100)),
    (r"investment of " + NUM + r" grows at " + NUM + r"% compounded annually\. What is its value after " + NUM + r" years",
     lambda m: num(m[1]) * (1 + num(m[2]) / 100) ** num(m[3])),
    (r"generates " + NUM + r" in revenue against " + NUM + r" in cost of goods sold\. What is the gross margin",
     lambda m: (num(m[1]) - num(m[2])) / num(m[1]) * 100),
    (r"Fixed costs are " + NUM + r"\. Each unit sells for " + NUM + r" with a variable cost of " + NUM + r"\. How many units must be sold to break even",
     lambda m: num(m[1]) / (num(m[2]) - num(m[3]))),
    (r"revenue moved from " + NUM + r" to " + NUM + r"\. What is the percentage change",
     lambda m: (num(m[2]) - num(m[1])) / num(m[1]) * 100),
    (r"Region A had " + NUM + r" orders averaging " + NUM + r"; region B had " + NUM + r" orders averaging " + NUM + r"\. What is the overall average",
     lambda m: (num(m[1]) * num(m[2]) + num(m[3]) * num(m[4])) / (num(m[1]) + num(m[3]))),
    (r"holds " + NUM + r" billing tickets, " + NUM + r" technical tickets and " + NUM + r" account tickets\. If one is drawn at random, what is the probability it is a (billing|technical|account) ticket",
     lambda m: 100 * num(m[{"billing": 1, "technical": 2, "account": 3}[m[4]]])
     / (num(m[1]) + num(m[2]) + num(m[3]))),
    (r"A " + NUM + r" marketing budget is split between two channels in the ratio " + NUM + r":" + NUM + r"\. What does the (first|second) channel receive",
     lambda m: num(m[1]) * num(m[2 if m[4] == "first" else 3]) / (num(m[2]) + num(m[3]))),
    (r"series runs ([-\d., ]+?)\. If the pattern continues, what is the next value",
     lambda m: _series_next([num(x) for x in m[1].split(", ")])),
    (r"signed on (\d{1,2}) ([A-Za-z]+) (\d{4}) has a " + NUM + r"-day settlement period",
     lambda m: date(int(m[3]), MONTHS[m[2]], int(m[1])) + timedelta(days=int(num(m[4])))),
]


def compute(text):
    for pat, fn in RULES:
        m = re.search(pat, text)
        if m:
            try:
                return fn(m)
            except Exception:
                return None
    return None


def solve(row):
    """row: dict with question_text and option_a..option_d."""
    value = compute(row["question_text"])
    if value is None:
        return None, "unrecognized question"
    opts = [parse_option(row["option_" + l.lower()]) for l in LETTERS]
    if isinstance(value, date):
        hits = [i for i, o in enumerate(opts) if o == value]
        if len(hits) == 1:
            return LETTERS[hits[0]], value
        return None, f"computed date {value} not among options"
    if "break even" in row["question_text"]:
        value = float(round(value))  # "to the nearest unit"
    cands = [(abs(o - value), i) for i, o in enumerate(opts) if isinstance(o, float)]
    if not cands:
        return None, "options unparseable"
    cands.sort()
    best_err, best = cands[0]
    tol = max(0.011, abs(value) * 0.0005)
    if "%" in row["option_" + LETTERS[best].lower()]:
        tol = max(tol, 0.051)
    # Flag if nothing is close, or the runner-up is essentially as close (a tie).
    if best_err > tol or (len(cands) > 1 and cands[1][0] - best_err < 1e-6):
        return None, f"computed {value:.4f}; no unique close option"
    return LETTERS[best], value
