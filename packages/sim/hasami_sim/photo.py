"""Synthetic desk photos rendered by MuJoCo: a table, a sheet of A4 paper, some objects and a tilted
camera, and nothing else (no arm, no depth). They are the input the digital twin is tested on: render a
scene whose poses are known, feed only the RGB image back through the twin pipeline, and compare what
it recovers with the poses that were rendered. Labelled "synthetic photo" wherever it is shown.

Coordinates are the simulator's world frame (metres; the arm's base at the origin, facing +x). The
sheet is a thin white box lying on the table.
"""

from __future__ import annotations

from dataclasses import dataclass

import mujoco
import numpy as np

from .scene import COLOURS, SIDE_BIN, SIDE_TRAY, ObjectSpec, build_objects, place_objects

A4_M = (0.210, 0.297)
TABLE_RGB = (0.62, 0.52, 0.42)  # a mid-brown desk
PAPER_RGB = (0.94, 0.94, 0.92)


@dataclass
class SheetPose:
    """Where the A4 sheet lies: centre (x, y) in m, yaw of its long side from the world x axis."""

    x: float = 0.215
    y: float = 0.0
    yaw: float = np.pi / 2  # long side along world y (landscape as the arm sees it)

    def corners(self) -> np.ndarray:
        """The four corners in world metres (z = 0)."""
        c, s = np.cos(self.yaw), np.sin(self.yaw)
        half = np.array([[A4_M[1] / 2, A4_M[0] / 2], [-A4_M[1] / 2, A4_M[0] / 2], [-A4_M[1] / 2, -A4_M[0] / 2],
                         [A4_M[1] / 2, -A4_M[0] / 2]])
        return half @ np.array([[c, s], [-s, c]]) + [self.x, self.y]


@dataclass
class PhotoCamera:
    """A pinhole camera at `pos` looking at `target` (world, m), with a vertical field of view."""

    pos: tuple[float, float, float] = (0.62, 0.08, 0.42)
    target: tuple[float, float, float] = (0.215, 0.0, 0.0)
    fovy_deg: float = 50.0
    width: int = 960
    height: int = 720

    def xyaxes(self) -> tuple[np.ndarray, np.ndarray]:
        """MuJoCo camera x (right) and y (up) axes in the world."""
        f = np.asarray(self.target, float) - np.asarray(self.pos, float)
        f /= np.linalg.norm(f)
        x = np.cross(f, [0.0, 0.0, 1.0])
        x /= np.linalg.norm(x)
        y = np.cross(x, f)
        return x, y

    def quat(self) -> np.ndarray:
        """MuJoCo camera orientation (w, x, y, z): columns x, y, z = right, up, backwards."""
        x, y = self.xyaxes()
        q = np.zeros(4)
        mujoco.mju_mat2Quat(q, np.c_[x, y, np.cross(x, y)].flatten())
        return q

    def project(self, pts: np.ndarray) -> np.ndarray:
        """World points (N, 3) -> pixels (N, 2), u right, v down."""
        x, y = self.xyaxes()
        z = np.cross(x, y)
        rel = np.atleast_2d(pts) - np.asarray(self.pos, float)
        pc = rel @ np.c_[x, y, z]
        f = 0.5 * self.height / np.tan(np.radians(self.fovy_deg) / 2)
        return np.c_[(self.width - 1) / 2 + f * pc[:, 0] / -pc[:, 2], (self.height - 1) / 2 - f * pc[:, 1] / -pc[:, 2]]

    @property
    def hfov_deg(self) -> float:
        f = 0.5 * self.height / np.tan(np.radians(self.fovy_deg) / 2)
        return float(np.degrees(2 * np.arctan(0.5 * self.width / f)))


