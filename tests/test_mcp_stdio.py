"""End-to-end over real MCP stdio: profiles list the right tools and calls return JSON."""
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def write_cfg(cfg, tmp_path):
    p = tmp_path / "opmagent.toml"
    p.write_text(f'[paths]\ndatabase = "{cfg.database}"\nfile_store = "{cfg.file_store}"\n')
    return p


async def _session(cfg_path, profile, fn):
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "opmagent.servers.data_server", "--profile", profile, "--config", str(cfg_path)])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


def _payload(result):
    if getattr(result, "structured_content", None):
        sc = result.structured_content
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


def test_stdio_profiles(cfg, tmp_path):
    cfg_path = write_cfg(cfg, tmp_path)

    async def tools(s):
        return {t.name for t in (await s.list_tools()).tools}

    fluid = asyncio.run(_session(cfg_path, "fluid", tools))
    reviewer = asyncio.run(_session(cfg_path, "reviewer", tools))
    assert "write_tables" in fluid and "approve" not in fluid
    assert "approve" in reviewer and "write_tables" not in reviewer

    async def create(s):
        return _payload(await s.call_tool("create_version", {
            "kind": "fluid", "model_name": "MCP_TEST", "header": {"unit_system": "METRIC", "phases": "OIL,WATER,GAS"}}))

    out = asyncio.run(_session(cfg_path, "fluid", create))
    assert out["ok"] and out["version"] == 1 and "draft" in out["spoken_summary"]


def test_pvt_stdio_lists_tools(cfg, tmp_path):
    cfg_path = write_cfg(cfg, tmp_path)
    params = StdioServerParameters(command=sys.executable, args=["-m", "opmagent.servers.pvt_server", "--config", str(cfg_path)])

    async def run():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                sat = _payload(await s.call_tool("characterize", {"use_default": True}))
                return names, sat

    names, sat = asyncio.run(run())
    assert {"characterize", "tune", "generate_black_oil"} <= names
    assert sat["ok"] and sat["psat_bar"] > 50
