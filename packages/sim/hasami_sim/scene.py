"""Procedural tabletop scenes built with mujoco.MjSpec: objects of several shapes and sizes, distractor
colours, and one or two containers, on top of the SO-101 base scene (robot, table, cameras, lights).

A SceneSpec is plain data (seeded, reproducible, JSON-able). `build()` turns it into a compiled MjModel;
`sample_scene()` draws one from a seed. Ground truth (which body is which object, how big it is) lives
in the spec and is used only to score perception and to check goals, never as perception input.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

import mujoco
import numpy as np

from .arm import ASSETS

BASE = ASSETS / "scene_base.xml"

COLOURS: dict[str, tuple[float, float, float]] = {
    "red": (0.87, 0.16, 0.16),
    "green": (0.16, 0.70, 0.32),
    "blue": (0.18, 0.36, 0.90),
    "yellow": (0.95, 0.80, 0.15),
    "purple": (0.55, 0.25, 0.75),
    "orange": (0.98, 0.50, 0.10),
}

Shape = Literal["cube", "block", "flat", "cylinder"]

# Full sizes in metres. box: (x, y, z); cylinder: (diameter, diameter, height). Every object is narrow
# enough across one axis to fit the ~40 mm pre-grasp opening. Cylinders are 40 mm tall: shorter ones
# (30-35 mm) slip out of the hinged jaw in this model (measured: 1/6 placed at 35 mm, 6/6 at 40 mm).
SHAPES: dict[str, tuple[Shape, tuple[float, float, float]]] = {
    "small cube": ("cube", (0.020, 0.020, 0.020)),
    "cube": ("cube", (0.025, 0.025, 0.025)),
    "big cube": ("cube", (0.030, 0.030, 0.030)),
    "block": ("block", (0.036, 0.022, 0.025)),
    "flat box": ("flat", (0.040, 0.026, 0.016)),
    "cylinder": ("cylinder", (0.024, 0.024, 0.040)),
}

OBJECT_FRICTION = (1.6, 0.02, 0.002)
OBJECT_SOLREF = (0.004, 1.0)


@dataclass
class ObjectSpec:
    name: str  # MuJoCo body (and geom) name
    kind: str  # key of SHAPES, e.g. "cube", "cylinder"
    colour: str  # key of COLOURS
    x: float
    y: float
    yaw: float = 0.0
    size: tuple[float, float, float] | None = None  # defaults to SHAPES[kind]
    mass: float = 0.02
    # A digital twin's objects are measured, not drawn from SHAPES: they set their own form ("block" for
    # any box, or "cylinder") and the mean colour seen in the photo (`colour` is then its nearest name).
    form: Shape | None = None
    rgba: tuple[float, float, float, float] | None = None
    base_z: float = 0.0  # height it stands on (a container's floor when it starts inside one)
    # 4 adds torsional friction at its contacts: a fridge item gripped on its sides would otherwise spin
    # about the jaw axis like a pinned plank (3: sliding friction only, as on the tabletop).
    condim: int = 3

    @property
    def shape(self) -> Shape:
        return self.form or SHAPES[self.kind][0]

    @property
    def dims(self) -> tuple[float, float, float]:
        return tuple(self.size or SHAPES[self.kind][1])  # type: ignore[return-value]

    @property
    def height(self) -> float:
        return self.dims[2]

    @property
    def label(self) -> str:
        return f"{self.colour} {self.kind}"

    @property
    def grip_width(self) -> float:
        """Width across the jaws when gripped across the narrow side (diameter for a cylinder)."""
        return min(self.dims[0], self.dims[1])


@dataclass
class ContainerSpec:
    name: str
    label: str
    x: float
    y: float
    half: float = 0.05  # outer half-size of the square footprint
    wall_h: float = 0.024
    floor_h: float = 0.006
    rgba: tuple[float, float, float, float] = (0.18, 0.20, 0.24, 1.0)

    @property
    def inner_half(self) -> float:
        return self.half - 0.006


@dataclass
class FridgeSpec:
    """An open fridge in front of the arm, built from static boxes (they collide with the arm and the items).

    Coordinates are the arm's: x forward, y left, z up from the plane the arm stands on. The cabinet's open
    front is the plane x = front; the item shelf's top is at shelf_z (the lower compartment below it is a
    closed drawer block), a second shelf at upper_z, the ceiling at height. The door is hinged on the -y
    front edge and swung open by door_deg, so it stands beside the robot, out of the arm's way."""

    front: float = 0.255
    depth: float = 0.24  # inside, front to back wall
    y: float = 0.0  # centre of the opening
    inner_half_w: float = 0.15  # half the inside width
    shelf_z: float = 0.055
    upper_z: float = 0.215
    height: float = 0.36  # inside ceiling
    wall: float = 0.015
    door_deg: float = 115.0
    rgba: tuple[float, float, float, float] = (0.93, 0.94, 0.95, 1.0)  # inside
    shell_rgba: tuple[float, float, float, float] = (0.80, 0.82, 0.85, 1.0)  # outside and door

    @property
    def back(self) -> float:
        return self.front + self.depth


