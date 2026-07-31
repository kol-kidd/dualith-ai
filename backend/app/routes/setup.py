"""First-run provider configuration, and the session token the frontend needs.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
)

from ..providers import (
    PROVIDERS,
    ProviderConfig,
    apply_provider_config,
    delete_provider_config,
    describe_provider_config,
    list_provider_models,
    provider_config_exists,
    save_provider_config,
    test_provider_slot,
)
from ..schemas import (
    SetupModelsRequest,
    SetupSaveRequest,
    SetupTestRequest,
)
from ..security import (
    _SESSION_TOKEN,
    require_session_token,
)
from ..status_refresh import (
    refresh_eco_pricing,
)
from ..store import (
    utc_now,
)

log = logging.getLogger("dualith")

router = APIRouter()

@router.get("/api/setup/status")
async def setup_status() -> dict[str, Any]:
    # Token is safe to expose here: cross-origin pages are blocked from reading
    # this response by the CORS policy (only allowed origins get the body).
    return {
        "configured": provider_config_exists(),
        "token": _SESSION_TOKEN,
        "slots": describe_provider_config(),
    }


@router.post("/api/setup/test", dependencies=[Depends(require_session_token)])
async def setup_test(request: SetupTestRequest) -> dict[str, Any]:
    runner_a_result, runner_b_result = await asyncio.gather(
        test_provider_slot(request.runner_a),
        test_provider_slot(request.runner_b),
    )
    return {"runner_a": runner_a_result, "runner_b": runner_b_result}


@router.post("/api/setup/save", dependencies=[Depends(require_session_token)])
async def setup_save(request: SetupSaveRequest) -> dict[str, Any]:
    config = ProviderConfig(
        runner_a=request.runner_a,
        runner_b=request.runner_b,
        configured_at=utc_now(),
    )
    save_provider_config(config)
    apply_provider_config(config)
    await refresh_eco_pricing()
    log.info("Provider config saved and applied: runner_a=%s/%s runner_b=%s/%s",
             config.runner_a.provider, config.runner_a.mode,
             config.runner_b.provider, config.runner_b.mode)
    return {"ok": True}


@router.delete("/api/setup/config", dependencies=[Depends(require_session_token)])
async def setup_delete_config() -> dict[str, Any]:
    delete_provider_config()
    log.info("Provider config deleted — wizard will re-run on next load")
    return {"ok": True}


@router.post("/api/setup/models", dependencies=[Depends(require_session_token)])
async def setup_models(request: SetupModelsRequest) -> dict[str, Any]:
    return await list_provider_models(request.slot)


@router.get("/api/setup/providers")
async def setup_providers() -> dict[str, Any]:
    return {"providers": PROVIDERS}
