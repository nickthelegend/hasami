# Hasami: method, sim-to-real, limits

## Perception

1. **Grounding.** Muse receives the overhead frame (640×480 PNG, in the user message) and the
   instruction, and must answer with a forced `submit_plan` tool call: a list of steps, each naming the
   object and a pixel inside it, and the destination and a pixel there. The plan is validated strictly
   (pixels inside the frame, at most six steps, no extra fields). Without a key a person supplies the
   same pixels by clicking.
2. **Segmentation.** SAM 2.1 hiera-tiny turns each pixel into a mask (of its three hypotheses, the
   best-scored one that contains the pixel). SAM 3.1 on the Meta Model API can take the object name
   instead.
3. **Lifting to 3D.** Every mask pixel is back-projected with its metric depth through the camera
   intrinsics. Pixels at table height (shadows, leaks) are dropped; the top surface is the highest band;
   the centre sits under it at half the measured height.
4. **Orientation.** The yaw of the minimum-area rectangle of the top face, folded into ±45°. PCA does not
   work for cubes: a square's covariance is isotropic, so it has no principal axis.

## Control

- **IK:** damped least squares on the gripper site's position and its approach axis (pointing down);
  the wrist roll is solved separately so the jaws straddle two faces of the box.
- **Sag correction:** after each move the measured site position error is added to the aim point and
  the IK solved again (up to three times). The STS3215 servos are force-limited, as on the real arm.
- **Grasp:** the fixed finger's inner face is the gripper site's z = 0 plane; the box centre is placed
  half its width plus 5 mm along site z, and the moving jaw closes the gap. The jaws open only ~40 mm so
  closing does not sweep neighbouring objects.
- **Success** is judged from the physics state (the object's centre inside the tray walls and resting
  low), never from the camera.

## Sim to real

- `hasami_robot` maps the five arm joints and the gripper to LeRobot's SO-101 follower keys
  (`shoulder_pan.pos` … in degrees, gripper 0–100). Calibration offsets come from LeRobot's calibration
  file for the specific arm.
- With `HASAMI_SO101_PORT` set, `Session` connects the follower and runs it in **mirror mode**: every
  joint target the simulated arm is commanded (~30 Hz of sim time) is sent to the real arm, at most 6°
  per joint per command. The sim remains the source of truth for perception and success. This has never
  been run on hardware.
- The real camera needs intrinsics (from a checkerboard) and the camera-to-base transform (a few
  touches with the gripper on known points). Depth needs an RGB-D camera, or a known table height with a
  plain RGB camera (the top of a box is then the table height plus its size).
- Expect larger localisation error from lighting, blur and calibration; the closed-loop IK does not see
  the camera, so grasp tolerance (the 5 mm clearance) is the main margin.

## Limits

Simulation only so far (perfect depth, no sensor noise, ideal lighting); boxes on a table; top-down grasps only; the Muse and SAM 3.1 paths need a Meta
Model API key and their response handling is unverified until one is used. Not safety-audited.
