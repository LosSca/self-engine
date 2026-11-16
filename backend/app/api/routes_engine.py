"""The `/api/v1` engine facade.

The handlers live in the sibling `routes_engine_*` modules, grouped by concern:
authoring (worlds, the studio, world sub-resources), campaign state, actions,
and the campaign trail. This module only registers them, and re-exports the
handful of names other modules and tests reach for, so the move out of a single
3209-line file stayed invisible from the outside.

Registration order matters - see the comment above the `_register` calls.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.api import (
    routes_engine_actions,
    routes_engine_authoring,
    routes_engine_authoring_body,
    routes_engine_authoring_tail,
    routes_engine_campaigns,
    routes_engine_campaigns_trail,
    routes_engine_state,
    routes_engine_state_trail,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["engine"])


def _register(sub: APIRouter) -> APIRouter:
    """Attach one group of routes to the engine router.

    Registration is an explicit call rather than an `include_router` in the
    caller so that the order - and therefore which handler wins a
    `/snapshots/prune` style shadowing - is decided in one place.
    """
    router.include_router(sub)
    return sub

# Registration order is not cosmetic: FastAPI matches routes in the order they
# are added, so a '/worlds/{world_id}' declared before '/worlds/{world_id}/map'
# would swallow the latter. These calls reproduce the sequence the single module
# used to have, and backend/tests/api/test_routes_contracts.py pins it.
_register(routes_engine_authoring.router_head)      # world presets, worlds, evolution
_register(routes_engine_campaigns.router_bind)      # bind a world to a campaign
_register(routes_engine_state.router_head)          # lane, export, import, state, story
_register(routes_engine_state.router)               # diagnostics, snapshots, rollback
_register(routes_engine_actions.router)             # combat, preview, actions, suggestions
_register(routes_engine_campaigns_trail.router_events)  # the campaign event log
_register(routes_engine_campaigns_trail.router_quest_progress)  # quest progress
_register(routes_engine_state_trail.router_event_executions)  # event executions
_register(routes_engine_campaigns_trail.router_requests)  # requests
_register(routes_engine_authoring_tail.router_tail)  # /agents
_register(routes_engine_campaigns_trail.router_trail)  # agent calls, actions/{id}
_register(routes_engine_authoring_body.router_body)  # authoring studio, world sub-resources
_register(routes_engine_authoring_tail.router_end)  # /local-model

# --- Re-exports -------------------------------------------------------------
#
# `_get_narrator` is the one name that must stay a single shared binding: tests
# (tests/api/test_acceptance_story.py, test_narrative_diagnostics_api.py) patch
# `routes_engine._get_narrator` to force a narrator. The action routes therefore
# resolve the narrator through `routes_engine_actions.current_narrator()`, which
# reads this name at call time. Everything else is a plain pass-through.
from app.api import routes_engine_actions as _actions  # noqa: E402
from app.api.routes_engine_campaigns_trail import progress_quest_objective  # noqa: E402
from app.api.routes_engine_actions import (  # noqa: E402  (re-export after registration)
    _build_advisor,
    _graph_mirror_sink,
    _history_provider,
    _service,
)
from app.api.routes_engine_authoring_tail import local_model_status  # noqa: E402
from app.api.routes_engine_state import (  # noqa: E402  (re-export after registration)
    LaneResponse,
    _debug_tools_enabled,
    _get_store,
    _narrator_cache,
    get_campaign_lane,
)

_get_narrator = _actions._get_narrator  # patched here, read by current_narrator()
