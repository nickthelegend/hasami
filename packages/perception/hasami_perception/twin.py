"""A digital twin from one photo: the table plane from an A4 sheet, objects from SAM 2.1 automatic masks,
and each object fitted as an upright box or cylinder with a footprint, height and mean colour.

Geometry only (numpy + SAM 2.1); the simulator side lives in hasami_core.twin.

1. Sheet. `find_sheet` finds the largest bright, unsaturated quadrilateral (or the caller passes four
   clicked corners). `sheet_frame` fits the plane homography (realcam.homography) for both ways an A4 sheet
   can lie and keeps the one whose camera pose is a rotation (with the assumed field of view, the two
   columns of K^-1 H must be orthogonal and of equal length); this also gives the camera pose.
2. Masks. A grid of points on the table plane around the sheet is projected into the photo; points on
   the paper or on table-coloured pixels are skipped; the rest prompt SAM 2.1 (one click each, the image
   encoded once). A prompt that lands inside an accepted mask is skipped; for each prompt the
   best-scored hypothesis that is not mostly paper or table is kept.
3. Fit. For a trial height h, the footprint is the set of plane points whose column from the table to
   h projects inside the mask (single-view carving). A box (minimum-area rectangle) and a cylinder (disc)
   are fitted to it, extruded to h, projected back and compared with the mask (IoU). The height (unless
   typed) and the shape are those with the best IoU. A single silhouette cannot tell a box from a flat
   cut-out of its outline, but the upright-box / cylinder prior can: at the wrong height the carved
   footprint is not a rectangle or a disc. Objects that fit best below 8 mm are flat (shadows, paper,
   stickers) and dropped, as are masks no shape explains (silhouette IoU < 0.6, or a side over 160 mm).

Known limits (also in the docs): white objects on the sheet and objects the colour of the table are not
prompted; an object covering a sheet corner breaks the automatic corner search (click the corners); the
field of view is assumed (70 deg horizontal by default) unless the caller knows it; non-convex objects
are fitted by their convex footprint.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from .colour import components, name_colour, rgb_to_hsv
from .locate import footprint_yaw
from .realcam import a4_corners, apply_h, homography, intrinsics_from_fov, pose_from_homography

PAPER_MAX_SAT = 0.18
PAPER_MIN_VAL = 0.72
FLAT_MM = 8.0
MIN_FIT_IOU = 0.6  # below this no upright box or cylinder explains the mask (table patches, reflections)
MAX_SIDE_MM = 160.0


# ---- the sheet --------------------------------------------------------------------------------------
def paper_mask(rgb: np.ndarray) -> np.ndarray:
    _, s, v = rgb_to_hsv(rgb)
    return (s < PAPER_MAX_SAT) & (v > PAPER_MIN_VAL)


def _hull(pts: np.ndarray) -> np.ndarray:
    """Convex hull (monotone chain), counter-clockwise in (x, y), no repeated end point."""
    p = np.unique(np.asarray(pts, float), axis=0)
    if len(p) < 3:
        return p
    p = p[np.lexsort((p[:, 1], p[:, 0]))]

    def half(seq):
        out: list = []
        for q in seq:
            while len(out) >= 2 and (out[-1][0] - out[-2][0]) * (q[1] - out[-2][1]) - \
                    (out[-1][1] - out[-2][1]) * (q[0] - out[-2][0]) <= 0:
                out.pop()
            out.append(tuple(q))
        return out

    lower, upper = half(p), half(p[::-1])
    return np.array(lower[:-1] + upper[:-1])


def _max_quad(hull: np.ndarray) -> np.ndarray:
    """The largest-area quadrilateral with vertices on the hull (O(n^3) vectorised over the 4th vertex)."""
    n = len(hull)
    if n < 4:
        raise ValueError("the sheet outline has fewer than four corners")
    if n > 80:  # thin the hull evenly; the corners are refined from the edge pixels afterwards
        hull = hull[np.linspace(0, n, 80, endpoint=False).astype(int)]
        n = 80
    x, y = hull[:, 0], hull[:, 1]
    # twice the signed area of triangle (i, j, k)
    tri = (x[None, :, None] - x[:, None, None]) * (y[None, None, :] - y[:, None, None]) - \
          (y[None, :, None] - y[:, None, None]) * (x[None, None, :] - x[:, None, None])
    best, quad = -1.0, None
    idx = np.arange(n)
    for i in range(n):
        for k in range(i + 2, n):
            js = idx[i + 1:k]
            ls = np.r_[idx[k + 1:], idx[:i]]
            if not len(js) or not len(ls):
                continue
            a1 = tri[i, js, k]
            a2 = tri[i, k, ls]
            area = a1.max() + a2.max()
            if area > best:
                best, quad = area, (i, int(js[a1.argmax()]), k, int(ls[a2.argmax()]))
    return hull[list(quad)]


def order_corners(pts: np.ndarray) -> np.ndarray:
    """Four image points as top-left, top-right, bottom-right, bottom-left (clockwise on screen)."""
    pts = np.asarray(pts, float)
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    p = pts[np.argsort(ang)]  # clockwise on screen (v points down)
    start = int(np.argmin(p.sum(axis=1)))
    return np.roll(p, -start, axis=0)


def _refine(corners: np.ndarray, edge_px: np.ndarray, band: float = 4.0) -> np.ndarray:
    """Fit a line to the outline pixels near each side (total least squares) and intersect neighbours."""
    lines = []
    for i in range(4):
        a, b = corners[i], corners[(i + 1) % 4]
        d = b - a
        L = np.linalg.norm(d)
        n = np.array([-d[1], d[0]]) / L
        rel = edge_px - a
        t = rel @ d / L**2
        near = (np.abs(rel @ n) < band) & (t > 0.08) & (t < 0.92)
        if near.sum() < 10:
            return corners
        p = edge_px[near]
        c = p.mean(axis=0)
        _, _, vt = np.linalg.svd(p - c)
        lines.append((c, vt[0]))
    out = []
    for i in range(4):
        (p1, d1), (p2, d2) = lines[i - 1], lines[i]
        m = np.c_[d1, -d2]
        if abs(np.linalg.det(m)) < 1e-9:
            return corners
        s = np.linalg.solve(m, p2 - p1)
        out.append(p1 + s[0] * d1)
    return np.array(out)


def find_sheet(rgb: np.ndarray) -> np.ndarray:
    """The A4 sheet's corners in the photo (TL, TR, BR, BL), from the largest bright unsaturated region.
    Raises ValueError if there is no plausible sheet."""
    h, w = rgb.shape[:2]
    step = max(1, int(round(max(h, w) / 480)))  # components on a reduced grid, outline at full resolution
    small = paper_mask(rgb[::step, ::step])
    comps = components(small, min_pixels=int(0.01 * small.size))
    if not comps:
        raise ValueError("no sheet of paper found (no large bright, unsaturated region); click its corners")
    comp = comps[0]
    region = np.zeros(small.shape, bool)
    region[comp[:, 0], comp[:, 1]] = True
    full = np.repeat(np.repeat(region, step, 0), step, 1)[:h, :w] & paper_mask(rgb)
    inner = full & np.roll(full, 1, 0) & np.roll(full, -1, 0) & np.roll(full, 1, 1) & np.roll(full, -1, 1)
    vs, us = np.nonzero(full & ~inner)
    edge = np.c_[us, vs].astype(float)
    hull = _hull(edge)
    quad = order_corners(_max_quad(hull))
    d1, d2 = quad[2] - quad[0], quad[3] - quad[1]
    area = 0.5 * abs(d1[0] * d2[1] - d1[1] * d2[0])
    if area < 0.01 * h * w:
        raise ValueError("the bright region is too small to be the sheet; click its corners")
    if np.any(quad[:, 0] <= 1) or np.any(quad[:, 1] <= 1) or np.any(quad[:, 0] >= w - 2) or np.any(quad[:, 1] >= h - 2):
        raise ValueError("the sheet touches the photo's edge; move the camera back or click its corners")
    return order_corners(_refine(quad, edge))


@dataclass
class SheetFrame:
    """The table plane measured from the sheet. Plane coordinates are mm on the sheet: x along TL -> TR,
    y along TL -> BL; `up` is +1 or -1, the sign of plane z that points away from the table."""

    corners_px: np.ndarray
    landscape: bool  # the TL -> TR side is the long (297 mm) side
    h: np.ndarray
    k: np.ndarray
    r: np.ndarray
    t: np.ndarray
    up: float
    orthogonality: float  # |cos| between the pose's two plane axes (0 for a perfect fit)
    hfov_deg: float

    @property
    def size_mm(self) -> tuple[float, float]:
        return (297.0, 210.0) if self.landscape else (210.0, 297.0)

    def project(self, xy_mm: np.ndarray, z_mm=0.0) -> np.ndarray:
        """Plane points (N, 2) at height z_mm above the table -> pixels (N, 2)."""
        xy = np.atleast_2d(np.asarray(xy_mm, float))
        z = np.broadcast_to(np.asarray(z_mm, float) * self.up, (len(xy),))
        p = (self.k @ (self.r @ np.c_[xy, z].T + self.t[:, None])).T
        return p[:, :2] / p[:, 2:3]

    def camera_centre_mm(self) -> np.ndarray:
        """The camera centre in plane coordinates (z along `up`, i.e. its height above the table)."""
        c = -self.r.T @ self.t
        return np.array([c[0], c[1], c[2] * self.up])

    def to_dict(self) -> dict:
        return {"corners_px": np.round(self.corners_px, 2).tolist(), "landscape": self.landscape,
                "size_mm": list(self.size_mm), "hfov_deg": self.hfov_deg,
                "orthogonality": round(float(self.orthogonality), 4),
                "camera_mm": np.round(self.camera_centre_mm(), 1).tolist()}


def pick_orientation(corners_px, width: int, height: int, hfov_deg: float = 70.0) -> tuple[bool, np.ndarray, float]:
    """Which way an A4 sheet lies, for its four corners in the order given (any first corner, either way
    round; nothing is re-ordered): landscape if edge 1 -> 2 is the long (297 mm) side. The homography is fitted
    both ways to the corners as given, and the one whose camera pose is a rotation wins (with the assumed field
    of view, the two columns of K^-1 H must be orthogonal and of equal length). Returns (landscape, H, |cos|
    between the pose's plane axes); H maps a4_corners(landscape) onto the corners in the order given."""
    img = np.asarray(corners_px, float)
    k = intrinsics_from_fov(width, height, hfov_deg)
    best = None
    for landscape in (False, True):
        h = homography(a4_corners(landscape), img)
        m = np.linalg.inv(k) @ h
        a, b = m[:, 0], m[:, 1]
        cos = abs(a @ b) / (np.linalg.norm(a) * np.linalg.norm(b))
        ratio = abs(np.log(np.linalg.norm(a) / np.linalg.norm(b)))
        score = cos + ratio
        if best is None or score < best[0]:
            best = (score, landscape, h, cos)
    _, landscape, h, cos = best
    return landscape, h, float(cos)


def sheet_frame(corners_px, width: int, height: int, hfov_deg: float = 70.0) -> SheetFrame:
    """The plane homography and camera pose from the sheet's four corners, re-ordered TL, TR, BR, BL on screen."""
    img = order_corners(np.asarray(corners_px, float))
    k = intrinsics_from_fov(width, height, hfov_deg)
    landscape, h, cos = pick_orientation(img, width, height, hfov_deg)
    r, t = pose_from_homography(h, k)
    c = -r.T @ t
    return SheetFrame(img, landscape, h, k, r, t, 1.0 if c[2] > 0 else -1.0, float(cos), float(hfov_deg))


# ---- masks ------------------------------------------------------------------------------------------
def table_colour(rgb: np.ndarray, frame: SheetFrame, margin_mm: float = 60.0) -> np.ndarray:
    """Median colour of a ring of table just outside the sheet (sampled on the plane)."""
    w, h = frame.size_mm
    pts = []
    for s in np.linspace(0, 1, 40):
        pts += [[s * w, -margin_mm], [s * w, h + margin_mm], [-margin_mm, s * h], [w + margin_mm, s * h]]
    px = np.round(frame.project(np.array(pts))).astype(int)
    hh, ww = rgb.shape[:2]
    ok = (px[:, 0] >= 0) & (px[:, 0] < ww) & (px[:, 1] >= 0) & (px[:, 1] < hh)
    if ok.sum() < 10:
        return np.full(3, np.nan)
    return np.median(rgb[px[ok, 1], px[ok, 0]].astype(float), axis=0)


def prompt_points(rgb: np.ndarray, frame: SheetFrame, reach_mm: float = 200.0, step_mm: float = 12.0,
                  table_rgb: np.ndarray | None = None, table_tol: float = 28.0) -> list[tuple[float, float]]:
    """A grid on the table plane (the sheet plus `reach_mm` around it) projected into the photo, without
    points on the paper or on table-coloured pixels."""
    w, h = frame.size_mm
    xs = np.arange(-reach_mm, w + reach_mm + 1e-6, step_mm)
    ys = np.arange(-reach_mm, h + reach_mm + 1e-6, step_mm)
    g = np.stack(np.meshgrid(xs, ys), -1).reshape(-1, 2)
    px = frame.project(g)
    hh, ww = rgb.shape[:2]
    ok = (px[:, 0] >= 2) & (px[:, 0] < ww - 2) & (px[:, 1] >= 2) & (px[:, 1] < hh - 2)
    px = px[ok]
    ui, vi = np.round(px[:, 0]).astype(int), np.round(px[:, 1]).astype(int)
    keep = ~paper_mask(rgb[vi, ui].reshape(-1, 1, 3)).reshape(-1)
    if table_rgb is not None and np.all(np.isfinite(table_rgb)):
        keep &= np.linalg.norm(rgb[vi, ui].astype(float) - table_rgb, axis=1) > table_tol
    return [(float(u), float(v)) for u, v in px[keep]]


def _mask_ok(mask: np.ndarray, paper: np.ndarray, tablelike: np.ndarray, min_px: int, max_px: int) -> bool:
    n = int(mask.sum())
    if not (min_px <= n <= max_px):
        return False
    return paper[mask].mean() < 0.15 and tablelike[mask].mean() < 0.3


def auto_masks(sam, rgb: np.ndarray, frame: SheetFrame, points: list[tuple[float, float]] | None = None,
               batch: int = 16, min_px: int = 120, max_frac: float = 0.05) -> list[dict]:
    """SAM 2.1 automatic masks from point prompts: [{mask, score, point}], duplicates merged."""
    points = prompt_points(rgb, frame, table_rgb=table_colour(rgb, frame)) if points is None else points
    paper = paper_mask(rgb)
    trgb = table_colour(rgb, frame)
    tablelike = (np.linalg.norm(rgb.astype(float) - trgb, axis=-1) < 28.0) if np.all(np.isfinite(trgb)) \
        else np.zeros(rgb.shape[:2], bool)
    max_px = int(max_frac * rgb.shape[0] * rgb.shape[1])
    covered = np.zeros(rgb.shape[:2], bool)
    accepted: list[dict] = []
    pending = list(points)
    while pending:
        chunk = [p for p in pending[:batch] if not covered[int(round(p[1])), int(round(p[0]))]]
        pending = pending[batch:]
        if not chunk:
            continue
        for p, (masks, scores) in zip(chunk, sam.segment_many(rgb, chunk, batch=batch)):
            if covered[int(round(p[1])), int(round(p[0]))]:
                continue
            for i in np.argsort(-scores):
                m = masks[i]
                if m[int(round(p[1])), int(round(p[0]))] and _mask_ok(m, paper, tablelike, min_px, max_px):
                    accepted.append({"mask": m, "score": float(scores[i]), "point": [round(p[0], 1), round(p[1], 1)]})
                    covered |= m
                    break
    # merge: a mask mostly inside a bigger one is a part of it
    accepted.sort(key=lambda a: -int(a["mask"].sum()))
    out: list[dict] = []
    for a in accepted:
        m = a["mask"]
        if any((m & b["mask"]).sum() > 0.5 * m.sum() for b in out):
            continue
        out.append(a)
    return out


# ---- fitting an upright box or cylinder -------------------------------------------------------------
def _rasterize_convex(poly: np.ndarray, shape: tuple[int, int], box: tuple[int, int, int, int]) -> np.ndarray:
    """Pixels (within box = v0, v1, u0, u1) inside a convex polygon given counter-clockwise or clockwise."""
    v0, v1, u0, u1 = box
    vs, us = np.mgrid[v0:v1, u0:u1]
    inside = np.ones(vs.shape, bool)
    n = len(poly)
    area = 0.0
    for i in range(n):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    sign = 1.0 if area > 0 else -1.0
    for i in range(n):
        (x0, y0), (x1, y1) = poly[i], poly[(i + 1) % n]
        inside &= sign * ((x1 - x0) * (vs - y0) - (y1 - y0) * (us - x0)) >= 0
    return inside


def _rect(xy: np.ndarray, pitch: float) -> tuple[np.ndarray, float, tuple[float, float]]:
    yaw = footprint_yaw(xy)
    c, s = np.cos(yaw), np.sin(yaw)
    rot = np.array([[c, -s], [s, c]])  # columns: along, across
    p = xy @ rot
    lo, hi = p.min(axis=0) - pitch / 2, p.max(axis=0) + pitch / 2
    centre = rot @ ((lo + hi) / 2)
    return centre, yaw, (float(hi[0] - lo[0]), float(hi[1] - lo[1]))


def _outline(kind: str, centre, yaw: float, size) -> np.ndarray:
    if kind == "cylinder":
        a = np.linspace(0, 2 * np.pi, 48, endpoint=False)
        return np.c_[np.cos(a), np.sin(a)] * size[0] / 2 + centre
    c, s = np.cos(yaw), np.sin(yaw)
    half = np.array([[1, 1], [-1, 1], [-1, -1], [1, -1]]) * np.array(size) / 2
    return half @ np.array([[c, s], [-s, c]]) + centre


@dataclass
class TwinObject:
    """One object measured from the photo, in sheet millimetres (plane x, y; height above the table)."""

    index: int
    kind: str  # "box" or "cylinder"
    x_mm: float
    y_mm: float
    yaw: float  # radians, folded into [-pi/4, pi/4); 0 for a cylinder
    size_mm: list[float]  # box: along yaw, across, height; cylinder: diameter, diameter, height
    height_source: str  # "typed", "estimated" (best silhouette fit) or "default"
    fit_iou: float  # the extruded shape projected back vs the SAM mask
    rgb: list[int]  # mean colour of the top face
    colour: str  # nearest named colour, or "grey" / "white" / "dark"
    sam_score: float
    point: list[float]
    mask_px: int
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _neutral_name(rgb: np.ndarray) -> str:
    _, s, v = rgb_to_hsv(rgb.reshape(1, 3) / 255.0)
    if v[0] < 0.25:
        return "dark"
    return "white" if v[0] > 0.8 and s[0] < 0.15 else "grey"


def _nelder_mead(f, x0: np.ndarray, step: np.ndarray, iters: int = 160, tol: float = 1e-4) -> tuple[np.ndarray, float]:
    """Minimise f from x0 (a plain Nelder-Mead; IoU is piecewise constant, so the simplex is restarted once)."""
    best_x, best_f = np.asarray(x0, float), f(np.asarray(x0, float))
    for restart in range(2):
        n = len(best_x)
        pts = [best_x] + [best_x + np.eye(n)[i] * step[i] * (0.5 if restart else 1.0) for i in range(n)]
        vals = [f(p) for p in pts]
        for _ in range(iters):
            order = np.argsort(vals)
            pts, vals = [pts[i] for i in order], [vals[i] for i in order]
            if abs(vals[-1] - vals[0]) < tol:
                break
            c = np.mean(pts[:-1], axis=0)
            xr = c + (c - pts[-1])
            fr = f(xr)
            if fr < vals[0]:
                xe = c + 2 * (c - pts[-1])
                fe = f(xe)
                pts[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
            elif fr < vals[-2]:
                pts[-1], vals[-1] = xr, fr
            else:
                xc = c + 0.5 * (pts[-1] - c)
                fc = f(xc)
                if fc < vals[-1]:
                    pts[-1], vals[-1] = xc, fc
                else:
                    pts = [pts[0] + 0.5 * (p - pts[0]) for p in pts]
                    vals = [f(p) for p in pts]
        i = int(np.argmin(vals))
        if vals[i] < best_f:
            best_x, best_f = pts[i], vals[i]
    return best_x, best_f


def top_face_pixels(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray | None:
    """The mask's brightest connected region, if it is distinct: under light from above an upright object's
    top face is its brightest face. None when the shading does not separate a top (20-85 % of the mask)."""
    _, _, v = rgb_to_hsv(rgb)
    vals = v[mask]
    if len(vals) < 40:
        return None
    hi = float(np.percentile(vals, 95))
    lo = float(np.percentile(vals, 5))
    if hi - lo < 0.12:
        return None
    cand = mask & (v >= hi - 0.35 * (hi - lo))
    comps = components(cand, min_pixels=10)
    if not comps:
        return None
    top = np.zeros_like(mask)
    top[comps[0][:, 0], comps[0][:, 1]] = True
    frac = top.sum() / mask.sum()
    return top if 0.2 <= frac <= 0.85 else None


def fit_object(mask: np.ndarray, rgb: np.ndarray, frame: SheetFrame, height_mm: float | None = None,
               default_height_mm: float | None = None, grid_mm: float = 1.0) -> dict | None:
    """Fit an upright box or cylinder to one mask. Returns None if the mask fits best as something flat
    (and no height was typed). Keys: kind, centre, yaw, size (mm, along / across or diameter), height, iou,
    height_source.

    A coarse search over heights (single-view carving, then a rectangle / disc fitted to the footprint)
    gives a start; then centre, yaw, size and height are refined together to maximise the IoU between the
    mask and the extruded shape's projected silhouette (Nelder-Mead)."""
    vs, us = np.nonzero(mask)
    if len(us) < 20:
        return None
    hh, ww = mask.shape
    pad = 12
    box = (max(0, vs.min() - pad), min(hh, vs.max() + pad + 1), max(0, us.min() - pad), min(ww, us.max() + pad + 1))
    win = mask[box[0]:box[1], box[2]:box[3]]
    n_mask = float(win.sum())
    # plane points whose z = 0 projection lands in the mask (the footprint is a subset of these)
    base = apply_h(np.linalg.inv(frame.h), np.c_[us, vs])
    lo, hi = base.min(axis=0) - 2, base.max(axis=0) + 2
    gx, gy = np.meshgrid(np.arange(lo[0], hi[0], grid_mm), np.arange(lo[1], hi[1], grid_mm))
    g = np.c_[gx.ravel(), gy.ravel()]

    def inside(px: np.ndarray) -> np.ndarray:
        u, v = np.round(px[:, 0]).astype(int), np.round(px[:, 1]).astype(int)
        ok = (u >= 0) & (u < ww) & (v >= 0) & (v < hh)
        out = np.zeros(len(px), bool)
        out[ok] = mask[v[ok], u[ok]]
        return out

    g0 = g[inside(frame.project(g, 0.0))]
    if len(g0) < 10:
        return None

    top = top_face_pixels(rgb, mask)
    top_win = top[box[0]:box[1], box[2]:box[3]] if top is not None else None
    n_top = float(top_win.sum()) if top_win is not None else 0.0

    def _iou_poly(poly: np.ndarray, ref: np.ndarray, n_ref: float) -> float:
        hull = _hull(poly)
        if len(hull) < 3:
            return 0.0
        sil = _rasterize_convex(hull, mask.shape, box)
        inter = float((sil & ref).sum())
        # silhouette pixels outside the window count against the fit too
        outside = not (hull[:, 0].min() >= box[2] and hull[:, 0].max() < box[3] and hull[:, 1].min() >= box[0]
                       and hull[:, 1].max() < box[1])
        union = float(sil.sum()) + n_ref - inter
        return inter / union * (0.5 if outside else 1.0) if union else 0.0

    def iou(kind: str, c, y: float, sz, h: float) -> float:
        if min(sz) <= 0.5 or h < 0:
            return 0.0
        ring = _outline(kind, c, y, sz)
        return _iou_poly(np.r_[frame.project(ring, 0.0), frame.project(ring, h)], win, n_mask)

    def score(kind: str, c, y: float, sz, h: float) -> float:
        """Silhouette IoU, plus half the top face's IoU when the photo shows a distinct (brighter) top."""
        v = iou(kind, c, y, sz, h)
        if top_win is None or v == 0.0:
            return v
        return v + 0.5 * _iou_poly(frame.project(_outline(kind, c, y, sz), h), top_win, n_top)

    def start(h: float) -> list[tuple[float, str, np.ndarray, float, tuple]]:
        foot = g0[inside(frame.project(g0, h))] if h > 0 else g0
        if len(foot) < 8:
            return []
        centre, yaw, size = _rect(foot, grid_mm)
        r = np.sqrt(len(foot) * grid_mm**2 / np.pi)
        cands = (("box", centre, yaw, size), ("cylinder", foot.mean(axis=0), 0.0, (2 * r, 2 * r)))
        return [(iou(k, c, y, sz, h), k, np.asarray(c, float), y, sz) for k, c, y, sz in cands]

    typed = height_mm is not None
    trials = [float(height_mm)] if typed else list(np.arange(0.0, 122.0, 4.0))
    # per shape, the best start overall and the best flat one (h < FLAT_MM): a flat card's outline also
    # fits a squat box, and the local refinement would not climb out of that valley on its own
    starts: dict[tuple[str, bool], tuple] = {}
    for h in trials:
        for v, kind, c, y, sz in start(h):
            for key in ((kind, False), (kind, True)) if h < FLAT_MM else ((kind, False),):
                if key not in starts or v > starts[key][0]:
                    starts[key] = (v, c, y, sz, h)
    if not starts:
        return None

    fits = []
    for (kind, _flat), (_, c, y, sz, h) in starts.items():
        if kind == "box":
            x0 = np.array([c[0], c[1], y, sz[0], sz[1], h])
            step = np.array([1.5, 1.5, 0.08, 2.0, 2.0, 3.0])

            def unpack(x):
                return x[:2], float(x[2]), (x[3], x[4]), (float(height_mm) if typed else float(x[5]))
        else:
            x0 = np.array([c[0], c[1], sz[0], h])
            step = np.array([1.5, 1.5, 2.0, 3.0])

            def unpack(x):
                return x[:2], 0.0, (x[2], x[2]), (float(height_mm) if typed else float(x[3]))
        if typed:
            x0, step = x0[:-1], step[:-1]
            unpack_ = unpack

            def unpack(x, _u=unpack_):  # noqa: E731 - pad the fixed height back in
                return _u(np.r_[x, 0.0])
        x, fval = _nelder_mead(lambda x: -score(kind, *unpack(x)), x0, step)
        c2, y2, sz2, h2 = unpack(x)
        fits.append((-fval, kind, np.asarray(c2, float), y2, (float(sz2[0]), float(sz2[1])), h2))
    _, kind, c, y, sz, h = max(fits, key=lambda f: f[0])
    best_iou = iou(kind, c, y, sz, h)
    source = "typed" if typed else "estimated"
    if not typed and h < FLAT_MM:
        if default_height_mm is None:
            return None
        return fit_object(mask, rgb, frame, float(default_height_mm), None, grid_mm) | {"height_source": "default"}
    if kind == "box":  # fold the yaw into [-pi/4, pi/4), swapping the sides if needed
        k = int(np.round(y / (np.pi / 2)))
        y -= k * np.pi / 2
        if k % 2:
            sz = (sz[1], sz[0])
    return {"kind": kind, "centre": c, "yaw": float(y) if kind == "box" else 0.0,
            "size": [abs(sz[0]), abs(sz[1])], "height": float(h), "iou": float(best_iou), "height_source": source}


def top_face_colour(rgb: np.ndarray, mask: np.ndarray, frame: SheetFrame, fit: dict) -> np.ndarray:
    """Mean colour of the fitted top face's pixels inside the mask (the lit face; sides are shaded)."""
    ring = _outline(fit["kind"], fit["centre"], fit["yaw"], fit["size"])
    poly = frame.project(ring, fit["height"])
    hh, ww = mask.shape
    box = (int(max(0, poly[:, 1].min())), int(min(hh, poly[:, 1].max() + 1)),
           int(max(0, poly[:, 0].min())), int(min(ww, poly[:, 0].max() + 1)))
    if box[1] <= box[0] or box[3] <= box[2]:
        return rgb[mask].mean(axis=0)
    top = np.zeros_like(mask)
    top[box[0]:box[1], box[2]:box[3]] = _rasterize_convex(_hull(poly), mask.shape, box)
    sel = top & mask
    # shrink away from the outline so edge pixels (blended with the sides) do not count
    inner = sel & np.roll(sel, 2, 0) & np.roll(sel, -2, 0) & np.roll(sel, 2, 1) & np.roll(sel, -2, 1)
    sel = inner if inner.sum() >= 12 else sel
    return rgb[sel].reshape(-1, 3).astype(float).mean(axis=0) if sel.any() else rgb[mask].mean(axis=0)


def _chroma(c: np.ndarray) -> np.ndarray:
    c = np.asarray(c, float)
    return c / max(float(c.sum()), 1e-6)


def shadow_like(rgb: np.ndarray, frame: SheetFrame, masks: list[np.ndarray]) -> list[bool]:
    """Which masks are shadows: the paper's or the table's own colour, darker (same chromaticity, 20-85 %
    of its brightness), touching a mask that is not like that. A grey object right next to another object
    is dropped too (a limit, in the docs)."""
    paper = paper_mask(rgb)
    surfaces = []
    if paper.any():
        surfaces.append(np.median(rgb[paper].reshape(-1, 3).astype(float), axis=0))
    t = table_colour(rgb, frame)
    if np.all(np.isfinite(t)):
        surfaces.append(t)
    like = []
    for m in masks:
        c = np.median(rgb[m].reshape(-1, 3).astype(float), axis=0)
        hit = False
        for sc in surfaces:
            ratio = float(c.sum() / max(sc.sum(), 1e-6))
            if np.abs(_chroma(c) - _chroma(sc)).max() < 0.03 and 0.2 <= ratio <= 0.85:
                hit = True
        like.append(hit)
    grown = []
    for m in masks:
        g = m.copy()
        for _ in range(3):
            g = g | np.roll(g, 1, 0) | np.roll(g, -1, 0) | np.roll(g, 1, 1) | np.roll(g, -1, 1)
        grown.append(g)
    return [bool(like[i] and any(not like[j] and (grown[i] & masks[j]).any() for j in range(len(masks)) if j != i))
            for i in range(len(masks))]


def measure(sam, rgb: np.ndarray, frame: SheetFrame, heights_mm: dict[int, float] | None = None,
            default_height_mm: float | None = None) -> tuple[list[TwinObject], list[np.ndarray], dict]:
    """Every object in the photo: (objects, their masks, stats). heights_mm maps an object index (from a
    previous call on the same photo) to a typed height."""
    import time

    t0 = time.perf_counter()
    found = auto_masks(sam, rgb, frame)
    t1 = time.perf_counter()
    shadows = shadow_like(rgb, frame, [a["mask"] for a in found])
    found = [a for a, sh in zip(found, shadows) if not sh]
    # stable order: left to right along the sheet's x, then y
    order = []
    for a in found:
        vs, us = np.nonzero(a["mask"])
        xy = apply_h(np.linalg.inv(frame.h), np.c_[[us.mean()], [vs.mean()]])[0]
        order.append((xy[1] // 40, xy[0], a))
    order.sort(key=lambda o: (o[0], o[1]))
    objs, masks, dropped, loose = [], [], 0, 0
    for idx, (_, _, a) in enumerate(order):
        typed = (heights_mm or {}).get(len(objs))
        fit = fit_object(a["mask"], rgb, frame, typed, default_height_mm)
        if fit is None:
            dropped += 1
            continue
        if typed is None and (fit["iou"] < MIN_FIT_IOU or max(fit["size"]) > MAX_SIDE_MM):
            loose += 1  # a patch of table or a reflection: no upright box or cylinder explains it
            continue
        col = top_face_colour(rgb, a["mask"], frame, fit)
        name = name_colour(col.reshape(1, 3).astype(np.uint8)) or _neutral_name(col)
        sx, sy = fit["size"]
        size = [sx, sx, fit["height"]] if fit["kind"] == "cylinder" else [sx, sy, fit["height"]]
        notes = []
        if fit["iou"] < 0.75:
            notes.append(f"loose fit (IoU {fit['iou']:.2f}): not an upright box or cylinder?")
        objs.append(TwinObject(len(objs), fit["kind"], round(float(fit["centre"][0]), 1), round(float(fit["centre"][1]), 1),
                               round(fit["yaw"], 4), [round(v, 1) for v in size], fit["height_source"],
                               round(fit["iou"], 3), [int(round(c)) for c in col], name, round(a["score"], 3),
                               a["point"], int(a["mask"].sum()), notes))
        masks.append(a["mask"])
    stats = {"masks": len(found) + sum(shadows), "dropped_shadow": int(sum(shadows)), "dropped_flat": dropped, "dropped_loose": loose, "sam_ms": round((t1 - t0) * 1000, 1),
             "fit_ms": round((time.perf_counter() - t1) * 1000, 1)}
    return objs, masks, stats


__all__ = ["FLAT_MM", "SheetFrame", "TwinObject", "auto_masks", "find_sheet", "fit_object", "measure",
           "order_corners", "paper_mask", "pick_orientation", "prompt_points", "shadow_like", "sheet_frame", "table_colour",
           "top_face_colour"]
