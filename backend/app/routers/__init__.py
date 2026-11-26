"""业务路由汇总：固定路径的回退运行接口先注册，通用资源路由后注册。"""
from __future__ import annotations

from app.routers import fallback_run, resource

ROUTERS = [fallback_run, resource]