@dataclass
class SceneSpec:
    objects: list[ObjectSpec]
    containers: list[ContainerSpec] = field(default_factory=list)
    seed: int | None = None
    name: str = "custom"
    # Extra cameras {name, pos, quat, fovy} and visual-only boxes {name, size (half), pos, quat, rgba}
    # (a digital twin adds the photo's camera and the A4 sheet).
    cameras: list[dict] = field(default_factory=list)
    visuals: list[dict] = field(default_factory=list)
    fridge: FridgeSpec | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SceneSpec":
        fridge = d.get("fridge")
        return cls(objects=[ObjectSpec(**o) for o in d["objects"]],
                   containers=[ContainerSpec(**c) for c in d.get("containers", [])],
                   seed=d.get("seed"), name=d.get("name", "custom"), cameras=list(d.get("cameras", [])),
                   visuals=list(d.get("visuals", [])), fridge=FridgeSpec(**fridge) if fridge else None)

    def object(self, name: str) -> ObjectSpec:
        for o in self.objects:
            if o.name == name:
                return o
        raise KeyError(name)

    def container(self, name: str) -> ContainerSpec:
        for c in self.containers:
            if c.name == name:
                return c
        raise KeyError(name)

    @property
    def labels(self) -> dict[str, str]:
        out = {o.name: o.label for o in self.objects}
        out.update({c.name: c.label for c in self.containers})
        return out


TRAY = ContainerSpec("tray", "tray", 0.17, 0.17)
GREY_BIN = ContainerSpec("bin", "grey bin", 0.17, -0.17, rgba=(0.55, 0.57, 0.60, 1.0))
# The procedural scenes move the containers out to the sides (still reachable, measured) so the reach
# annulus in front of the arm has room for up to eight objects.
# They are also larger (130 mm) so the fingers clear the walls with several objects inside.
SIDE_TRAY = ContainerSpec("tray", "tray", 0.10, 0.21, half=0.065)
SIDE_BIN = ContainerSpec("bin", "grey bin", 0.10, -0.21, half=0.065, rgba=(0.55, 0.57, 0.60, 1.0))


def classic(seed: int | None = None) -> SceneSpec:
    """The original scene: a red, a green and a blue 25 mm cube and the tray (cube poses set by the caller)."""
    objs = [ObjectSpec("red_cube", "cube", "red", 0.235, 0.075), ObjectSpec("green_cube", "cube", "green", 0.2, -0.07),
            ObjectSpec("blue_cube", "cube", "blue", 0.27, -0.02)]
    return SceneSpec(objs, [TRAY], seed=seed, name="classic")


def build(spec: SceneSpec, base: Path = BASE) -> mujoco.MjModel:
    """Compile the base scene plus the spec's containers (fixed) and objects (free bodies)."""
    s = mujoco.MjSpec.from_file(str(base))
    wb = s.worldbody
    for c in spec.containers:
        b = wb.add_body(name=c.name, pos=[c.x, c.y, 0.0])
        h, t = c.half, 0.003
        parts = {"floor": ([h, h, c.floor_h / 2], [0, 0, c.floor_h / 2]),
                 "wall_n": ([h, t, c.wall_h / 2], [0, h - t, c.wall_h / 2]),
                 "wall_s": ([h, t, c.wall_h / 2], [0, -(h - t), c.wall_h / 2]),
                 "wall_e": ([t, h, c.wall_h / 2], [h - t, 0, c.wall_h / 2]),
                 "wall_w": ([t, h, c.wall_h / 2], [-(h - t), 0, c.wall_h / 2])}
        for part, (size, pos) in parts.items():
            b.add_geom(name=f"{c.name}_{part}", type=mujoco.mjtGeom.mjGEOM_BOX, size=size, pos=pos, rgba=list(c.rgba))
    for v in spec.visuals:
        wb.add_geom(name=v["name"], type=mujoco.mjtGeom.mjGEOM_BOX, size=list(v["size"]), pos=list(v["pos"]),
                    quat=list(v.get("quat", (1, 0, 0, 0))), rgba=list(v["rgba"]), contype=0, conaffinity=0)
    for c in spec.cameras:
        wb.add_camera(name=c["name"], pos=list(c["pos"]), quat=list(c["quat"]), fovy=float(c["fovy"]))
    if spec.fridge is not None:
        build_fridge(s, spec.fridge)
    build_objects(s, spec.objects)
    return s.compile()


