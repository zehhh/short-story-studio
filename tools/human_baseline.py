# -*- coding: utf-8 -*-
"""探针：从 onehu HTML 里抽正文，量「段落/小句/动作句」的人类基线。"""
import re, os, html, random, glob

TAG = re.compile(r"<[^>]+>")
DROP = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
CLAUSE = re.compile(r"[^。！？；…，]+[。！？；…，]?")
SENT = re.compile(r"[^。！？…]*[。！？…]+|[^。！？…]+$")
DQ = re.compile(r"「[^」]*」|“[^”]*”|\"[^\"]*\"")

ACT = re.compile(
    r"走|跑|退|进|出|推|拉|拽|扯|抓|握|捏|按|压|抬|举|放|丢|扔|递|接|拿|取|夺|抢|"
    r"跪|站|坐|躺|倒|扑|撞|踢|踩|跳|转|回|冲|扑|拔|抽|塞|插|掀|踢|抱|扶|扶住|拍|敲|"
    r"打|劈|砍|刺|挥|甩|抖|颤|缩|退后|上前|低头|抬头|转身|起身|俯身|弯腰|伸手|开口|说|问|答|喊|叫"
)


def html_text(p):
    raw = open(p, encoding="utf-8", errors="ignore").read()
    raw = DROP.sub(" ", raw)
    # 取 <body> 或全文
    txt = TAG.sub("\n", raw)
    txt = html.unescape(txt)
    lines = [l.strip() for l in txt.split("\n")]
    paras = [l for l in lines if len(l) >= 2]
    return "\n".join(paras)


def stats(text):
    body = DQ.sub("", text)
    paras = [p for p in body.split("\n") if len(p.strip()) >= 2]
    if not paras:
        return None
    n_chars = len(re.sub(r"\s", "", body)) or 1
    cls_per_para, act_ratio, long_para = [], [], 0
    for p in paras:
        cls = [c.strip() for c in CLAUSE.findall(p) if c.strip()]
        if not cls:
            continue
        cls_per_para.append(len(cls))
        a = sum(1 for c in cls if ACT.search(c))
        act_ratio.append(a / len(cls))
        if len(p) > 120:
            long_para += 1
    if not cls_per_para:
        return None
    m = lambda xs: sum(xs) / len(xs)
    return {
        "chars": n_chars,
        "paras": len(paras),
        "para_len": n_chars / len(paras),
        "cls_per_para": m(cls_per_para),
        "act_ratio": m(act_ratio),
        "long_para_pct": long_para / len(paras),
        "pct_1cls": sum(1 for x in cls_per_para if x == 1) / len(cls_per_para),
    }


def main():
    root = r"E:\小说\短篇\dataset-forge\data\raw\onehu"
    files = []
    for dp, _, fn in os.walk(root):
        for f in fn:
            if f.endswith(".html"):
                files.append(os.path.join(dp, f))
    random.seed(11)
    files = random.sample(files, min(120, len(files)))
    rows = []
    for p in files:
        try:
            t = html_text(p)
        except Exception:
            continue
        s = stats(t)
        if not s or s["chars"] < 4000:
            continue
        rows.append(s)
    def agg(key):
        xs = sorted(r[key] for r in rows)
        k = len(xs)
        return f"中位 {xs[k//2]:.2f}  p90 {xs[int(k*0.9)]:.2f}  max {xs[-1]:.2f}"
    print(f"人类语料 n={len(rows)}")
    for k in ("para_len", "cls_per_para", "act_ratio", "long_para_pct", "pct_1cls"):
        print(f"  {k:14s} {agg(k)}")

    print("\n我们的成稿：")
    for p in sorted(glob.glob(r"E:\小说\短篇\workspace\projects\2026110*\final\成稿.md"))[-5:]:
        t = open(p, encoding="utf-8").read()
        s = stats(t)
        if not s:
            continue
        name = os.path.basename(os.path.dirname(os.path.dirname(p)))[6:14]
        print(f"  {name} 段长 {s['para_len']:5.1f} 小句/段 {s['cls_per_para']:4.2f} "
              f"动作句占比 {s['act_ratio']:.2f} 长段 {s['long_para_pct']:.0%} 单句段 {s['pct_1cls']:.0%}")


if __name__ == "__main__":
    main()
