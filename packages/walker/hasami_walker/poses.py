"""Named arm poses taught on the real robot: move the arm with the phone page, then `python -m hasami_walker teach
<name>` stores the arm's current angles. The fetch uses them for the parts that are physical facts about this robot and
this fridge (where the camera can see the shelf, how to carry a bottle), so nothing is guessed.

Stored in HASAMI_WALKER_POSES (default ~/.hasami/walker_poses.json), one entry per name: six joint angles in the
robot's calibrated degrees (thenar-walker CLAUDE.md: pan 0 = forward, + = clockwise from above, ...).
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def store() -> Path:
    return Path(os.environ.get("HASAMI_WALKER_POSES", Path.home() / ".hasami" / "walker_poses.json"))


def load() -> dict[str, list[float]]:
    p = store()
    if not p.exists():
        return {}
    d = json.loads(p.read_text())
    return {k: [float(x) for x in v] for k, v in d.items() if isinstance(v, list) and len(v) == 6}


def save(name: str, q: list[float]) -> Path:
    if len(q) != 6:
        raise ValueError("a pose has six joint angles")
    poses = load()
    poses[name] = [round(float(x), 1) for x in q]
    p = store()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(poses, indent=1, sort_keys=True))
    os.replace(tmp, p)
    return p


def get(name: str) -> list[float]:
    poses = load()
    if name not in poses:
        have = ", ".join(sorted(poses)) or "none yet"
        raise KeyError(f"no pose {name!r} (taught: {have}); move the arm with the phone, then: python -m hasami_walker "
                       f"teach {name}")
    return poses[name]
