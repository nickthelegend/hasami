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
class SceneSpec:
    objects: list[ObjectSpec]
    containers: list[ContainerSpec] = field(default_factory=list)
    seed: int | None = None
    name: str = "custom"
    # Extra cameras {name, pos, quat, fovy} and visual-only boxes {name, size (half), pos, quat, rgba}
    # (a digital twin adds the photo's camera and the A4 sheet).
    cameras: list[dict] = field(default_factory=list)
    visuals: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SceneSpec":
        return cls(objects=[ObjectSpec(**o) for o in d["objects"]],
                   containers=[ContainerSpec(**c) for c in d.get("containers", [])],
                   seed=d.get("seed"), name=d.get("name", "custom"), cameras=list(d.get("cameras", [])),
                   visuals=list(d.get("visuals", [])))

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
    build_objects(s, spec.objects)
    return s.compile()


def build_objects(s: mujoco.MjSpec, objects: list[ObjectSpec], z0: float = 0.0) -> None:
    """Add each object as a free body standing on the table (its base at height z0)."""
    wb = s.worldbody
    for o in objects:
        dx, dy, dz = o.dims
        b = wb.add_body(name=o.name, pos=[o.x, o.y, z0 + dz / 2], quat=[np.cos(o.yaw / 2), 0, 0, np.sin(o.yaw / 2)])
        b.add_freejoint(name=o.name)
        if o.shape == "cylinder":
            g = b.add_geom(name=o.name, type=mujoco.mjtGeom.mjGEOM_CYLINDER, size=[dx / 2, dz / 2, 0])
        else:
            g = b.add_geom(name=o.name, type=mujoco.mjtGeom.mjGEOM_BOX, size=[dx / 2, dy / 2, dz / 2])
        g.rgba = list(o.rgba) if o.rgba is not None else [*COLOURS[o.colour], 1.0]
        g.mass = o.mass
        g.friction = list(OBJECT_FRICTION)
        g.solref = list(OBJECT_SOLREF)


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
                  gap: float = 0.024, tries: int = 400) -> bool:
    """Give each object a random x, y, yaw in reach, at least `gap` between footprints (room for the jaws)
    and clear of the containers. Returns False if no layout was found."""
    containers = containers or []
    for _ in range(50):
        placed: list[ObjectSpec] = []
        for o in objs:
            r = footprint_radius(o)
            for _ in range(tries):
                x, y = rng.uniform(*REGION_X), rng.uniform(*REGION_Y)
                if not reachable(x, y, r, containers):
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
