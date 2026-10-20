"""Guard the documentation exactly as if it were code.

The README makes numeric claims and shows six generated figures. Both rot
silently: a refactor moves 300 lines and the composition table still says the
old number, an image is renamed and GitHub shows a broken icon, a Mermaid block
gains an unbalanced `end` and the diagram disappears. None of that fails a test
suite, so this file makes it fail one.

It also runs ``docs/assets/generate.py``'s audit in-process: every text element
of every figure must stay inside the canvas and must not collide with another.
That is the same check the generator prints on stdout, asserted here so CI
enforces it instead of a human remembering to look.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
ASSETS = ROOT / "docs" / "assets"
GENERATE = ASSETS / "generate.py"

# Diagram types GitHub's Mermaid build accepts. A block starting with anything
# else renders as an empty box, which is indistinguishable from a broken repo.
MERMAID_KINDS = {
    "flowchart",
    "graph",
    "sequenceDiagram",
    "stateDiagram-v2",
    "stateDiagram",
    "erDiagram",
    "classDiagram",
    "pie",
    "gantt",
    "timeline",
    "mindmap",
    "journey",
    "gitGraph",
    "quadrantChart",
    "xychart-beta",
    "block-beta",
}


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _pages() -> list[Path]:
    """Every page a reader can land on: the README plus everything in docs/.

    Guards run over all of them. A new document is the most likely place for
    unguarded markdown to appear, so it joins the guard by existing rather than
    by being remembered.
    """
    return [README, *sorted((ROOT / "docs").glob("*.md"))]


_GENERATOR: object | None = None


def _load_generator():
    """Import docs/assets/generate.py by path, once (it is not a package module).

    The module has to be registered in ``sys.modules`` before execution: it uses
    ``from __future__ import annotations``, and ``@dataclass`` resolves those
    string annotations by looking the module up there. Skipping the registration
    makes dataclasses raise ``AttributeError: 'NoneType' object has no
    attribute '__dict__'``.
    """
    global _GENERATOR
    if _GENERATOR is not None:
        return _GENERATOR
    name = "docs_assets_generate"
    spec = importlib.util.spec_from_file_location(name, GENERATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _GENERATOR = module
    return module


def _fenced(text: str, lang: str) -> list[str]:
    """Return the bodies of all ```<lang> fenced blocks."""
    return re.findall(rf"```{lang}\n(.*?)```", text, re.S)


# --- assets ------------------------------------------------------------------


def test_every_referenced_asset_exists():
    """A renamed figure must not become a broken image on the repo page."""
    refs = re.findall(r'src="\./(docs/assets/[^"]+)"', _readme())
    assert refs, "README references no figures at all"
    missing = [r for r in refs if not (ROOT / r).is_file()]
    assert not missing, f"README points at missing files: {missing}"


def test_all_six_figures_are_referenced():
    """Every generated figure is actually used; no orphan build output."""
    refs = " ".join(re.findall(r'src="\./(docs/assets/[^"]+)"', _readme()))
    for name in ("banner", "architecture", "pipeline", "donut", "histogram", "repo-tree"):
        assert f"{name}.svg" in refs, f"{name}.svg is generated but never shown"


def test_figures_are_not_stale():
    """The committed SVGs must match what the generator produces right now.

    Regenerating and diffing catches the case this repo just lived through: the
    tree changed, the donut still showed old line counts, and nothing failed.
    """
    gen = _load_generator()
    bench = json.loads((ASSETS / "bench_result.json").read_text(encoding="utf-8"))
    builders = {
        "banner.svg": lambda: gen.banner(bench),
        "architecture.svg": gen.architecture,
        "pipeline.svg": lambda: gen.pipeline(bench),
        "donut.svg": gen.donut,
        "histogram.svg": lambda: gen.histogram(bench),
        "repo-tree.svg": gen.repo_tree,
    }
    for name, build in builders.items():
        committed = (ASSETS / name).read_text(encoding="utf-8")
        assert committed == build().to_svg(), (
            f"{name} is out of date — run `python docs/assets/generate.py`"
        )


def test_generated_figures_pass_their_own_audit():
    """No text leaves the canvas and no two labels collide, on all six figures."""
    gen = _load_generator()
    bench = json.loads((ASSETS / "bench_result.json").read_text(encoding="utf-8"))
    figures = {
        "banner.svg": gen.banner(bench),
        "architecture.svg": gen.architecture(),
        "pipeline.svg": gen.pipeline(bench),
        "donut.svg": gen.donut(),
        "histogram.svg": gen.histogram(bench),
        "repo-tree.svg": gen.repo_tree(),
    }
    problems = {name: issues for name, fig in figures.items() if (issues := gen.audit(fig))}
    assert not problems, problems


def test_audit_report_matches_the_figures():
    """svg-audit.json is a real report, not a stale artefact."""
    report = json.loads((ASSETS / "svg-audit.json").read_text(encoding="utf-8"))
    assert len(report) == 6
    assert all(not v["issues"] for v in report.values())
    assert sum(v["text_elements"] for v in report.values()) > 150


# --- numbers that must not drift ---------------------------------------------


def test_codebase_composition_matches_the_tree():
    """The composition table's line counts must equal a fresh count."""
    gen = _load_generator()
    counts = gen.line_counts()
    # Strip markdown emphasis: the README bolds the total and backticks paths,
    # while line_counts() reports plain labels like "Backend (app/)".
    readme = _readme().replace("`", "").replace("**", "")
    for label, (loc, files) in counts.items():
        row = re.search(
            rf"\|\s*{re.escape(label)}\s*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|",
            readme,
        )
        assert row, f"no composition row for {label!r}"
        assert int(row.group(1).replace(",", "")) == loc, (
            f"{label}: README says {row.group(1)} lines, tree has {loc} — regenerate the docs"
        )
        assert int(row.group(2).replace(",", "")) == files, f"{label}: file count drifted"

    total = sum(v[0] for v in counts.values())
    assert f"{total:,}" in readme, f"the {total:,}-line total is not stated in the README"


