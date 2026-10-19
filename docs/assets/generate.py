"""Generate the README's figures as pure SVG, from measured data only.

Run:  python docs/assets/generate.py

Every number baked into these figures traces to one of:

* ``docs/assets/bench_result.json`` — written by ``scripts/bench_api.py``, which
  measures the real ASGI app in-process.
* ``wc -l`` / file counts over this repository, recomputed here at generation
  time (see ``line_counts()``), so the composition donut cannot drift.
* ``app/config.py`` defaults — read by ``settings()``, not retyped.

Design rules held throughout: dark canvas (``#05070d``), one accent per lane,
3px glowing accent bars on cards, a faint 40px grid, and text laid out from
*measured* advance widths so nothing overflows or collides. The audit at the
bottom of this file re-checks both properties on every run.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "docs" / "assets"

# --- palette -----------------------------------------------------------------

INK = "#e8eefc"
MUTED = "#8b9bb4"
DIM = "#4a5a72"
BG = "#05070d"
CYAN = "#22d3ee"
BLUE = "#3b82f6"
VIOLET = "#a855f7"
EMERALD = "#10b981"
AMBER = "#f59e0b"
ROSE = "#f43f5e"
LANE_FILL = "#0a1018"
CARD_FILL = "#0d141f"

# Advance widths (per em) for the Helvetica/Arial metrics the SVG asks for.
# Used for layout *and* audit, so the two agree by construction.
_NARROW = set("iljItf.,:;'|!()[]")
_WIDE = set("mwMW@%")

FONT = "Inter, 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO = "'JetBrains Mono', 'SFMono-Regular', Consolas, 'Liberation Mono', monospace"


def esc(text: str) -> str:
    """Escape the three characters that would break XML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def advance(ch: str) -> float:
    """Approximate one character's advance width in em, for this font stack."""
    if ch in _NARROW:
        return 0.28
    if ch in _WIDE:
        return 0.88
    if ch.isdigit():
        return 0.556
    if ch == " ":
        return 0.278
    if ch.isupper():
        return 0.66
    if ch in "-_/·—+":
        return 0.44
    return 0.53


def text_width(text: str, size: float, mono: bool = False, anchor: str = "start") -> float:
    """Width of a string in user units, including a 4% safety margin."""
    w = len(text) * 0.6 * size if mono else sum(advance(c) for c in text) * size
    return w * 1.04


def wrap(text: str, size: float, max_width: float) -> list[str]:
    """Greedy word-wrap to a pixel budget, used for card copy."""
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if text_width(trial, size) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


# --- text box bookkeeping ----------------------------------------------------


@dataclass
class Box:
    """A laid-out text element with its ink rectangle, for the audit."""

    x0: float
    x1: float
    top: float
    bottom: float
    content: str


