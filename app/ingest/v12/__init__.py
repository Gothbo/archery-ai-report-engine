# -*- coding: utf-8 -*-
"""v1.2 上行数据接口接收适配层（设备/网关 → 引擎）。传输无关核心见 receiver.ingest_messages。"""
from app.ingest.v12.receiver import ingest_messages

__all__ = ["ingest_messages"]
