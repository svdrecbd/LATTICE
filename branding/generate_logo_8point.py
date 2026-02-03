#!/usr/bin/env python3
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw


@dataclass(frozen=True)
class LogoSpec:
    # Base canvas
    width: int = 900
    height: int = 300
    scale: int = 4  # supersample for smoother edges

    # Star geometry (8-point)
    star_outer_r: int = 90
    inner_radius_ratio: float = 0.33  # deeper crevices
    star_gap: int = 16  # gap between star edges (used if align_star_ruler is False)
    align_star_ruler: bool = True  # keep level spacing based on cross-star ruler

    # Line geometry
    line_thickness: int = 0
    line_overhang: int = 0  # extra extension past geometric end

    # Style
    fg: tuple[int, int, int, int] = (12, 14, 16, 255)  # near-black
    bg: tuple[int, int, int, int] = (0, 0, 0, 0)  # transparent


def star_points_8(cx: float, cy: float, outer_r: float, inner_r: float) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    # 16 vertices: 8 outer + 8 inner, 22.5° step
    for i in range(16):
        angle = math.radians(-90 + i * 22.5)
        r = outer_r if i % 2 == 0 else inner_r
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return points


def render_logo(spec: LogoSpec, out_path: str) -> None:
    width = spec.width * spec.scale
    height = spec.height * spec.scale
    outer_r = spec.star_outer_r * spec.scale
    inner_r = int(outer_r * spec.inner_radius_ratio)

    if spec.align_star_ruler:
        center_dist = 2 * outer_r * math.sqrt(3)
        star_gap = center_dist - 2 * outer_r
    else:
        star_gap = spec.star_gap * spec.scale
        center_dist = 2 * outer_r + star_gap

    cx = width // 2
    cy = height // 2

    centers = [
        (cx - center_dist, cy),
        (cx, cy),
        (cx + center_dist, cy),
    ]

    img = Image.new("RGBA", (width, height), spec.bg)
    draw = ImageDraw.Draw(img)

    if spec.line_thickness > 0:
        # Draw line first so stars sit on top.
        line_overhang = spec.line_overhang * spec.scale
        line_thickness = spec.line_thickness * spec.scale
        align_offset = outer_r * math.sqrt(3)
        left = int(centers[0][0] - align_offset - line_overhang)
        right = int(centers[-1][0] + align_offset + line_overhang)
        y0 = cy - line_thickness // 2
        y1 = cy + line_thickness // 2
        draw.rectangle([left, y0, right, y1], fill=spec.fg)

    for sx, sy in centers:
        draw.polygon(star_points_8(sx, sy, outer_r, inner_r), fill=spec.fg)

    # Downsample for anti-aliasing.
    img = img.resize((spec.width, spec.height), Image.Resampling.LANCZOS)
    img.save(out_path, "PNG")

    print(f"inner_radius_ratio={spec.inner_radius_ratio}")
    print(f"inner_radius_px={inner_r // spec.scale}")
    print(f"gap_px={int(star_gap // spec.scale)}")


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "lattice-logo-8point.png"
    render_logo(LogoSpec(), str(out))