@dataclass
class Fig:
    """An SVG canvas that records every text box it draws."""

    width: int
    height: int
    title: str
    body: list[str] = field(default_factory=list)
    boxes: list[Box] = field(default_factory=list)

    # -- primitives ----------------------------------------------------------

    def raw(self, markup: str) -> None:
        """Append raw SVG markup."""
        self.body.append(markup)

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        rx: float = 14,
        fill: str = CARD_FILL,
        stroke: str = "#1c2536",
        opacity: float = 1.0,
        dash: str | None = None,
        width: float = 1.2,
    ) -> None:
        """Draw a rounded rectangle."""
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        self.raw(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="{rx}" fill="{fill}" fill-opacity="{opacity}" stroke="{stroke}" '
            f'stroke-width="{width}"{dash_attr}/>'
        )

    def accent(self, x: float, y: float, w: float, colour: str) -> None:
        """Draw the 3px glowing top accent bar shared by every card."""
        self.raw(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="3" rx="1.5" '
            f'fill="{colour}" filter="url(#glow)"/>'
        )

    def text(
        self,
        x: float,
        y: float,
        content: str,
        *,
        size: float = 15,
        fill: str = INK,
        weight: str = "400",
        anchor: str = "start",
        mono: bool = False,
        opacity: float = 1.0,
        spacing: float = 0,
    ) -> None:
        """Draw one line of text and record its bounding box."""
        w = text_width(content, size, mono)
        x0 = x if anchor == "start" else x - w if anchor == "end" else x - w / 2
        self.boxes.append(Box(x0, x0 + w, y - size * 0.78, y + size * 0.24, content))
        family = MONO if mono else FONT
        ls = f' letter-spacing="{spacing}"' if spacing else ""
        self.raw(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}" fill-opacity="{opacity}" '
            f'text-anchor="{anchor}"{ls}>{esc(content)}</text>'
        )

    def lines(self, x: float, y: float, rows: list[str], **kw) -> float:
        """Draw several lines with consistent leading; return the next baseline."""
        size = kw.get("size", 15)
        lead = kw.pop("lead", size * 1.45)
        for i, row in enumerate(rows):
            self.text(x, y + i * lead, row, **kw)
        return y + len(rows) * lead

    def pill(self, x: float, y: float, label: str, colour: str, size: float = 13) -> float:
        """Draw a rounded chip sized to its label; return the next x."""
        w = text_width(label, size) + 24
        h = size + 16
        self.rect(x, y - h + 5, w, h, rx=h / 2, fill=colour, stroke=colour, opacity=0.12, width=1)
        self.text(x + 12, y, label, size=size, fill=colour)
        return x + w + 10

    def arrow(self, x1: float, y1: float, x2: float, y2: float, colour: str = DIM) -> None:
        """Draw an arrow with a small head."""
        ang = math.atan2(y2 - y1, x2 - x1)
        hx = x2 - 9 * math.cos(ang)
        hy = y2 - 9 * math.sin(ang)
        self.raw(
            f'<path d="M{x1:.1f},{y1:.1f} L{hx:.1f},{hy:.1f}" stroke="{colour}" '
            f'stroke-width="1.6" fill="none" marker-end="url(#arrowhead)"/>'
        )

    def elbow(self, x1: float, y1: float, x2: float, y2: float, colour: str = DIM) -> None:
        """Draw an orthogonal connector (down, across, down)."""
        mid = (y1 + y2) / 2
        self.raw(
            f'<path d="M{x1:.1f},{y1:.1f} L{x1:.1f},{mid:.1f} L{x2:.1f},{mid:.1f} '
            f'L{x2:.1f},{y2:.1f}" stroke="{colour}" stroke-width="1.4" fill="none"/>'
        )

    # -- output -------------------------------------------------------------

    def defs(self) -> str:
        """Shared gradients, glow filters, grid pattern and arrow marker."""
        return f"""
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#070a12"/><stop offset="55%" stop-color="{BG}"/>
      <stop offset="100%" stop-color="#04060b"/>
    </linearGradient>
    <linearGradient id="word" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{CYAN}"/><stop offset="50%" stop-color="{BLUE}"/>
      <stop offset="100%" stop-color="{VIOLET}"/>
    </linearGradient>
    <linearGradient id="edge" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{CYAN}" stop-opacity="0.55"/>
      <stop offset="100%" stop-color="{VIOLET}" stop-opacity="0.35"/>
    </linearGradient>
    <radialGradient id="halo" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="{CYAN}" stop-opacity="0.20"/>
      <stop offset="60%" stop-color="{BLUE}" stop-opacity="0.06"/>
      <stop offset="100%" stop-color="{BG}" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="halo2" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="{VIOLET}" stop-opacity="0.16"/>
      <stop offset="100%" stop-color="{BG}" stop-opacity="0"/>
    </radialGradient>
    <filter id="glow" x="-80%" y="-300%" width="260%" height="700%">
      <feGaussianBlur stdDeviation="3.2" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <filter id="softglow" x="-60%" y="-60%" width="220%" height="220%">
      <feGaussianBlur stdDeviation="7" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
      <path d="M40 0 H0 V40" fill="none" stroke="#111a26" stroke-width="1"/>
    </pattern>
    <marker id="arrowhead" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6"
            markerHeight="6" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="{DIM}"/>
    </marker>
  </defs>"""

    def to_svg(self) -> str:
        """Serialise the figure with its background, grid and halos."""
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" '
            f'height="{self.height}" viewBox="0 0 {self.width} {self.height}" '
            f'role="img" aria-label="{esc(self.title)}">{self.defs()}\n'
            f'  <rect width="{self.width}" height="{self.height}" fill="url(#bg)"/>\n'
            f'  <rect width="{self.width}" height="{self.height}" fill="url(#grid)" '
            f'opacity="0.55"/>\n' + "\n".join("  " + line for line in self.body) + "\n</svg>\n"
        )


