"""The overhead camera: RGB, metric depth, ground-truth segmentation, and pixel <-> world geometry.

Depth and segmentation come straight from MuJoCo's renderer. Ground truth is used only to
*score* perception in tests and in the run report; the pipeline itself sees RGB + depth only.
"""

from __future__ import annotations

from dataclasses import dataclass

import mujoco
import numpy as np


@dataclass
class Frame:
    rgb: np.ndarray  # H x W x 3 uint8
    depth: np.ndarray  # H x W float32, metres along the optical axis
    seg_geom: np.ndarray  # H x W int32 geom id (-1 = none)
    cam_pos: np.ndarray
    cam_mat: np.ndarray  # 3x3, columns = camera x, y, z in world (camera looks along -z)
    fovy_deg: float

    @property
    def size(self) -> tuple[int, int]:
        return self.rgb.shape[1], self.rgb.shape[0]

    def intrinsics(self) -> tuple[float, float, float, float]:
        w, h = self.size
        f = 0.5 * h / np.tan(np.radians(self.fovy_deg) / 2)
        return f, f, (w - 1) / 2, (h - 1) / 2

    def pixel_to_world(self, u: float, v: float, depth: float | None = None) -> np.ndarray:
        """Back-project pixel (u right, v down) with its depth into world coordinates."""
        fx, fy, cx, cy = self.intrinsics()
        z = float(self.depth[int(round(v)), int(round(u))]) if depth is None else depth
        # MuJoCo camera frame: x right, y up, looking along -z.
        x_c = (u - cx) / fx * z
        y_c = -(v - cy) / fy * z
        p_cam = np.array([x_c, y_c, -z])
        return self.cam_pos + self.cam_mat @ p_cam

    def world_to_pixel(self, p: np.ndarray) -> tuple[float, float]:
        fx, fy, cx, cy = self.intrinsics()
        pc = self.cam_mat.T @ (np.asarray(p) - self.cam_pos)
        z = -pc[2]
        return float(cx + fx * pc[0] / z), float(cy - fy * pc[1] / z)


def capture(model: mujoco.MjModel, data: mujoco.MjData, camera: str = "overhead",
            width: int = 640, height: int = 480) -> Frame:
    cid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, camera)
    if cid < 0:
        raise KeyError(f"no camera named {camera!r}")
    r = mujoco.Renderer(model, height=height, width=width)
    try:
        r.update_scene(data, camera=camera)
        rgb = r.render().copy()
        r.enable_depth_rendering()
        r.update_scene(data, camera=camera)
        depth = r.render().copy().astype(np.float32)
        r.disable_depth_rendering()
        r.enable_segmentation_rendering()
        r.update_scene(data, camera=camera)
        seg = r.render().copy()
        r.disable_segmentation_rendering()
    finally:
        r.close()
    # seg[...,0] = object id, seg[...,1] = object type; keep geoms only.
    geom = np.where(seg[..., 1] == int(mujoco.mjtObj.mjOBJ_GEOM), seg[..., 0], -1).astype(np.int32)
    return Frame(rgb, depth, geom, data.cam_xpos[cid].copy(), data.cam_xmat[cid].reshape(3, 3).copy(),
                 float(model.cam_fovy[cid]))


def ground_truth_mask(model: mujoco.MjModel, frame: Frame, geom_name: str) -> np.ndarray:
    gid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, geom_name)
    if gid < 0:
        raise KeyError(f"no geom named {geom_name!r}")
    return frame.seg_geom == gid