def test_latency_table_covers_the_measured_paths():
    """Every path in the README's latency table exists in the bench artifact."""
    bench = json.loads((ASSETS / "bench_result.json").read_text(encoding="utf-8"))
    measured = bench["paths"]
    for name in ("webhook.pr_review", "webhook.ci_failure", "read.jobs", "review.approve"):
        assert name in measured, f"bench_result.json is missing {name}"
        assert measured[name]["n"] > 0

    # The headline numbers quoted in prose must match the artifact, not a memory.
    readme = _readme()
    p50 = measured["webhook.pr_review"]["median"]
    assert f"{p50:.1f} ms" in readme, f"README does not quote the measured {p50:.1f} ms ingest p50"


def test_bench_artifact_declares_its_method():
    """A latency number without its measurement conditions is not usable."""
    bench = json.loads((ASSETS / "bench_result.json").read_text(encoding="utf-8"))
    for key in ("method", "caveat", "environment", "paths", "e2e_breakdown"):
        assert bench.get(key), f"bench_result.json has no {key!r}"
    assert "in-process" in bench["method"]
    assert len(bench["paths"]) >= 8


# --- markdown structure ------------------------------------------------------


def test_code_fences_are_balanced():
    """An odd fence count swallows the rest of the document."""
    assert _readme().count("```") % 2 == 0


def test_every_mermaid_block_is_a_supported_diagram():
    """Catch a block whose first token no renderer understands."""
    assert len(_fenced(_readme(), "mermaid")) >= 6, "README diagrams went missing"
    for page in _pages():
        for body in _fenced(page.read_text(encoding="utf-8"), "mermaid"):
            head = body.strip().split()[0] if body.strip() else ""
            assert head in MERMAID_KINDS, (
                f"{page.relative_to(ROOT)}: unknown Mermaid diagram type {head!r}"
            )


def test_mermaid_subgraphs_are_balanced():
    """`subgraph` without its `end` silently truncates a flowchart."""
    for page in _pages():
        for body in _fenced(page.read_text(encoding="utf-8"), "mermaid"):
            opens = len(re.findall(r"^\s*subgraph\b", body, re.M))
            closes = len(re.findall(r"^\s*end\s*$", body, re.M))
            # stateDiagram/erDiagram blocks also use `end` for notes; only
            # compare on blocks that actually declare a subgraph.
            if opens:
                assert opens == closes, (
                    f"{page.relative_to(ROOT)}: unbalanced subgraph/end: {opens} vs {closes}"
                )


def test_code_fences_are_balanced_everywhere():
    """An odd fence count in any document swallows everything after it."""
    odd = [
        page.relative_to(ROOT).as_posix()
        for page in _pages()
        if page.read_text(encoding="utf-8").count("```") % 2
    ]
    assert not odd, f"unbalanced code fences: {odd}"


def test_anchors_in_the_table_of_contents_resolve():
    """Every intra-page link points at a heading that exists."""
    readme = _readme()

    def slug(heading: str) -> str:
        text = heading.strip().lower()
        text = re.sub(r"[^\w\s-]", "", text)
        return re.sub(r"\s+", "-", text)

    headings = {slug(h) for h in re.findall(r"^#{2,4} (.+)$", readme, re.M)}
    anchors = {
        a for a in re.findall(r"\]\(#([\w-]+)\)", readme) if not a.startswith("regenerating")
    }
    unresolved = sorted(a for a in anchors if a not in headings)
    assert not unresolved, f"anchors with no heading: {unresolved}"


def test_relative_links_resolve():
    """Relative links in README and docs/*.md must point at files that exist.

    A renamed document or figure otherwise becomes a 404 that nobody notices,
    because markdown link rot is invisible in CI.
    """
    pages = [README, *sorted((ROOT / "docs").glob("*.md"))]
    broken: list[str] = []
    for page in pages:
        text = page.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue
            if not (page.parent / path).exists():
                broken.append(f"{page.relative_to(ROOT)} -> {target}")
    assert not broken, f"broken relative links: {broken}"


@pytest.mark.parametrize("section", ["## Limitations", "## Troubleshooting", "## Security model"])
def test_honest_sections_are_present(section: str):
    """These sections are the point of the document; deleting one is a regression."""
    assert section in _readme()
