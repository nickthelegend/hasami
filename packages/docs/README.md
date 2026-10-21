# Hasami: method, checks, sim-to-real, limits

Everything here runs in a MuJoCo simulation of the official SO-101 model. The simulation is kind: perfect
metric depth, no sensor noise, ideal lighting, rigid objects. Numbers are scored against the simulator's
ground truth, which the pipeline itself never sees.

## Perception

1. **Grounding.** Muse receives the overhead frame (640×480 PNG) and the instruction, and must answer with
   a forced `submit_plan` tool call: steps that name the object and give a pixel on it, and the destination
   and a pixel there. The plan is validated strictly (pixels inside the frame, at most six steps, no extra
   fields). A planner's pixel is a hint, not a fact (`hasami_perception.grounding`): SAM is prompted at the
   hint and the mask is accepted only if it has a sensible size, is near the hint, is raised off the table
   and has the colour the label names; otherwise SAM is prompted at the nearest blob of that colour, then at
   the nearest raised blob. Every try is recorded with its rejection reasons; if none passes, the step fails
   loudly. Without a key, a person (or the benchmark's click oracle) supplies the pixels; a click is taken
   as the intent and not second-guessed.
2. **Segmentation.** SAM 2.1 hiera-tiny turns a pixel into a mask (of its three hypotheses, the best-scored
   one that contains the pixel); its image encoder runs once per frame. SAM 3.1 on the Meta Model API can
   take the object's name instead; its boxes are validated the same way.
3. **Lifting to 3D.** Every mask pixel is back-projected with its metric depth. Pixels at table height
   (shadows, leaks) are dropped; the top surface is the highest band; the centre sits under it at half the
   measured height.
4. **Shape and orientation.** The top face's footprint gives the yaw (fourth-order moment for square
   footprints, minimum-area rectangle otherwise), its size along and across that yaw, and whether it is
   round (equal outline radius in every direction). That decides the grasp: any face of a square, across
   the narrow side of an elongated box, any direction for a cylinder.

## Control

- **IK:** damped least squares on the gripper site's position and its approach axis (pointing down); the
  wrist roll is chosen separately so the jaws straddle the object.
- **Sag correction:** after each move the measured site position error is added to the aim point and the
  IK solved again (up to three times). The STS3215 servos are force-limited, as on the real arm.
- **Grasp:** the fixed finger's inner face is the gripper site's z = 0 plane; the object's centre is placed
  half its width plus 5 mm along site z and the moving jaw closes the gap. The jaws open only ~40 mm so
  closing does not sweep neighbouring objects.
- **Place height from the measured surface.** The place point's surface height comes from depth (table,
  tray floor, or the top of the object below). The object is lowered until its bottom is 4 mm above it,
  and released by opening just 8 mm past its width; lowering and retreat follow straight-line waypoints.
  This is what makes stacking work.
- **A failed move never carries the object away:** it is put down where it is.

## Checking every step, and retrying

After each attempt the step is checked the way a real robot could:

