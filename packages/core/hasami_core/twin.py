"""Your desk, twinned: one photo with an A4 sheet in it becomes a MuJoCo scene the simulated SO-101 can work
in. Labelled everywhere: "digital twin from one photo · simulated arm".

  photo ─ A4 corners ─ plane + camera pose ─ SAM 2.1 automatic masks ─ upright box / cylinder per mask
        ─ MjSpec scene (sheet, objects in their measured colours, tray + grey bin, the photo's camera)
        ─ side-by-side render (photo | twin from the same viewpoint) ─ tasks on the twin, scored by the
          same goal predicates and run with the same verify-and-retry loop as the benchmark.

Where the arm stands: the SO-101's base is `offset_mm` (default 215) from the sheet's centre, beyond the
edge named by `side` ("far" = the edge at the top of the photo), facing across the sheet. That puts the
sheet's centre at the middle of the arm's reach. Move the sheet, not the numbers, to match a real desk.

The twin is only as good as one photo: heights are estimated from the silhouette (or typed), the field
of view is assumed unless known, and the arm in it is simulated. Nothing here touches a real arm.
"""

from __future__ import annotations

import io
import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import mujoco
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from hasami_perception.twin import SheetFrame, TwinObject, _outline, find_sheet, measure, sheet_frame
from hasami_sim.arm import ARM_JOINTS, GRIPPER, GRIPPER_OPEN, Arm
from hasami_sim.photo import PAPER_RGB
from hasami_sim.scene import REACH_R, SIDE_BIN, SIDE_TRAY, ObjectSpec, SceneSpec, build

from .session import ROOT, _json_default
from .tasks import Task, in_any_box

TWIN_LABEL = "digital twin from one photo · simulated arm"
SIDES = ("far", "near", "left", "right")
MAX_GRIP_M = 0.036  # widest side the jaws close across (the pre-grasp opening is ~40 mm)
PARK_Q = np.array([0.0, -1.5, 0.8, -0.6, 0.0])


@dataclass
class ArmPlacement:
    side: str = "far"
    offset_mm: float = 215.0

    def frame(self, sheet: SheetFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(base, forward, left) in sheet mm: the arm's base point and its forward / left directions."""
        if self.side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}")
        w, h = sheet.size_mm
        c = np.array([w / 2, h / 2])
        fwd = {"far": (0, 1), "near": (0, -1), "left": (1, 0), "right": (-1, 0)}[self.side]
        f = np.array(fwd, float)
        base = c - f * self.offset_mm
        left = sheet.up * np.array([-f[1], f[0]])  # world z is `up` along plane z; left = z x forward
        return base, f, left


@dataclass
class Twin:
    id: str
    created_at: str
    width: int
    height: int
    sheet: SheetFrame
    placement: ArmPlacement
    objects: list[TwinObject]
    world: list[dict]  # per object: name, x, y (m), yaw, in_twin, notes
    spec: SceneSpec
    stats: dict
    corners_source: str  # "detected" or "clicked"
    photo_source: str = "upload"  # or "synthetic" (a MuJoCo render, e.g. GET /api/twin/sample.png)
    label: str = TWIN_LABEL
    masks: list[np.ndarray] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        return {"twin_id": self.id, "label": self.label, "created_at": self.created_at,
                "photo": {"width": self.width, "height": self.height, "source": self.photo_source},
                "sheet": self.sheet.to_dict() | {"corners_source": self.corners_source},
                "arm": asdict(self.placement) | {"note": "simulated SO-101; base placed relative to the sheet"},
                "objects": [o.to_dict() | w for o, w in zip(self.objects, self.world)],
                "scene": self.spec.to_dict(), "stats": self.stats, "tasks": [t for t in task_menu(self)]}


# ---- sheet -> world -----------------------------------------------------------------------------
def to_world(sheet: SheetFrame, placement: ArmPlacement, xy_mm) -> np.ndarray:
    base, f, left = placement.frame(sheet)
    d = np.atleast_2d(np.asarray(xy_mm, float)) - base
    return np.c_[d @ f, d @ left] / 1000.0


