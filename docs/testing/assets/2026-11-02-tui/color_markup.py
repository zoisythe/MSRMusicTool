import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import textual.color as color
from textual.content import Content

original = color.get_suggestion
counts = Counter()


def counted(word: str, possible_words: Sequence[str]) -> str | None:
    counts[word] += 1
    return original(word, possible_words)


suggestion_patch = patch.object(color, "get_suggestion", counted)
suggestion_patch.start()
results = []
try:
    for name in ("grey50", "#808080", "gray", "green"):
        counts.clear()
        before = perf_counter()
        for i in range(604):
            Content.from_markup(f"[{name}]○[/{name}]  专辑 {i}")
        results.append(
            {
                "color": name,
                "labels": 604,
                "ms": round((perf_counter() - before) * 1000, 2),
                "suggestions": dict(counts),
            }
        )
finally:
    suggestion_patch.stop()
text = json.dumps(results, ensure_ascii=False, indent=2)
print(text)
Path(__file__).with_name("color-markup.json").write_text(text, encoding="utf-8")