def photo_model(objects: list[ObjectSpec], sheet: SheetPose | None, cam: PhotoCamera) -> mujoco.MjModel:
    s = mujoco.MjSpec()
    s.option.timestep = 0.002
    s.visual.global_.offwidth = max(cam.width, 640)
    s.visual.global_.offheight = max(cam.height, 480)
    s.visual.headlight.diffuse = [0.35, 0.35, 0.35]
    s.visual.headlight.ambient = [0.25, 0.25, 0.25]
    s.visual.headlight.specular = [0, 0, 0]
    wb = s.worldbody
    wb.add_light(pos=[0.25, -0.3, 0.9], dir=[-0.2, 0.3, -1], diffuse=[0.55, 0.55, 0.55], castshadow=True,
                 type=mujoco.mjtLightType.mjLIGHT_DIRECTIONAL)
    wb.add_geom(name="table", type=mujoco.mjtGeom.mjGEOM_PLANE, size=[1.0, 1.0, 0.02], rgba=[*TABLE_RGB, 1])
    if sheet is not None:
        wb.add_geom(name="a4", type=mujoco.mjtGeom.mjGEOM_BOX, size=[A4_M[1] / 2, A4_M[0] / 2, 0.0002],
                    pos=[sheet.x, sheet.y, 0.0002], quat=[np.cos(sheet.yaw / 2), 0, 0, np.sin(sheet.yaw / 2)],
                    rgba=[*PAPER_RGB, 1], contype=0, conaffinity=0)
    wb.add_camera(name="photo", pos=list(cam.pos), fovy=cam.fovy_deg, quat=list(cam.quat()))
    build_objects(s, objects, z0=0.0004 if sheet is not None else 0.0)
    return s.compile()


def render_photo(objects: list[ObjectSpec], sheet: SheetPose | None = None,
                 cam: PhotoCamera | None = None) -> np.ndarray:
    """RGB (H x W x 3 uint8) of the objects on a desk with an A4 sheet, from `cam`. Static: no physics is
    stepped, objects are drawn where their specs say (standing on the table)."""
    cam = cam or PhotoCamera()
    model = photo_model(objects, sheet if sheet is not None else SheetPose(), cam)
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    r = mujoco.Renderer(model, height=cam.height, width=cam.width)
    try:
        r.update_scene(data, camera="photo")
        return r.render().copy()
    finally:
        r.close()


DESK_KINDS = ["cube", "small cube", "big cube", "block", "flat box", "cylinder"]


def random_desk(seed: int) -> tuple[list[ObjectSpec], PhotoCamera]:
    """3-5 objects of distinct colours in a layout the arm can work in (the benchmark's sampler, clear of
    the twin's tray and bin), and a camera at a random pose in front of the sheet (azimuth +-25 deg,
    elevation 38-65 deg, 0.45-0.62 m, vertical FOV 45-60 deg) that sees the whole sheet."""
    rng = np.random.default_rng([seed, 2026])
    n = int(rng.integers(3, 6))
    cols = [str(c) for c in rng.choice(list(COLOURS), size=n, replace=False)]
    objs = [ObjectSpec(f"truth{i}", str(rng.choice(DESK_KINDS)), c, 0.0, 0.0) for i, c in enumerate(cols)]
    if not place_objects(rng, objs, [SIDE_TRAY, SIDE_BIN]):
        raise ValueError(f"no layout for seed {seed}")
    sheet = SheetPose()
    for _ in range(200):
        az = np.radians(rng.uniform(-25, 25))
        el = np.radians(rng.uniform(38, 65))
        dist = rng.uniform(0.45, 0.62)
        tgt = np.array([0.215 + rng.uniform(-0.02, 0.02), rng.uniform(-0.02, 0.02), 0.0])
        pos = tgt + dist * np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
        cam = PhotoCamera(pos=tuple(float(v) for v in pos), target=tuple(float(v) for v in tgt),
                          fovy_deg=float(rng.uniform(45, 60)))
        px = cam.project(np.c_[sheet.corners(), np.zeros(4)])
        if px[:, 0].min() > 20 and px[:, 0].max() < cam.width - 20 and px[:, 1].min() > 20 and \
                px[:, 1].max() < cam.height - 20:
            return objs, cam
    raise ValueError(f"no camera pose for seed {seed}")


__all__ = ["A4_M", "DESK_KINDS", "PhotoCamera", "SheetPose", "photo_model", "random_desk", "render_photo"]