def yaw_to_world(sheet: SheetFrame, placement: ArmPlacement, yaw: float) -> float:
    _, f, left = placement.frame(sheet)
    d = np.array([np.cos(yaw), np.sin(yaw)])
    return float(np.arctan2(d @ left, d @ f))


def camera_in_world(sheet: SheetFrame, placement: ArmPlacement) -> dict:
    """The photo's camera as a MuJoCo camera (pos, quat, fovy) in the twin's world."""
    base, f, left = placement.frame(sheet)

    def vec(v3: np.ndarray) -> np.ndarray:
        return np.array([v3[:2] @ f, v3[:2] @ left, v3[2] * sheet.up])

    c = sheet.camera_centre_mm()  # z is already the height above the table
    pos = np.array([(c[:2] - base) @ f, (c[:2] - base) @ left, c[2]]) / 1000.0
    x_cv, y_cv, z_cv = sheet.r[0], sheet.r[1], sheet.r[2]  # camera axes in plane coordinates
    xm, ym, zm = vec(x_cv), -vec(y_cv), -vec(z_cv)  # MuJoCo: x right, y up, looking along -z
    q = np.zeros(4)
    mujoco.mju_mat2Quat(q, np.c_[xm, ym, zm].flatten())
    height_px = 2 * sheet.k[1, 2] + 1  # intrinsics_from_fov puts the principal point at (H - 1) / 2
    fovy = float(np.degrees(2 * np.arctan(0.5 * height_px / sheet.k[1, 1])))
    return {"name": "photo", "pos": pos.tolist(), "quat": q.tolist(), "fovy": fovy}


# ---- the scene -------------------------------------------------------------------------------------
def _in_overhead_view(x: float, y: float) -> bool:
    return -0.05 < x < 0.50 and abs(y) < 0.36


