# -*- coding: utf-8 -*-
"""开关必须接在客户端真正调用的那条路上。

ENGINE_INTENT_ADVISOR 原本只注入到 /api/engine 的 TextActionService，而前端
（frontend/src/api.js:108）只调用 /api/game/action。结果是：配了模型、开了开关，
规则认不出的句子仍然一律被拒——功能存在，但接错了地方。
"""
from __future__ import annotations

import inspect

from app.api import routes_game


def test_the_game_action_lane_builds_an_advisor():
    source = inspect.getsource(routes_game._unified_service)
    assert "advisor=_build_advisor()" in source, (
        "前端只调 /api/game/action；这条路上的服务不注入 advisor，兜底就形同虚设"
    )


def test_the_advisor_builder_degrades_instead_of_raising():
    # Off by default and never fatal: a turn must not fail because advice is
    # unavailable.
    advisor = routes_game._build_advisor()
    assert advisor is None or hasattr(advisor, "suggest")