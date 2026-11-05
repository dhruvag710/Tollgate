"""B4's chart (SPEC §9: "plot the curves; commit the chart"): false-hit rate
and recall against the threshold, for QQP and the adversarial set.

Two panels sharing the threshold axis, not two scales on one plot. The 2%
target is marked on the false-hit panel, and the chosen threshold on both.
Drawn twice, for a light and a dark page (BENCHMARKS.md shows the one that
matches the reader's theme); the sweep's markdown table is its table view.
Colors are the reference palette's first two categorical slots, validated
for both modes (CVD separation 24.7 light, 26.8 dark).
"""

from pathlib import Path
from typing import Any

from scoring import MAX_FALSE_HIT_RATE, Report

THEMES: dict[str, dict[str, str]] = {
    "light": {
        "surface": "#fcfcfb", "primary": "#0b0b0b", "secondary": "#52514e",
        "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7",
        "qqp": "#2a78d6", "adversarial": "#eb6834",
    },
    "dark": {
        "surface": "#1a1a19", "primary": "#f0efec", "secondary": "#c3c2b7",
        "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835",
        "qqp": "#3987e5", "adversarial": "#d95926",
    },
}  # fmt: skip
SERIES = (("qqp", "QQP"), ("adversarial", "Adversarial"))
SANS = ("Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans")  # the system sans, else matplotlib's


def _sans() -> str:
    """The first of SANS installed, found once (asking for a list logs a
    warning for every missing face on every label)."""
    from matplotlib import font_manager

    installed = {f.name for f in font_manager.fontManager.ttflist}
    return next((name for name in SANS if name in installed), "DejaVu Sans")


def draw(report: Report, title: str, path: Path, theme: str = "light") -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FormatStrFormatter, MultipleLocator, PercentFormatter

    c = THEMES[theme]
    plt.rcParams.update(
        {
            "font.family": _sans(),
            "font.size": 10,
            "axes.edgecolor": c["axis"],
            "axes.labelcolor": c["secondary"],
            "xtick.color": c["muted"],
            "ytick.color": c["muted"],
            "text.color": c["primary"],
        }
    )
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4), sharex=True, facecolor=c["surface"])
    thresholds = [row.threshold for row in report.rows]
    panels: list[tuple[Any, str, str]] = [
        (axes[0], "false_hit_rate", "False-hit rate (should not have hit)"),
        (axes[1], "recall", "Recall (should have hit)"),
    ]
    chosen = report.chosen
    for ax, measure, heading in panels:
        ax.set_facecolor(c["surface"])
        ax.grid(axis="y", color=c["grid"], linewidth=1, linestyle="-")
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(c["axis"])
        ax.tick_params(length=0)
        ax.set_ylim(0, 1.02)
        ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
        ax.set_xlim(min(thresholds) - 0.005, max(thresholds) + 0.005)
        ax.xaxis.set_major_locator(MultipleLocator(0.05))
        ax.xaxis.set_major_formatter(FormatStrFormatter("%.2f"))
        ax.set_xlabel("Similarity threshold")
        ax.set_title(heading, loc="left", color=c["primary"], fontsize=11, pad=10)
        curves = {
            key: [getattr(getattr(row, key), measure) for row in report.rows] for key, _ in SERIES
        }
        # Direct labels where the curves are furthest apart: at the right
        # edge they often converge on 0% and the labels would collide.
        both = [i for i in range(len(thresholds)) if None not in (curves["qqp"][i],
                curves["adversarial"][i])]  # fmt: skip
        spot = max(both, key=lambda i: abs(curves["qqp"][i] - curves["adversarial"][i]),
                   default=len(thresholds) // 2)  # fmt: skip
        for key, label in SERIES:
            values = curves[key]
            if all(v is None for v in values):
                continue  # e.g. no hard positives
            points = [(t, v) for t, v in zip(thresholds, values, strict=True) if v is not None]
            xs, ys = zip(*points, strict=True)
            ax.plot(xs, ys, color=c[key], linewidth=2, solid_capstyle="round", label=label)
            y = values[spot]
            if y is not None:
                # Above and to the right: both curves only fall as the
                # threshold rises, so that side of either is always clear.
                ax.annotate(
                    label, (thresholds[spot], y), xytext=(5, 6),
                    textcoords="offset points", color=c["secondary"], fontsize=9,
                )  # fmt: skip
            if chosen is not None:
                value = getattr(getattr(chosen, key), measure)
                if value is not None:
                    ax.scatter(
                        [chosen.threshold], [value], s=64, color=c[key],
                        edgecolors=c["surface"], linewidths=2, zorder=3,
                    )  # fmt: skip
        if chosen is not None:
            ax.axvline(chosen.threshold, color=c["muted"], linewidth=1, zorder=1)
            ax.annotate(
                f"chosen {chosen.threshold:.2f}", (chosen.threshold, 1.0),
                xytext=(4, -2), textcoords="offset points", va="top",
                color=c["secondary"], fontsize=9,
            )  # fmt: skip
    target = axes[0]
    top = 0.93
    if chosen is None:
        # Under the title, not on the plot: wherever it sat inside, some
        # model's curves ran through it.
        fig.text(
            0.01, 0.905, f"No threshold meets the {MAX_FALSE_HIT_RATE:.0%} target on both sets.",
            ha="left", va="top", color=c["secondary"], fontsize=10,
        )  # fmt: skip
        top = 0.86
    target.axhline(MAX_FALSE_HIT_RATE, color=c["muted"], linewidth=1, zorder=1)
    target.annotate(
        f"{MAX_FALSE_HIT_RATE:.0%} target", (min(thresholds), MAX_FALSE_HIT_RATE),
        xytext=(0, 4), textcoords="offset points", color=c["secondary"], fontsize=9,
    )  # fmt: skip
    handles, labels = axes[1].get_legend_handles_labels()
    if not handles:
        handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", frameon=False, ncol=2, labelcolor=c["secondary"])
    fig.suptitle(title, x=0.01, ha="left", color=c["primary"], fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, top))
    fig.savefig(path, dpi=150, facecolor=c["surface"])
    plt.close(fig)
