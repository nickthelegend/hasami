"""Digital twin from one photo, tested as a round trip through MuJoCo (no mocks): render a desk photo with an
A4 sheet and known objects (no arm, no depth), give only its RGB image to the twin pipeline, and compare
what comes back with what was rendered. The webcam is never opened."""

import io

import mujoco
import numpy as np
import pytest
from PIL import Image

from hasami_core.tasks import oracle_steps, world_from
from hasami_core.twin import (TWIN_LABEL, ArmPlacement, camera_in_world, make_twin, render_twin, side_by_side,
                              twin_tasks)
from hasami_perception.twin import find_sheet, fit_object, order_corners, sheet_frame
from hasami_sim.photo import PhotoCamera, SheetPose, photo_model, random_desk, render_photo
from hasami_sim.scene import ObjectSpec

CAM = PhotoCamera(pos=(0.52, 0.07, 0.34), fovy_deg=50)
OBJS = [ObjectSpec("a", "cube", "red", 0.20, 0.06, 0.3), ObjectSpec("b", "block", "blue", 0.25, -0.05, -0.4),
        ObjectSpec("c", "cylinder", "green", 0.17, -0.08), ObjectSpec("d", "flat box", "yellow", 0.24, 0.09, 0.7)]


def truth_masks(objs, cam):
    model = photo_model(objs, SheetPose(), cam)
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    r = mujoco.Renderer(model, cam.height, cam.width)
    try:
        r.enable_segmentation_rendering()
        r.update_scene(data, camera="photo")
        seg = r.render()[..., 0].copy()
    finally:
        r.close()
    return {o.name: seg == mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, o.name) for o in objs}


@pytest.mark.parametrize("seed", [0, 3])
def test_sheet_corners_plane_and_camera_pose(seed):
    objs, cam = random_desk(seed)
    rgb = render_photo(objs, SheetPose(), cam)
    corners = find_sheet(rgb)
    truth = order_corners(cam.project(np.c_[SheetPose().corners(), np.zeros(4)]))
    assert np.abs(corners - truth).max() < 2.0  # pixels
    frame = sheet_frame(corners, rgb.shape[1], rgb.shape[0], cam.hfov_deg)
    assert frame.landscape and frame.orthogonality < 0.02
    # the photo's camera, recovered in the twin's world, is where it was rendered from
    pos = np.array(camera_in_world(frame, ArmPlacement())["pos"])
    assert np.linalg.norm(pos - np.array(cam.pos)) < 0.012


def test_no_sheet_is_a_readable_error():
    rgb = np.full((480, 640, 3), (150, 120, 100), np.uint8)
    with pytest.raises(ValueError, match="no sheet"):
        find_sheet(rgb)


def test_fit_box_and_cylinder_on_truth_masks():
    """Geometry alone (truth masks, no SAM): heights from the silhouette within 2 mm, sizes within 1.5 mm."""
    rgb = render_photo(OBJS, SheetPose(), CAM)
    frame = sheet_frame(find_sheet(rgb), rgb.shape[1], rgb.shape[0], CAM.hfov_deg)
    masks = truth_masks(OBJS, CAM)
    for o in OBJS:
        f = fit_object(masks[o.name], rgb, frame)
        assert f is not None, o.label
        assert f["kind"] == ("cylinder" if o.shape == "cylinder" else "box"), o.label
        assert abs(f["height"] - o.height * 1000) < 2.0, (o.label, f["height"])
        assert sorted(f["size"]) == pytest.approx(sorted(np.array(o.dims[:2]) * 1000), abs=1.5), (o.label, f["size"])
        assert f["iou"] > 0.94
    typed = fit_object(masks["a"], rgb, frame, height_mm=40.0)
    assert typed["height"] == 40.0 and typed["height_source"] == "typed"