def build_fridge(s: mujoco.MjSpec, f: FridgeSpec) -> None:
    """The fridge as one static body named "fridge": drawer block, item shelf, upper shelf, walls, back,
    ceiling, and the open door (with two door bins) beside it. A light inside, as a real fridge has."""
    b = s.worldbody.add_body(name="fridge", pos=[0.0, 0.0, 0.0])
    w, hw = f.wall, f.inner_half_w
    x0, x1 = f.front, f.back
    xc, xl = (x0 + x1) / 2, (x1 - x0) / 2
    top = f.height + w

    def box(name: str, half, pos, rgba, quat=(1.0, 0.0, 0.0, 0.0), solid: bool = True) -> None:
        g = b.add_geom(name=f"fridge_{name}", type=mujoco.mjtGeom.mjGEOM_BOX, size=list(half), pos=list(pos),
                       quat=list(quat), rgba=list(rgba))
        if not solid:
            g.contype, g.conaffinity = 0, 0

    inner, shell = f.rgba, f.shell_rgba
    # The lower compartment is closed (a drawer): its top is the item shelf.
    box("drawer", [xl, hw, f.shelf_z / 2], [xc, f.y, f.shelf_z / 2], inner)
    box("drawer_front", [0.003, hw - 0.01, f.shelf_z / 2 - 0.006], [x0 - 0.003, f.y, f.shelf_z / 2],
        (0.86, 0.90, 0.93, 1.0))
    box("drawer_handle", [0.006, 0.05, 0.004], [x0 - 0.009, f.y, f.shelf_z - 0.014], (0.62, 0.64, 0.68, 1.0))
    box("shelf_lip", [0.004, hw, 0.002], [x0 + 0.004, f.y, f.shelf_z + 0.002], (0.70, 0.84, 0.92, 1.0))
    box("upper_shelf", [xl, hw, 0.004], [xc, f.y, f.upper_z - 0.004], (0.82, 0.90, 0.95, 1.0))
    for side, sy in (("left", 1.0), ("right", -1.0)):
        box(f"wall_{side}", [xl + w / 2, w / 2, top / 2], [xc + w / 2, f.y + sy * (hw + w / 2), top / 2], shell)
        box(f"liner_{side}", [xl, 0.001, (f.height - f.shelf_z) / 2],
            [xc, f.y + sy * (hw - 0.001), (f.height + f.shelf_z) / 2], inner, solid=False)
    box("back", [w / 2, hw + w, top / 2], [x1 + w / 2, f.y, top / 2], inner)
    box("ceiling", [xl + w / 2, hw + w, w / 2], [xc + w / 2, f.y, f.height + w / 2], shell)
    # The door: hinged on the -y front edge, swung open towards the robot.
    a = np.radians(f.door_deg)
    hinge = np.array([x0, f.y - hw - w])
    d_out = np.array([-np.sin(a), np.cos(a)])  # from the hinge along the door's width (closed: +y)
    door_w, door_t = 2 * (hw + w), 0.03
    normal = np.array([np.cos(a), np.sin(a)])  # out of the door's inside face (closed: +x, into the cabinet)
    q = [np.cos(a / 2), 0.0, 0.0, np.sin(a / 2)]
    centre = hinge + d_out * door_w / 2 - normal * door_t / 2
    box("door", [door_t / 2, door_w / 2, top / 2], [centre[0], centre[1], top / 2], shell, quat=q)
    for k, z in enumerate((0.10, 0.24)):  # door bins on the inside face
        c = hinge + d_out * door_w / 2 + normal * 0.022
        box(f"door_bin{k}", [0.022, door_w / 2 - 0.03, 0.022], [c[0], c[1], z], (0.84, 0.92, 0.97, 1.0), quat=q,
            solid=False)
    s.worldbody.add_light(name="fridge_light", pos=[x0 + 0.03, f.y, f.height - 0.03], dir=[0.5, 0.0, -1.0],
                          diffuse=[0.55, 0.55, 0.52], castshadow=False)
    s.worldbody.add_light(name="fridge_fill", pos=[x0 - 0.25, f.y, f.upper_z], dir=[1.0, 0.0, -0.25],
                          diffuse=[0.35, 0.35, 0.35], castshadow=False)