- **Gripper position.** With nothing between them the jaws close to their stop (a 2.5 mm reading from the
  model's jaw geometry); a held object stops them early (14–25 mm for the 20–30 mm objects here). This is
  read after closing, after the lift and after the carry. The real STS3215 reports its position too.
- **Camera.** With the arm parked, SAM 2.1 is prompted at the old pick pixel (no raised mask may remain
  there) and at the place spot (a raised mask of the object's colour must be there, its top where the
  object's top should be).

The physics outcome is recorded next to the check, so the check itself is scored (`agree`). If the check
fails, the object is re-found in the new image (the blob of its colour nearest the previous pick pixel,
never on the parked arm) and the step retried, up to twice. `HASAMI_DISTURB=offset_mm:6,slip:0.2` injects
labelled disturbances to exercise this: the first attempt's grasp point shifted 6 mm so the fixed finger
lands on the object, and a probability that the grip lets go just after the lift.

## Scenes and benchmark

`hasami_sim.scene` builds scenes with `mujoco.MjSpec`: cubes of 20/25/30 mm, blocks, flat boxes and
40 mm cylinders in six colours, 4–8 objects in the measured reach annulus, one or two containers.
`hasami_core.tasks` defines eight families with seeded samplers and goal predicates over the physics state:
all-to-tray, sort-by-colour, stack-two, clear-except-X, tower-of-3, sort-by-shape (cylinders to the tray,
cubes to the bin), line-up (three cubes in a row on the bare table by size: `in_row` checks the line, the
order along -y, the spacing and that each stands on the table) and pick-from-bin (objects start on the
bin's floor; the named one goes to the tray, the others must stay within 15 mm). `scripts/bench.py` runs
them. The benchmark's planner is a **simulated-click oracle**: it knows which object each step moves and
clicks the middle of its colour blob in the RGB image. It measures perception, grasping, placing and
recovery, not planning. `--planner scripted` writes the same plans in Muse's format (label + hint) with
each pick hint moved `--hint-px` off, so the grounding layer is exercised without a key; a place named
"the table" is used as given rather than searched. Neither is a language model.

## Your desk, twinned (digital twin from one photo · simulated arm)

`POST /api/twin` (dashboard: **Twin** tab) turns one photo of a desk with an A4 sheet on it into a MuJoCo
scene the simulated SO-101 can work in. Code: `hasami_perception.twin` (geometry), `hasami_core.twin`
(scene, render, tasks).

1. **Sheet.** The largest bright, unsaturated quadrilateral (or four clicked corners); its corners are
   refined by fitting lines to the outline. The plane homography is fitted for both ways an A4 sheet can
   lie, keeping the one whose camera pose is a rotation given the field of view (default 70° horizontal,
   an assumption for a phone or webcam). That also gives the camera's pose.
2. **Masks.** A grid of points on the table plane (the sheet plus 200 mm around it, every 12 mm) is
   projected into the photo; points on the paper or on table-coloured pixels are skipped; the rest prompt
   SAM 2.1, one click each, with the image encoded once. A prompt inside an accepted mask is skipped.
3. **Shapes.** Each mask is fitted as an upright box or cylinder: single-view carving (plane points whose
   column from the table to a trial height projects inside the mask) gives a start, then centre, yaw, size
   and height are refined together to maximise the IoU between the mask and the extruded shape's
   projected silhouette (plus half the IoU of the top face when shading shows a brighter top). A single
   silhouette cannot tell a box from a flat cut-out of its outline; the upright-box prior can, because at
   the wrong height the carved footprint is not a rectangle or a disc. Flat fits (< 8 mm: paper, cards),
   shadows (the paper's or table's own colour, darker, touching an object) and masks no shape explains
   (IoU < 0.6) are dropped. A typed height overrides the estimate.
4. **Scene.** The arm's base stands 215 mm from the sheet's centre beyond the edge at the top of the photo
   (or another side), facing across it, so the sheet's centre is mid-reach. Objects keep their measured
   size and mean top-face colour; the tray and grey bin are added beside the sheet; objects out of view,
   under the base or a container are left out with the reason; out-of-reach, too-wide and too-close ones
   are noted. The photo's camera is added to the twin, so the side-by-side render is from the same viewpoint.
5. **Tasks.** clear-desk, sort-by-colour and stack-two (when the objects allow) are ordinary benchmark
   tasks: the simulated-click oracle plans, the arm runs with the same grounding, checks and retries, and
   the goal predicate is read from the twin's physics.

**Accuracy** (`scripts/twin_accuracy.py`, `deployments/twins/accuracy-20261021T164701Z.json`): 20 desk
photos rendered by MuJoCo (a tilted camera at a random pose; no arm, no depth), only the RGB image given
to the pipeline, 86 objects. With the camera's true field of view: 86/86 found, 0 false positives, shape
83/86, colour 86/86, centre error mean 1.2 mm (max 4.9), yaw mean 1.0° (median 0.35°, max 22°), sides
mean 0.8 / 0.6 mm, height mean 1.6 mm (median 0.9, max 13.7). With the 70° default instead of the true
field of view: 83/86 found, centre mean 3.3 mm (max 14.3), height mean 2.8 mm. clear-desk met its goal on
16 of the 20 twins. These are rendered photos (perfect lens, clean lighting, a plain desk); a real photo
has not been measured yet.

## Meta Model API: what is confirmed and what is not

Checked against dev.meta.ai on 2026-10-21 (no key, so no call has been made):

| | Confirmed in the docs | Unverified until a key exists |
|---|---|---|
| Muse | model id `muse-spark-1.3` (also `-contributor`, 1.2, 1.1); base URL `https://api.meta.ai/v1`; `POST /v1/chat/completions` exists; image input to Muse Spark | image parts as `image_url` data URLs on Chat Completions, and forcing `tool_choice` to a named function (the Chat Completions page defers both to other pages) |
| SAM 3.1 | model id `sam-3.1`; `POST /v1/responses` with an `input_text` prompt and an `input_image`; output is special-token text `<|box;x1;y1;x2;y2;w;h[;c]|><|mask;x;y;data=H,W,<enc>payload|>` with boxes in source pixels; `metadata.mask_encoding: "one_bit"` | the base85 mask payload (decoded only through Meta's `meta-sam-parser` package, not installed here) and whether a mask is relative to its box. Without it the box centre prompts SAM 2.1 |

## Sim to real

- `hasami_robot` maps the five arm joints and the gripper to LeRobot's SO-101 follower keys
  (`shoulder_pan.pos` … in degrees, gripper 0–100). Calibration offsets come from LeRobot's calibration
  file for the specific arm.
- With `HASAMI_SO101_PORT` set, `Session` connects the follower and runs it in **mirror mode**: every joint
  target the simulated arm is commanded (~30 Hz of sim time) is sent to the real arm, at most 6° per joint
  per command. The sim remains the source of truth for perception and success. This has never been run on
  hardware.
- **Real camera** (`hasami_perception.realcam`, dashboard tab): a webcam frame or a photo, the table plane
  from four clicked corners of an A4 sheet (or an ArUco marker), SAM 2.1 on a clicked object, and its x, y,
  yaw and footprint on the plane. The object's height is typed (a webcam has no depth); with an assumed
  field of view the top face is corrected for parallax. The geometry is tested on synthetic cameras; it has
  not been measured against a ruler yet.
- **Digital twin** (above): the same A4 geometry, but the whole photo becomes a scene and the arm works in
  the twin. Tested only on MuJoCo-rendered photos; white objects on the sheet and objects the colour of the
  table are not prompted, an object covering a sheet corner needs the corners clicked, and the field of
  view is assumed unless given.
- Expect larger localisation error from lighting, blur and calibration; the grasp tolerance (5 mm
  clearance) and the verify-and-retry loop are the margins. `HASAMI_DISTURB=depth_mm:2,cam_mm:3,light:0.3`
  measures some of that in the sim: noisy depth, a camera calibration error and lighting jitter.

## Limits

Simulation only so far; rigid objects on a table; top-down grasps only; the Muse and SAM 3.1 paths need a
Meta Model API key; the real arm, the real camera and the twin from a real photo are untested against the
physical world. Not safety-audited.
