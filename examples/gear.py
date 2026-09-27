# Technical drawing of a small spur gear (m1, z12): exact involute teeth, section, gear data table.
# Usage: python examples/gear.py | localidraw new "spur gear" --open
import json
import math
from typing import Any

Point = tuple[float, float]

# ---- gear parameters (ISO 53 standard basic rack, full-depth)
m, z, alpha = 1.0, 12, math.radians(20)  # module [mm], teeth, pressure angle
b, bore, hub_d, hub_w = 6.0, 4.0, 8.0, 4.0  # face width, bore, hub diameter, hub length (one side)
key_w, key_h = 1.5, 0.8  # keyway (width, depth into hub from bore)
d = m * z  # pitch diameter 12
da = d + 2 * m  # tip 14
df = d - 2.5 * m  # root 9.5
db = d * math.cos(alpha)  # base 11.276
S = 22  # drawing scale: 22 canvas units per mm (≈ 22:1 on the sheet)

CODE = 3
INK, THIN, CENTER, DIM, HATCH = "#1e1e1e", "#1e1e1e", "#e03131", "#1971c2", "#495057"
els: list[dict[str, Any]] = []


def line(
    pts: list[Point], color: str = INK, width: float = 2, dashed: str | None = None, *, closed: bool = False
) -> dict[str, Any]:
    if closed:
        pts = [*pts, pts[0]]
    x0, y0 = pts[0]
    rel = [[round(x - x0, 2), round(y - y0, 2)] for x, y in pts]
    e = {
        "type": "line",
        "version": 1,
        "versionNonce": 1,
        "seed": 1,
        "x": x0,
        "y": y0,
        "width": max(p[0] for p in rel) - min(p[0] for p in rel),
        "height": max(p[1] for p in rel) - min(p[1] for p in rel),
        "points": rel,
        "strokeColor": color,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": width,
        "strokeStyle": dashed or "solid",
        "roughness": 0,
        "opacity": 100,
        "angle": 0,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "isDeleted": False,
        "boundElements": None,
        "updated": 1,
        "link": None,
        "locked": False,
        "startBinding": None,
        "endBinding": None,
        "startArrowhead": None,
        "endArrowhead": None,
        "lastCommittedPoint": None,
    }
    els.append(e)
    return e


def text(x: float, y: float, t: str, size: float = 16, color: str = INK, align: str = "left") -> None:
    els.append(
        {
            "type": "text",
            "x": x,
            "y": y,
            "text": t,
            "fontSize": size,
            "fontFamily": CODE,
            "strokeColor": color,
            "textAlign": align,
        }
    )


def rect(x: float, y: float, w: float, h: float, width: float = 2, color: str = INK, bg: str = "transparent") -> None:
    els.append(
        {
            "type": "rectangle",
            "x": x,
            "y": y,
            "width": w,
            "height": h,
            "strokeColor": color,
            "backgroundColor": bg,
            "fillStyle": "solid",
            "strokeWidth": width,
            "roughness": 0,
            "roundness": None,
        }
    )


def circle(
    cx: float, cy: float, r: float, color: str = INK, width: float = 2, dashed: str | None = None, n: int = 180
) -> None:
    line(
        [(cx + r * math.cos(t), cy + r * math.sin(t)) for t in [2 * math.pi * i / n for i in range(n)]],
        color,
        width,
        dashed,
        closed=True,
    )


def arrowhead(x: float, y: float, ang: float, color: str = DIM, size: float = 9) -> None:
    a1, a2 = ang + math.radians(160), ang - math.radians(160)
    line(
        [
            (x + size * math.cos(a1), y + size * math.sin(a1)),
            (x, y),
            (x + size * math.cos(a2), y + size * math.sin(a2)),
        ],
        color,
        1.5,
    )


def dim_h(x1: float, x2: float, y: float, label: str, ext_from: float | None = None) -> None:
    """Horizontal dimension line at y between x1 and x2, arrows inward-pointing at the ends."""
    line([(x1, y), (x2, y)], DIM, 1.5)
    arrowhead(x1, y, math.pi)
    arrowhead(x2, y, 0)
    if ext_from is not None:
        for x in (x1, x2):
            line([(x, ext_from), (x, y + (6 if y > ext_from else -6))], DIM, 1)
    text((x1 + x2) / 2 - len(label) * 4.8, y - 26, label, 16, DIM)


def dim_v(y1: float, y2: float, x: float, label: str, ext_from: float | None = None) -> None:
    line([(x, y1), (x, y2)], DIM, 1.5)
    arrowhead(x, y1, -math.pi / 2)
    arrowhead(x, y2, math.pi / 2)
    if ext_from is not None:
        for y in (y1, y2):
            line([(ext_from, y), (x + (6 if x > ext_from else -6), y)], DIM, 1)
    text(x + 10, (y1 + y2) / 2 - 38, label, 16, DIM)


# ---- involute tooth profile (exact), mirrored, repeated z times
def inv(a: float) -> float:
    return math.tan(a) - a


