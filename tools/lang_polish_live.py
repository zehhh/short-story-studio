"""对已有成稿单独跑一次「全稿语言打磨」，用来度量语言质量改动是否真的有效。

为什么要这个脚本：整篇 `run` 要 30 分钟 + 十几万 token，而语言打磨只占其中
很小一段。调语言质量时如果每次都跑全流程，成本高到无法迭代。

用法：
    python tools/lang_polish_live.py <项目目录> [--rounds 2] [--dry]

输出：`out/lang_polish_live/<项目名>/` 下的 before.txt / after.txt / report.json
      不覆盖原项目任何文件。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from studio import langqa, language  # noqa: E402
from studio.config import load_config  # noqa: E402
from studio.store import Project  # noqa: E402
from studio.util import count_words_zh  # noqa: E402


def _profile(text: str, rules: dict) -> dict:
    sc = language.defect_score(text, rules)
    out = {k: v for k, v in sc.items() if k != "hits"}
    out["words"] = count_words_zh(text)
    out["kinds"] = sorted({h["kind"] for h in sc.get("hits", [])})
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    proj_dir = Path(args[0]).resolve()
    rounds = 2
    for a in sys.argv[1:]:
        if a.startswith("--rounds="):
            rounds = int(a.split("=", 1)[1])
    dry = "--dry" in sys.argv

    project = Project(proj_dir)
    full = (proj_dir / "final" / "成稿.txt").read_text(encoding="utf-8")
    rules = langqa.load_rules()
    before = _profile(full, rules)
    print("[before]", json.dumps(before, ensure_ascii=False))

    if dry:
        print("[dry] 只体检，不改写")
        return 0

    cfg = load_config()
    from studio.llm import get_llm

    llm = get_llm(cfg)
    concept = project.load_json("concept.json") or {}
    direction = concept.get("direction", "") or ""
    try:
        from studio import kb

        style_sample = kb.style_samples(cfg, direction=direction, n=3, max_chars=900)
    except Exception:  # noqa: BLE001
        style_sample = ""
    try:
        from studio import pipeline as P

        genre_guide = P._genre_guide(cfg, direction) if hasattr(P, "_genre_guide") else ""
        kbcard = P._lang_kbcard(cfg, direction, budget=900)
    except Exception:  # noqa: BLE001
        genre_guide, kbcard = "", ""

    def log(msg: str) -> None:
        print(msg, flush=True)

    new_full, rep = language.polish(
        cfg, llm, project, full, rounds=rounds,
        style_sample=style_sample, genre_guide=genre_guide,
        kbcard=kbcard, log=log,
    )
    after = _profile(new_full, rules)
    print("[after]", json.dumps(after, ensure_ascii=False))

    out = ROOT / "out" / "lang_polish_live" / proj_dir.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "before.txt").write_text(full, encoding="utf-8")
    (out / "after.txt").write_text(new_full, encoding="utf-8")
    (out / "report.json").write_text(
        json.dumps({"before": before, "after": after, "polish": rep},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