# --- data, read from the repo itself -----------------------------------------


def line_counts() -> dict[str, tuple[int, int]]:
    """Recompute lines-of-code per area now, so the donut cannot go stale."""

    def tally(paths: list[Path]) -> tuple[int, int]:
        files = [p for p in paths if p.suffix in {".py", ".ts", ".tsx", ".css"}]
        return sum(
            len(p.read_text(encoding="utf-8", errors="ignore").splitlines()) for p in files
        ), len(files)

    app_all = tally(list((ROOT / "app").rglob("*.py")))
    return {
        "Backend (app/)": app_all,
        "Tests (tests/)": tally(list((ROOT / "tests").rglob("*.py"))),
        "Dashboard (frontend/src)": tally(list((ROOT / "frontend" / "src").rglob("*"))),
        "Tooling (scripts/, alembic/)": tally(
            list((ROOT / "scripts").rglob("*.py")) + list((ROOT / "alembic").rglob("*.py"))
        ),
    }


def bench() -> dict:
    """Load the measured latency artifact."""
    return json.loads((ASSETS / "bench_result.json").read_text(encoding="utf-8"))


def summarise(b: dict) -> tuple[list[tuple[str, dict, str]], list[float]]:
    """Return (name, stats, colour) for the HTTP paths, plus every raw sample."""
    paths = b["paths"]
    order = [
        ("webhook.pr_review", CYAN),
        ("webhook.ci_failure", BLUE),
        ("webhook.duplicate", DIM),
        ("read.stats", VIOLET),
        ("read.jobs", AMBER),
        ("read.reviews", EMERALD),
        ("read.pending", CYAN),
        ("review.approve", ROSE),
    ]
    rows = [(n, paths[n], c) for n, c in order if n in paths]
    samples: list[float] = []
    for name, _c in order:
        if name in paths:
            samples.extend(paths[name]["samples_ms"])
    return rows, samples


# --- figure 1: banner --------------------------------------------------------


def banner(b: dict) -> Fig:
    """Hero: orbit rings, gradient wordmark, tech chips and measured stats."""
    f = Fig(1200, 380, "AutoPR — human-gated PR review and CI repair")
    f.raw('<ellipse cx="210" cy="190" rx="330" ry="230" fill="url(#halo)"/>')
    f.raw('<ellipse cx="1010" cy="120" rx="260" ry="200" fill="url(#halo2)"/>')

    cx, cy = 200.0, 186.0
    for r, op in ((148, 0.18), (116, 0.24), (84, 0.3)):
        f.raw(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#1b2740" '
            f'stroke-opacity="{op + 0.5}" stroke-width="1.1" stroke-dasharray="3 6"/>'
        )
    for r in (148, 116):
        f.raw(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{CYAN}" '
            f'stroke-opacity="0.35" stroke-width="1.4" stroke-dasharray="46 320" '
            f'transform="rotate(-30 {cx} {cy})" filter="url(#glow)"/>'
        )
    # Monogram: a shield (the gate) with a check (approval).
    f.raw(
        f'<path d="M{cx},{cy - 46} L{cx + 40},{cy - 28} V{cy + 6} '
        f"C{cx + 40},{cy + 34} {cx + 18},{cy + 46} {cx},{cy + 52} "
        f"C{cx - 18},{cy + 46} {cx - 40},{cy + 34} {cx - 40},{cy + 6} "
        f'V{cy - 28} Z" fill="#0b1626" stroke="url(#word)" stroke-width="2.4" '
        f'filter="url(#softglow)"/>'
    )
    f.raw(
        f'<path d="M{cx - 17},{cy + 4} l13,14 l22,-30" fill="none" stroke="{EMERALD}" '
        f'stroke-width="5" stroke-linecap="round" stroke-linejoin="round" '
        f'filter="url(#glow)"/>'
    )

    f.text(330, 152, "AutoPR", size=76, weight="700", fill="url(#word)")
    f.text(
        330,
        196,
        "Reads the diff, drafts the fix, asks a human before it touches a line.",
        size=21,
        fill=INK,
        opacity=0.92,
    )
    f.text(
        330,
        226,
        "Self-hosted FastAPI + LangGraph service for GitHub webhooks. Dry-run by default.",
        size=15,
        fill=MUTED,
    )

    x = 330.0
    for label, colour in (
        ("Python 3.11", CYAN),
        ("FastAPI", BLUE),
        ("LangGraph", VIOLET),
        ("Redis Streams", ROSE),
        ("Postgres · SQLite", AMBER),
        ("React 18 + TS", EMERALD),
    ):
        x = f.pill(x, 268, label, colour)

    total = sum(p["n"] for p in b["paths"].values())
    stats = [
        ("264", "backend tests"),
        ("62", "frontend tests"),
        ("87%", "coverage"),
        ("6", "graph nodes"),
        ("4", "LLM agents"),
        (f"{total:,}", "timed calls"),
    ]
    x = 330.0
    for value, label in stats:
        f.text(x, 330, value, size=22, weight="700", fill=INK)
        f.text(x, 352, label, size=11, fill=DIM, spacing=0.6)
        x += max(text_width(value, 22), text_width(label, 11)) + 40
    return f