rb, ra, rf, r = db / 2, da / 2, df / 2, d / 2
half_thick = math.pi / (2 * z) + inv(alpha)  # half tooth angle at the base circle, from tooth centre


def flank(sign: int, rot: float) -> list[Point]:
    pts = []
    for i in range(24):
        rr = rb + (ra - rb) * i / 23
        phi = math.acos(rb / rr)
        th = rot + sign * (half_thick - inv(phi))
        pts.append((rr, th))
    return pts


cx, cy = 520, 560  # front view centre
outline: list[Point] = []
for k in range(z):
    rot = 2 * math.pi * k / z
    left = flank(-1, rot)[::-1]
    right = flank(+1, rot)
    # root: radial line from base circle down to root circle on each side (root < base for z=12)
    a_left, a_right = rot - half_thick, rot + half_thick
    outline += [(rf, a_left - 0.06), (rf, a_left), (rb, a_left)] if False else []
    outline += [(rf, a_left), (rb, a_left), *left[::-1][::-1]] if False else []
    seg = [(rf, rot - half_thick), *flank(-1, rot)[::-1][::-1]]
    tooth = [(rf, rot - half_thick), *flank(-1, rot), *flank(+1, rot)[::-1], (rf, rot + half_thick)]
    # flank(-1) runs base→tip on the leading side; flank(+1) reversed runs tip→base on the trailing side
    tooth = [(rf, rot - half_thick), *flank(-1, rot), *flank(+1, rot)[::-1], (rf, rot + half_thick)]
    outline += tooth
    # root arc to the next tooth
    nxt = 2 * math.pi * (k + 1) / z - half_thick
    outline += [(rf, rot + half_thick + (nxt - rot - half_thick) * j / 6) for j in range(1, 6)]
# flank(-1): th = rot - (half - inv(phi)) → moves toward rot as r grows; flank(+1) symmetric: correct orientation
pts = [(cx + rr * S * math.cos(t - math.pi / 2), cy + rr * S * math.sin(t - math.pi / 2)) for rr, t in outline]
line(pts, INK, 2.5, closed=True)

# bore with keyway (keyway at top)
kb = bore / 2
ang = math.asin((key_w / 2) / kb)
bore_pts = [
    (cx + kb * S * math.cos(t), cy + kb * S * math.sin(t))
    for t in [(-math.pi / 2 + ang) + (2 * math.pi - 2 * ang) * i / 120 for i in range(121)]
]
top = cy - (kb + key_h) * S
bore_pts += [
    (cx - key_w / 2 * S, bore_pts[-1][1]),
    (cx - key_w / 2 * S, top),
    (cx + key_w / 2 * S, top),
    (cx + key_w / 2 * S, bore_pts[0][1]),
]
line(bore_pts, INK, 2.5, closed=True)
# pitch circle (thin chain), hub circle hidden behind? hub visible on front: hub_d circle solid
circle(cx, cy, r * S, CENTER, 1, "dashed")
circle(cx, cy, hub_d / 2 * S, INK, 1.5, "dashed")  # hub is on the back face: hidden line
# centre lines
ext = (ra + 1.2) * S
line([(cx - ext, cy), (cx + ext, cy)], CENTER, 1, "dashed")
line([(cx, cy - ext), (cx, cy + ext)], CENTER, 1, "dashed")
# front view dimensions: tip diameter, pitch diameter (leader), bore
dim_v(cy - ra * S, cy + ra * S, cx - ra * S - 70, f"⌀{da:g}", ext_from=cx)
text(cx + r * S * 0.72 + 30, cy - r * S * 0.72 - 58, f"⌀{d:g} pitch", 16, CENTER)
line([(cx + r * S * 0.707, cy - r * S * 0.707), (cx + r * S * 0.72 + 26, cy - r * S * 0.72 - 32)], CENTER, 1)
dim_h(cx - kb * S, cx + kb * S, cy + ra * S + 110, f"⌀{bore:g} H7", ext_from=cy + kb * S)
dim_h(cx - key_w / 2 * S, cx + key_w / 2 * S, cy - ra * S - 70, f"{key_w:g}", ext_from=top)
text(cx - 60, cy + ra * S + 170, "FRONT VIEW", 18, INK)

# ---- section A–A (side view), to the right, same scale, aligned with the front view centre
sx = 1080  # left face x of the gear body
body_l, body_r = sx, sx + b * S
hub_r = body_r + hub_w * S


def hatch(x1: float, y1: float, x2: float, y2: float, step: int = 14) -> None:
    """45° hatch inside an axis-aligned rectangle."""
    w, h = x2 - x1, y2 - y1
    for k in range(-int(h), int(w), step):
        a = (x1 + max(k, 0), y1 + max(-k, 0))
        bb = (x1 + min(k + h, w), y1 + min(h, w - k))
        if a[0] < bb[0]:
            line([a, bb], HATCH, 1)


