"""Self-test for the LaTeX/template layer.

Renders the generic template with (a) a real result value injected from
results/fit.json via \\VAR{...}, and (b) an embedded figure, then compiles to PDF
with tectonic. Tests English and Chinese (cjk=true). Run:
    ./.venv/bin/python -m scripts.check_latex
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from mathmodel.latex.compile import compile_tex
from mathmodel.latex.quality import inspect_paper
from mathmodel.latex.render import render_report
from mathmodel.latex.structure import inspect_latex_structure

os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"


def build_workdir(d: Path) -> None:
    (d / "results").mkdir(parents=True, exist_ok=True)
    (d / "figures").mkdir(parents=True, exist_ok=True)
    # A "computed" result the paper will cite.
    json.dump({"a": 3.058, "b": 1.774, "rmse": 0.422}, open(d / "results" / "fit.json", "w"))
    # A figure produced by "code".
    x = np.linspace(0, 10, 50); y = 3.058 * x + 1.774
    plt.figure(figsize=(4, 3)); plt.plot(x, y); plt.xlabel("x"); plt.ylabel("y")
    plt.title("Fitted line"); plt.tight_layout()
    plt.savefig(d / "figures" / "fit.png", dpi=120); plt.close()


def run_case(d: Path, cjk: bool, tag: str) -> None:
    if cjk:
        context = {
            "title": "线性拟合建模报告",
            "author": "mathmodel-agent",
            "abstract": "本文用最小二乘法拟合直线，斜率为 \\VAR{results['fit']['a']}。",
            "keywords": "最小二乘, 线性回归",
            "cjk": True,
            "sections": [
                {"heading": "一、模型建立",
                 "body": "\\subsection{1.1 基本思路}\n"
                         "\\subsubsection{1.1.1 参数求解}\n"
                         "拟合直线 $y = ax + b$，由代码求得 "
                         "$a = \\VAR{results['fit']['a']}$，$b = \\VAR{results['fit']['b']}$，"
                         "RMSE $= \\VAR{results['fit']['rmse']}$。\n\n"
                         "\\begin{center}\\includegraphics[width=0.5\\textwidth]{figures/fit.png}\\end{center}"},
            ],
        }
    else:
        context = {
            "title": "Linear Fit Modeling Report",
            "author": "mathmodel-agent",
            "abstract": "Least-squares line fit; slope is \\VAR{results['fit']['a']}.",
            "keywords": "least squares, linear regression",
            "sections": [
                {"heading": "Model",
                 "body": "We fit $y = ax + b$. Code gives "
                         "$a = \\VAR{results['fit']['a']}$, $b = \\VAR{results['fit']['b']}$, "
                         "RMSE $= \\VAR{results['fit']['rmse']}$.\n\n"
                         "\\begin{center}\\includegraphics[width=0.5\\textwidth]{figures/fit.png}\\end{center}"},
            ],
        }

    tex = render_report(context, workdir=d, template="generic")
    tex_path = d / f"main_{tag}.tex"
    tex_path.write_text(tex)
    # Sanity: the real value must have been injected (no leftover \VAR).
    assert "3.058" in tex and "\\VAR{" not in tex, "result value not injected"
    if cjk:
        assert "\\section{模型建立}" in tex
        assert "\\subsection{基本思路}" in tex
        assert "\\subsubsection{参数求解}" in tex
        assert "\\section{一、" not in tex and "\\subsection{1.1 " not in tex

    print(f"\n[{tag}] compiling (cjk={cjk}) ...")
    res = compile_tex(tex_path)
    print(f"  ok={res.ok}  pdf={res.pdf_path}")
    if not res.ok:
        print("  --- compile log tail ---")
        print("\n".join(res.log.splitlines()[-20:]))
    assert res.ok, f"{tag} compile failed"
    size = res.pdf_path.stat().st_size
    print(f"  pdf size: {size} bytes")
    assert size > 1000
    metrics = inspect_paper(res.pdf_path, tex_path)
    assert metrics.first_section_page == 2, (
        "the template must reserve page 1 for title + abstract"
    )
    assert metrics.abstract_fill_ratio < 0.72, (
        "this deliberately short test abstract should be detected as under-filled"
    )


def run_competition_case(d: Path, *, template: str, cjk: bool) -> None:
    """Compile flexible headings plus template-owned appendix/references."""
    context = {
        "title": "结构化论文测试" if cjk else "Structured Paper Test",
        "abstract": "结构化摘要。" if cjk else "A structured summary.",
        "keywords": "结构, 模型" if cjk else "structure, model",
        "cjk": cjk,
        "sections": [
            {"heading": "问题分析" if cjk else "Problem Analysis", "body": "正文一。" if cjk else "Main text one."},
            {"heading": "微分方程模型" if cjk else "Differential Equation Framework", "body": "正文二。" if cjk else "Main text two."},
            {"heading": "模型检验" if cjk else "Model Validation", "body": "正文三。" if cjk else "Main text three."},
        ],
        "appendices": [
            {"heading": "补充代码" if cjk else "Supplementary Code", "body": "附录内容。" if cjk else "Appendix material."},
        ],
        "references": [
            {"key": "smith2024", "text": "Smith A. Verified modeling reference. 2024."},
        ],
    }
    tex = render_report(context, workdir=d, template=template)
    assert not inspect_latex_structure(tex), inspect_latex_structure(tex)
    assert tex.index(r"\appendix") < tex.index(r"\begin{thebibliography}")
    assert tex.index(r"\end{thebibliography}") < tex.index(r"\end{document}")

    tex_path = d / f"main_{template}.tex"
    tex_path.write_text(tex)
    res = compile_tex(tex_path)
    assert res.ok, "\n".join(res.log.splitlines()[-30:])
    metrics = inspect_paper(res.pdf_path, tex_path)
    assert metrics.first_section_page == 2
    assert metrics.appendix_start_page is not None
    assert metrics.references_start_page is not None
    assert metrics.counted_page_count < metrics.page_count
    assert metrics.appendix_page_count >= 1
    assert metrics.reference_page_count >= 1
    print(
        f"[{template}] total={metrics.page_count}, "
        f"counted={metrics.counted_page_count}, "
        f"appendix={metrics.appendix_page_count}, "
        f"references={metrics.reference_page_count}"
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        build_workdir(d)
        run_case(d, cjk=False, tag="en")
        run_case(d, cjk=True, tag="zh")
        run_competition_case(d, template="cumcm", cjk=True)
        run_competition_case(d, template="mcm-icm", cjk=False)
    print("\nOK: rendered + injected real results + compiled PDF (EN & ZH).")


if __name__ == "__main__":
    main()
