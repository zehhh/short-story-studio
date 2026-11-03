"""A/B 对比：两篇成稿的确定性指标（零 token）。

用法：python -m tools.ab_compare <项目目录A> <项目目录B> [--label A --label B]

指标口径全部复用 studio.audit / studio.langqa / studio.language，
不新造判据（决策 30：收口用哪套判据，对比就必须用哪一套）。
"""
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio import audit, langqa, language  # noqa: E402

_ONE_X = re.compile(r"一[下眼抖颤步颗片寸种声口只]")


def _load(proj):
    p = os.path.join(proj, "final", "成稿.md")
    if not os.path.exists(p):
        p = os.path.join(proj, "final", "成稿.txt")
    text = io.open(p, encoding="utf-8").read()
    rep = {}
    rp = os.path.join(proj, "report.json")
    if os.path.exists(rp):
        rep = json.load(io.open(rp, encoding="utf-8"))
    return text, rep


def metrics(proj):
    text, rep = _load(proj)
    secs = audit.sections(text)
    body = "\n\n".join(s[1] for s in secs) if secs else text
    lens = [len(re.sub(r"\s+", "", s[1])) for s in secs]
    words = len(re.sub(r"\s+", "", body))
    k = max(1.0, words / 1000.0)
    rules = langqa.load_rules()
    sc = langqa.scan(body, rules)
    from collections import Counter
    labs = Counter(h["label"] for h in sc["hits"])
    ai = sum(v for k2, v in labs.items() if "AI" in k2 or "黑名单" in k2)
    d = language.defect_score(body)
    return {
        "字数": words,
        "节数": len(secs),
        "节长极差": round((max(lens) / max(1, min(lens))), 2) if lens else 0,
        "语言病分": d["score"],
        "碎句": d["frag"],
        "欧化": d["euro"],
        "冗余": d["redundancy"],
        "详略": d["detail"],
        "一X/千字": round(len(_ONE_X.findall(body)) / k * 1000, 1),
        "对白占比": round(language.dialog_ratio(body), 3),
        "AI口癖条数": ai,
        "终审分": (rep.get("final_review") or {}).get("score"),
        "high": (rep.get("final_review") or {}).get("high_count"),
        "review_score": rep.get("review_score"),
    }


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    a, b = argv[1], argv[2]
    ma, mb = metrics(a), metrics(b)
    keys = list(ma.keys())
    print(f"{'指标':<12}{'A':>10}{'B':>10}{'变化':>12}")
    print("-" * 46)
    for key in keys:
        va, vb = ma[key], mb[key]
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and va:
            delta = f"{(vb - va):+.2f}"
        else:
            delta = "-"
        print(f"{key:<12}{str(va):>10}{str(vb):>10}{delta:>12}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
