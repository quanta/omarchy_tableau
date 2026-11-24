#!/usr/bin/env python3
"""A captured desk is rebuilt from its window rectangles, not rounded columns."""

import importlib.machinery
import importlib.util
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def load_cli(path: Path):
    loader = importlib.machinery.SourceFileLoader("tableau_exact_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def node_boxes(tree, box, out):
    """The area each leaf gets when dwindle applies the tree's ratios."""
    if "leaf" in tree:
        out[tree["leaf"]] = box
        return out
    axis, share = tree["axis"], tree["ratio"] / 2
    first, second = list(box), list(box)
    first[2 + axis] = box[2 + axis] * share
    second[axis] = box[axis] + box[2 + axis] * share
    second[2 + axis] = box[2 + axis] * (1 - share)
    node_boxes(tree["first"], first, out)
    node_boxes(tree["second"], second, out)
    return out


GAP = 14  # pixels between neighbouring windows

# A 5120-wide desk: an uneven four columns, two of them split in two rows.
DESK = {
    (0, 0): [12, 38, 1264, 688], (0, 1): [12, 740, 1264, 688],
    (1, 0): [1290, 38, 1550, 1390],
    (2, 0): [2854, 38, 1300, 1390],
    (3, 0): [4168, 38, 940, 900], (3, 1): [4168, 952, 940, 476],
}


def columns_of(desk):
    cols = {}
    for (ci, wi), rect in sorted(desk.items()):
        cols.setdefault(ci, []).append({"term": f"w{ci}{wi}", "rect": rect})
    return [{"windows": wins} for _, wins in sorted(cols.items())]


with tempfile.TemporaryDirectory() as tmp:
    os.environ["OMARCHY_TABLEAU_CONFIG"] = str(Path(tmp) / "tableau.toml")
    os.environ["XDG_STATE_HOME"] = str(Path(tmp) / "state")
    cli = load_cli(Path(__file__).parents[1] / "bin/omarchy-tableau")

    # -- the tree reproduces every window to the pixel -----------------------
    columns = columns_of(DESK)
    tree = cli.exact_tree({"columns": columns}, columns)
    assert tree is not None
    boxes = node_boxes(tree, [12, 38, 5096, 1390], {})
    for key, (x, y, w, h) in DESK.items():
        bx, by, bw, bh = boxes[key]
        # A window is its area less half a gap on each inner edge.
        left = x - (GAP / 2 if x > 12 else 0)
        top = y - (GAP / 2 if y > 38 else 0)
        right = x + w + (GAP / 2 if x + w < 5108 else 0)
        bottom = y + h + (GAP / 2 if y + h < 1428 else 0)
        for got, want in ((bx, left), (by, top), (bx + bw, right), (by + bh, bottom)):
            assert abs(got - want) <= 1, (key, boxes[key], DESK[key])

    def ratios_in_range(node):
        return "leaf" in node or (0.1 <= node["ratio"] <= 1.9 and ratios_in_range(node["first"])
                                  and ratios_in_range(node["second"]))
    assert ratios_in_range(tree)

    # A pinwheel is not a dwindle layout: no straight cut separates it.
    pinwheel = [("a", 0, 0, 60, 30), ("b", 60, 0, 40, 60), ("c", 40, 60, 60, 40),
                ("d", 0, 30, 40, 70), ("e", 40, 30, 20, 30)]
    assert cli.split_tree(pinwheel, [0, 0, 100, 100]) is None

    # No rect on a window, a fold, or a single window: no exact tree.
    bare = [{"windows": [{"term": "a"}]}, {"windows": [{"term": "b", "rect": [0, 0, 9, 9]}]}]
    assert cli.exact_tree({"columns": bare}, bare) is None
    assert cli.exact_tree({"columns": columns}, columns[:2]) is None
    one = [{"windows": [{"term": "a", "rect": [0, 0, 9, 9]}]}]
    assert cli.exact_tree({"columns": one}, one) is None
    # A window kept floating is not part of the tiled tree.
    with_float = columns + [{"windows": [{"term": "f", "float": True}]}]
    assert cli.exact_tree({"columns": with_float}, with_float) is not None

    # -- building: open everything floating, then tile it into the tree ------
    events = []
    opened = iter(range(1, 100))

    def fake_wait(before, timeout, want_class=""):
        n = next(opened)
        return {"address": f"0x{n:x}", "workspace": {"id": 1}, "floating": False}

    def fake_json(*args):
        if args and args[0] == "getoption":
            return {"bool": False}
        return []

    stubs = {
        "spawn_window": lambda win: events.append(("spawn", win["term"])),
        "wait_for_window": fake_wait,
        "dispatch": lambda lua: events.append(("dispatch", lua)),
        "hypr_eval": lambda lua: events.append(("eval", lua)),
        "hypr_json": fake_json,
        "clients": lambda: [],
        "read_workspace_mode": lambda number: "dwindle",
        "apply_sizes": lambda *a: events.append(("apply_sizes",)),
    }
    with patch.multiple(cli, **stubs), patch.object(cli.time, "sleep"):
        problems = cli.build_workspace({"columns": columns}, {"number": 1, "columns": 4},
                                       {}, lambda step: None)
    assert problems == [], problems
    last_spawn = max(i for i, e in enumerate(events) if e[0] == "spawn")
    first_preselect = min(i for i, e in enumerate(events)
                          if e[0] == "dispatch" and "preselect" in e[1])
    assert first_preselect > last_spawn, "no split may be made while apps are still opening"
    floats_on = [e for e in events if e[0] == "dispatch" and 'action = "on"' in e[1]]
    assert len(floats_on) == len(DESK)
    ratios = [e for e in events if e[0] == "dispatch" and "splitratio" in e[1]]
    assert len(ratios) == len(DESK) - 1 and all(e[1].endswith(' exact")') for e in ratios)
    assert ("apply_sizes",) not in events
    evals = [e[1] for e in events if e[0] == "eval" and e[1].startswith("hl.config")]
    assert len(evals) == 2 and "= true" in evals[0] and "= false" in evals[1], evals

    # A window that never opens: tile what did open, rebuild nothing.
    events.clear()
    opened = iter(range(1, 100))
    calls = {"n": 0}

    def flaky_wait(before, timeout, want_class=""):
        calls["n"] += 1
        return fake_wait(before, timeout) if calls["n"] < 3 else None

    with patch.multiple(cli, **{**stubs, "wait_for_window": flaky_wait}), \
            patch.object(cli.time, "sleep"):
        problems = cli.build_workspace({"columns": columns}, {"number": 1, "columns": 4},
                                       {}, lambda step: None)
    assert problems == ["w10 never opened"], problems
    floats_off = [e for e in events if e[0] == "dispatch" and 'action = "off"' in e[1]]
    assert len(floats_off) == 2, events
    assert not any(e[0] == "dispatch" and ("splitratio" in e[1] or "preselect" in e[1])
                   for e in events)

    # A folded plan keeps the column path.
    events.clear()
    opened = iter(range(1, 100))
    with patch.multiple(cli, **stubs), patch.object(cli.time, "sleep"):
        cli.build_workspace({"columns": columns}, {"number": 1, "columns": 2},
                            {}, lambda step: None)
    assert ("apply_sizes",) in events
    assert not any(e[0] == "dispatch" and "splitratio" in e[1] for e in events)

    # -- capture writes rectangles relative to the screen --------------------
    def client(address, x, y, w, h):
        return {"address": address, "workspace": {"id": 1}, "at": [x, y], "size": [w, h],
                "floating": False, "mapped": True, "class": "app.example", "pid": 0}

    snap_json = {
        ("monitors",): [{"name": "DP-1", "x": 1920, "y": 0, "width": 3840, "height": 2160,
                         "scale": 2, "transform": 0}],
        ("workspaces",): [{"id": 1, "monitor": "DP-1"}],
    }
    with patch.multiple(cli, hypr_json=lambda *a: snap_json.get(a, []),
                        clients=lambda: [client("0x1", 1930, 38, 940, 1032),
                                         client("0x2", 2884, 38, 946, 1032)],
                        load_monitor_blocks=lambda: {}):
        captured = cli.snapshot()
    ws = captured[0]
    assert ws["screen"] == [1920, 1080], ws
    rects = [w["rect"] for c in ws["columns"] for w in c["windows"]]
    assert rects == [[10, 38, 940, 1032], [964, 38, 946, 1032]], rects
    rendered = cli.render_setup("Desk", captured, [])
    assert "screen = [1920, 1080]" in rendered
    assert "rect = [10, 38, 940, 1032]" in rendered

    # -- the config accepts what capture writes, and refuses nonsense ---------
    Path(os.environ["OMARCHY_TABLEAU_CONFIG"]).write_text(rendered)
    cfg = cli.load_config()
    loaded = cfg["setups"][0]["workspaces"][0]
    assert cli.exact_tree(loaded, loaded["columns"]) is not None
    for bad in ('rect = [1, 2, 3]', 'rect = [0, 0, 0, 10]', 'rect = [0, 0, "a", 1]',
                'rect = [0, 0, 1e9, 10]'):
        Path(os.environ["OMARCHY_TABLEAU_CONFIG"]).write_text(
            rendered.replace("rect = [10, 38, 940, 1032]", bad))
        try:
            cli.load_config()
        except cli.ConfigError:
            pass
        else:
            raise AssertionError(f"accepted {bad}")
    Path(os.environ["OMARCHY_TABLEAU_CONFIG"]).write_text(
        rendered.replace("screen = [1920, 1080]", "screen = [1920]"))
    try:
        cli.load_config()
    except cli.ConfigError:
        pass
    else:
        raise AssertionError("accepted a one-number screen")

    # Rounded column weights keep their proportions when no ratio fits.
    assert cli.nice_weights([1000, 1000]) == [1, 1]
    uneven = cli.nice_weights([1000, 1731])
    assert uneven != [1, 1] and abs(uneven[0] / sum(uneven) - 1000 / 2731) < 0.06, uneven

    # -- a captured screen's columns are not folded on a screen about as wide
    wide = [{"name": "DP-1", "description": "", "focused": True, "width": 5120,
             "height": 1440, "scale": 1, "transform": 0}]
    narrow = [{**wide[0], "width": 1920, "height": 1080}]
    six = {"number": 1, "screen": [5120, 1440], "columns": [{"windows": []}] * 6}
    plain = {"number": 2, "columns": [{"windows": []}] * 6}
    setup = {"name": "Wide", "workspaces": [six, plain]}
    with patch.object(cli, "load_monitor_blocks", lambda: {}):
        on_wide = cli.resolve_plan(setup, wide, None)["workspaces"]
        on_narrow = cli.resolve_plan(setup, narrow, None)["workspaces"]
    assert [w["columns"] for w in on_wide] == [6, 4], on_wide
    assert [w["columns"] for w in on_narrow] == [3, 3], on_narrow

print("exact layout checks passed")