@pytest.mark.parametrize("seed", [1, 4])
def test_round_trip_photo_to_twin(session, seed):
    """SAM 2.1 automatic masks on the rendered photo: every object found, nothing extra, poses close."""
    objs, cam = random_desk(seed)
    rgb = render_photo(objs, SheetPose(), cam)
    twin = make_twin(rgb, session.sam, hfov_deg=cam.hfov_deg)
    assert len(twin.objects) == len(objs), [o.colour for o in twin.objects]
    for t in objs:
        w, o = min(((w, o) for o, w in zip(twin.objects, twin.world)), key=lambda p: np.hypot(p[0]["x_m"] - t.x,
                                                                                              p[0]["y_m"] - t.y))
        assert np.hypot(w["x_m"] - t.x, w["y_m"] - t.y) < 0.005, t.label
        assert o.kind == ("cylinder" if t.shape == "cylinder" else "box") and o.colour == t.colour, t.label
        assert abs(o.size_mm[2] - t.height * 1000) < 10.0, (t.label, o.size_mm)
        assert sorted(o.size_mm[:2]) == pytest.approx(sorted(np.array(t.dims[:2]) * 1000), abs=8.0), t.label
        if t.shape != "cylinder":
            period = np.pi / 2 if abs(t.dims[0] - t.dims[1]) < 0.004 else np.pi
            d = (w["yaw_world"] - t.yaw + period / 2) % period - period / 2
            assert abs(np.degrees(d)) < 3.0, t.label
        assert w["in_twin"] and w["reachable"] and w["graspable"]
    assert twin.label == TWIN_LABEL and twin.to_dict()["label"] == TWIN_LABEL


def test_flat_things_are_not_objects(session):
    """A 2 mm coloured card on the sheet is flat by its silhouette, so it is not modelled."""
    objs = [ObjectSpec("card", "flat box", "purple", 0.22, -0.04, 0.2, size=(0.05, 0.035, 0.002)),
            ObjectSpec("cube", "cube", "red", 0.20, 0.07, 0.3)]
    rgb = render_photo(objs, SheetPose(), CAM)
    twin = make_twin(rgb, session.sam, hfov_deg=CAM.hfov_deg)
    assert [o.colour for o in twin.objects] == ["red"]
    assert twin.stats["dropped_flat"] >= 1


def test_twin_scene_render_and_task_on_the_twin(session):
    """The twin loads into the simulator, renders from the photo's viewpoint, and the arm clears it
    (simulated-click oracle, verify + retry, goal checked on the twin's physics)."""
    rgb = render_photo(OBJS, SheetPose(), CAM)
    twin = make_twin(rgb, session.sam, hfov_deg=CAM.hfov_deg, heights_mm={0: 40.0})
    assert twin.objects[0].height_source == "typed" and twin.objects[0].size_mm[2] == 40.0
    img = render_twin(twin.spec, twin.width, twin.height)
    assert img.shape == rgb.shape
    png = side_by_side(rgb, img)
    sbs = Image.open(io.BytesIO(png))
    assert sbs.width == 2 * rgb.shape[1] + 8 and sbs.height > rgb.shape[0]
    tasks = twin_tasks(twin)
    assert {"clear-desk", "sort-by-colour"} <= set(tasks)
    task = tasks["clear-desk"]
    session.load_scene(twin.spec)
    try:
        w0 = world_from(session)
        rec = session.run(oracle_steps(task, session))
        ok, detail = task.goal(w0, world_from(session))
        assert ok, (detail, [s["result"] for s in rec["steps"]])
    finally:
        session.reset(1)


def test_objects_outside_the_twin_say_why():
    """An object where the arm's base stands, one under the virtual tray, one out of reach."""
    from hasami_core.twin import twin_spec
    from hasami_perception.twin import TwinObject

    rgb = render_photo([], SheetPose(), CAM)
    frame = sheet_frame(find_sheet(rgb), rgb.shape[1], rgb.shape[0], CAM.hfov_deg)
    pl = ArmPlacement()

    def obj(i, wx, wy):
        base, f, left = pl.frame(frame)
        xy = base + f * wx * 1000 + left * wy * 1000
        return TwinObject(i, "box", float(xy[0]), float(xy[1]), 0.0, [25, 25, 25], "typed", 1.0, [200, 30, 30], "red",
                          1.0, [0, 0], 100)

    spec, world = twin_spec(frame, pl, [obj(0, 0.03, 0.0), obj(1, 0.10, 0.21), obj(2, 0.30, 0.0), obj(3, 0.22, 0.0)])
    assert [w["in_twin"] for w in world] == [False, False, True, True]
    assert "arm's base" in world[0]["twin_notes"][0] and "tray" in world[1]["twin_notes"][0]
    assert not world[2]["reachable"] and "reach" in world[2]["twin_notes"][0]
    assert world[3]["reachable"] and not world[3]["twin_notes"]
    assert [o.name for o in spec.objects] == ["twin2_red_box", "twin3_red_box"]