def twin_spec(sheet: SheetFrame, placement: ArmPlacement, objs: list[TwinObject]) -> tuple[SceneSpec, list[dict]]:
    """The MjSpec scene for a twin, and per object where it went (or why it was left out)."""
    containers = [SIDE_TRAY, SIDE_BIN]
    w_mm, h_mm = sheet.size_mm
    centre = to_world(sheet, placement, [[w_mm / 2, h_mm / 2]])[0]
    sheet_yaw = yaw_to_world(sheet, placement, 0.0)
    visuals = [{"name": "a4_sheet", "size": [w_mm / 2000, h_mm / 2000, 0.0002], "pos": [*centre, 0.0002],
                "quat": [np.cos(sheet_yaw / 2), 0, 0, np.sin(sheet_yaw / 2)], "rgba": [*PAPER_RGB, 1.0]}]
    specs, world = [], []
    for o in objs:
        x, y = to_world(sheet, placement, [[o.x_mm, o.y_mm]])[0]
        yaw = yaw_to_world(sheet, placement, o.yaw) if o.kind == "box" else 0.0
        notes: list[str] = []
        size = (o.size_mm[0] / 1000, o.size_mm[1] / 1000, o.size_mm[2] / 1000)
        if size[1] > size[0]:  # long side first, along the yaw; a rectangle repeats every 180 deg
            size, yaw = (size[1], size[0], size[2]), yaw + np.pi / 2
        yaw = float((yaw + np.pi / 2) % np.pi - np.pi / 2)
        r_obj = 0.5 * float(np.hypot(size[0], size[1]))
        r = float(np.hypot(x, y))
        in_twin = True
        if not _in_overhead_view(x, y):
            notes.append("outside the simulated overhead camera's view: left out")
            in_twin = False
        elif r < 0.09 + r_obj:
            notes.append("where the arm's base stands: left out")
            in_twin = False
        elif any(max(abs(x - c.x), abs(y - c.y)) < c.half + r_obj for c in containers):
            notes.append(f"overlaps the virtual {next(c.label for c in containers if max(abs(x - c.x), abs(y - c.y)) < c.half + r_obj)}: left out")
            in_twin = False
        reach = REACH_R[0] <= r <= REACH_R[1]
        if in_twin and not reach:
            notes.append(f"{r * 1000:.0f} mm from the arm: outside its reach ({REACH_R[0] * 1000:.0f}-{REACH_R[1] * 1000:.0f} mm)")
        graspable = min(size[0], size[1]) <= MAX_GRIP_M and 0.010 <= size[2] <= 0.09
        if in_twin and not graspable:
            notes.append("too wide or too tall for the SO-101's jaws")
        name = f"twin{o.index}_{o.colour}_{o.kind}"
        if in_twin:
            rgba = (*[c / 255 for c in o.rgb], 1.0)
            specs.append(ObjectSpec(name, o.kind, o.colour, float(x), float(y), float(yaw), size,
                                    mass=0.02, form="cylinder" if o.kind == "cylinder" else "block", rgba=rgba))
        world.append({"name": name if in_twin else None, "x_m": round(float(x), 4), "y_m": round(float(y), 4),
                      "yaw_world": round(float(yaw), 4), "in_twin": in_twin, "reachable": bool(reach),
                      "graspable": bool(graspable), "twin_notes": notes})
    # overlapping footprints get pushed apart by the physics; close neighbours get in the jaws' way
    for i, a in enumerate(specs):
        for b in specs[i + 1:]:
            d = float(np.hypot(a.x - b.x, a.y - b.y))
            ra, rb = 0.5 * float(np.hypot(*a.dims[:2])), 0.5 * float(np.hypot(*b.dims[:2]))
            if d < 0.5 * (min(a.dims[:2]) + min(b.dims[:2])):
                note = "overlaps another object in the twin (the physics separates them)"
            elif d - ra - rb < 0.024:
                note = f"{max(0.0, d - ra - rb) * 1000:.0f} mm from a neighbour: the jaws may knock it"
            else:
                continue
            for w in world:
                if w["name"] in (a.name, b.name):
                    w["twin_notes"].append(note)
    cam = camera_in_world(sheet, placement)
    return SceneSpec(specs, containers, seed=0, name="twin", cameras=[cam], visuals=visuals), world


def _park(model: mujoco.MjModel, data: mujoco.MjData) -> None:
    for j, v in zip(ARM_JOINTS, PARK_Q):
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, j)
        aid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, j)
        data.qpos[model.jnt_qposadr[jid]] = v
        data.ctrl[aid] = v
    gid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, GRIPPER)
    data.qpos[model.jnt_qposadr[gid]] = GRIPPER_OPEN
    data.ctrl[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, GRIPPER)] = GRIPPER_OPEN


def render_twin(spec: SceneSpec, width: int, height: int, model_data: tuple | None = None) -> np.ndarray:
    """The twin (arm parked) from the photo's camera, at the photo's size (capped at 1280 x 960)."""
    if model_data is None:
        model = build(spec)
        data = mujoco.MjData(model)
        _park(model, data)
        mujoco.mj_forward(model, data)
        for _ in range(200):
            mujoco.mj_step(model, data)
    else:
        model, data = model_data
    scale = min(1.0, 1280 / width, 960 / height)
    w, h = int(width * scale), int(height * scale)
    r = mujoco.Renderer(model, height=h, width=w)
    try:
        r.update_scene(data, camera="photo")
        img = r.render().copy()
    finally:
        r.close()
    if (w, h) != (width, height):
        img = np.asarray(Image.fromarray(img).resize((width, height), Image.BILINEAR))
    return img


