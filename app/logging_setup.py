# -*- coding: utf-8 -*-
"""日志初始化：控制台 + logs/engine.log 滚动文件（UTF-8，避免中文乱码）。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: str = "logs") -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path / "engine.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
