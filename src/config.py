"""Application configuration for ok-RwtD (Rogue with the Dead)."""

import os


version = "dev"
app_profile = os.environ.get("PYAPPIFY_APP_PROFILE", "")


gui_config = {
    "type": "web" if app_profile.casefold() == "web" else "qt",
    "window_size": {
        "width": 1200,
        "height": 800,
        "min_width": 600,
        "min_height": 450,
    },
}

if gui_config["type"] == "web":
    gui_config["launch_mode"] = "pywebview"


config = {
    "custom_tasks": True,
    "debug": False,
    "gui": gui_config,
    "config_folder": "configs",
    "gui_icon": "icons/icon.png",
    "gui_title": "ok-RwtD",
    "wait_until_before_delay": 0,
    "wait_until_check_delay": 0,
    "wait_until_settle_time": 0,
    "ocr": {
        "lib": "onnxocr",
        "auto_simplify": False,
        "params": {
            "use_openvino": True,
        },
    },
    "windows": {
        "interaction": [
            "Pynput",
            "PostMessage",
            "Genshin",
            "PyDirect",
            "ForegroundPostMessage",
        ],
        "capture_method": ["WGC", "BitBlt_RenderFull", "BitBlt"],
        "check_hdr": False,
        "force_no_hdr": False,
        "require_bg": True,
    },
    "adb": {
        "packages": ["net.room6.horizon"],
    },
    "start_timeout": 120,
    "supported_resolution": {
        # OK accepts the selected ADB display; RwtD tasks validate physical
        # dimensions and normalize to the annotated 1080x2400 canvas.
        "ratio": None,
        "min_size": None,
        "resize_to": [],
    },
    "links": {
        "default": {
            "github": "https://github.com/GammaHH/ok-RwtD",
            "share": "https://github.com/GammaHH/ok-RwtD",
            "faq": "https://github.com/GammaHH/ok-RwtD",
        },
    },
    "screenshots_folder": "screenshots",
    "template_matching": {
        "coco_feature_json": os.path.join("assets", "coco_annotations.json"),
        "default_horizontal_variance": 0.002,
        "default_vertical_variance": 0.002,
        "default_threshold": 0.8,
    },
    "version": version,
    "my_app": ["src.globals", "Globals"],
    "onetime_tasks": [
        ["ok", "DiagnosisTask"],
    ],
}
