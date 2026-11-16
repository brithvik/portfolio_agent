"""Citations: the code writes every number in an answer, so the model can't get one wrong.

Before a tool result goes to the model, every number in it gets a handle:

    {"top_3_positions": {"weight_pct": 60.67}}   becomes   {"top_3_positions": {"weight_pct": "60.67% [F7]"}}

The model is told to write the handle ([F7]) wherever it wants that figure. When the answer
comes back, the code swaps each handle for the exact value, formatted consistently. So a
figure in the final answer was never typed by the model: it can't be mistyped, misrounded,
or the result of the model's own arithmetic.

Two things follow from that:
- Every cited figure is traceable. The run's record lists which field each handle pointed
  to (for example "get_portfolio.totals.top_3_positions.weight_pct"), so a reviewer can see
  not just that a number is real, but which number it is.
- Numbers the model types anyway (it may round: "about 60%") still go through the existing
  grounding check. A handle the model invented, or a typed number that isn't in the data,
  triggers one repair attempt and is then reported, never silently passed.
"""
import re

from grounding import _kind_of_field

HANDLE = re.compile(r"\[F(\d+)\]")


def format_value(value, kind):
    """One consistent way to write each kind of figure."""
    if kind == "percent":
        return f"{value:,.2f}".rstrip("0").rstrip(".") + "%"
    if kind == "dollars":
        return f"{'-' if value < 0 else ''}${abs(value):,.2f}"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


class Citations:
    """The handles issued during one agent run."""

    def __init__(self):
        self.facts = {}        # "F7" -> {"path", "value", "kind", "text"}

    def tag(self, tool, result):
        """A copy of a tool result with every number replaced by 'formatted [handle]'."""
        return self._walk(result, tool, "plain")

    def _walk(self, data, path, kind):
        if isinstance(data, dict):
            return {key: self._walk(value, f"{path}.{key}", _kind_of_field(str(key))) for key, value in data.items()}
        if isinstance(data, (list, tuple)):
            return [self._walk(value, f"{path}[{self._label(value, i)}]", kind) for i, value in enumerate(data)]
        if isinstance(data, bool) or not isinstance(data, (int, float)):
            return data
        handle = f"F{len(self.facts) + 1}"
        text = format_value(data, kind)
        self.facts[handle] = {"path": path, "value": data, "kind": kind, "text": text}
        return f"{text} [{handle}]"

    @staticmethod
    def _label(item, index):
        """Name list entries by what they are (a symbol, a sector, a date) when they say."""
        if isinstance(item, dict):
            for key in ("symbol", "sector", "group", "date", "rule"):
                if isinstance(item.get(key), str):
                    return item[key]
        return str(index)

    def resolve_argument(self, value):
        """If the model passed a bare handle as a tool argument, give the tool the real number."""
        if isinstance(value, str):
            match = HANDLE.fullmatch(value.strip())
            if match and f"F{match.group(1)}" in self.facts:
                return self.facts[f"F{match.group(1)}"]["value"]
        return value

    def render(self, text):
        """Swap handles for their values. Returns (final text, handles used, handles that don't exist)."""
        used, unknown = [], []

        def swap(match):
            handle = f"F{match.group(1)}"
            fact = self.facts.get(handle)
            if not fact:
                unknown.append(handle)
                return match.group()
            used.append(handle)
            return fact["text"]

        # The model sometimes copies the value as well as the handle ("60.67% [F7]"): keep one.
        for handle, fact in self.facts.items():
            text = re.sub(rf"{re.escape(fact['text'])}\s*\(?\[{handle}\]\)?", f"[{handle}]", text)
        return HANDLE.sub(swap, text), used, unknown

    def paths(self, handles):
        return [self.facts[h]["path"] for h in dict.fromkeys(handles)]
