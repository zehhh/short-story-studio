# -*- coding: utf-8 -*-
"""自动发布：把过审成稿发布到平台，或生成投稿包。

现实约束（诚实工程）：
- 番茄/盐选/七猫均无公开发布 API。番茄作家助手是网页端，社区工具靠登录 cookie +
  未公开接口，随时可能变化。因此提供三种模式：
  1. manual —— 生成投稿包（成稿 + 书名简介 + 平台要求 + 逐步粘贴指引），永远可用；
  2. custom —— 用户抓包后把发布接口模板写进 config（POST，支持 {title}/{content} 占位），
     本模块负责填充与调用，并提供 --probe 探测；
  3. mock   —— 离线测试，写一条假发布记录。

发布记录落盘 <project>/publish.json，供看板与数据分析关联。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import requests

from .config import data_path
from .store import Project
from .util import now_ts


# ---------------------------------------------------------------------------
# 投稿包（manual 模式，永远可用）
# ---------------------------------------------------------------------------



# 对话引号标准化：全平台一致（知乎盐选 / 番茄 / 七猫都要求「」）
# 旧实现只在 zhihu_format 里做、且只作用于成稿.md，投稿用的 成稿.txt 保留了原始
# “ ” 引号 —— 一份稿子两种引号，且不符合平台规范（实测 146 对 “” 未转换）。
_QUOTE_PAIRS = (
    (re.compile(r"“([^”]*)”"), "「\\1」"),   # 全角双引号（不限长度、可跨行）
    (re.compile(r'"([^"]*)"'), "「\\1」"),     # 半角双引号
)


def normalize_quotes(text: str) -> str:
    """把成对的 “…” / "…" 统一成平台要求的「…」。

    幂等；不碰已有的「」；对不配对的残留引号做兜底替换（“→「、”→」），
    避免成稿里出现半截引号（旧 zhihu_format 限 2-120 字，跨行/长对话会漏，
    实测 md 里仍残留 3 处 “”）。
    """
    out = text or ""
    for pat, rep in _QUOTE_PAIRS:
        out = pat.sub(rep, out)
    # 兜底：仍不配对的引号（奇数个）按方向就近替换
    if out.count("“") or out.count("”"):
        out = out.replace("“", "「").replace("”", "」")
    return out


def strip_section_titles(text: str) -> str:
    """小节头剥标题：『01 标题』→『01』（知乎盐言纯数字规范）。"""
    out = re.sub(r"(?m)^(\d{2}) [^\n]{2,30}$", r"\1", text)
    out = re.sub(r"(?m)^#{1,3} (\d{2}) [^\n]{2,40}$", r"\1", out)
    return out


def zhihu_format(text: str) -> str:
    """知乎盐言格式：对话引号「」化 + 小节头剥标题只留纯数字序号。

    写作层仍用「01 标题」节头(便于门禁引用),投稿排版层做最终规范化——
    盐言规范:小节序号纯阿拉伯数字、不加小节标题、对话用「」。
    """
    return strip_section_titles(normalize_quotes(text))


def resolve_platform(cfg: dict, project: Project, explicit: str | None = None) -> str:
    """平台单一来源：显式参数 > 项目 pitch.platform > config.studio.platform > config.publish.platform。

    pitch.platform 是门禁与投稿包的依据（pipeline 按它选 min_words/格式），
    必须优先——否则 config 里的 publish.platform 会让 publish.json 记成 A 平台、
    而投稿包按 B 平台排版，同一次投稿出现两个身份。
    """
    if explicit:
        return explicit
    pitch = project.load_json("pitch.json") or {}
    return (pitch.get("platform")
            or cfg.get("studio", {}).get("platform")
            or cfg.get("publish", {}).get("platform", "zhihu"))


def build_submission_package(cfg: dict, project: Project, platform: str | None = None) -> Path:
    """生成可直接投稿的完整材料包：成稿 + 简介 + 平台要求清单 + 粘贴指引。"""
    pitch = project.load_json("pitch.json") or {}
    concept = project.load_json("concept.json") or {}
    report = project.load_json("report.json") or {}
    plat = resolve_platform(cfg, project, platform)
    plats = json.loads(data_path("platforms.json").read_text(encoding="utf-8"))["platforms"]
    spec = plats.get(plat, {})
    final_txt = (project.root / "final" / "成稿.txt")
    body = final_txt.read_text(encoding="utf-8") if final_txt.exists() else "（成稿缺失，请先跑 polish）"

    pkg = project.root / "submission"
    pkg.mkdir(exist_ok=True)
    (pkg / "01_正文.txt").write_text(zhihu_format(body), encoding="utf-8")
    md = [
        f"# 投稿包：《{pitch.get('title', '')}》",
        "",
        f"- 目标平台：{spec.get('name', plat)}",
        f"- 题材方向：{concept.get('direction', '')}",
        f"- 字数：{report.get('final_words', '?')}（平台要求 ≥{spec.get('min_words', '?')}）",
        "",
        "## 书名候选（按优先级）",
        *[f"{i}. {t}" for i, t in enumerate(pitch.get("titles", [pitch.get("title", "")]), 1)],
        "",
        "## 一句话简介（投稿简介栏直接粘贴）",
        pitch.get("logline", ""),
        "",
        "## 平台要点提醒",
        f"- {spec.get('notes', '')}",
        f"- 文风要求：{spec.get('style', '')}",
        f"- 结尾要求：{spec.get('ending', '')}",
        "",
        "## 投稿步骤",
        {
            "zhihu": "1. 注册知乎盐选创作者（盐言故事）→ 2. 先以回答形式发布前三章测转化 → 3. 后台投稿给制作人 → 4. 过稿后按编辑意见修改上架",
            "fanqie": "1. 注册番茄作家助手（writer.changdunovel.com，手机号+验证码）→ 2. 新建作品（填书名/简介/封面）→ 3. 逐章粘贴正文（每次 1-3 章）→ 4. 提交审核，过审自动上架",
            "uc": "1. 入驻 UC 互娛/夸克故事创作者平台 → 2. 按后台指引新建故事并逐章发布",
            "qimao": "1. 注册七猫作家助手 → 2. 新建短篇并逐章粘贴发布",
        }.get(plat, "按平台后台指引发布"),
        "",
        "## 发布后",
        "记录作品链接到 publish.json（studio publish 已自动生成），然后用 `studio analytics fetch` 回收阅读数据。",
    ]
    (pkg / "02_投稿指引.md").write_text("\n".join(md), encoding="utf-8")
    # 排版稿：按目标平台规范化（章节号/全角标点/空行分段），可直接逐章粘贴
    try:
        from .typeset import draft_text, typeset

        ts = typeset(body, book_title=pitch.get("title", ""), logline=pitch.get("logline", ""))
        # 文件名带平台名：以前写死"03_番茄排版稿.txt"，投知乎的项目也顶着番茄的名字，
        # 用户按文件名判断就会贴错平台。
        # 平台名含 "/"（如"知乎盐选/盐言故事"）会被当成路径分隔符导致写盘失败，
        # 必须净化——以前这个异常被下面的 except 吞掉，投稿包静默缺排版稿。
        _pname = re.sub(r"[\\/:*?\"<>|]+", "-", str(spec.get("name") or plat)).strip("-") or plat
        # 节头规范按平台分叉：知乎盐言"不加小节标题"用纯数字，番茄/七猫保留「NN 标题」
        _keep = spec.get("format") == "lead_numbered" and plat != "zhihu"
        (pkg / f"03_{_pname}排版稿.txt").write_text(
            draft_text(ts, keep_titles=_keep), encoding="utf-8")
        if ts["issues"]:
            (pkg / "04_排版自检.txt").write_text("\n".join(ts["issues"]), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        # 不再静默：排版稿是投稿必需件，失败必须让用户看见
        print(f"[publish] ⚠ 排版稿生成失败（投稿包缺 03 文件）：{type(e).__name__}: {e}")
    return pkg


# ---------------------------------------------------------------------------
# 发布执行
# ---------------------------------------------------------------------------

def publish(cfg: dict, project: Project, platform: str | None = None, mode: str | None = None,
            cookie: str | None = None) -> dict:
    """发布成稿。mode: manual / custom / mock。已有成功记录时默认幂等跳过。"""
    rec = project.load_json("publish.json")
    if rec and rec.get("status") == "published":
        print(f"[publish] 该项目已于 {rec.get('published_at')} 发布到 {rec.get('platform')}（{rec.get('mode')}），跳过")
        return rec

    plat = resolve_platform(cfg, project, platform)
    mode = mode or cfg["publish"].get("mode", "manual")
    pitch = project.load_json("pitch.json") or {}
    title = pitch.get("title", "无题")
    body = (project.root / "final" / "成稿.txt")
    content = body.read_text(encoding="utf-8") if body.exists() else ""
    if not content:
        raise SystemExit("成稿缺失，先执行 polish 生成 final/成稿.txt")

    rec = {
        "platform": plat,
        "mode": mode,
        "title": title,
        "published_at": None,
        "status": "failed",
        "work_id": "",
        "url": "",
        "note": "",
    }

    if mode == "manual":
        pkg = build_submission_package(cfg, project, platform=plat)
        rec.update(status="pending_manual", note=f"投稿包已生成：{pkg.name}/（按 02_投稿指引.md 操作）")
        print(f"[publish] 投稿包已生成：{pkg}")
        print(f"[publish] 按 {pkg / '02_投稿指引.md'} 操作后，用 `studio analytics import` 手动录入数据")

    elif mode == "mock":
        rec.update(status="published", work_id=f"mock_{abs(hash(title)) % 10**8}", published_at=now_ts(),
                   note="mock 发布（离线测试）")
        print(f"[publish] mock 发布成功：work_id={rec['work_id']}")

    elif mode == "custom":
        ep = (cfg["publish"].get("endpoints") or {}).get("publish", "")
        if not ep:
            raise SystemExit(
                "custom 模式需要在 config.yaml 的 publish.endpoints.publish 配置发布接口模板"
                "（POST，支持 {title}/{content} 占位）。接口需自行抓包获取。"
            )
        c = cookie if cookie is not None else cfg["publish"].get("cookie", "")
        payload = {"title": title, "content": content}
        # 模板形如 https://x/api/publish?title={title}
        url = ep.replace("{title}", requests.utils.quote(title))
        headers = {"Content-Type": "application/json", "User-Agent": "short-story-studio/0.1"}
        if c:
            headers["Cookie"] = c
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json() if "json" in resp.headers.get("content-type", "") else {}
        rec.update(
            status="published",
            work_id=str(data.get("work_id") or data.get("data", {}).get("id", "")) if isinstance(data, dict) else "",
            url=(data.get("url") or "") if isinstance(data, dict) else "",
            published_at=now_ts(),
            note="custom 接口发布",
        )
        print(f"[publish] custom 接口发布成功")

    elif mode == "fanqie_draft":
        # 浏览器自动化推草稿箱：排版 → 新建作品/章节 → 存草稿（不自动提交审核）
        from .fanqie_draft import push_draft
        from .typeset import draft_text, typeset

        plat = "fanqie"
        rec["platform"] = plat
        ts = typeset(content, book_title=title, logline=pitch.get("logline", ""))
        (project.root / "submission").mkdir(exist_ok=True)
        (project.root / "submission" / "03_番茄排版稿.txt").write_text(
            draft_text(ts, keep_titles=True), encoding="utf-8")
        res = push_draft(cfg, ts, progress=lambda s: print(f"[publish]   {s}"))
        rec.update(
            status="published" if res["status"] == "draft_ok" else "failed",
            published_at=now_ts() if res["status"] == "draft_ok" else None,
            note=res.get("note", ""),
            chapters_pushed=len(res.get("chapters", [])),
        )
    else:
        raise SystemExit(f"未知发布模式：{mode}（可选 manual/custom/mock/fanqie_draft）")

    project.save_json("publish.json", rec)
    return rec


def probe(cfg: dict) -> dict:
    """探测 custom 接口连通性（GET 模板），用于发布/数据接口配置后的自检。"""
    eps = cfg["publish"].get("endpoints") or {}
    out = {}
    for key in ("publish", "stats"):
        url = eps.get(key, "")
        if not url:
            out[key] = "未配置"
            continue
        try:
            r = requests.get(url.split("{")[0], timeout=10,
                             headers={"Cookie": cfg["publish"].get("cookie", "")} if cfg["publish"].get("cookie") else {})
            out[key] = f"HTTP {r.status_code}（连通，注意确认接口方法与字段）"
        except Exception as e:  # noqa: BLE001
            out[key] = f"失败：{type(e).__name__}: {str(e)[:60]}"
    return out


def publish_status_of(project: Project) -> dict | None:
    return project.load_json("publish.json")
