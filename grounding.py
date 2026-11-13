"""Grounding check: is every number in an answer backed by the data the agent actually saw?

LLMs sometimes state numbers that look right but aren't in their data, usually because they did
arithmetic themselves. This module finds every number in an answer (dollars, percentages, plain
numbers) and checks it against every number in the tool results from that same conversation.

Pure code, no AI: the same answer always gets the same verdict.
"""
import re
from dataclasses import dataclass

# Things that look like numbers but aren't claims about the portfolio
_NOISE = [
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?",   # 2026-11-11T14:50:08+00:00
    r"\d{4}-\d{2}-\d{2}",                                                         # 2026-11-11
    r"\b\d{1,2}:\d{2}(:\d{2})?\s?(AM|PM|am|pm|UTC|ET|EST|EDT)?",                  # 9:54 AM
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(st|nd|rd|th)?(,?\s+\d{4})?",
    r"(?m)^\s*\d+[.)]\s",                                                         # "1. " list markers
    r"\b(run|proposal|step|Q[1-4]|H[12]|FY)\s?#?\d+\b",                           # run 3, Q4
    r"\b\d+-(day|week|month|year|hour|minute)s?\b",                               # 7-day
    r"\bS&P\s?500\b",
]
_NUMBER = re.compile(
    r"(?<![\w.,])(?P<sign>[-+\u2212]?)(?P<dollar>\$?)\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s?(?P<suffix>%|percent\b|[KkMmBb]\b|thousand\b|million\b|billion\b)?"
    r"(?P<unit>\s?(dollars?|USD)\b)?")
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}


@dataclass(frozen=True)
class Number:
    value: float        # as written, before any K/M scaling
    decimals: int       # how many decimal places were written
    scale: float        # 1, 1e3, 1e6 ...
    kind: str           # 'dollars', 'percent', or 'plain'
    raw: str
    context: str


def extract_numbers(text):
    """Every number in the text that makes a claim, with how precisely it was written."""
    cleaned = text
    for pattern in _NOISE:
        cleaned = re.sub(pattern, lambda m: " " * len(m.group()), cleaned)
    found = []
    for m in _NUMBER.finditer(cleaned):
        digits = m.group("num").replace(",", "")
        suffix = (m.group("suffix") or "").lower()
        kind = ("percent" if suffix in ("%", "percent")
                else "dollars" if m.group("dollar") or m.group("unit") else "plain")
        start, end = max(0, m.start() - 45), min(len(text), m.end() + 25)
        found.append(Number(
            value=float(digits), decimals=len(digits.split(".")[1]) if "." in digits else 0,
            scale=_SCALE.get(suffix, 1.0), kind=kind, raw=m.group().strip(),
            context=" ".join(text[start:end].split())))
    return found


def _kind_of_field(key):
    """What a tool-result field measures, judged by its name."""
    key = key.lower()
    if any(word in key for word in ("pct", "percent", "weight", "rate")):
        return "percent"
    if any(word in key for word in ("value", "cash", "price", "cost", "_pl", "dollars", "notional",
                                    "equity", "amount", "buying_power")):
        return "dollars"
    return "plain"


def numbers_in(data, kind="plain"):
    """Every number anywhere in a tool result, as (value, kind) pairs.

    The kind matters: a claim of "47%" has to match a percentage in the data. Without this, any
    unrelated figure that happens to round to 47 (a share count, a price) would vouch for it.
    """
    out = []
    if isinstance(data, bool) or data is None:
        return out
    if isinstance(data, (int, float)):
        out.append((abs(float(data)), kind))
    elif isinstance(data, str):
        out.extend((n.value * n.scale, n.kind) for n in extract_numbers(data))
    elif isinstance(data, dict):
        for key, value in data.items():
            out.extend(numbers_in(value, _kind_of_field(str(key))))
    elif isinstance(data, (list, tuple)):
        for value in data:
            out.extend(numbers_in(value, kind))
    return out


def matches(number, source_value):
    """Does a written number agree with a source value, allowing for normal rounding?

    "17.9%" matches 17.86, "$17.8K" matches 17,842.85, "nearly 18%" matches 17.86.
    "62.67%" does not match 60.67.
    """
    scaled = abs(source_value) / number.scale
    if abs(round(scaled, number.decimals) - number.value) < 1e-9:
        return True
    # rounding half-way cases and float noise
    return abs(scaled - number.value) <= 0.5 * 10 ** -number.decimals + 1e-9 if number.decimals else False


def _compatible(claim_kind, source_kind):
    """A dollar claim needs a dollar figure behind it; a percent claim needs a percentage.
    A bare number ("up 2.3") could be either, so it may match anything."""
    return claim_kind == "plain" or source_kind == "any" or claim_kind == source_kind


def is_trivial(number):
    """Small counts ("top 3", "two stocks") and bare years aren't data claims worth flagging."""
    if number.kind != "plain" or number.scale != 1:
        return False
    return (number.decimals == 0 and number.value <= 12) or 1900 <= number.value <= 2100


def check_grounding(answer, sources, question=""):
    """Compare an answer against its sources (tool results, plus the user's own question).

    Returns a dict with every cited number sorted into grounded or ungrounded.
    """
    # Numbers the user typed can back a claim of any kind ("1500 dollars" -> "$1,500")
    available = numbers_in(sources) + [(value, "any") for value, _ in numbers_in(question)]
    grounded, ungrounded = [], []
    for number in extract_numbers(answer):
        if is_trivial(number):
            continue  # "top 3", "9 positions": not a data claim, so not counted either way
        if any(matches(number, value) and _compatible(number.kind, kind) for value, kind in available):
            grounded.append(number)
        else:
            ungrounded.append(number)
    cited = len(grounded) + len(ungrounded)
    return {
        "numbers_cited": cited,
        "numbers_grounded": len(grounded),
        "ungrounded": [{"number": n.raw, "context": n.context} for n in ungrounded],
        "grounded_rate": round(len(grounded) / cited * 100, 1) if cited else 100.0,
    }


def contains_number(answer, expected):
    """Does the answer state this value (to whatever precision it chose to write)?"""
    return any(matches(n, expected) for n in extract_numbers(answer))
