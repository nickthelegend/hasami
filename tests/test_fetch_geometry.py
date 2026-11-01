import numpy as np

from hasami_walker.fetch import _verification
from hasami_walker.look import _found


def test_verification_requires_jaw_overlap_and_shelf_change():
    before_mask = np.zeros((120, 120), bool)
    before_mask[60:95, 45:75] = True
    before = _found(before_mask, "synthetic", 1)
    raised_mask = np.zeros_like(before_mask)
    raised_mask[15:50, 45:75] = True
    raised = _found(raised_mask, "synthetic", 1)
    assert _verification(before, raised, [40, 10, 80, 55])["held"]
    assert not _verification(before, raised, [0, 0, 20, 20])["held"]
    assert not _verification(before, before, [40, 55, 80, 100])["held"]
