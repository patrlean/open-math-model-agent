"""Self-test for localized paper editing.

Builds a real LaTeX paper, edits its abstract, one section body, and one exact
paragraph, compiling after every change. No model or network API is used.

Run:
    ./.venv/bin/python -m scripts.check_edit_paragraph
"""

from __future__ import annotations

import os
import json
import tempfile
from pathlib import Path

from mathmodel.agent.loop import Agent
from mathmodel.latex.render import render_report
from mathmodel.providers.base import ChatResponse, Provider, ToolCall
from mathmodel.sandbox.local import LocalSandbox
from mathmodel.tools.base import Tool, ToolContext, ToolRegistry
from mathmodel.tools.edit_paragraph import _edit_paragraph, _inspect_paper_blocks
from mathmodel.tools.run_code import _run_code
from mathmodel.tools.write_paper import _write_paper

os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"


class _UnusedProvider(Provider):
    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        raise AssertionError("provider should not be called in this check")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        paper_dir = workdir / "paper"
        paper_dir.mkdir()
        (workdir / "results").mkdir()
        (workdir / "results" / "metrics.json").write_text(
            json.dumps({"score": 42})
        )
        tex = render_report({
            "title": "局部修订工具测试",
            "abstract": "原始摘要较短。",
            "keywords": "局部修订, 编译验证",
            "cjk": True,
            "sections": [
                {
                    "heading": "问题分析",
                    "body": (
                        "这是需要保留的原始分析段落。\n\n"
                        "\\subsection{模型选择}\n"
                        "原始模型选择说明。\n"
                        "\\begin{equation}\\label{eq:existing}x=1\\end{equation}\n"
                        "\\subsubsection{适用范围}\n"
                        "这个既有子标题必须保留。"
                    ),
                },
                {
                    "heading": "模型检验",
                    "body": "原始检验结论。",
                },
                {
                    "heading": "结构替换测试",
                    "body": (
                        "旧的章节引言。\n"
                        "\\subsection{旧子节}\n"
                        "旧子节正文。\n"
                        "\\subsubsection{旧孙节}\n"
                        "旧孙节正文。"
                    ),
                },
            ],
        }, workdir=workdir, template="generic")
        tex_path = paper_dir / "main.tex"
        tex_path.write_text(tex)

        ctx = ToolContext(
            workdir=workdir,
            sandbox=None,  # type: ignore[arg-type]
            settings={"paper": {
                "target_pages": 5,
                "min_pages": 1,
                "max_pages": 5,
                "abstract_cjk_min_chars": 1,
                "abstract_fill_min_ratio": 0,
                "min_display_equations": 0,
                "figures_english_only": False,
            }},
        )

        abstract_result = _edit_paragraph(ctx, {
            "target_type": "abstract",
            "operation": "replace",
            "content": "修订后的摘要说明模型、计算结果与验证过程。",
        })
        assert "acceptance PASSED" in abstract_result, abstract_result
        assert "修订后的摘要" in tex_path.read_text()
        assert r"\noindent\textbf{Keywords:}" in tex_path.read_text()
        assert "局部修订, 编译验证" in tex_path.read_text()
        assert (paper_dir / "main.pdf").is_file()
        print("[1] abstract replaced without deleting template-owned keywords")

        section_result = _edit_paragraph(ctx, {
            "target_type": "section",
            "target": "模型检验",
            "operation": "append",
            "content": (
                "\\subsection{2.5 2.5 敏感性分析}\n"
                "补充参数扰动范围、响应变化与稳健性解释。"
            ),
        })
        assert "acceptance PASSED" in section_result, section_result
        revised = tex_path.read_text()
        assert "原始检验结论" in revised
        assert r"\subsection{敏感性分析}" in revised
        assert "2.5 2.5 敏感性分析" not in revised
        print("[2] localized headings use the same number normalization as write_paper")

        structural_result = _edit_paragraph(ctx, {
            "target_type": "section",
            "target": "结构替换测试",
            "operation": "replace",
            "content": (
                "新的章节引言。\n"
                "\\subsection{新子节}\n"
                "新子节正文。"
            ),
        })
        assert "acceptance PASSED" in structural_result, structural_result
        revised = tex_path.read_text()
        assert revised.count(r"\section{结构替换测试}") == 1
        assert revised.count(r"\subsection{新子节}") == 1
        assert "旧子节" not in revised
        assert "旧孙节" not in revised
        print("[3] structural replace removes old descendants before inserting new ones")

        paragraph_result = _edit_paragraph(ctx, {
            "target_type": "text",
            "target": "原始模型选择说明。",
            "operation": "replace",
            "content": (
                "修订后的模型选择说明包含适用条件与方法理由，"
                "验证得分为 \\VAR{results['metrics']['score']}。"
            ),
        })
        assert "acceptance PASSED" in paragraph_result, paragraph_result
        revised = tex_path.read_text()
        assert "原始模型选择说明" not in revised
        assert "修订后的模型选择说明" in revised
        assert "验证得分为 42" in revised
        assert "这是需要保留的原始分析段落" in revised
        assert "适用范围" in revised
        print("[4] result variables resolve and descendant headings stay intact")

        before_duplicate_label = revised
        duplicate_label_result = _edit_paragraph(ctx, {
            "target_type": "text",
            "target": (
                "\\begin{equation}\\label{eq:existing}x=1\\end{equation}"
            ),
            "operation": "replace",
            "content": (
                "\\begin{equation}\\label{eq:existing}x=1\\end{equation}\n"
                "\\begin{equation}\\label{eq:existing}x=1\\end{equation}"
            ),
        })
        assert "status=rejected" in duplicate_label_result
        assert "duplicate LaTeX labels" in duplicate_label_result
        assert tex_path.read_text() == before_duplicate_label
        print("[5] localized edits cannot introduce duplicate LaTeX labels")

        prose_formula_result = _edit_paragraph(ctx, {
            "target_type": "text",
            "target": "原始检验结论。",
            "operation": "insert_after",
            "content": r"\begin{equation}y=2\end{equation}",
        })
        assert "status=rejected" in prose_formula_result
        assert "complete containing block" in prose_formula_result
        assert tex_path.read_text() == before_duplicate_label
        print("[6] text edits cannot append isolated semantic blocks to prose")

        before_layout_attack = revised
        layout_result = _edit_paragraph(ctx, {
            "target_type": "text",
            "target": "原始检验结论。",
            "operation": "insert_before",
            "content": r"\newgeometry{margin=0.2in}不应写入。",
        })
        assert "status=rejected" in layout_result
        assert tex_path.read_text() == before_layout_attack
        print("[7] document-level layout commands rejected before mutation")

        compile_failure = _edit_paragraph(ctx, {
            "target_type": "text",
            "target": "原始检验结论。",
            "operation": "replace",
            "content": r"\textbf{无法闭合的粗体",
        })
        assert "rejected and rolled back" in compile_failure, compile_failure
        assert tex_path.read_text() == before_layout_attack
        print("[8] compilation failure rolls source and PDF back atomically")

        before_missing = revised
        missing_result = _edit_paragraph(ctx, {
            "target_type": "subsection",
            "target": "不存在的小节",
            "operation": "append",
            "content": "不应写入。",
        })
        assert "status=rejected" in missing_result
        assert tex_path.read_text() == before_missing
        print("[9] missing target rejected without changing the paper")

        backups = sorted((paper_dir / "revisions").glob("*.tex"))
        assert len(backups) == 5
        assert "原始摘要较短" in backups[0].read_text()
        print("[10] every attempted compiled edit preserved a revision backup")

        dispatched: list[dict] = []
        registry = ToolRegistry()
        registry.register(Tool(
            name="record",
            description="test",
            parameters={"type": "object"},
            handler=lambda _ctx, args: dispatched.append(args) or "ok",
        ))
        agent = Agent(
            provider=_UnusedProvider(model="unused"),
            registry=registry,
            ctx=ctx,
            system_prompt="test",
        )
        duplicate_result = agent._run_one_tool(ToolCall(
            id="duplicate",
            name="record",
            arguments='{"content":"complete section","content":"short title"}',
        ))
        assert "duplicate argument key 'content'" in duplicate_result
        assert dispatched == []
        print("[11] duplicate tool argument keys are rejected instead of overwritten")

        pending_ctx = ToolContext(
            workdir=workdir,
            sandbox=None,  # type: ignore[arg-type]
            settings={"paper": {
                **ctx.settings["paper"],
                "min_pages": 17,
                "max_pages": 20,
            }},
        )
        pending_result = _edit_paragraph(pending_ctx, {
            "target_type": "text",
            "target": "原始检验结论。",
            "operation": "replace",
            "content": "已完成一项局部修订，但整篇论文仍需继续扩展。",
        })
        assert "APPLIED" in pending_result, pending_result
        assert "DOCUMENT ACCEPTANCE IS STILL PENDING" in pending_result
        assert "已完成一项局部修订" in tex_path.read_text()
        print("[12] successful local edits are not mislabeled as failed documents")

        manifest = json.loads(_inspect_paper_blocks(ctx, {}))
        model_block = next(
            item for item in manifest["blocks"]
            if item["target_type"] == "section"
            and item["title"] == "模型检验"
        )
        abstract_block = next(
            item for item in manifest["blocks"]
            if item["target_type"] == "abstract"
        )
        inspected = json.loads(_inspect_paper_blocks(ctx, {
            "block_id": model_block["block_id"],
        }))
        assert inspected["blocks"][0]["content_hash"] == model_block["content_hash"]
        assert "已完成一项局部修订" in inspected["blocks"][0]["content"]
        print("[13] paper blocks expose stable IDs, hashes, and current full bodies")

        batch_result = _edit_paragraph(ctx, {
            "edits": [
                {
                    "block_id": abstract_block["block_id"],
                    "expected_hash": abstract_block["content_hash"],
                    "operation": "replace",
                    "content": "批量事务同步更新了摘要与模型检验结论。",
                },
                {
                    "block_id": model_block["block_id"],
                    "expected_hash": model_block["content_hash"],
                    "operation": "replace",
                    "content": (
                        "\\\\section{模型检验}\n"
                        "批量修订后的误差为 10\\\\%，并与式 "
                        "\\\\ref{eq:existing} 保持一致。"
                    ),
                },
            ],
        })
        assert "status=applied" in batch_result, batch_result
        assert "edits=2" in batch_result
        assert "matching outer heading" in batch_result
        revised = tex_path.read_text()
        assert revised.count(r"\section{模型检验}") == 1
        assert r"\ref{eq:existing}" in revised
        assert r"\\section{模型检验}" not in revised
        print("[14] related block edits compile once and normalize outer headings/escaping")

        flexible_result = _edit_paragraph(ctx, {
            "block_id": model_block["block_id"],
            "target_type": "text",
            "target": "批量修订后的误差为 10%，并与式 \\ref{eq:existing} 保持一致。",
            "operation": "replace",
            "content": "批量修订后的误差为 5\\%，并与式 \\ref{eq:existing} 保持一致。",
        })
        assert "status=applied" in flexible_result, flexible_result
        assert r"误差为 5\%" in tex_path.read_text()
        print("[15] text matching tolerates escaped percent differences and keeps valid references")

        stale_result = _edit_paragraph(ctx, {
            "block_id": model_block["block_id"],
            "expected_hash": model_block["content_hash"],
            "operation": "replace",
            "content": "不应覆盖新版本。",
        })
        assert "status=rejected" in stale_result
        assert "changed after inspection" in stale_result
        assert "不应覆盖新版本" not in tex_path.read_text()
        print("[16] stale block hashes prevent edits against outdated paper context")

        stable_tex = tex_path.read_bytes()
        stable_pdf = (paper_dir / "main.pdf").read_bytes()
        rewrite_result = _write_paper(ctx, {
            "title": "损坏的全文重写",
            "cjk": True,
            "sections": [{
                "heading": "模型",
                "body": r"\begin{table}未闭合的表格",
            }],
        })
        assert "compile FAILED" in rewrite_result, rewrite_result
        assert "restored" in rewrite_result
        assert tex_path.read_bytes() == stable_tex
        assert (paper_dir / "main.pdf").read_bytes() == stable_pdf
        print("[17] failed full rewrites restore the matching source and PDF")

        guard_ctx = ToolContext(
            workdir=workdir,
            sandbox=LocalSandbox(workdir),
            settings=ctx.settings,
        )
        protected_tex = tex_path.read_text()
        guard_result = _run_code(guard_ctx, {
            "code": (
                "from pathlib import Path\n"
                "Path('paper/main.tex').write_text('corrupted')\n"
                "Path('results/guard.json').write_text('{\"ok\": true}')\n"
            ),
        })
        assert "[blocked]" in guard_result, guard_result
        assert tex_path.read_text() == protected_tex
        assert (workdir / "results" / "guard.json").is_file()
        print("[18] run_code keeps numerical artifacts but rolls paper writes back")

    print("\nOK: localized paper edits preserve rendering and layout invariants.")


if __name__ == "__main__":
    main()
