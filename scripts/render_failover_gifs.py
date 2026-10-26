"""Renders the two failover GIFs for 模型网关容灾方案对比：APIM vs LiteLLM.md.

Both GIFs tell the same four-phase story so they can be compared side by side:
normal traffic, region 1 runs out of capacity (429), region 2 also fails and traffic
overflows to region 3, then recovery. The layout follows the AI-Gateway sample
``backend-pool-load-balancing.gif``, on a white background for light-theme documents.

Usage: python scripts/render_failover_gifs.py [output_dir]   (default: images/)
Requires Pillow and a CJK font (Microsoft YaHei on Windows, Noto Sans CJK elsewhere).
"""

from __future__ import annotations

import math
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H, S = 960, 460, 2  # S: supersampling factor for anti-aliasing
FPS = 20
T = 26.0  # loop length in seconds
PHASE = T / 4
N_REQ = 48  # requests per loop; divisible by 2 and 3 so the loop is seamless
INTERVAL = T / N_REQ
SPEED = 170.0  # px/s
SYNC = 1.4  # LiteLLM: seconds from cooldown to every replica knowing about it

BG = (255, 255, 255)
PANEL = (205, 211, 219)
TEXT = (31, 35, 41)
MUTED = (102, 110, 122)
LINE = (160, 188, 212)
POOL = (225, 238, 250)
GREEN = (24, 163, 84)
RED = (214, 48, 64)
ORANGE = (226, 135, 0)
GREY = (178, 184, 193)
BOX = (248, 250, 252)
AZURE = (0, 120, 212)
REQUEST = AZURE  # request dots
WHITE = (255, 255, 255)

REGION_Y = [140, 230, 320]
REGION_X = 680

