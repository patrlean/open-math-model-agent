"""Regression checks for flexible paper regions and hard hierarchy rules."""

from __future__ import annotations

from mathmodel.latex.structure import (
    inspect_latex_structure,
    validate_document_regions,
)


def main() -> None:
    valid = {
        "sections": [
            {"heading": "Problem Framing", "body": "Introductory analysis."},
            {
                "heading": "Coupled Differential Equation Framework",
                "body": "\\subsection{State Variables}\nModel details.",
            },
        ],
        "appendices": [
            {"heading": "Reproducibility Notes", "body": "Code details."},
        ],
        "references": [
            {"key": "smith2024", "text": "Smith A. A verified source. 2024."},
        ],
    }
    assert validate_document_regions(valid) == []
    print("[1] arbitrary section counts and model-chosen titles are accepted")

    many_questions = {
        "sections": [
            {"heading": f"Task-specific model {index}", "body": "Analysis."}
            for index in range(1, 7)
        ],
    }
    assert validate_document_regions(many_questions) == []
    print("[2] papers with two, five, or more top-level chapters remain flexible")

    collapsed = {
        "sections": [{
            "heading": "Test",
            "body": "\n".join(
                f"\\subsection{{Chapter {index}}}\nText."
                for index in range(1, 12)
            ),
        }],
    }
    failures = validate_document_regions(collapsed)
    assert any("placeholder" in failure for failure in failures)
    assert any("one top-level section" in failure for failure in failures)
    print("[3] the historical Test + 1.1...1.11 collapse is rejected")

    nested_section = {
        "sections": [{
            "heading": "Analysis",
            "body": "\\section{Hidden top-level chapter}\nText.",
        }],
    }
    assert any(
        "contains \\section" in failure
        for failure in validate_document_regions(nested_section)
    )
    print("[4] top-level sections cannot be hidden inside a section body")

    wrong_order = r"""
\begin{document}
\section{Analysis}
\begin{thebibliography}{99}
\bibitem{x} Source.
\end{thebibliography}
\appendix
\section{Code}
\end{document}
"""
    rendered_failures = inspect_latex_structure(wrong_order)
    assert "references must appear after all appendices" in rendered_failures
    assert "references must be the final document region" in rendered_failures
    print("[5] appendices after references and trailing content are rejected")

    print("paper structure checks: passed")


if __name__ == "__main__":
    main()