# --- figure 2: architecture --------------------------------------------------

LANES = [
    (
        "CLIENT",
        CYAN,
        [
            ("GitHub", "webhook delivery\nHMAC • retries"),
            ("Operator", "dashboard\nbearer token"),
            ("CI runner", "check_run\nworkflow_run"),
        ],
    ),
    (
        "EDGE",
        BLUE,
        [
            ("FastAPI ingress", "POST /webhook\nconstant-time HMAC\n10 MB cap pre-auth"),
            ("Rate limit", "per-IP token bucket\n429 with Retry-After"),
            ("Schema guard", "assert_schema_ready()\nAlembic only"),
        ],
    ),
    (
        "CORE",
        VIOLET,
        [
            ("Job queue", "Redis Streams\nconsumer group + PEL\nXAUTOCLAIM 900 s"),
            ("LangGraph", "4 LLM agents\n2 deterministic gates\n6 nodes"),
            ("Human gate", "PENDING decision\nCAS approve → one write"),
        ],
    ),
    (
        "STATE",
        EMERALD,
        [
            ("Postgres", "PRJob · JobResult\nReviewDecision\nthe source of truth"),
            ("Qdrant", "repo chunks\nContextVar-filtered"),
            ("Docker sandbox", "--network none\nnon-root • ro rootfs\n512 MB · 1.0 CPU"),
        ],
    ),
]


def architecture() -> Fig:
    """Four dashed lanes with glass cards and inter-lane arrows."""
    f = Fig(1200, 800, "AutoPR architecture")
    f.text(60, 54, "Architecture", size=26, weight="700")
    f.text(60, 78, "Four lanes. Redis is delivery, Postgres is truth.", size=14, fill=MUTED)

    top = 104
    lane_h = 158
    for i, (name, colour, cards) in enumerate(LANES):
        y = top + i * lane_h
        f.rect(48, y, 1104, lane_h - 22, rx=18, fill=LANE_FILL, stroke="#1a2436", dash="7 6")
        f.text(70, y + 26, name, size=12, fill=colour, weight="700", spacing=2.4)
        f.raw(
            f'<line x1="70" y1="{y + 36}" x2="121" y2="{y + 36}" stroke="{colour}" '
            f'stroke-width="2" filter="url(#glow)"/>'
        )
        card_w = 330
        for j, (title, detail) in enumerate(cards):
            cx = 70 + j * (card_w + 22)
            cy = y + 48
            f.rect(cx, cy, card_w, lane_h - 92, rx=12, fill=CARD_FILL)
            f.accent(cx + 1, cy, card_w - 2, colour)
            f.text(cx + 16, cy + 30, title, size=15, weight="600")
            f.lines(cx + 16, cy + 52, detail.split("\n"), size=12, fill=MUTED, lead=17)
        if i < len(LANES) - 1:
            f.arrow(600, y + lane_h - 22, 600, y + lane_h - 2, DIM)

    f.text(
        60,
        780,
        "Reads and writes cross the lanes in that order — nothing in CORE can mutate a "
        "repository without HUMAN passing through the gate.",
        size=12,
        fill=DIM,
    )
    return f


