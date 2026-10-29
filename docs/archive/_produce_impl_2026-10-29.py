def cmd_produce(cfg: dict, args):
    """全流程制作流水线：选题 → 写作门禁 → 发布 → 数据回流 → 复盘改进。

    单条命令跑通完整生产线；N 篇自动轮换题材；产出制作报告。
    """
    from . import analytics as an
    from . import publish as pub
    from .llm import get_llm
    from pathlib import Path as _P

    llm = get_llm(cfg)
    ws = _P(cfg["studio"]["workspace_abs"])
    count = max(1, int(args.count or 1))
    dirs = [d.strip() for d in (args.directions or "").split(",") if d.strip()]
    plat = args.platform or cfg["studio"].get("platform", "zhihu")
    publish_mode = (args.publish_mode or "none").lower()
    data_source = (args.data_source or "none").lower()
    with_improve = args.with_improve or data_source != "none"

    line = []
    print(f"=== 全流程制作：{count} 篇 | 题材：{'、'.join(dirs) or '跟随热点'} | "
          f"平台：{plat} | 单篇 {args.words or cfg['studio'].get('target_words', '默认')} 字 ===")

    for i in range(count):
        direction = dirs[i % len(dirs)] if dirs else ""
        print(f"\n========== 第 {i + 1}/{count} 篇（题材：{direction or '跟随热点'}） ==========")
        if args.idea and count == 1:
            project = create_project(ws, args.idea)
            concept = make_idea_concept(args.idea, direction or None)
            project.save_json("concept.json", concept)
            project.set_stage("concept", concept.get("direction", ""))
        else:
            project = create_project(ws, "produce_" + now_ts())
            if sources := (args.source or ""):
                stage_fetch(cfg, project, sources=[x.strip() for x in sources.split(",")],
                            top=8, direction_hint=direction)
            stage_mine(cfg, llm, project, direction_hint=direction)
            stage_select(project, pick="auto")
        if not args.no_kb:
            stage_kb(cfg, llm, project, force=args.kb_refresh)
        stage_pitch(cfg, llm, project)
        stage_lead(cfg, llm, project)
        stage_outline(cfg, llm, project)
        stage_bible(cfg, llm, project)
        stage_write(cfg, llm, project)
        stage_polish(cfg, llm, project)

        story = {"dir": direction or "—", "project": project.root.name,
                 "title": (project.load_json("pitch.json") or {}).get("title", ""),
                 "words": (project.load_json("report.json") or {}).get("final_words", 0),
                 "publish": "", "reads": None, "improve": ""}
        # 发布
        if publish_mode != "none":
            pub.publish(cfg, project, mode=publish_mode)
            story["publish"] = publish_mode
        # 数据回流
        if data_source != "none":
            try:
                snap = an.fetch_stats(cfg, project.root.name, source=data_source)
                story["reads"] = (snap.get("metrics") or {}).get("reads")
            except Exception as e:  # noqa: BLE001
                story["reads"] = f"失败({e})"
        # 复盘
        if with_improve and data_source != "none":
            try:
                imp = stage_improve(cfg, llm, project)
                story["improve"] = f"{len(imp.get('lessons', []))} 条教训"
            except Exception as e:  # noqa: BLE001
                story["improve"] = f"失败({e})"
        line.append(story)

    # 制作报告
    rep = ["=== 全流程制作报告 ==="]
    for x in line:
        rep.append(f"《{x['title']}》 [{x['dir']}] {x['words']}字 | 发布:{x['publish'] or '未'} | "
                   f"阅读:{x['reads'] if x['reads'] is not None else '—'} | {x['improve']}")
    report = "\n".join(rep)
    print("\n" + report)
    (ws / f"production_{time.strftime('%Y%m%d_%H%M%S')}.txt").write_text(report, encoding="utf-8")
    print("\n=== 生产完成 ===")
