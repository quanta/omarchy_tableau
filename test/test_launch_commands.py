#!/usr/bin/env python3
"""Capture records a command that reopens the same window.

Web apps share their browser's process, Electron apps pack their command line
into one string, and an upgraded binary reads back as "(deleted)". None of
these may turn into a command that opens something else, or nothing at all.
"""

import importlib.machinery
import importlib.util
import os
import shlex
import tempfile
from pathlib import Path


def load_cli(path: Path):
    loader = importlib.machinery.SourceFileLoader("tableau_launch_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    os.environ["OMARCHY_TABLEAU_CONFIG"] = str(root / "tableau.toml")
    os.environ["XDG_STATE_HOME"] = str(root / "state")
    os.environ["XDG_DATA_HOME"] = str(root / "user")
    os.environ["XDG_DATA_DIRS"] = str(root / "system")
    cli = load_cli(Path(__file__).parents[1] / "bin/omarchy-tableau")

    # -- a binary replaced by an upgrade keeps its name ----------------------
    real_readlink = cli.os.readlink
    try:
        cli.os.readlink = lambda path: "/usr/lib/app/app-bin (deleted)"
        assert cli.proc_exe(42) == "app-bin"
        cli.os.readlink = lambda path: "/usr/lib/app/app-bin"
        assert cli.proc_exe(42) == "app-bin"
    finally:
        cli.os.readlink = real_readlink

    # -- web-app classes decode to their URL ---------------------------------
    assert cli.webapp_url("chrome-mail.example.com__-Default") == "https://mail.example.com/"
    assert cli.webapp_url("brave-x.com__someone-Default") == "https://x.com/someone"
    assert cli.webapp_url("chrome-docs.example.org__doc_d-Profile_1") == "https://docs.example.org/doc/d"
    for plain in ("chromium", "chromium-browser", "google-chrome", "org.omarchy.btop", ""):
        assert cli.webapp_url(plain) == "", plain

    # -- desktop entries: user shadows system, field codes are dropped -------
    (root / "user/applications").mkdir(parents=True)
    (root / "system/applications").mkdir(parents=True)
    (root / "system/applications/Mail.desktop").write_text(
        "[Desktop Entry]\nName=Mail\nExec=system-mail %u\n"
        "StartupWMClass=chrome-mail.example.com__-Default\n")
    (root / "user/applications/Mail.desktop").write_text(
        "[Desktop Entry]\nName=Mail\nExec=mail-handler %u --flag=100%%\n"
        "StartupWMClass=chrome-mail.example.com__-Default\n"
        "\n[Desktop Action compose]\nExec=mail-handler compose\n")
    (root / "system/applications/Gone.desktop").write_text(
        "[Desktop Entry]\nExec=gone\nHidden=true\nStartupWMClass=gone.app\n")
    assert cli.desktop_command_for_class("chrome-mail.example.com__-Default") == \
        "mail-handler --flag=100%"
    assert cli.desktop_command_for_class("CHROME-MAIL.EXAMPLE.COM__-DEFAULT") == \
        "mail-handler --flag=100%"
    assert cli.desktop_command_for_class("gone.app") == ""
    assert cli.desktop_command_for_class("") == ""

    # -- a web app is captured as its launcher, or as a URL ------------------
    browser_argv = ["/usr/lib/chromium/chromium", "--ozone-platform=wayland"]
    cli.proc_cmdline = lambda pid: list(browser_argv)
    cli.proc_exe = lambda pid: "chromium"
    cli.child_pids = lambda pid: []
    spec = cli.window_to_spec({"pid": 42, "class": "chrome-mail.example.com__-Default"})
    assert spec == {"app": "mail-handler --flag=100%",
                    "class": "chrome-mail.example.com__-Default"}, spec
    spec = cli.window_to_spec({"pid": 42, "class": "chrome-chat.example.net__-Default"})
    assert spec["app"] == "omarchy-launch-webapp https://chat.example.net/", spec
    assert spec["class"] == "chrome-chat.example.net__-Default"

    # -- a plain browser window drops a web app's inherited --app= ----------
    browser_argv[:] = ["/usr/lib/chromium/chromium", "--app=https://mail.example.com/",
                       "--ozone-platform=wayland"]
    spec = cli.window_to_spec({"pid": 42, "class": "chromium"})
    assert "--app=" not in spec["app"], spec
    assert shlex.split(spec["app"])[0] == "/usr/lib/chromium/chromium"

    # -- an Electron title packed into argv[0] is split back -----------------
    app = root / "opt/app/app-bin"
    app.parent.mkdir(parents=True)
    app.write_text("#!/bin/sh\n")
    app.chmod(0o755)
    cli.proc_cmdline = lambda pid: [f"{app} --ozone-platform=wayland"]
    # The binary's name still matches once " (deleted)" is ignored, and an
    # executable path is enough when it does not match at all.
    for exe in ("app-bin", "electron", ""):
        cli.proc_exe = lambda pid, exe=exe: exe
        spec = cli.window_to_spec({"pid": 42, "class": "app.example"})
        assert shlex.split(spec["app"]) == [str(app), "--ozone-platform=wayland"], (exe, spec)

    # A title that is not a program path stays one argument.
    cli.proc_cmdline = lambda pid: ["worker: idle queue"]
    cli.proc_exe = lambda pid: "python3"
    spec = cli.window_to_spec({"pid": 42, "class": "worker"})
    assert shlex.split(spec["app"]) == ["worker: idle queue"], spec

print("launch command checks passed")
