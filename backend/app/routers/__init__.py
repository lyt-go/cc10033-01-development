"""业务路由汇总：尝试回执等专用路由先注册，通用资源路由最后兜底。"""
from __future__ import annotations

from app.routers import attempts, resource

ROUTERS = [attempts, resource]
