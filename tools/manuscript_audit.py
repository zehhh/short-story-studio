# -*- coding: utf-8 -*-
"""成稿体检：拿一份已完成的成稿，按短篇网文市场口径打分（离线、确定性、零 token）。

为什么单独做这个工具：
    流水线自己的 report.json 只能告诉你"门禁怎么记的"，不能回答"这篇能不能投"。
    上线前必须有一把**与流水线无关的尺子**，否则改提示词、改知识库之后无法判断是变好还是变坏。
    这个工具就是那把尺子：同一份稿子，任何时候跑出来的分必须一样。

用法：
    python tools/manuscript_audit.py <项目目录|成稿.txt> [--json]

判据分五组（阈值来自 104 篇历史成稿的分布标定，见 docs/dev/DECISIONS.md）：
    A 结构：字数、节数、节长极差、导语
    B 语言：碎句 frag / 同主语短句链 chain / 语序 order / 欧化 euro / 冗余 redundancy
    C 节奏：对话占比、连续铺陈段
    D 规范：引号统一、禁用道具、思考泄漏、节头编号
    E 门禁：report.json 里的主编分、读者分、平台检查
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from studio import audit as _audit  # noqa: E402
from studio import langqa, language, publish  # noqa: E402
from studio import verdict as _verdict  # noqa: E402
from studio.util import sec_head_no  # noqa: E402

# —— 判据阈值：全部来自 studio/language.py 的 MARKET_TARGET（单一权威源）——
# 这里不再自己写一套数字。流水线"要不要返修"与体检"能不能投"必须同一套阈值，
# 否则会出现"体检不及格、流水线认为已达标"或反过来的空洞通过。
MT = language.MARKET_TARGET
SCORE_PER_K_GOOD, SCORE_PER_K_BAD = MT["score_per_k"], MT["score_per_k"] * 4.5
CHAIN_PER_K_MAX = MT["chain_per_k"]
FRAG_PER_K_MAX = MT["frag_per_k"]
LONG_PER_K_MAX = MT["longsen_per_k"]
ORDER_PER_K_MAX = MT["order_per_k"]
EURO_PER_K_MAX = MT["euro_per_k"]
REDUN_PER_K_MAX = MT["redundancy_per_k"]
DETAIL_PER_K_MAX = MT["detail_per_k"]
DIALOG_RATIO_MIN = MT["dialog_ratio_min"]
SECTION_RATIO_MAX = MT["section_ratio_max"]
WORDS_MIN, WORDS_MAX = MT["words_min"], MT["words_max"]
LONG_PARA_CHARS = MT["long_para_chars"]


def _load(target: str) -> tuple[str, Path | None, dict]:
    p = Path(target)
    if p.is_dir():
        txt = p / "final" / "成稿.txt"
        rp = p / "report.json"
        rep = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {}
        # 老项目的 report.json 没有顶层 review_score（逐章分数只存 review/chNN.json
        # 与 project.json.meta.reviews）。验收工具站在项目层读，必须自己兜一次，
        # 否则所有历史稿都会显示"主编评分 None"，把口径缺失误读成"模型没打分"。
        if rep and rep.get("review_score") is None:
            try:
                meta = json.loads((p / "project.json").read_text(encoding="utf-8"))
                rvs = [float(v["review_score"]) for v in (meta.get("reviews") or {}).values()
                       if isinstance(v, dict) and isinstance(v.get("review_score"), (int, float))]
                if rvs:
                    rep["review_score"] = round(sum(rvs) / len(rvs), 2)
                    rep["review_score_min"] = round(min(rvs), 2)
                    rep["review_score_src"] = "meta_mean"
                    rep["review_score_n"] = len(rvs)
            except Exception:  # noqa: BLE001
                pass
        return txt.read_text(encoding="utf-8", errors="replace"), p, rep
    return p.read_text(encoding="utf-8", errors="replace"), None, {}


def _sections(text: str) -> list[tuple[str, str]]:
    """按权威节头切成 [(no, body)]；节头判据只走 util.sec_head_no（单一权威源）。"""
    out: list[tuple[str, str]] = []
    cur_no, buf = "", []
    for ln in text.split("\n"):
        no = sec_head_no(ln)
        if no is not None:
            if cur_no or buf:
                out.append((cur_no, "\n".join(buf).strip()))
            cur_no = f"{no:02d}"
            buf = []
        else:
            buf.append(ln)
    if cur_no or buf:
        out.append((cur_no, "\n".join(buf).strip()))
    if out and out[0][0] == "":
        # 节头之前的内容是「标题 + 导语」，不属于任何小节：不能并进 01，
        # 否则 01 凭空多出 400 字，节长极差被虚报（e2e5 实测 1.36× 变 1.69×）。
        out.pop(0)
    return out


def audit(target: str) -> dict:
    """读项目/文件 → 调 `studio.audit.audit_text`（判据单一权威源）。"""
    text, proj, rep = _load(target)
    res = _audit.audit_text(text, rep)
    res["target"] = str(target)
    res["report"] = ({k: rep.get(k) for k in
                      ("final_words", "review_score", "platform_check",
                       "lang_polish", "mode")} if rep else {})
    return res


def render(res: dict) -> str:
    out = []
    out.append(f"【成稿体检】{res['target']}")
    out.append(f"  字数 {res['chars']} | 节 {len(res['sections'])} {res['sections']} | "
               f"节长极差 {res['level_ratio']}× | 对话占比 {res['dialog_ratio']:.1%}")
    out.append(f"  缺陷密度/千字 " + " ".join(f"{k}={v}" for k, v in res["density"].items()))
    out.append("")
    group = ""
    for c in res["checks"]:
        if c["group"] != group:
            group = c["group"]
            out.append(f"  ── {group} ──")
        mark = "ok " if c["ok"] else ("WARN" if c["level"] == "warn" else "FAIL")
        out.append(f"   [{mark}] {c['name']:12s} 实测 {str(c['got'])[:26]:26s} 要求 {c['want']}")
    out.append("")
    out.append(f"  最需要返修的节：")
    for s in res["worst_sections"]:
        out.append(f"    第{str(s['no']):>3s} 节 {s['chars']:5d} 字 缺陷分 {s['score']:4d} "
                   f"(链{s['chain']} 碎{s['frag']} 序{s['order']} 冗{s['redundancy']} 详{s['detail']} 欧{s['euro']})")
    out.append("")
    out.append(f"  结论：{res['grade']}"
               + (f" | 失败项 {res['fails']}" if res["fails"] else "")
               + (f" | 警告项 {res['warns']}" if res["warns"] else ""))
    return "\n".join(out)


def heal(path: str, rounds: int = 2) -> dict:
    """对成稿跑确定性返修（零 token），返回修复前后对照。

    为什么需要：`deterministic_repair` 是**机械**改法（合并同主语短句链、去重复段、
    切超长段），规则唯一、可验证。流水线里它跑在出口，但历史项目与离线稿件没有
    机会重跑整条流水线——给体检工具一个 `--heal` 就能回答"这篇到底还有没有救"，
    而不是只报一句"不可投"。
    """
    import tempfile

    from studio import language as L

    p = Path(path)
    txt_file = (p / "final" / "成稿.txt") if p.is_dir() else p
    src = txt_file.read_text(encoding="utf-8", errors="replace")
    before = audit(str(txt_file))
    out, stats = src, {}
    for _ in range(max(1, rounds)):
        out, rep = L.deterministic_repair(out, log=lambda *a: None)
        for k, v in rep.items():
            stats[k] = int(stats.get(k) or 0) + int(v or 0)
        if not any(rep.values()):
            break
    after = None
    if out != src:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(out)
            tmp = fh.name
        try:
            after = audit(tmp)
        finally:
            try:
                Path(tmp).unlink()
            except OSError:
                pass
    return {"file": str(txt_file), "before": before, "after": after or before,
            "stats": stats, "changed": out != src, "text": out}


def write_recheck(path: str, res: dict) -> Path | None:
    """把本次体检结论写回项目的 report.json / project.json（非破坏式）。

    为什么需要：历史项目的 `report["market_lang"]` 是**当时那版判据**的结论。
    判据修好之后（例如对话句被句号切断导致台词误判碎句），老报告会永远显示
    「语言超线」，看板与复盘据此做决策就是错的。重跑流水线太贵，所以让体检
    工具把复检结论单独存成 `lang_recheck`，**不覆盖** `market_lang`——
    原始判定保留可追溯，新判定可比较。
    """
    from studio import util

    p = Path(path)
    if not p.is_dir():
        return None
    payload = {
        "pass": not res.get("fails"),
        "grade": res.get("grade"),
        "fails": res.get("fails") or [],
        "warns": res.get("warns") or [],
        "density": res.get("density") or {},
        "chars": res.get("chars"),
        "dialog_ratio": res.get("dialog_ratio"),
        "at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    rp, pp = p / "report.json", p / "project.json"
    if rp.exists():
        rep = json.loads(rp.read_text(encoding="utf-8"))
        rep["lang_recheck"] = payload
        util.atomic_write_json(rp, rep)
    if pp.exists():
        meta = json.loads(pp.read_text(encoding="utf-8"))
        meta["lang_recheck"] = payload
        util.atomic_write_json(pp, meta)
    return rp


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法：python tools/manuscript_audit.py <项目目录|成稿.txt> "
              "[--json] [--heal] [--write]")
        return 2
    if "--heal" in argv:
        h = heal(argv[1])
        b, a = h["before"], h["after"]
        print(f"== 修复前 == 等级 {b['grade']} | 失败 {b['fails']} | 警告 {b['warns']}")
        print(f"== 确定性返修（零 token）== {h['stats']} | 文本有变化 {h['changed']}")
        print(f"== 修复后 == 等级 {a['grade']} | 失败 {a['fails']} | 警告 {a['warns']}")
        if "--json" in argv:
            print(json.dumps({k: v for k, v in h.items() if k != "text"},
                             ensure_ascii=False, indent=2))
        return 0
    res = audit(argv[1])
    if "--write" in argv:
        dst = write_recheck(argv[1], res)
        print(f"[recheck] 已写回 {dst}" if dst else "[recheck] 目标不是项目目录，跳过写回")
    if "--json" in argv:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render(res))
    return 0 if not res["fails"] else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main(sys.argv))