def build_objects(s: mujoco.MjSpec, objects: list[ObjectSpec], z0: float = 0.0) -> None:
    """Add each object as a free body standing on the table (its base at height z0)."""
    wb = s.worldbody
    for o in objects:
        dx, dy, dz = o.dims
        b = wb.add_body(name=o.name, pos=[o.x, o.y, z0 + o.base_z + dz / 2], quat=[np.cos(o.yaw / 2), 0, 0, np.sin(o.yaw / 2)])
        b.add_freejoint(name=o.name)
        if o.shape == "cylinder":
            g = b.add_geom(name=o.name, type=mujoco.mjtGeom.mjGEOM_CYLINDER, size=[dx / 2, dz / 2, 0])
        else:
            g = b.add_geom(name=o.name, type=mujoco.mjtGeom.mjGEOM_BOX, size=[dx / 2, dy / 2, dz / 2])
        g.rgba = list(o.rgba) if o.rgba is not None else [*COLOURS[o.colour], 1.0]
        g.mass = o.mass
        g.friction = list(OBJECT_FRICTION)
        g.solref = list(OBJECT_SOLREF)
        if o.condim != 3:
            g.condim = o.condim


# ---- sampling -----------------------------------------------------------------------------------
# Where loose objects may start: the annulus a top-down grasp at the 65 mm travel height reaches
# (measured: 0.15-0.285 m from the base axis), clear of the containers.
REACH_R = (0.16, 0.272)
REGION_X = (0.15, 0.28)
REGION_Y = (-0.14, 0.14)
MAX_YAW = np.radians(40.0)


def footprint_radius(o: ObjectSpec) -> float:
    dx, dy, _ = o.dims
    return 0.5 * float(np.hypot(dx, dy))


def reachable(x: float, y: float, r_obj: float, containers: list[ContainerSpec], margin: float = 0.012) -> bool:
    """Is (x, y) inside the reach annulus and clear of every container's footprint?"""
    r = float(np.hypot(x, y))
    if not (REACH_R[0] <= r <= REACH_R[1]):
        return False
    return all(max(abs(x - c.x), abs(y - c.y)) > c.half + r_obj + margin for c in containers)


def place_objects(rng: np.random.Generator, objs: list[ObjectSpec], containers: list[ContainerSpec] | None = None,
                  gap: float = 0.024, tries: int = 400,
                  avoid: list[tuple[float, float, float]] | None = None) -> bool:
    """Give each object a random x, y, yaw in reach, at least `gap` between footprints (room for the jaws)
    and clear of the containers (and of each (x, y, radius) in `avoid`). Returns False if no layout was
    found."""
    containers = containers or []
    for _ in range(50):
        placed: list[ObjectSpec] = []
        for o in objs:
            r = footprint_radius(o)
            for _ in range(tries):
                x, y = rng.uniform(*REGION_X), rng.uniform(*REGION_Y)
                if not reachable(x, y, r, containers):
                    continue
                if avoid and any(np.hypot(x - ax, y - ay) < ar + r for ax, ay, ar in avoid):
                    continue
                if all(np.hypot(x - p.x, y - p.y) >= r + footprint_radius(p) + gap for p in placed):
                    o.x, o.y = float(x), float(y)
                    o.yaw = 0.0 if o.shape == "cylinder" else float(rng.uniform(-MAX_YAW, MAX_YAW))
                    placed.append(o)
                    break
            else:
                break
        if len(placed) == len(objs):
            return True
    return False


def sample_scene(seed: int, n: int | None = None, kinds: list[str] | None = None, colours: list[str] | None = None,
                 containers: list[ContainerSpec] | None = None, name: str = "mixed") -> SceneSpec:
    """A random scene: n objects (4-8 by default) of random kinds and colours, and the given containers."""
    rng = np.random.default_rng(seed)
    n = int(n if n is not None else rng.integers(4, 9))
    kinds = kinds or list(SHAPES)
    colours = colours or list(COLOURS)
    objs = []
    for i in range(n):
        kind = str(rng.choice(kinds))
        colour = str(rng.choice(colours))
        objs.append(ObjectSpec(f"obj{i}_{colour}_{kind.replace(' ', '_')}", kind, colour, 0.0, 0.0))
    conts = list(containers if containers is not None else [SIDE_TRAY])
    if not place_objects(rng, objs, conts):
        raise ValueError(f"could not place {n} objects without overlap (seed {seed})")
    return SceneSpec(objs, conts, seed=seed, name=name)


