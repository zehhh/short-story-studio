"""语言打磨的行为回归：喂真病句，看它是否真被改写。

这个用例是被一次真实事故逼出来的：`studio/language.py:polish()` 里写了
`if len(_count_sec_heads(new_text)) > 1`，而 `_count_sec_heads` 返回 int，
于是每次改写都抛 TypeError；外层 `stage_polish` 用 `except Exception` 兜住
并 print 一句就继续 —— 语言打磨**上线以来从未生效过**，成稿照样入库、
照样打分。原来的测试只断言"源码里有 CRAFT= 字样"，绿灯得很安心。

教训：接线类断言必须升级为行为断言。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from studio import language as L
from studio.store import Project


SICK = (
    "01\n\n"
    "门开了。\n\n"
    "我站着。\n\n"
    "没动。\n\n"
    "看着他。\n\n"
    "他没有说话。\n\n"
    "我也没有说话。\n\n"
    "空气很静。\n\n"
    "作为这个家的儿媳妇，我不得不承认，这一刻我感到了一种前所未有的屈辱感。\n\n"
    "我手里的碗掉在地上。碗碎了。我蹲下去捡。碎片划破了我的手。血珠冒出来。\n\n"
)

GOOD = (
    "01\n\n"
    "门开了，我站在原地没动，就那么看着他。\n\n"
    "他和我都没有说话，屋里的空气静得发沉。\n\n"
    "作为这家的儿媳妇，这一刻我觉得脸上烧得厉害。\n\n"
    "手里的碗掉在地上碎了，我蹲下去捡，碎片划破手指，血珠立刻冒了出来。\n"
)


class _StubLLM:
    """假装写手的桩：把碎句合并、把欧化句改回中文语序。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, system: str, user: str = "") -> str:  # noqa: ARG002
        self.calls += 1
        return GOOD


def _proj(tmp_path) -> Project:
    proj = Project(tmp_path / "langlive_probe")
    (proj.root / "concept.json").write_text(
        json.dumps({"direction": "彩礼纠纷"}, ensure_ascii=False), encoding="utf-8")
    return proj


def test_polish_really_rewrites_sections(tmp_path, monkeypatch):
    import studio.llm as _llmmod

    llm = _StubLLM()
    # polish 内部用 resolve_llm(cfg, llm, "writer") 拿写手，这里直接换成桩，
    # 避免测试依赖真实 config.yaml（里面有真 key）。
    monkeypatch.setattr(_llmmod, "resolve_llm", lambda *a, **k: llm)
    cfg = {"review": {"lang_polish_enabled": True, "lang_polish_rounds": 1}}
    rules = L.langqa.load_rules()
    before = L.defect_score(SICK, rules)["score"]

    out, rep = L.polish(cfg, llm, _proj(tmp_path), SICK, rounds=1,
                        log=lambda *_: None)

    assert llm.calls >= 1, "语言打磨根本没调用模型（改写从未发生）"
    assert rep.get("sections_rewritten", 0) >= 1, (
        f"一节都没改写成功：rewritten={rep.get('sections_rewritten')} "
        f"rejected={rep.get('sections_rejected')} —— 打磨链路是空转的")
    after = L.defect_score(out, rules)["score"]
    assert after < before, f"改写后缺陷分没降：{before} → {after}"
    assert "门开了" in out, "改写把正文弄丢了"
    assert L._count_sec_heads(out) == 1, "改写引入了多余小节头"


def test_polish_survives_multiple_section_heads_from_model(tmp_path, monkeypatch):
    import studio.llm as _llmmod

    """模型返回多个小节头 = 越界，必须整节拒收而不是把别的节覆盖掉。"""

    class BadLLM:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, system: str, user: str = "") -> str:  # noqa: ARG002
            self.calls += 1
            return GOOD + "\n02\n\n我另起了一节，这节不该存在。"

    llm = BadLLM()
    monkeypatch.setattr(_llmmod, "resolve_llm", lambda *a, **k: llm)
    cfg = {"review": {"lang_polish_enabled": True, "lang_polish_rounds": 1}}
    out, rep = L.polish(cfg, llm, _proj(tmp_path), SICK, rounds=1,
                        log=lambda *_: None)
    assert rep.get("sections_rejected", 0) >= 1, "越界输出未被拒收"
    assert L._count_sec_heads(out) == 1, f"越界内容混进了成稿：{out[:80]}"


if __name__ == "__main__":
    # 既能被 pytest 跑，也能被 `python -m studio verify` 直接当脚本跑
    # （verify 不依赖 pytest 是否安装）。
    import tempfile

    class _MP:
        """最小 monkeypatch 替身，只实现 setattr + 自动回滚。"""

        def __init__(self) -> None:
            self._undo: list[tuple[object, str, object]] = []

        def setattr(self, obj: object, name: str, val: object) -> None:
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def undo(self) -> None:
            for o, n, v in reversed(self._undo):
                setattr(o, n, v)
            self._undo.clear()

    _cases = (test_polish_really_rewrites_sections,
              test_polish_survives_multiple_section_heads_from_model)
    for _fn in _cases:
        _mp = _MP()
        try:
            with tempfile.TemporaryDirectory() as _td:
                _fn(Path(_td), _mp)  # type: ignore[arg-type]
        finally:
            _mp.undo()
        print(f"ok {_fn.__name__}")
    print("lang polish behaviour: 2 passed")
