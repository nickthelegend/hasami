"""The MCP server, spawned over stdio with the official MCP client: status, look, find."""

import base64
import io
import json
import os
import sys

import anyio
import numpy as np
from PIL import Image

from mcp import ClientSession, StdioServerParameters, stdio_client

from hasami_core import simulated_click


def test_mcp_status_look_find_over_stdio(tmp_path):
    log = tmp_path / "mcp.jsonl"
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "hasami_mcp"], cwd=str(tmp_path),
        env={"HF_HOME": os.environ["HF_HOME"], "HASAMI_SEED": "5", "HASAMI_MCP_LOG": str(log),
             "HASAMI_RUNS_DIR": str(tmp_path / "runs"), "MODEL_API_KEY": "", "HASAMI_SO101_PORT": ""},
    )

    async def go():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                await s.initialize()
                tools = {t.name for t in (await s.list_tools()).tools}
                assert tools == {"status", "look", "find", "pick_place"}

                st = await s.call_tool("status", {})
                assert not st.is_error
                status = st.structured_content or json.loads(st.content[0].text)
                assert status["arm"] == "sim" and status["planner"]["ready"] is False

                look = await s.call_tool("look", {})
                assert not look.is_error
                img = next(c for c in look.content if c.type == "image")
                rgb = np.array(Image.open(io.BytesIO(base64.b64decode(img.data))).convert("RGB"))
                assert rgb.shape == (480, 640, 3)
                info = json.loads(next(c for c in look.content if c.type == "text").text)
                assert "red cube" in info["objects"]

                u, v = simulated_click(rgb, "blue cube")
                found = await s.call_tool("find", {"point": [u, v], "label": "blue cube"})
                assert not found.is_error, found.content
                f = found.structured_content or json.loads(found.content[0].text)
                assert f["truth_label"] == "blue cube" and f["iou_vs_truth"] >= 0.6
                assert f["target"]["top_z"] > 0.02

                # text-only find needs SAM 3.1 on the Meta Model API: a tool error naming the key, not a guess
                nokey = await s.call_tool("find", {"label": "red cube"})
                assert nokey.is_error
                assert "MODEL_API_KEY" in nokey.content[0].text

    anyio.run(go)
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert [x["tool"] for x in lines] == ["status", "look", "find", "find"]
    assert [x["ok"] for x in lines] == [True, True, True, False]