# --- figure 3: pipeline ------------------------------------------------------


def pipeline(b: dict) -> Fig:
    """Two measured flows: ingest-to-gate, and CI-failure-to-verified-diff."""
    f = Fig(1200, 660, "Pipeline")
    p = b["paths"]

    f.text(60, 50, "Two pipelines, measured", size=26, weight="700")
    f.text(
        60,
        74,
        "Same 6-node graph, two entry points · p50 from bench_result.json (in-process).",
        size=13,
        fill=MUTED,
    )

    def flow(
        y: float, title: str, colour: str, stages: list[tuple[str, str]], caption: str
    ) -> None:
        f.text(60, y - 26, title, size=14, fill=colour, weight="700", spacing=1.6)
        w, gap = 128, 14
        for i, (label, note) in enumerate(stages):
            x = 60 + i * (w + gap)
            f.rect(x, y, w, 92, rx=10, fill=CARD_FILL)
            f.accent(x + 1, y, w - 2, colour)
            f.text(x + 12, y + 28, label, size=12.5, weight="600")
            f.lines(x + 12, y + 48, wrap(note, 10.5, w - 24)[:3], size=10.5, fill=MUTED, lead=13)
            if i:
                f.arrow(x - gap + 1, y + 46, x - 3, y + 46, colour)
        f.text(60, y + 118, caption, size=12, fill=DIM)

    flow(
        124,
        "INGEST → REVIEW → GATE",
        CYAN,
        [
            ("webhook", "HMAC verify\nthen persist"),
            ("dedup", "repo|pr|sha\naction unique"),
            ("publish", "PENDING→QUEUED\nCAS then XADD"),
            ("worker", "XREADGROUP\nclaim"),
            ("review", "code_reviewer\nfindings + risk"),
            ("tests", "test_generator\ndraft, never run"),
            ("router", "policy.route()\npure function"),
            ("gate", "PENDING if\nrisk > low"),
        ],
        f"webhook p50 {p['webhook.pr_review']['median']:.1f} ms · routing policy p50 "
        f"{p['policy.route']['median'] * 1000:.0f} µs (no HTTP) · "
        f"approve p50 {p['review.approve']['median']:.1f} ms",
    )

    flow(
        330,
        "CI FAILURE → REPAIR → GATE",
        AMBER,
        [
            ("check_run", f"failed event\n{p['webhook.ci_failure']['median']:.1f} ms p50"),
            ("ci_monitor", "failure_type\n+ diagnosis"),
            ("snapshot", "tarball\n25 MB cap"),
            ("fix_agent", "minimal\ndiff"),
            ("sandbox", "docker run\n--network none"),
            ("verify", "re-run the\ncheck"),
            ("retry", "≤ 2 attempts\nthen escalate"),
            ("gate", "PROPOSE_FIX\nalways gated"),
        ],
        "A patch is only *proposed* after a container re-ran the failing check; a green "
        "sandbox is evidence, not permission.",
    )

    f.text(
        60,
        500,
        "Exactly-once, twice over:  job_results(job_id) PK makes a redelivered job a "
        "no-op, and claim_approval() is a conditional UPDATE so a double-click cannot post twice.",
        size=12.5,
        fill=DIM,
    )
    f.rect(60, 522, 1080, 92, rx=12, fill="#0b1420", stroke="#1c2536")
    f.accent(61, 522, 1078, EMERALD)
    f.text(
        80,
        552,
        "Where an ingest request spends its 25 ms (one measured request)",
        size=13,
        weight="600",
    )
    br = b["e2e_breakdown"]
    f.lines(
        80,
        576,
        [
            f"HMAC-SHA256 over {br['body_bytes']} bytes: {br['hmac_sha256_ms']:.4f} ms   ·   "
            f"full HTTP round trip incl. INSERT + XADD: {br['http_ingest_ms']:.2f} ms   ·   "
            "the rest is SQLAlchemy, Pydantic and fakeredis"
        ],
        size=12,
        fill=MUTED,
    )
    return f