# gear body: solid from bore to root circle across the face width; teeth shown to tip (upper and lower),
# no hatch in teeth (convention)
for sgn in (-1, 1):
    bore_edge = kb + key_h if sgn < 0 else kb  # keyway at the top
    y_bore, y_root, y_tip = cy + sgn * bore_edge * S, cy + sgn * rf * S, cy + sgn * ra * S
    y_hub = cy + sgn * hub_d / 2 * S
    top_, bot_ = sorted((y_bore, y_root))
    rect(body_l, top_, b * S, bot_ - top_, 2)
    hatch(body_l, top_, body_r, bot_)
    t1, t2 = sorted((y_root, y_tip))
    rect(body_l, t1, b * S, t2 - t1, 2)  # teeth zone (unhatched)
    h1, h2 = sorted((y_bore, y_hub))
    rect(body_r, h1, hub_w * S, h2 - h1, 2)  # hub
    hatch(body_r, h1, hub_r, h2)
# the cut runs through the keyway: beyond the cut you see the edges where the keyway
# walls meet the bore (at √(r² − (w/2)²) from the axis), and the part's end faces
# close the empty keyway/bore space at the front and back
y_edge = cy - math.sqrt(kb**2 - (key_w / 2) ** 2) * S
line([(body_l, y_edge), (hub_r, y_edge)], INK, 2)
for x in (body_l, hub_r):
    line([(x, cy - (kb + key_h) * S), (x, cy + kb * S)], INK, 2)
line([(body_l - 30, cy), (hub_r + 30, cy)], CENTER, 1, "dashed")  # axis
line([(body_l, cy - r * S), (body_r, cy - r * S)], CENTER, 1, "dashed")  # pitch line
line([(body_l, cy + r * S), (body_r, cy + r * S)], CENTER, 1, "dashed")
# section dimensions
dim_h(body_l, body_r, cy - ra * S - 60, f"{b:g}", ext_from=cy - ra * S)
dim_h(body_l, hub_r, cy + ra * S + 60, f"{b + hub_w:g}", ext_from=cy + hub_d / 2 * S)
dim_v(cy - hub_d / 2 * S, cy + hub_d / 2 * S, hub_r + 60, f"⌀{hub_d:g}", ext_from=hub_r)
dim_v(cy - rf * S, cy + rf * S, body_l - 60, f"⌀{df:g}", ext_from=body_l)
text(body_l + 40, cy + ra * S + 170, "SECTION A–A", 18, INK)
# cutting plane indicator on the front view
for yy, lab in ((cy - ext - 16, "A"), (cy + ext + 16, "A")):
    line([(cx - 10, yy), (cx + 10, yy)], INK, 3)
    text(cx + 20, yy - 12, lab, 18)

# ---- gear data table
tx, ty = 1650, 140
rows = [
    ("GEAR DATA", ""),
    ("module", f"m = {m:g} mm"),
    ("teeth", f"z = {z}"),
    ("pressure angle", "α = 20°"),
    ("profile", "ISO 53, involute"),
    ("pitch ⌀", f"d = {d:g} mm"),
    ("tip ⌀", f"da = {da:g} mm"),
    ("root ⌀", f"df = {df:g} mm"),
    ("base ⌀", f"db = {db:.3f} mm"),
    ("face width", f"b = {b:g} mm"),
    ("accuracy", "ISO 1328-1 class 8"),
]
rect(tx, ty, 420, 36 * len(rows), 2)
for i, (key, v) in enumerate(rows):
    if i:
        line([(tx, ty + 36 * i), (tx + 420, ty + 36 * i)], INK, 1)
    text(tx + 14, ty + 36 * i + 8, key, 16, INK)
    text(tx + 200, ty + 36 * i + 8, v, 16)
line([(tx + 190, ty + 36), (tx + 190, ty + 36 * len(rows))], INK, 1)

# ---- notes
text(
    1650,
    600,
    "NOTES\n1. all dimensions in mm\n2. third-angle projection\n3. general tolerances ISO 2768-m\n"
    "4. break sharp edges 0.2 × 45°\n5. material: POM-C (acetal)\n6. keyway 1.5 JS9",
    16,
)

# ---- sheet frame + title block
W, H = 2140, 1200
rect(-40, -40, W, H, 4)
rect(-20, -20, W - 40, H - 40, 1.5)
bx, by, bw, bh = W - 40 - 640, H - 40 - 150, 620, 130
rect(bx, by, bw, bh, 2)
for yy in (by + 44, by + 88):
    line([(bx, yy), (bx + bw, yy)], INK, 1)
for xx in (bx + 310, bx + 460):
    line([(xx, by + 44), (xx, by + bh)], INK, 1)
text(bx + 14, by + 10, "SPUR GEAR  m1 z12", 24)
text(bx + 14, by + 54, "drawn: localidraw", 14)
text(bx + 324, by + 54, "scale: enlarged", 14)
text(bx + 474, by + 54, "sheet 1/1", 14)
text(bx + 14, by + 98, "part no. G-M1-Z12", 14)
text(bx + 324, by + 98, "mass ≈ 1 g", 14)
text(bx + 474, by + 98, "rev A", 14)
text(-10, -10, " ", 1)

print(json.dumps(els))