# ---- the fridge ---------------------------------------------------------------------------------
# What a fridge holds, scaled to the SO-101's jaws (41 mm open, measured): the narrow side of every item fits
# between them. Real ones are bigger (a 330 ml can is 66 mm across); the sizes here are labelled as scaled.
# name -> (form, full size (diameter, diameter, height) or (x, y, z), colour name, rgba, mass kg, words)
ITEMS: dict[str, tuple[Shape, tuple[float, float, float], str, tuple[float, float, float, float], float, tuple[str, ...]]] = {
    "juice carton": ("block", (0.032, 0.032, 0.080), "orange", (0.97, 0.56, 0.12, 1.0), 0.06,
                     ("juice", "carton", "orange juice")),
    "can": ("cylinder", (0.030, 0.030, 0.056), "red", (0.80, 0.08, 0.11, 1.0), 0.05, ("can", "soda", "cola", "coke")),
    "bottle": ("cylinder", (0.026, 0.026, 0.085), "green", (0.12, 0.56, 0.30, 1.0), 0.05,
               ("bottle", "water", "milk bottle")),
}
FRIDGE = FridgeSpec()
# The robot's own basket (blue plastic), on a riser on its deck beside the arm (+y side): the "bring it back" target.
BASKET = ContainerSpec("basket", "basket", 0.105, 0.235, half=0.06, wall_h=0.075, floor_h=0.04,
                       rgba=(0.17, 0.38, 0.82, 1.0))
BASKET_COLOUR = "blue"
FRIDGE_SLOTS_Y = (-0.075, 0.0, 0.075)
FRIDGE_INSET = 0.045  # item centres sit this far behind the shelf's front edge


def look_at(pos, target, fovy: float, name: str) -> dict:
    """A camera {name, pos, quat, fovy} at `pos` looking at `target` with the world's z up in the image."""
    p, t = np.asarray(pos, float), np.asarray(target, float)
    f = (t - p) / np.linalg.norm(t - p)
    x = np.cross(f, [0.0, 0.0, 1.0])
    x /= np.linalg.norm(x)
    z = -f
    y = np.cross(z, x)
    q = np.zeros(4)
    mujoco.mju_mat2Quat(q, np.column_stack([x, y, z]).ravel())
    return {"name": name, "pos": [float(v) for v in p], "quat": [float(v) for v in q], "fovy": float(fovy)}


# The robot's head camera: on a mast behind the arm, looking down into the open fridge at the item shelf.
HEAD_CAMERA = look_at((-0.06, 0.0, 0.29), (0.31, 0.0, 0.085), 44.0, "head")
# A three-quarter view of the fridge, the arm and the basket, for the fetch video only (perception never sees it).
FETCH_DEMO_CAMERA = look_at((-0.30, 0.42, 0.42), (0.20, 0.02, 0.09), 50.0, "fetch_demo")


def item_spec(name: str, x: float, y: float, yaw: float = 0.0, fridge: FridgeSpec = FRIDGE) -> ObjectSpec:
    form, size, colour, rgba, mass, _ = ITEMS[name]
    return ObjectSpec(name.replace(" ", "_"), name, colour, x, y, yaw=yaw, size=size, mass=mass, form=form, rgba=rgba,
                      base_z=fridge.shelf_z, condim=4)


def fridge_scene(seed: int = 0, items: list[str] | None = None) -> SceneSpec:
    """The open fridge with the juice carton, the can and the bottle on the item shelf (their order across the
    shelf and a few millimetres of position and yaw drawn from the seed), the basket and the head camera."""
    rng = np.random.default_rng([seed, 31])
    names = list(items or ITEMS)
    if len(names) > len(FRIDGE_SLOTS_Y):
        raise ValueError(f"at most {len(FRIDGE_SLOTS_Y)} items fit on the shelf")
    slots = rng.permutation(len(FRIDGE_SLOTS_Y))[:len(names)]
    objs = []
    for name, k in zip(names, slots):
        x = FRIDGE.front + FRIDGE_INSET + float(rng.uniform(-0.004, 0.008))
        y = FRIDGE.y + FRIDGE_SLOTS_Y[int(k)] + float(rng.uniform(-0.006, 0.006))
        yaw = float(rng.uniform(-np.radians(15), np.radians(15))) if ITEMS[name][0] != "cylinder" else 0.0
        objs.append(item_spec(name, x, y, yaw))
    return SceneSpec(objs, [BASKET], seed=seed, name="fridge", cameras=[HEAD_CAMERA, FETCH_DEMO_CAMERA], fridge=FRIDGE)