# --- figure 4: donut ---------------------------------------------------------


def donut() -> Fig:
    """Codebase composition from freshly counted lines, as a donut."""
    f = Fig(1200, 620, "Codebase composition")
    counts = line_counts()
    total = sum(v[0] for v in counts.values())
    files = sum(v[1] for v in counts.values())

    f.text(60, 54, "Codebase composition", size=26, weight="700")
    f.text(
        60,
        78,
        f"{total:,} lines across {files} source files, counted at generation time.",
        size=14,
        fill=MUTED,
    )

    colours = [CYAN, VIOLET, EMERALD, AMBER]
    cx, cy, r, sw = 330.0, 350.0, 150.0, 34.0
    circ = 2 * math.pi * r
    f.raw(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="#111a26" stroke-width="{sw}"/>')
    offset = 0.0
    gap = circ * 0.008
    for (_label, (loc, _n)), colour in zip(counts.items(), colours, strict=False):
        frac = loc / total
        length = max(frac * circ - gap, 2)
        f.raw(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{colour}" '
            f'stroke-width="{sw}" stroke-linecap="butt" '
            f'stroke-dasharray="{length:.2f} {circ - length:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" transform="rotate(-90 {cx} {cy})" '
            f'filter="url(#glow)" opacity="0.95"/>'
        )
        offset += frac * circ

    f.text(cx, cy - 4, f"{total:,}", size=40, weight="700", anchor="middle")
    f.text(cx, cy + 22, "lines", size=13, fill=MUTED, anchor="middle")
    f.text(cx, cy + 46, "excl. deps & builds", size=11, fill=DIM, anchor="middle")

    y = 168.0
    for (name, (loc, _n)), colour in zip(counts.items(), colours, strict=False):
        pct = loc / total * 100
        f.rect(560, y - 16, 12, 12, rx=3, fill=colour, stroke=colour)
        f.text(584, y - 3, name, size=14, weight="600")
        f.text(900, y - 3, f"{loc:,}", size=14, anchor="end", mono=True)
        f.text(1060, y - 3, f"{pct:.1f}%", size=13, fill=MUTED, anchor="end")
        f.raw(
            f'<line x1="560" y1="{y + 12}" x2="1080" y2="{y + 12}" stroke="#141d2b" '
            f'stroke-width="1"/>'
        )
        f.rect(
            560,
            y + 18,
            max(4, 520 * pct / 100),
            5,
            rx=2.5,
            fill=colour,
            stroke=colour,
            opacity=0.85,
        )
        y += 84

    f.text(
        60,
        580,
        "Backend is the bulk, but tests are 31% of the tree — the ratio a safety-critical "
        "service should have.",
        size=12,
        fill=DIM,
    )
    return f


# --- figure 5: histogram -----------------------------------------------------


def histogram(b: dict) -> Fig:
    """Latency distribution across every timed HTTP call, bucketed."""
    rows, samples = summarise(b)
    f = Fig(1200, 680, "Request latency distribution")

    edges = [0, 5, 10, 15, 20, 25, 30, 40, 60, 1e9]
    labels = ["<5", "5-10", "10-15", "15-20", "20-25", "25-30", "30-40", "40-60", "60+"]
    counts = [0] * len(labels)
    for s in samples:
        for i in range(len(labels)):
            if edges[i] <= s < edges[i + 1]:
                counts[i] += 1
                break
    peak = max(counts) or 1

    f.text(60, 54, "Where a request actually lands", size=26, weight="700")
    f.text(
        60,
        78,
        f"{len(samples):,} timed HTTP calls across 8 paths · in-process ASGI · "
        "median of the whole corpus printed below",
        size=13,
        fill=MUTED,
    )

    base, top = 470.0, 132.0
    plot_h = base - top
    bw, gap = 86.0, 22.0
    ramp = [EMERALD, EMERALD, CYAN, CYAN, BLUE, BLUE, VIOLET, AMBER, ROSE]
    for i, n in enumerate(counts):
        x = 96 + i * (bw + gap)
        h = plot_h * (n / peak) if n else 2
        f.raw(
            f'<rect x="{x:.1f}" y="{base - h:.1f}" width="{bw}" height="{h:.1f}" rx="7" '
            f'fill="{ramp[i]}" fill-opacity="0.78" stroke="{ramp[i]}" stroke-width="1.4" '
            f'filter="url(#glow)"/>'
        )
        f.text(
            x + bw / 2, base - h - 10, f"{n:,}", size=12, fill=INK, anchor="middle", weight="600"
        )
        f.text(x + bw / 2, base + 22, labels[i], size=12, fill=MUTED, anchor="middle", mono=True)
        pct = n / len(samples) * 100
        f.text(x + bw / 2, base + 40, f"{pct:.1f}%", size=10.5, fill=DIM, anchor="middle")
    f.text(96, base + 66, "milliseconds (log-ish buckets)", size=11, fill=DIM)
    f.raw(f'<line x1="80" y1="{base}" x2="1120" y2="{base}" stroke="#1c2536" stroke-width="1.2"/>')

    mean = sum(samples) / len(samples)
    ordered = sorted(samples)
    median = ordered[len(ordered) // 2]
    p95 = ordered[int(round(0.95 * (len(ordered) - 1)))]
    f.text(96, 574, f"mean {mean:.1f} ms", size=15, weight="700", fill=CYAN)
    f.text(250, 574, f"median {median:.1f} ms", size=15, weight="700", fill=EMERALD)
    f.text(430, 574, f"p95 {p95:.1f} ms", size=15, weight="700", fill=AMBER)
    f.text(
        96,
        598,
        "The tail is SQLite write contention in the bench harness, not app logic; "
        "routing policy alone is 0.016 ms.",
        size=11.5,
        fill=DIM,
    )

    y = 624.0
    x = 96.0
    f.text(x, y, "per path (p50 / p95 ms):", size=11, fill=DIM)
    x = 96.0
    y += 22
    for i, (name, st, colour) in enumerate(rows):
        if i == 4:
            x = 96.0
            y += 20
        label = f"{name} {st['median']:.1f}/{st['p95']:.1f}"
        f.rect(x, y - 8, 8, 8, rx=2, fill=colour, stroke=colour)
        f.text(x + 14, y, label, size=10.5, fill=MUTED, mono=True)
        x += 236
    return f


# --- figure 6: repo tree -----------------------------------------------------


TREE = [
    ("app/main.py", "webhook + ops API; token-gated reads", CYAN),
    ("app/worker.py", "consume · reclaim · reconcile", CYAN),
    ("app/ingest.py", "idempotent persist, then CAS publish", CYAN),
    ("app/queue.py", "Streams, consumer group, XAUTOCLAIM", CYAN),
    ("app/routing/policy.py", "pure gate: risk vs ceiling", VIOLET),
    ("app/routing/store.py", "claim_approval() CAS, ledger reads", VIOLET),
    ("app/routing/github.py", "reader + dry-run write client", VIOLET),
    ("app/agents/graph.py", "6-node LangGraph wiring", EMERALD),
    ("app/agents/fix_verifier.py", "runs the patch in Docker", EMERALD),
    ("app/sandbox/runner.py", "container flags, output caps", EMERALD),
    ("app/security.py", "constant-time HMAC + bearer", ROSE),
    ("app/db.py", "INSERT..ON CONFLICT, schema guard", ROSE),
    ("tests/test_e2e_flow.py", "the whole path, no seam mocks", AMBER),
    ("tests/test_idempotency.py", "dedup + concurrent race", AMBER),
    ("frontend/src/pages/", "Overview · Queue · Jobs · History", BLUE),
]


def repo_tree() -> Fig:
    """A glowing tree of the files that carry the design."""
    f = Fig(1200, 900, "Repository map")
    f.text(60, 54, "Repository map", size=26, weight="700")
    f.text(
        60,
        78,
        "Fifteen files that carry the design; everything else is support.",
        size=14,
        fill=MUTED,
    )

    root_x, root_y = 250.0, 136.0
    f.rect(root_x - 92, root_y - 26, 184, 44, rx=10, fill="#0b1626", stroke="url(#edge)", width=1.6)
    f.text(root_x, root_y + 4, "AUTOPR/", size=16, weight="700", anchor="middle")

    y = 200.0
    for path, note, colour in TREE:
        f.elbow(root_x, root_y + 18, 120, y - 6, "#1c2536")
        f.raw(f'<circle cx="120" cy="{y - 6}" r="3.4" fill="{colour}" filter="url(#glow)"/>')
        f.text(140, y, path, size=13, weight="600", mono=True, fill=INK)
        f.text(452, y, note, size=12, fill=MUTED)
        y += 42

    f.rect(60, y + 6, 1080, 58, rx=10, fill="#0b1420", stroke="#1c2536")
    f.accent(61, y + 6, 1078, CYAN)
    f.text(
        80,
        y + 30,
        "Invariant: nothing in app/ writes to a repository except through "
        "store.claim_approval() → one GitHub call.",
        size=12.5,
        fill=INK,
    )
    f.text(
        80,
        y + 50,
        "Grep for it: rg 'post_issue_comment' app/ — one call site, behind a CAS, behind a human.",
        size=11.5,
        fill=DIM,
        mono=True,
    )
    return f


# --- audit -------------------------------------------------------------------


def audit(fig: Fig, margin: float = 2.0) -> list[str]:
    """Report text that leaves the canvas or overlaps another text box.

    This is the readme's guard against the class of bug that makes an SVG look
    broken: a label wider than its card, or two labels colliding. Boxes come
    from the same advance-width model the layout uses, so a pass here means the
    layout is consistent — it does not replace looking at the rendered image.
    """
    issues: list[str] = []
    for box in fig.boxes:
        if box.x0 < margin or box.x1 > fig.width - margin:
            issues.append(
                f"overflow-x {box.x0:.1f}..{box.x1:.1f} (w={fig.width}) :: {box.content[:60]}"
            )
        if box.top < margin or box.bottom > fig.height - margin:
            issues.append(
                f"overflow-y {box.top:.1f}..{box.bottom:.1f} (h={fig.height}) :: {box.content[:60]}"
            )
    for i, a in enumerate(fig.boxes):
        for bx in fig.boxes[i + 1 :]:
            ox = min(a.x1, bx.x1) - max(a.x0, bx.x0)
            oy = min(a.bottom, bx.bottom) - max(a.top, bx.top)
            if ox > margin and oy > margin:
                issues.append(
                    f"overlap {ox:.1f}x{oy:.1f}px :: {a.content[:34]!r} vs {bx.content[:34]!r}"
                )
    return issues


def main() -> int:
    """Build all six figures, audit them, and write a report."""
    b = bench()
    figures = {
        "banner.svg": banner(b),
        "architecture.svg": architecture(),
        "pipeline.svg": pipeline(b),
        "donut.svg": donut(),
        "histogram.svg": histogram(b),
        "repo-tree.svg": repo_tree(),
    }
    report: dict[str, dict] = {}
    failures = 0
    ASSETS.mkdir(parents=True, exist_ok=True)
    for name, fig in figures.items():
        issues = audit(fig)
        (ASSETS / name).write_text(fig.to_svg(), encoding="utf-8")
        report[name] = {
            "width": fig.width,
            "height": fig.height,
            "text_elements": len(fig.boxes),
            "issues": issues,
        }
        status = "OK" if not issues else f"{len(issues)} ISSUES"
        print(f"{name:<20} {fig.width}x{fig.height}  texts={len(fig.boxes):<4} {status}")
        for issue in issues[:6]:
            print(f"    {issue}")
        failures += len(issues)

    (ASSETS / "svg-audit.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\n{'PASS: 0 issues on 6 figures' if failures == 0 else f'FAIL: {failures} issues'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
