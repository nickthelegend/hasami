import os

import pytest


@pytest.mark.live
def test_muse_fetch_intent_live():
    if os.environ.get("HASAMI_LIVE_META") != "1" or not os.environ.get("MODEL_API_KEY"):
        pytest.skip("set HASAMI_LIVE_META=1 and MODEL_API_KEY to call Muse")
    from hasami_planner.muse import fetch_intent
    intent, ms, model = fetch_intent("bring me the juice from the fridge")
    assert "juice" in intent.item_text.lower()
    assert intent.destination and ms > 0 and model
