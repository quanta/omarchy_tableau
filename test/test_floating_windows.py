#!/usr/bin/env python3
"""`capture_floating` keeps floating windows, and a load puts them back in place."""

import importlib.machinery
import importlib.util
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import patch


def load_cli(path: Path):
    loader = importlib.machinery.SourceFileLoader("tableau_float_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def client(address, wid, x, y, w, h, *, floating=False, cls="app.example", **extra):
    return {"address": address, "workspace": {"id": wid}, "at": [x, y], "size": [w, h],
            "floating": floating, "mapped": True, "class": cls, "pid": 0, **extra}


with tempfile.TemporaryDirectory() as tmp:
    config = Path(tmp) / "tableau.toml"
    os.environ["OMARCHY_TABLEAU_CONFIG"] = str(config)
    os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
    cli = load_cli(Path(__file__).parents[1] / "bin/omarchy-tableau")

    # -- which windows are captured -----------------------------------------
    user_float = client("0x1", 1, 0, 0, 10, 10, floating=True)
    mode_float = client("0x2", 1, 0, 0, 10, 10, floating=True, tags=["omarchy-mode-floated"])
    pinned = client("0x3", 1, 0, 0, 10, 10, floating=True, pinned=True)
    about = client("0x4", 1, 0, 0, 10, 10, floating=True, cls="org.omarchy.about")
    assert cli.capture_window(user_float) is False          # off by default, as before
    assert cli.capture_window(user_float, floating=True) is True
    assert cli.capture_window(mode_float) is True           # the layout itself
    assert cli.capture_window(pinned, floating=True) is False
    assert cli.capture_window(about, floating=True) is False

    # -- capture: floats get their own list with screen-relative rects ------
    json_replies = {
        ("monitors",): [{"name": "DP-1", "x": 1920, "y": 0, "width": 2560, "height": 1440,
                         "scale": 1, "transform": 0}],
        ("workspaces",): [{"id": 1, "monitor": "DP-1"}, {"id": 2, "monitor": "DP-1"}],
    }
    desk = [
        client("0xa", 1, 1930, 38, 1260, 1392),
        client("0xb", 1, 3204, 38, 1266, 1392),
        client("0xc", 1, 2400, 300, 800, 600, floating=True, cls="calc.example"),
        client("0xd", 2, 2000, 100, 600, 400, floating=True, cls="notes.example"),
    ]
    with patch.multiple(cli, hypr_json=lambda *a: json_replies.get(a, []),
                        clients=lambda: desk, load_monitor_blocks=lambda: {}):
        without = cli.snapshot()
        captured = cli.snapshot(capture_floating=True)
    assert [ws["number"] for ws in without] == [1]
    assert all("floating" not in ws for ws in without)
    by_number = {ws["number"]: ws for ws in captured}
    assert len(by_number[1]["columns"]) == 2
    assert by_number[1]["floating"] == [
        {"app": "calc.example", "class": "calc.example", "rect": [480, 300, 800, 600]}]
    assert by_number[2]["columns"] == []                    # a workspace of floats only
    assert by_number[2]["floating"][0]["rect"] == [80, 100, 600, 400]

    rendered = cli.render_setup("Desk", captured, [])
    assert "  floating = [" in rendered
    config.write_text("[options]\ncapture_floating = true\n\n" + rendered)
    cfg = cli.load_config()
    assert cfg["options"]["capture_floating"] is True
    loaded = cfg["setups"][0]["workspaces"]
    assert loaded[0]["floating"][0]["rect"] == [480, 300, 800, 600]
    _shape, windows = cli.setup_shape(cfg["setups"][0])
    assert windows == 4, windows

    for bad in ('[options]\ncapture_floating = "yes"\n',
                rendered.replace("rect = [80, 100, 600, 400]", "rect = [80, 100, 0, 400]")):
        config.write_text(bad)
        try:
            cli.load_config()
        except cli.ConfigError:
            pass
        else:
            raise AssertionError(f"accepted:\n{bad}")

    # -- the save path asks for floats only when the option is on -----------
    calls = []
    def fake_snapshot(**kwargs):
        calls.append(kwargs)
        return captured
    with patch.multiple(cli, snapshot=fake_snapshot, fingerprint=lambda *a: "fp",
                        load_monitor_blocks=lambda: {},
                        resolve_workspace_number=lambda n, m, e, b: (n, m)):
        config.write_text("")
        cli.store_snapshot("Desk", {"options": {"capture_floating": True}, "setups": []},
                           create=True)
        cli.store_snapshot("Desk", {"options": {}, "setups": []}, create=True)
    assert calls == [{"capture_floating": True}, {}], calls
    plans = cli.read_layouts()["fp"]["Desk"]["workspaces"]
    assert all(w["columns"] >= 1 for w in plans), plans

    # -- loading: floats open last and land on their rect, scaled -----------
    events = []
    opened = iter(range(1, 100))

    def fake_wait(before, timeout, want_class=""):
        if want_class == "never.example":
            return None
        n = next(opened)
        return {"address": f"0x{n:x}", "workspace": {"id": 1}, "floating": False}

    # Loaded on a screen half as wide and tall as the captured 2560x1440.
    now_json = {("monitors",): [{"name": "eDP-1", "x": 0, "y": 0, "width": 1280,
                                 "height": 720, "scale": 1, "transform": 0}],
                ("workspaces",): [{"id": 1, "monitor": "eDP-1"}]}
    stubs = {
        "spawn_window": lambda win: events.append(("spawn", win.get("app") or win.get("term"))),
        "wait_for_window": fake_wait,
        "dispatch": lambda lua: events.append(("dispatch", lua)),
        "hypr_eval": lambda lua: None,
        "hypr_json": lambda *a: now_json.get(a, []),
        "clients": lambda: [],
        "read_workspace_mode": lambda number: "dwindle",
        "apply_sizes": lambda *a: None,
    }
    ws_cfg = {
        "screen": [2560, 1440],
        "columns": [{"windows": [{"term": ""}]}],
        "floating": [{"app": "calc", "rect": [480, 300, 800, 600]},
                     {"app": "never", "class": "never.example", "rect": [0, 0, 100, 100]}],
    }
    with patch.multiple(cli, **stubs), patch.object(cli.time, "sleep"):
        problems = cli.build_workspace(ws_cfg, {"number": 1, "columns": 1}, {}, lambda s: None)
    assert problems == ["never never opened"], problems
    assert [e[1] for e in events if e[0] == "spawn"] == ["", "calc", "never"]
    lua = [e[1] for e in events if e[0] == "dispatch"]
    assert any('action = "on"' in line and "0x2" in line for line in lua), lua
    resize = next(line for line in lua if "window.resize" in line)
    move = next(line for line in lua if "x = 240" in line)
    assert re.search(r"x = 400, y = 300, relative = false", resize), resize
    assert re.search(r"x = 240, y = 150, relative = false", move), move

    # A floating-mode workspace puts its column windows where they were too.
    events.clear()
    opened = iter(range(1, 100))
    floating_mode = {"screen": [1280, 720],
                     "columns": [{"windows": [{"term": "", "rect": [10, 20, 300, 200]}]}]}
    with patch.multiple(cli, **{**stubs, "read_workspace_mode": lambda n: "floating"}), \
            patch.object(cli.time, "sleep"):
        cli.build_workspace(floating_mode, {"number": 1, "columns": 1}, {}, lambda s: None)
    lua = [e[1] for e in events if e[0] == "dispatch"]
    assert any("x = 300, y = 200, relative = false" in line for line in lua), lua
    assert any("x = 10, y = 20, relative = false" in line for line in lua), lua

print("floating window checks passed")