_FONT_CANDIDATES = {
    False: [
        "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ],
    True: [
        "C:/Windows/Fonts/msyhbd.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ],
}
_fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _fonts:
        path = next((p for p in _FONT_CANDIDATES[bold] if Path(p).exists()), None)
        if path is None:
            raise SystemExit("No CJK font found; edit _FONT_CANDIDATES")
        _fonts[key] = ImageFont.truetype(path, size * S)
    return _fonts[key]


Point = tuple[float, float]
Color = tuple[int, ...]


class Canvas:
    def __init__(self) -> None:
        self.img = Image.new("RGB", (W * S, H * S), BG)
        self.d = ImageDraw.Draw(self.img, "RGBA")

    @staticmethod
    def _p(q: Point) -> tuple[float, float]:
        return (q[0] * S, q[1] * S)

    @staticmethod
    def _b(b: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        return (b[0] * S, b[1] * S, b[2] * S, b[3] * S)

    def rrect(self, b, r, fill=None, outline=None, width=1.0) -> None:  # type: ignore[no-untyped-def]
        self.d.rounded_rectangle(
            self._b(b), r * S, fill=fill, outline=outline, width=round(width * S)
        )

    def ellipse(self, b, fill=None, outline=None, width=1.0) -> None:  # type: ignore[no-untyped-def]
        self.d.ellipse(self._b(b), fill=fill, outline=outline, width=round(width * S))

    def circle(self, c: Point, r: float, fill=None, outline=None, width=1.0) -> None:  # type: ignore[no-untyped-def]
        self.ellipse((c[0] - r, c[1] - r, c[0] + r, c[1] + r), fill, outline, width)

    def text(
        self,
        xy: Point,
        s: str,
        size: int,
        fill: Color = TEXT,
        bold: bool = False,
        anchor: str = "mm",
    ) -> None:
        self.d.text(self._p(xy), s, font=font(size, bold), fill=fill, anchor=anchor)

    def text_width(self, s: str, size: int, bold: bool = False) -> float:
        return self.d.textlength(s, font=font(size, bold)) / S

    def line(self, pts: list[Point], fill: Color, width: float = 1.5) -> None:
        self.d.line(
            [self._p(q) for q in pts], fill=fill, width=max(1, round(width * S)), joint="curve"
        )

    def dashed(
        self, a: Point, b: Point, fill: Color, width: float = 1.2, dash: float = 5, gap: float = 4
    ) -> None:
        length = math.dist(a, b)
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        pos = 0.0
        while pos < length:
            end = min(pos + dash, length)
            self.line(
                [(a[0] + ux * pos, a[1] + uy * pos), (a[0] + ux * end, a[1] + uy * end)],
                fill,
                width,
            )
            pos = end + gap

    def dashed_rect(self, b: tuple[float, float, float, float], fill: Color) -> None:
        x0, y0, x1, y1 = b
        for a, c in [
            ((x0, y0), (x1, y0)),
            ((x1, y0), (x1, y1)),
            ((x1, y1), (x0, y1)),
            ((x0, y1), (x0, y0)),
        ]:
            self.dashed(a, c, fill, 1.3, 7, 5)

    def arrow(self, a: Point, b: Point, fill: Color, width: float = 1.5, head: float = 7) -> None:
        ang = math.atan2(b[1] - a[1], b[0] - a[0])
        base = (b[0] - head * math.cos(ang), b[1] - head * math.sin(ang))
        self.line([a, base], fill, width)
        left = (base[0] + head * 0.55 * math.sin(ang), base[1] - head * 0.55 * math.cos(ang))
        right = (base[0] - head * 0.55 * math.sin(ang), base[1] + head * 0.55 * math.cos(ang))
        self.d.polygon([self._p(b), self._p(left), self._p(right)], fill=fill)

    def frame(self) -> Image.Image:
        return self.img.resize((W, H), Image.Resampling.LANCZOS)


# -- moving dots -----------------------------------------------------------------------------


@dataclass
class Leg:
    pts: list[Point]
    color: Color
    label: str | None = None
    fixed_duration: float | None = None
    lens: list[float] = field(init=False)
    length: float = field(init=False)
    duration: float = field(init=False)

    def __post_init__(self) -> None:
        self.lens = [math.dist(a, b) for a, b in zip(self.pts, self.pts[1:], strict=False)]
        self.length = sum(self.lens)
        self.duration = self.fixed_duration or self.length / SPEED

    def at(self, fraction: float) -> Point:
        remaining = max(0.0, min(1.0, fraction)) * self.length
        for (a, b), seg in zip(zip(self.pts, self.pts[1:], strict=False), self.lens, strict=False):
            if remaining <= seg or seg == 0:
                f = remaining / seg if seg else 0
                return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            remaining -= seg
        return self.pts[-1]


@dataclass
class Dot:
    start: float
    legs: list[Leg]
    radius: float = 4.5

    @property
    def end(self) -> float:
        return self.start + sum(leg.duration for leg in self.legs)

    def locate(self, t: float) -> tuple[Point, Leg, float] | None:
        elapsed = t - self.start
        if elapsed < 0:
            return None
        for leg in self.legs:
            if elapsed <= leg.duration:
                return leg.at(elapsed / leg.duration), leg, elapsed
            elapsed -= leg.duration
        return None


def draw_dots(c: Canvas, dots: list[Dot], t: float) -> None:
    for dot in dots:
        here = dot.locate(t)
        if here is None:
            continue
        pos, leg, _ = here
        for i, (alpha, shrink) in enumerate([(130, 1.0), (85, 1.6), (45, 2.2)], start=1):
            trail = dot.locate(t - 0.06 * i)
            if trail is not None:
                c.circle(trail[0], dot.radius - shrink, fill=(*trail[1].color, alpha))
        c.circle(pos, dot.radius * 2.2, fill=(*leg.color, 55))
        c.circle(pos, dot.radius, fill=leg.color)


def draw_labels(c: Canvas, dots: list[Dot], t: float) -> None:
    for dot in dots:
        here = dot.locate(t)
        if here is None:
            continue
        _, leg, leg_elapsed = here
        if leg.label and leg_elapsed < 1.3:
            x, y = leg.pts[0]
            c.rrect((x - 52, y - 30, x - 16, y - 12), 4, fill=(*RED, 230))
            c.text((x - 34, y - 21), leg.label, 11, WHITE, bold=True)


# -- region timeline -------------------------------------------------------------------------


@dataclass
class Region:
    name: str
    y: float
    overflow: bool
    fail_from: float | None = None
    trip: float | None = None  # filled in from the first observed 429
    reset: float | None = None

    def failing(self, t: float) -> bool:
        t %= T
        return (
            self.fail_from is not None
            and self.reset is not None
            and (self.fail_from <= t < self.reset)
        )

    def tripped(self, t: float, delay: float = 0.0) -> bool:
        t %= T
        return (
            self.trip is not None
            and self.reset is not None
            and (self.trip + delay <= t < self.reset)
        )


def make_regions() -> list[Region]:
    return [
        Region("区域 1", REGION_Y[0], False, fail_from=PHASE, reset=3 * PHASE),
        Region("区域 2", REGION_Y[1], False, fail_from=2 * PHASE, reset=3 * PHASE),
        Region("区域 3", REGION_Y[2], True),
    ]


def pick(
    regions: list[Region],
    blocked: Callable[[Region, float], bool],
    t: float,
    k: int,
    exclude: set[int],
) -> int:
    primary = [
        i
        for i, r in enumerate(regions)
        if not r.overflow and i not in exclude and not blocked(r, t)
    ]
    if primary:
        return primary[k % len(primary)]
    return next(i for i, r in enumerate(regions) if r.overflow)


def route(
    regions: list[Region],
    blocked: Callable[[Region, float], bool],
    k: int,
    first: Leg,
    t: float,
    decision: Point,
    forward: Callable[[int], list[Point]],
) -> tuple[list[Leg], list[tuple[int, float, float]]]:
    """Forward to a region; on a 429 bounce back to ``decision`` and retry once elsewhere."""
    legs, bounces, exclude = [first], [], set()
    for attempt in range(2):
        i = pick(regions, blocked, t, k, exclude)
        out = Leg([decision, *forward(i)], REQUEST)
        legs.append(out)
        t += out.duration
        if attempt == 0 and regions[i].failing(t) and not blocked(regions[i], t - out.duration):
            back = Leg(list(reversed(out.pts)), RED, label="429")
            legs.append(back)
            bounces.append((i, t, back.duration))
            t += back.duration
            exclude.add(i)
            continue
        break
    return legs, bounces


def overflow_active(regions: list[Region], t: float, delay: float) -> bool:
    return all(o.tripped(t, delay) or o.failing(t) for o in regions if not o.overflow)


def region_status(
    regions: list[Region], i: int, t: float, blocked_word: str, delay: float
) -> tuple[str, Color]:
    r = regions[i]
    tm = t % T
    if r.overflow:
        if overflow_active(regions, t, delay):
            return "接收溢出流量", GREEN
        return "待命 · 溢出备用", MUTED
    if r.tripped(t):
        return blocked_word, ORANGE
    if r.failing(t):
        return "容量不足 · 返回 429", RED
    if r.reset is not None and r.reset <= tm < r.reset + 2.5:
        return "已恢复 · 接收流量", GREEN
    return "正常 · 接收流量", GREEN


# -- shared furniture ------------------------------------------------------------------------


def draw_header(c: Canvas, title: str, cooled_word: str) -> None:
    c.text((24, 26), title, 16, TEXT, bold=True, anchor="lm")
    x = 936.0
    for label, color in reversed([("请求", REQUEST), ("429 响应", RED), (cooled_word, ORANGE)]):
        w = c.text_width(label, 11)
        x -= w
        c.text((x, 26), label, 11, MUTED, anchor="lm")
        c.circle((x - 9, 26), 4, fill=color)
        x -= 26


def draw_client(c: Canvas, box: tuple[float, float, float, float]) -> None:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    c.rrect(box, 8, fill=BOX, outline=PANEL, width=1.2)
    c.text((cx, cy - 10), "客户端", 13, TEXT, bold=True)
    c.text((cx, cy + 11), "OpenAI SDK", 10, MUTED)


def draw_regions(
    c: Canvas, regions: list[Region], t: float, blocked_word: str, delay: float
) -> None:
    for i, r in enumerate(regions):
        status, color = region_status(regions, i, t, blocked_word, delay)
        y = r.y
        border = color if color in (RED, ORANGE) else PANEL
        c.rrect((REGION_X, y - 36, 920, y + 36), 8, fill=BOX, outline=border, width=1.4)
        # Generic model-deployment glyph: a hexagon with a core.
        hx, hy = REGION_X + 30, y
        hexagon = [
            (
                hx + 14 * math.cos(math.pi / 3 * n + math.pi / 6),
                hy + 14 * math.sin(math.pi / 3 * n + math.pi / 6),
            )
            for n in range(7)
        ]
        c.line(hexagon, AZURE, 1.8)
        c.circle((hx, hy), 4, fill=AZURE)
        c.text((REGION_X + 56, y - 11), f"{r.name} · 模型部署", 13, TEXT, bold=True, anchor="lm")
        c.circle((REGION_X + 60, y + 13), 3.5, fill=color)
        c.text((REGION_X + 70, y + 13), status, 11, color, anchor="lm")


def draw_caption(c: Canvas, captions: list[str], t: float) -> None:
    c.rrect((24, 400, 936, 446), 10, fill=(244, 246, 249), outline=(222, 226, 232), width=1)
    phase = min(3, int((t % T) // PHASE))
    progress = ((t % T) - phase * PHASE) / PHASE
    for n in range(4):
        x0 = 40 + n * 26
        c.rrect((x0, 419, x0 + 20, 427), 4, fill=(214, 219, 226))
        if n < phase:
            c.rrect((x0, 419, x0 + 20, 427), 4, fill=AZURE)
        elif n == phase:
            c.rrect((x0, 419, x0 + max(8, 20 * progress), 427), 4, fill=AZURE)
    c.text((156, 423), captions[phase], 13, TEXT, anchor="lm")


def visible(dots: list[Dot], t: float) -> list[Dot]:
    return [d for d in dots if d.start <= t <= d.end + 0.2]


# -- scenario A: APIM backend pool -----------------------------------------------------------

A_CLIENT = (24, 195, 120, 265)
A_ROUTER = (246, 230)
A_PILL_X = (380, 560)


def a_forward(i: int) -> list[Point]:
    y = REGION_Y[i]
    return [(A_PILL_X[0], y), (A_PILL_X[1], y), (REGION_X + 18, y)]


def build_a(regions: list[Region]) -> list[Dot]:
    dots, first_bounce = [], {}
    blocked = lambda r, t: r.tripped(t)  # noqa: E731 - the breaker acts immediately
    for k in range(-10, N_REQ):
        s = k * INTERVAL
        first = Leg([(A_CLIENT[2], 230), A_ROUTER], REQUEST)
        legs, bounces = route(
            regions, blocked, k % N_REQ, first, s + first.duration, A_ROUTER, a_forward
        )
        dots.append(Dot(s, legs))
        for i, at, _ in bounces:
            # The breaker trips when the 429 reaches the gateway (the member pill).
            first_bounce.setdefault(i, (at + (REGION_X + 18 - A_PILL_X[1]) / SPEED) % T)
    for i, at in first_bounce.items():
        regions[i].trip = at
    return dots


def render_a() -> list[Image.Image]:
    regions = make_regions()
    build_a(regions)  # first pass finds when each breaker trips
    dots = build_a(regions)
    captions = [
        "正常：后端池把请求分给优先级 1 的区域 1、区域 2，区域 3 待命",
        "区域 1 容量不足返回 429 → retry 策略换区域 2 重发；熔断器跳闸，区域 1 暂停接流量",
        "区域 2 也熔断 → 优先级 1 全部不可用，流量自动溢出到优先级 2 的区域 3",
        "熔断到期自动恢复，流量回到优先级 1；网关由微软托管，客户只维护策略和配置",
    ]
    frames = []
    for f in range(int(T * FPS)):
        t = f / FPS
        c = Canvas()
        draw_header(c, "方案 A：APIM 后端池（Azure 托管）", "熔断")
        c.arrow((A_CLIENT[2], 230), (A_ROUTER[0] - 18, 230), LINE, 1.6)
        for y in REGION_Y:
            c.arrow((A_PILL_X[1], y), (REGION_X - 2, y), LINE, 1.4)
        c.rrect((170, 56, 600, 388), 14, outline=PANEL, width=1.3)
        # Azure-style cloud glyph.
        for cx, cy, r in [(196, 82, 7), (205, 77, 9), (214, 83, 7)]:
            c.circle((cx, cy), r, fill=(0, 120, 212))
        c.text((228, 80), "APIM 网关", 14, TEXT, bold=True, anchor="lm")
        c.text((228, 101), "Standard v2 · 微软托管 · 跨可用区", 11, MUTED, anchor="lm")
        c.ellipse((196, 150, 416, 310), fill=POOL)
        for r in regions:
            usable = overflow_active(regions, t, 0.0) if r.overflow else not r.tripped(t)
            c.arrow(
                (A_ROUTER[0] + 16, A_ROUTER[1] + (r.y - 230) * 0.12),
                (A_PILL_X[0] - 4, r.y),
                GREEN if usable else GREY,
                4.5,
                11,
            )
        c.circle(A_ROUTER, 15, fill=GREEN)
        c.text((262, 268), "后端池", 13, TEXT, bold=True)
        c.text((262, 287), "优先级 / 权重", 10, (70, 105, 140))
        c.text((385, 366), "熔断 + 遇到 429 换区域重试（retry 策略）", 11, MUTED)

        draw_dots(c, visible(dots, t), t)

        for r in regions:
            tripped = r.tripped(t)
            c.rrect(
                (A_PILL_X[0], r.y - 15, A_PILL_X[1], r.y + 15),
                15,
                fill=WHITE,
                outline=RED if tripped else (90, 120, 150),
                width=1.5,
            )
            label = f"{r.name} · 优先级 {2 if r.overflow else 1}"
            c.text((A_PILL_X[0] + 78, r.y), label, 12, TEXT if not tripped else MUTED)
            if tripped or r.failing(t):
                dot_color = RED
            elif r.overflow and not overflow_active(regions, t, 0.0):
                dot_color = GREY
            else:
                dot_color = GREEN
            c.circle((A_PILL_X[1] - 17, r.y), 6, fill=dot_color)
            if tripped:
                c.line([(A_PILL_X[1] - 20, r.y), (A_PILL_X[1] - 14, r.y)], WHITE, 2)
        draw_client(c, A_CLIENT)
        draw_regions(c, regions, t, "熔断中 · 暂停 60 秒", 0.0)
        draw_labels(c, visible(dots, t), t)
        draw_caption(c, captions, t)
        frames.append(c.frame())
    return frames


# -- scenario B: self-hosted LiteLLM ---------------------------------------------------------

B_CLIENT = (24, 195, 120, 265)
B_INGRESS = (148, 200, 238, 260)
REPLICA_Y = [165, 245, 325]
REPLICA_X = (290, 420)
REDIS = (485, 70, 595, 130)
REDIS_PORT = (505, 128)


def replica_port(r: int) -> Point:
    return (REPLICA_X[1] - 6, REPLICA_Y[r] - 24)


def b_forward(i: int) -> list[Point]:
    return [(REGION_X + 18, REGION_Y[i])]


def build_b(regions: list[Region]) -> tuple[list[Dot], dict[int, int]]:
    dots, first_bounce = [], {}
    blocked = lambda r, t: r.tripped(t, SYNC)  # noqa: E731 - replicas learn via Redis
    for k in range(-10, N_REQ):
        s = k * INTERVAL
        rep = k % 3
        y = REPLICA_Y[rep]
        first = Leg(
            [(B_CLIENT[2], 230), (B_INGRESS[2], 230), (REPLICA_X[0], y), (REPLICA_X[1], y)], REQUEST
        )
        legs, bounces = route(
            regions,
            blocked,
            (k % N_REQ) // 3,
            first,
            s + first.duration,
            (REPLICA_X[1], y),
            b_forward,
        )
        dots.append(Dot(s, legs))
        for i, at, back in bounces:
            first_bounce.setdefault(i, ((at + back) % T, rep))
    origins = {}
    for i, (at, rep) in first_bounce.items():
        regions[i].trip = at
        origins[i] = rep
    return dots, origins


def sync_pulses(regions: list[Region], origins: dict[int, int]) -> list[Dot]:
    pulses = []
    for i, rep in origins.items():
        trip = regions[i].trip
        assert trip is not None
        up = SYNC / 2
        pulses.append(
            Dot(trip, [Leg([replica_port(rep), REDIS_PORT], ORANGE, fixed_duration=up)], 3.5)
        )
        for r in range(3):
            pulses.append(
                Dot(
                    trip + up,
                    [Leg([REDIS_PORT, replica_port(r)], ORANGE, fixed_duration=SYNC - up)],
                    3.5,
                )
            )
    return pulses


def render_b() -> list[Image.Image]:
    regions = make_regions()
    build_b(regions)
    dots, origins = build_b(regions)
    pulses = sync_pulses(regions, origins)
    captions = [
        "正常：入口把请求分给多个 LiteLLM 副本，副本路由到主力区域 1、区域 2",
        "区域 1 返回 429 → LiteLLM 重试到区域 2；冷却状态写入 Redis，所有副本一起跳过区域 1",
        "区域 2 也进入冷却 → 主力区域全部不可用，流量溢出到区域 3",
        "冷却到期，流量恢复；LiteLLM 副本、Redis、容器平台都由客户自己运维",
    ]
    frames = []
    for f in range(int(T * FPS)):
        t = f / FPS
        c = Canvas()
        draw_header(c, "方案 B：自建 LiteLLM 网关（客户自己运维）", "冷却")
        c.arrow((B_CLIENT[2], 230), (B_INGRESS[0] - 2, 230), LINE, 1.6)
        for y in REPLICA_Y:
            c.arrow((B_INGRESS[2], 230), (REPLICA_X[0] - 2, y), LINE, 1.4)
            for ry in REGION_Y:
                c.line([(REPLICA_X[1], y), (REGION_X, ry)], (*LINE, 170), 1.1)
        c.dashed_rect((262, 56, 612, 388), PANEL)
        c.text((282, 80), "LiteLLM 网关", 14, TEXT, bold=True, anchor="lm")
        c.text((282, 101), "路由 · 冷却 · 重试", 11, MUTED, anchor="lm")
        c.text(
            (276, 372),
            "容器平台 · 例如 Azure Container Apps · 客户自己运维",
            10,
            MUTED,
            anchor="lm",
        )
        for r in range(3):
            c.dashed(replica_port(r), REDIS_PORT, (205, 90, 90, 170), 1.1, 4, 4)

        draw_dots(c, visible(dots, t) + visible(pulses, t), t)

        # Redis cylinder.
        x0, y0, x1, y1 = REDIS
        red_fill, red_line = (253, 236, 236), (205, 80, 80)
        c.rrect((x0, y0 + 6, x1, y1 - 6), 2, fill=red_fill)
        c.ellipse((x0, y1 - 14, x1, y1), fill=red_fill, outline=red_line, width=1.3)
        c.rrect((x0 + 0.6, y0 + 6, x1 - 0.6, y1 - 7), 0, fill=red_fill)
        c.line([(x0, y0 + 7), (x0, y1 - 7)], red_line, 1.3)
        c.line([(x1, y0 + 7), (x1, y1 - 7)], red_line, 1.3)
        c.ellipse((x0, y0, x1, y0 + 14), fill=(249, 222, 224), outline=red_line, width=1.3)
        cooled = [r.name for r in regions if r.tripped(t)]
        c.text((540, 94), "Redis", 12, TEXT, bold=True)
        c.text(
            (540, 111),
            "冷却：" + ("、".join(n.replace("区域 ", "区域") for n in cooled) if cooled else "无"),
            10,
            ORANGE if cooled else MUTED,
        )

        for rep, y in enumerate(REPLICA_Y):
            c.rrect(
                (REPLICA_X[0], y - 24, REPLICA_X[1], y + 24), 8, fill=BOX, outline=PANEL, width=1.2
            )
            c.text((REPLICA_X[0] + 12, y - 9), "LiteLLM", 12, TEXT, bold=True, anchor="lm")
            c.text(
                (REPLICA_X[0] + 12, y + 11),
                f"副本 {'N' if rep == 2 else rep + 1}",
                10,
                MUTED,
                anchor="lm",
            )
            # What this replica currently believes about each region.
            for i, r in enumerate(regions):
                px = REPLICA_X[0] + 74 + i * 15
                if r.tripped(t, SYNC):
                    color = ORANGE
                elif r.overflow and not overflow_active(regions, t, SYNC):
                    color = GREY
                else:
                    color = GREEN
                c.rrect((px, y + 6, px + 10, y + 16), 2, fill=color)
                c.text((px + 5, y - 6), str(i + 1), 8, MUTED)
        c.rrect(B_INGRESS, 8, fill=BOX, outline=PANEL, width=1.2)
        c.text((193, 220), "入口", 13, TEXT, bold=True)
        c.text((193, 241), "Ingress / LB", 10, MUTED)
        draw_client(c, B_CLIENT)
        draw_regions(c, regions, t, "冷却中 · 跳过该部署", SYNC)
        draw_labels(c, visible(dots, t), t)
        draw_caption(c, captions, t)
        frames.append(c.frame())
    return frames


# -- output ----------------------------------------------------------------------------------


TRANSPARENT = 255  # palette index reserved for "unchanged since the previous frame"


def save_gif(frames: list[Image.Image], path: Path) -> None:
    # One shared palette avoids colour flicker between frames.
    samples = frames[:: max(1, len(frames) // 12)]
    sheet = Image.new("RGB", (W, H * len(samples)))
    for n, f in enumerate(samples):
        sheet.paste(f, (0, n * H))
    palette = sheet.quantize(colors=TRANSPARENT, method=Image.Quantize.MEDIANCUT)
    indexed = [np.asarray(f.quantize(palette=palette, dither=Image.Dither.NONE)) for f in frames]
    colors = palette.getpalette() or []
    colors = colors[: TRANSPARENT * 3] + [0, 0, 0] * (256 - TRANSPARENT)
    # Each frame stores only the pixels that changed since the previous one; the rest are
    # transparent and show the previous frame through (disposal 1 = leave in place).
    out = []
    for n, arr in enumerate(indexed):
        delta = arr.copy()
        if n:
            delta[arr == indexed[n - 1]] = TRANSPARENT
        img = Image.fromarray(delta, mode="P")
        img.putpalette(colors)
        out.append(img)
    out[0].save(
        path,
        save_all=True,
        append_images=out[1:],
        duration=1000 // FPS,
        loop=0,
        optimize=False,
        disposal=1,
        transparency=TRANSPARENT,
    )
    print(f"{path}  {len(frames)} frames  {path.stat().st_size / 1024:.0f} KB")


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent / "images"
    out.mkdir(parents=True, exist_ok=True)
    save_gif(render_a(), out / "apim-backend-pool-failover.gif")
    save_gif(render_b(), out / "litellm-failover.gif")


if __name__ == "__main__":
    main()
