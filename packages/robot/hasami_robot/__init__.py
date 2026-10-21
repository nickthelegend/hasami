"""Real SO-101 follower arm through LeRobot (imported lazily). BLOCKED without HASAMI_SO101_PORT.

`RealMirror` streams the simulated arm's joint targets to a connected follower (mirror mode). It has not
been run on hardware."""

from hasami_perception import SetupRequired

from .so101 import (LEROBOT_KEYS, SETUP_MESSAGE, SIM_JOINTS, SIM_RANGES_RAD, SO101, RealMirror, lerobot_to_sim,
                    limit_step, sim_to_lerobot, so101_status)

__all__ = ["LEROBOT_KEYS", "SETUP_MESSAGE", "SIM_JOINTS", "SIM_RANGES_RAD", "SO101", "RealMirror", "SetupRequired",
           "lerobot_to_sim", "limit_step", "sim_to_lerobot", "so101_status"]