def overlay_photo(rgb: np.ndarray, twin: Twin) -> np.ndarray:
    """The photo with the sheet outline, each SAM mask's outline and each fitted shape's wireframe."""
    img = Image.fromarray(rgb.copy())
    d = ImageDraw.Draw(img)
    c = [tuple(map(float, p)) for p in twin.sheet.corners_px]
    d.line(c + [c[0]], fill=(255, 140, 0), width=3)
    for o, w, m in zip(twin.objects, twin.world, twin.masks):
        edge = m & ~(np.roll(m, 1, 0) & np.roll(m, -1, 0) & np.roll(m, 1, 1) & np.roll(m, -1, 1))
        a = np.asarray(img)
        a = a.copy()
        a[edge] = (56, 242, 196) if w["in_twin"] else (230, 60, 60)
        img = Image.fromarray(a)
        d = ImageDraw.Draw(img)
        ring = _outline(o.kind, (o.x_mm, o.y_mm), o.yaw, o.size_mm[:2])
        lo, hi = twin.sheet.project(ring, 0.0), twin.sheet.project(ring, o.size_mm[2])
        for poly in (lo, hi):
            pts = [tuple(map(float, p)) for p in poly]
            d.line(pts + [pts[0]], fill=(20, 20, 20), width=1)
        step = max(1, len(ring) // 4)
        for i in range(0, len(ring), step):
            d.line([tuple(map(float, lo[i])), tuple(map(float, hi[i]))], fill=(20, 20, 20), width=1)
        u, v = hi.mean(axis=0)
        d.text((float(u) + 6, float(v) - 18), str(o.index), fill=(20, 20, 20), font=_font(16),
               stroke_width=2, stroke_fill=(255, 255, 255))
    return np.asarray(img)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def side_by_side(photo: np.ndarray, twin_img: np.ndarray, label: str = TWIN_LABEL,
                 left: str = "photo", right: str = "twin (MuJoCo)") -> bytes:
    """Photo | twin, same size, with a caption bar naming what each side is."""
    h, w = photo.shape[:2]
    bar = max(28, h // 18)
    out = Image.new("RGB", (2 * w + 8, h + bar), (16, 16, 16))
    out.paste(Image.fromarray(photo), (0, 0))
    out.paste(Image.fromarray(twin_img), (w + 8, 0))
    d = ImageDraw.Draw(out)
    f = _font(int(bar * 0.55))
    d.text((10, h + bar * 0.2), f"{left}  |  {right}  ·  {label}", fill=(240, 240, 240), font=f)
    for x0, text in ((8, left), (w + 16, right)):
        d.text((x0, 8), text, fill=(255, 255, 255), font=_font(int(bar * 0.5)), stroke_width=2, stroke_fill=(0, 0, 0))
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def make_twin(rgb: np.ndarray, sam, *, corners=None, hfov_deg: float = 70.0, placement: ArmPlacement | None = None,
              heights_mm: dict[int, float] | None = None, default_height_mm: float | None = None,
              twin_id: str | None = None, photo_source: str = "upload") -> Twin:
    """Photo -> Twin. Raises ValueError with a readable reason if the sheet cannot be found."""
    import time

    t0 = time.perf_counter()
    h, w = rgb.shape[:2]
    if corners is None:
        corners_px, source = find_sheet(rgb), "detected"
    else:
        corners_px, source = np.asarray(corners, float), "clicked"
    sheet = sheet_frame(corners_px, w, h, hfov_deg)
    if sheet.orthogonality > 0.08:
        raise ValueError(f"the sheet's corners do not fit a flat A4 sheet with a {hfov_deg:.0f} deg field of view "
                         f"(|cos| {sheet.orthogonality:.2f}); click the corners or set the field of view")
    placement = placement or ArmPlacement()
    objs, masks, stats = measure(sam, rgb, sheet, heights_mm, default_height_mm)
    spec, world = twin_spec(sheet, placement, objs)
    stats["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    stats["in_twin"] = sum(1 for x in world if x["in_twin"])
    tid = twin_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + secrets.token_hex(2)
    return Twin(tid, datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), w, h, sheet,
                placement, objs, world, spec, stats, source, photo_source, masks=masks)


# ---- storage ---------------------------------------------------------------------------------------
def twins_dir() -> Path:
    """deployments/twins at the repo root, or HASAMI_TWINS_DIR."""
    return Path(os.environ.get("HASAMI_TWINS_DIR") or ROOT / "deployments" / "twins")


def save_twin(twin: Twin, photo: np.ndarray, out_dir: Path | None = None) -> Path:
    """Write <id>/twin.json, photo.png and side_by_side.png (photo with the fits drawn | the twin)."""
    from .session import png_bytes

    d = (out_dir or twins_dir()) / twin.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "photo.png").write_bytes(png_bytes(photo))
    left = "synthetic photo (MuJoCo render)" if twin.photo_source == "synthetic" else "photo"
    (d / "side_by_side.png").write_bytes(side_by_side(overlay_photo(photo, twin),
                                                      render_twin(twin.spec, twin.width, twin.height), left=left))
    (d / "twin.json").write_text(json.dumps(twin.to_dict(), indent=2, default=_json_default) + "\n")
    return d


# ---- tasks on the twin ------------------------------------------------------------------------------
def _usable(twin: Twin) -> list[ObjectSpec]:
    ok = {w["name"] for w in twin.world if w["in_twin"] and w["reachable"] and w["graspable"]}
    return [o for o in twin.spec.objects if o.name in ok]


def twin_tasks(twin: Twin) -> dict[str, Task]:
    """The tasks this twin supports, keyed by id. Each is an ordinary benchmark Task (goal predicate on
    the physics state); the instruction names objects by colour and shape as a person would."""
    objs = _usable(twin)
    out: dict[str, Task] = {}
    if not objs:
        return out
    ids = [o.name for o in objs][:6]
    tray_n = min(4, len(ids))
    moves = [(n, "tray") for n in ids[:tray_n]] + [(n, "bin") for n in ids[tray_n:]]
    out["clear-desk"] = Task("clear-except-X", 0, "put every object in the tray (the grey bin takes any beyond "
                             "four)", twin.spec, moves, [], {"twin_task": "clear-desk"})
    cols = []
    for o in objs:
        if o.colour not in cols:
            cols.append(o.colour)
    if len(cols) >= 2:
        a, b = cols[0], cols[1]
        mv = [(o.name, "tray") for o in objs if o.colour == a][:4] + [(o.name, "bin") for o in objs if o.colour == b][:2]
        keep = [o.name for o in twin.spec.objects if o.name not in {m for m, _ in mv}]
        out["sort-by-colour"] = Task("sort-by-colour", 0, f"put the {a} things in the tray and the {b} things in "
                                     "the grey bin; leave everything else where it is", twin.spec, mv, keep,
                                     {"twin_task": "sort-by-colour", "tray": a, "bin": b})
    boxes = sorted([o for o in objs if o.shape != "cylinder"], key=lambda o: o.dims[0] * o.dims[1])
    if len(boxes) >= 2:
        top, base = boxes[0], boxes[-1]
        if min(base.dims[:2]) >= 0.8 * max(top.dims[:2]) and base.height + top.height < 0.07:
            keep = [o.name for o in twin.spec.objects if o.name != top.name]
            out["stack-two"] = Task("stack-two", 0, f"put the {top.colour} {top.kind} on the {base.colour} {base.kind}",
                                    twin.spec, [(top.name, base.name)], keep, {"twin_task": "stack-two"})
    return out


def task_menu(twin: Twin) -> list[dict]:
    return [{"id": k, "instruction": t.instruction, "steps": len(t.moves)} for k, t in twin_tasks(twin).items()]


__all__ = ["ArmPlacement", "TWIN_LABEL", "Twin", "camera_in_world", "in_any_box", "make_twin", "overlay_photo",
           "render_twin", "save_twin", "side_by_side", "task_menu", "to_world", "twin_spec", "twin_tasks", "twins_dir",
           "yaw_to_world"]
