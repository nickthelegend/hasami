"""The fridge fetch, simulated: "bring me the juice from the fridge".

An open fridge stands in front of the SO-101 (hasami_sim.scene.fridge_scene); a juice carton, a can and a
bottle stand on its reachable shelf, and the robot's basket is beside the arm. One fetch:

  1. understand: with MODEL_API_KEY, Muse (the existing planner) reads the sentence and the head-camera frame
     and names the item (and points at it). Without the key that step says "Setup required: MODEL_API_KEY"
     and the item the user named is used as given (matched to the fridge's item list by its words).
  2. look: the head camera looks into the fridge. hasami_perception.shelf finds the shelf from depth, the
     things standing on it, prompts SAM 2.1 at each (a boxed point prompt) and measures each mask: centre,
     width, height, colour. The item's entry in the list (colour, height, width) picks the one to fetch;
     nothing that fails it is fetched (the step stops, the arm does not move).
  3. the basket: the overhead camera finds the basket by its colour (blue), SAM 2.1 segments it, and the place point
     and the floor height come from that mask and depth, as for the tabletop tray.
  4. fetch: Arm.plan_front picks the flattest gripper tilt at which every pose of the fetch is reachable;
     Arm.fetch_front goes in from the front, closes, lifts, slides back out, carries and lowers into the basket.
  5. verify: the gripper reading (after closing, the withdrawal and the carry), the head camera (is the item
     still on the shelf?) and the overhead camera (is a raised thing of the item's colour in the basket, its
     top where the item's top should be?). A failed check re-surveys the shelf from a new frame and tries
     again, up to max_retries times, as Session does for the tabletop.

The simulator's ground truth scores the run (IoU, localisation error, whether the item is really in the
basket) and is never used to decide anything.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict

import mujoco
import numpy as np

from hasami_perception import Target, locate
from hasami_perception.colour import colour_mask, components, mean_colour
from hasami_perception.shelf import Shelf, ShelfItem, box_of, measure, survey
from hasami_planner import SETUP_MESSAGE
from hasami_sim.arm import ARM_JOINTS, StepLog, in_container
from hasami_sim.scene import BASKET_COLOUR, ITEMS, fridge_scene
from .session import (SIMULATION, Segmentation, Session, _hue_close, _now, _r, _round_t, _target_dict, new_run_id,
                      png_bytes, runs_dir)

HEAD, OVERHEAD, DEMO = "head", "overhead", "fetch_demo"
# Where the arm waits during a fetch: folded on the door side, so the head camera sees the whole shelf and the
# overhead camera the whole basket (measured: no arm pixel over the shelf's items or the basket).
FETCH_PARK_Q = np.array([1.6, -1.74, 1.6, 0.0, 0.0])
TASK = "fridge fetch"
EventFn = Callable[[str, dict], None]


def item_named(text: str) -> str | None:
    """The fridge item a sentence names, by its words ('bring me the juice' -> 'juice carton'); None if none.
    The longest matching word wins ('orange juice' over 'juice')."""
    t = " " + re.sub(r"[^a-z]+", " ", text.lower()) + " "
    best: tuple[str, int] | None = None
    for name, spec in ITEMS.items():
        for w in (name, *spec[5]):
            if f" {w} " in t and (best is None or len(w) > best[1]):
                best = (name, len(w))
    return best[0] if best else None


def check_item(item: ShelfItem, name: str) -> list[str]:
    """Reasons a measured thing on the shelf is not the named item (empty = it is). The list says what each
    item looks like: its colour, its height (+-8 mm) and its width across the view (+-5 mm; a carton turned
    on the shelf shows up to its diagonal)."""
    form, size, colour, _, _, _ = ITEMS[name]
    reasons = []
    if item.colour != colour:
        reasons.append(f"looks {item.colour or 'uncoloured'}, the {name} is {colour}")
    if abs(item.height - size[2]) > 0.008:
        reasons.append(f"{item.height * 1000:.0f} mm tall, the {name} is {size[2] * 1000:.0f} mm")
    lo = min(size[0], size[1])
    hi = size[0] if form == "cylinder" else float(np.hypot(size[0], size[1]))
    if not lo - 0.005 <= item.width <= hi + 0.005:
        reasons.append(f"{item.width * 1000:.0f} mm wide, the {name} is {lo * 1000:.0f}"
                       + ("" if hi == lo else f"-{hi * 1000:.0f}") + " mm")
    return reasons


class FridgeFetch:
    """A Session (SAM 2.1, the simulated SO-101, run records) on the fridge scene, running fetches."""

    def __init__(self, session: Session | None = None, seed: int = 0, max_retries: int | None = None):
        self.session = session or Session(connect_real=False)
        if max_retries is not None:
            self.session.max_retries = max_retries
        self.load(seed)

    @property
    def arm(self):
        return self.session.arm

    def load(self, seed: int) -> dict:
        """The fridge scene for `seed`, the arm parked; every item must be visible to the head camera."""
        return self.session.load_scene(fridge_scene(seed), camera=HEAD, park_q=FETCH_PARK_Q)

    def park(self) -> None:
        """Fold where it is, then turn to the parked pose (servo moves, not a teleport)."""
        arm = self.arm
        here = arm.data.ctrl[[arm._aid(j) for j in ARM_JOINTS]].copy()
        tuck = FETCH_PARK_Q.copy()
        tuck[0] = here[0]
        arm.move_to(tuck, 600)
        arm.move_to(FETCH_PARK_Q, 600)

    # ---- 1. understand ---------------------------------------------------------------------------
    def understand(self, sentence: str) -> dict:
        """Which item the sentence asks for. Muse when MODEL_API_KEY is set (its errors are raised, never
        replaced by a guess); otherwise the user's own words, and the Muse step says Setup required."""
        out: dict = {"sentence": sentence}
        if not os.environ.get("MODEL_API_KEY"):
            out.update(source="user words", muse=SETUP_MESSAGE, item=item_named(sentence))
        else:
            from hasami_planner import Planner

            frame = self.session.observe(HEAD)
            plan, ms = Planner().plan(sentence, png_bytes(frame.rgb), labels=[*ITEMS, "basket"])
            step = plan.steps[0]
            out.update(source="muse", model=plan.model, latency_ms=round(ms, 1), plan=plan.model_dump(),
                       item=item_named(step.pick), hint=step.pick_point, camera=HEAD)
            if out["item"] is None:
                raise ValueError(f"Muse asked for {step.pick!r}, which is none of the fridge's items ({', '.join(ITEMS)})")
        if out["item"] is None:
            raise ValueError(f"no fridge item named in {sentence!r}; the fridge holds: {', '.join(ITEMS)}")
        return out

    # ---- a fetch ---------------------------------------------------------------------------------
    def run(self, sentence: str, on_event: EventFn | None = None, run_id: str | None = None) -> dict:
        """One fetch, verified and retried. Returns the run record (Session.record writes it)."""
        emit = on_event or (lambda kind, data: None)
        s = self.session
        run_id = run_id or new_run_id()
        s._run_id = run_id
        t_run = time.perf_counter()
        rec: dict = {"run_id": run_id, "started_at": _now(), "segmenter": "local", "seed": s.seed,
                     "simulation": SIMULATION, "scene": s.scene.name, "task": TASK, "plan": None,
                     "initial_poses": None, "steps": [], "ok": False}
        if s.disturb is not None:
            rec["disturb"] = s.disturb.to_dict()
        with s.lock:
            rec["initial_poses"] = s.poses()
            try:
                und = self.understand(sentence)
                rec["planner"] = und
                name = und["item"]
                rec["plan"] = und["plan"] if und["source"] == "muse" else {
                    "steps": [{"pick": name, "pick_point": None, "place": "basket", "place_point": None,
                               "reason": "the item the user named (Muse: Setup required)"}],
                    "source": "user words", "model": None, "instruction": sentence}
                rec["segmenter_name"] = s.sam.name
                emit("plan", {"run_id": run_id, "source": und["source"], "instruction": sentence, "item": name,
                              "muse": und.get("muse") or und.get("model"), "simulation": SIMULATION,
                              "disturb": None if s.disturb is None else s.disturb.to_dict()})
                rec["steps"].append(self._run_step(name, und.get("hint"), emit))
                self.park()
            except Exception as e:
                rec.update(error=f"{type(e).__name__}: {e}", finished_at=_now(), final_poses=s.poses(),
                           timings={"total_ms": round((time.perf_counter() - t_run) * 1000, 1)})
                s.last_run = rec
                emit("error", {"run_id": run_id, "message": str(e), "type": type(e).__name__})
                raise
        placed = sum(1 for st in rec["steps"] if st["result"]["ok"])
        rec.update(ok=placed == 1, placed=placed, total=1, finished_at=_now(), final_poses=s.poses(),
                   timings={"total_ms": round((time.perf_counter() - t_run) * 1000, 1)})
        ious = [a["pick"]["iou_vs_truth"] for st in rec["steps"] for a in st["attempts"]
                if a.get("pick") and a["pick"].get("iou_vs_truth") is not None]
        rec["iou"] = {"values": ious, "mean": round(float(np.mean(ious)), 4) if ious else None,
                      "min": round(float(np.min(ious)), 4) if ious else None}
        checks = [a["verify"] for st in rec["steps"] for a in st["attempts"] if a.get("verify")]
        rec["verify"] = {"checks": len(checks), "agree": sum(1 for c in checks if c["agree"]),
                         "retries": sum(len(st["attempts"]) - 1 for st in rec["steps"])}
        s.last_run = rec
        emit("done", {"run_id": run_id, "ok": rec["ok"], "placed": placed, "total": 1,
                      "total_ms": rec["timings"]["total_ms"], "retries": rec["verify"]["retries"]})
        return rec

    def _run_step(self, name: str, hint, emit: EventFn) -> dict:
        s = self.session
        out: dict = {"step": 0, "pick_label": name, "place_label": "basket", "attempts": []}
        att: dict = {}
        for attempt in range(1, s.max_retries + 2):
            att = self._attempt(name, attempt, hint if attempt == 1 else None, emit)
            out["attempts"].append(att["record"])
            if att.get("skip") or att["verified"] or attempt > s.max_retries:
                break
            emit("retry", {"step": 0, "attempt": attempt + 1, "reason": att["reason"], "pick_point": None,
                           "how": "re-surveyed the shelf in a new head-camera frame"})
        last = out["attempts"][-1]
        for key in ("pick", "place", "place_xy", "place_how", "surface_z", "phases", "timings"):
            if key in last:
                out[key] = last[key]
        out.setdefault("phases", [])
        out["result"] = self._result(att, len(out["attempts"]))
        emit("result", out["result"])
        return out

    def _fail(self, res: dict, failure: str, reason: str) -> dict:
        res["record"]["failure"] = failure
        res["record"]["note"] = reason
        res["reason"] = reason
        if self.session.store_images:
            res["record"]["images"] = self._store(res)
        return res

    def _attempt(self, name: str, attempt: int, hint, emit: EventFn) -> dict:
        s = self.session
        t: dict[str, float] = {}
        t0 = time.perf_counter()
        frame = s.observe(HEAD)
        rec: dict = {"attempt": attempt, "camera": HEAD, "pick_point": None, "place_point": None}
        res: dict = {"record": rec, "frame": frame, "verified": False, "reason": None, "pp": None, "obj": None,
                     "physics_ok": False}
        t["observe_ms"] = (time.perf_counter() - t0) * 1000

        # 2. look into the fridge
        t1 = time.perf_counter()
        try:
            shelf, found = survey(frame, lambda point, box: s.sam.segment(frame.rgb, point=point, box=box))
        except ValueError as e:
            rec["timings"] = _round_t(t)
            res["skip"] = True
            return self._fail(res, "segment", f"no shelf in view: {e}")
        t["segment_pick_ms"] = (time.perf_counter() - t1) * 1000
        rec["shelf"] = shelf.to_dict()
        tries = []
        for e in found:
            entry = {"point": e["point"], "box": e["box"], "pixels": int(e["mask"].sum())}
            if "item" in e:
                entry.update(e["item"].to_dict(), rejected=check_item(e["item"], name))
            else:
                entry["rejected"] = [e["error"]]
            tries.append(entry)
        rec["target_check"] = {"ok": False, "source": "shelf survey", "item": name, "tries": tries}
        passing = [i for i, tr in enumerate(tries) if not tr["rejected"]]
        if hint is not None:  # Muse pointed at one: take the thing under (or nearest) its pixel
            def dist(i: int) -> float:
                vs, us = np.nonzero(found[i]["mask"])
                return float(np.min(np.hypot(us - hint[0], vs - hint[1]))) if len(us) else np.inf
            order = sorted(range(len(found)), key=dist)
            passing = [i for i in order if not tries[i]["rejected"]][:1]
            rec["target_check"]["hint"] = list(hint)
        if not passing:
            rec["timings"] = _round_t(t)
            res["skip"] = True
            why = "; ".join(f"thing {k + 1}: {', '.join(tr['rejected'])}" for k, tr in enumerate(tries)) or "nothing on it"
            return self._fail(res, "segment", f"no {name} on the shelf ({why})")
        k = passing[0]
        item: ShelfItem = found[k]["item"]
        rec["target_check"].update(ok=True, chosen=k)
        res.update(item=item, shelf=shelf)
        target = Target(center=item.center, top_z=item.top_z, yaw=0.0, pixels=item.pixels, centroid_px=item.centroid_px,
                        extent=[item.width, item.width])
        pick = Segmentation(mask=found[k]["mask"], segmenter=s.sam.name, latency_ms=t["segment_pick_ms"],
                            point=found[k]["point"], text=name, target=target,
                            grounding={"source": "shelf survey (SAM 2.1, boxed point prompt per thing on the shelf)",
                                       "tries": tries})
        s._score(pick, frame)
        res["pick"] = pick
        rec["pick_point"] = found[k]["point"]
        rec["pick"] = pick.to_dict(with_mask=False)
        rec["pick"]["measured"] = item.to_dict()
        emit("segment", {"step": 0, "attempt": attempt, "role": "pick", "camera": HEAD, **pick.to_dict()})
        emit("target", {"step": 0, "attempt": attempt, "role": "pick", "target": _target_dict(target),
                        "width_mm": round(item.width * 1000, 1), "height_mm": round(item.height * 1000, 1)})

        # 3. the basket, from the overhead camera
        oframe = s.observe(OVERHEAD)
        res["oframe"] = oframe
        blobs = components(colour_mask(oframe.rgb, BASKET_COLOUR), 200)  # the largest blue blob
        if not blobs:
            rec["timings"] = _round_t(t)
            res["skip"] = True
            return self._fail(res, "place", f"cannot see the {BASKET_COLOUR} basket in the overhead frame")
        vs, us = blobs[0][:, 0], blobs[0][:, 1]
        k = int(np.argmin((us - np.median(us)) ** 2 + (vs - np.median(vs)) ** 2))
        click = (float(us[k]), float(vs[k]))
        place = s.segment(point=click, segmenter="local", frame=oframe)
        t["segment_place_ms"] = place.latency_ms
        res["place"] = place
        rec["place_point"] = _r(click, 1)
        rec["place"] = place.to_dict(with_mask=False)
        rec["place"]["camera"] = OVERHEAD
        emit("segment", {"step": 0, "attempt": attempt, "role": "place", "camera": OVERHEAD, **place.to_dict()})
        try:
            place_xy, how, surface = s._place_target(oframe, place, item.width / 2)
        except ValueError as e:
            rec["timings"] = _round_t(t)
            return self._fail(res, "place", str(e))
        if how != "inside container":
            rec["timings"] = _round_t(t)
            res["skip"] = True
            return self._fail(res, "place", f"the basket's mask does not look like a container ({how})")
        rec.update(place_xy=_r(place_xy), place_how=how, surface_z=_r(surface))
        emit("target", {"step": 0, "attempt": attempt, "role": "place", "target": _target_dict(place.target),
                        "place_xy": _r(place_xy), "how": how, "surface_z": _r(surface)})

        # 4. plan the front approach
        arm = self.arm
        axis = arm.pan_axis
        # the cabinet's front corners, where the side walls meet the shelf's front edge (from the shelf's extent)
        corner = min(float(np.hypot(shelf.x[0] - axis[0], y - axis[1])) for y in shelf.y)
        t2 = time.perf_counter()
        plan = arm.plan_front(np.array(item.center), item.width, item.height, shelf.z, place_xy, surface,
                              clear_radius=corner - item.width / 2 - 0.025)
        t["plan_ms"] = (time.perf_counter() - t2) * 1000
        if plan is None:
            rec["timings"] = _round_t(t)
            res["skip"] = True
            r = float(np.hypot(*(np.array(item.center[:2]) - axis))) * 1000
            return self._fail(res, "implausible", f"skipped: the {name} ({r:.0f} mm from the pan axis) is out of reach "
                                                  "for a front grasp at any tilt from 15 to 40 degrees")
        rec["grasp"] = {"approach": "front", "pitch_deg": plan["pitch_deg"], "roll": round(plan["roll"], 3),
                        "width_mm": round(item.width * 1000, 1), "grip_height_mm": round(plan["grasp_h"] * 1000, 1),
                        "poses": {k: _r(v) for k, v in plan["poses"].items()}}

        # scoring only: the item the arm will actually meet, and where everything was
        obj = min(s.movables, key=lambda n: float(np.linalg.norm(arm.object_pos(n)[:2] - np.array(item.center[:2]))))
        before = {n: arm.object_pos(n) for n in s.movables}
        res.update(obj=obj, before=before, place_xy=place_xy, surface=surface)
        rec["localisation_error_mm"] = round(float(np.linalg.norm(before[obj][:2] - np.array(item.center[:2])) * 1000), 2)

        d = s.disturb
        injected: dict = {}
        short = 0.0
        if d is not None and d.short_mm and attempt == 1:
            short = d.short_mm / 1000
            injected["short_mm"] = d.short_mm
        slip = bool(d is not None and d.slip and s._rng.random() < d.slip)
        if slip:
            injected["slip"] = True
        rec["injected"] = injected

        def on_log(lg: StepLog) -> None:
            emit("phase", {"step": 0, "attempt": attempt, **asdict(lg)})

        t3 = time.perf_counter()
        pp = arm.fetch_front(plan, item.width, item.height, on_log=on_log, short=short, slip=slip)
        t["pick_place_ms"] = (time.perf_counter() - t3) * 1000
        res["pp"] = pp
        rec["phases"] = [asdict(lg) for lg in pp]
        rec["grasp_checks"] = pp.checks

        # 5. verify: the gripper reading, then both cameras
        t4 = time.perf_counter()
        self.park()
        after, oafter = s.observe(HEAD), s.observe(OVERHEAD)
        res.update(after=after, oafter=oafter)
        v = self._verify_images(after, oafter, item, pick, shelf, place_xy, surface)
        t["verify_ms"] = (time.perf_counter() - t4) * 1000
        v.update(grasp_held=pp.held, jaw_gap_mm=pp.jaw_gap_mm, completed=pp.completed)
        v["ok"] = bool(pp.completed and pp.held is not False and v["shelf_clear"] and v["in_basket_seen"])
        basket = s.scene.container("basket")
        physics_ok = in_container(arm, obj, basket)
        v["physics_ok"] = physics_ok  # ground truth, for scoring the check (never used to decide)
        v["agree"] = v["ok"] == physics_ok
        res["physics_ok"] = physics_ok
        rec["verify"] = v
        if s.store_images:
            rec["images"] = self._store(res)
        rec["timings"] = _round_t(t)
        emit("verify", {"step": 0, "attempt": attempt, **v, "injected": injected})
        if not physics_ok:
            rec["failure"] = "grasp" if pp.held is not True else "place"
        res["verified"] = v["ok"]
        if not v["ok"]:
            if pp.held is False:
                after_phase = next((c for c in pp.checks if not c["held"]), {}).get("after", "")
                res["reason"] = f"empty grasp (gripper reading after '{after_phase}')"
            elif not pp.completed:
                failed = next((lg for lg in pp if not lg.ok), None)
                res["reason"] = f"motion stopped at '{failed.phase}': {failed.note}" if failed else "motion incomplete"
            elif not v["shelf_clear"]:
                res["reason"] = f"the {name} is still on the shelf in the head-camera image"
            else:
                res["reason"] = f"no raised {item.colour or ''} thing of the {name}'s height in the basket in the overhead image"
            rec["note"] = res["reason"]
        return res

    def _verify_images(self, after, oafter, item: ShelfItem, pick: Segmentation, shelf: Shelf, place_xy, surface: float) -> dict:
        """shelf_clear: SAM 2.1 at the item's old pixel (boxed by its old mask) in the new head frame finds
        nothing standing where it stood. in_basket_seen: SAM 2.1 at the pixel where the item's top should be in
        the overhead frame finds a raised mask there, its top at the basket floor + the item's height, of the
        item's colour."""
        sam = self.session.sam
        out: dict = {}
        still = False
        m1 = sam.segment(after.rgb, point=tuple(item.centroid_px), box=box_of(pick.mask))
        try:
            it1 = measure(after, m1, shelf)
            still = (np.linalg.norm(np.array(it1.center[:2]) - np.array(item.center[:2])) < 0.010
                     and abs(it1.top_z - item.top_z) < 0.006)
            out["shelf_seen"] = {"center": _r(it1.center), "top_z": _r(it1.top_z), "colour": it1.colour}
        except ValueError as e:
            out["shelf_seen"] = {"error": str(e)}
        out["shelf_clear"] = not still
        exp_top = surface + item.height
        u, v = oafter.world_to_pixel(np.array([place_xy[0], place_xy[1], exp_top]))
        w, h = oafter.size
        seen = False
        if 0 <= u < w and 0 <= v < h:
            m2 = sam.segment(oafter.rgb, point=(u, v))
            try:
                t2 = locate(oafter, m2)
                near = bool(np.linalg.norm(np.array(t2.center[:2]) - np.asarray(place_xy)) < 0.025)
                level = bool(abs(t2.top_z - exp_top) < 0.010)
                same = bool(_hue_close(mean_colour(oafter.rgb, m2 & self.session._raised(oafter, m2)), np.array(item.rgb)))
                seen = near and level and same
                out["basket_seen"] = {"center": _r(t2.center), "top_z": _r(t2.top_z), "expected_top_z": _r(exp_top),
                                      "near": near, "level": level, "same_colour": same}
            except ValueError as e:
                out["basket_seen"] = {"error": str(e)}
        out["in_basket_seen"] = seen
        out["basket_px"] = _r([u, v], 1)
        # the tabletop's field names, for the dashboard: the pick spot is clear, the place spot has the item
        out["pick_clear"], out["place_raised"] = out["shelf_clear"], seen
        return out

    def _result(self, att: dict, attempts: int) -> dict:
        s = self.session
        rec = att["record"]
        obj = att.get("obj")
        if obj is None:
            return {"step": 0, "object": None, "object_name": None, "in_tray": False, "error_mm": None, "ok": False,
                    "note": att["reason"], "attempts": attempts, "verified": False,
                    "failure": rec.get("failure", "segment")}
        arm = self.arm
        final = arm.object_pos(obj)
        basket = s.scene.container("basket")
        inside = in_container(arm, obj, basket)
        bid = arm.model.body(obj).id
        upright = bool(arm.data.xmat[bid].reshape(3, 3)[2, 2] > 0.9)
        disturbed = [s.objects[n] for n in s.movables
                     if n != obj and np.linalg.norm(arm.object_pos(n)[:2] - att["before"][n][:2]) > 0.005]
        pp = att["pp"]
        ok = bool(pp is not None and pp.completed and att["physics_ok"])
        result = {"step": 0, "object": s.objects[obj], "object_name": obj, "in_tray": inside, "in_basket": inside,
                  "upright": upright, "error_mm": round(float(np.linalg.norm(final[:2] - att["place_xy"]) * 1000), 1),
                  "ok": ok, "localisation_error_mm": rec.get("localisation_error_mm"), "disturbed": disturbed,
                  "final_pos": _r(final), "attempts": attempts, "verified": bool(att["verified"]),
                  "failure": None if ok else rec.get("failure", "place")}
        if not ok:
            result["note"] = att["reason"] or "the item did not end up in the basket"
        return result

    def _store(self, res: dict) -> dict:
        """The attempt's frames and masks next to the run record (runs_dir()/<run_id>/): the head frame, its
        mask of the item and the truth mask (the dashboard's frame / pick / pick_truth), the head frame after,
        the overhead frames and basket mask (basket_frame / basket / basket_after), and the fetch_demo view after
        (scene_after; for people, never perceived)."""
        s = self.session
        d = runs_dir() / str(s._run_id)
        d.mkdir(parents=True, exist_ok=True)
        stem = f"s0a{res['record']['attempt']}"
        out: dict = {}

        def put(kind: str, img: np.ndarray) -> None:
            name = f"{stem}-{kind}.png"
            (d / name).write_bytes(png_bytes(img))
            out[kind] = name

        put("frame", res["frame"].rgb)
        if res.get("after") is not None:
            put("after", res["after"].rgb)
        pick = res.get("pick")
        if pick is not None:
            put("pick", pick.mask.astype(np.uint8) * 255)
            if res.get("obj"):
                put("pick_truth", s._truth_mask(res["frame"], res["obj"]).astype(np.uint8) * 255)
        if res.get("oframe") is not None:
            put("basket_frame", res["oframe"].rgb)
        if res.get("place") is not None:
            put("basket", res["place"].mask.astype(np.uint8) * 255)
        if res.get("oafter") is not None:
            put("basket_after", res["oafter"].rgb)
            if DEMO in {c["name"] for c in s.scene.cameras}:  # the three-quarter view, for people (never perceived)
                r = mujoco.Renderer(self.arm.model, height=480, width=640)
                try:
                    r.update_scene(self.arm.data, camera=DEMO)
                    put("scene_after", r.render())
                finally:
                    r.close()
        return out


__all__ = ["DEMO", "FETCH_PARK_Q", "FridgeFetch", "HEAD", "OVERHEAD", "check_item", "item_named"]
