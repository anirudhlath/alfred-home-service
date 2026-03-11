"""MCP JSON-RPC server for home-service.

Receives tool calls from Alfred's Home Agent and dispatches
to registered tool handlers via AlfredClient.dispatch().
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger(__name__)

ENTITY_REFRESH_INTERVAL = 300.0  # 5 minutes


class McpRequest(BaseModel):
    """JSON-RPC style MCP tool call request."""

    method: str
    params: dict[str, Any] = {}
    id: str


class McpResponse(BaseModel):
    """JSON-RPC style MCP tool call response."""

    id: str
    result: dict[str, Any] | None = None
    error: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    """Register tools and context with Alfred on startup, unregister on shutdown."""
    refresh_task: asyncio.Task[None] | None = None
    try:
        from alfred_ext.register import client

        await client.register()
        logger.info("Registered tools and context with Alfred registry")

        async def _refresh_loop() -> None:
            """Re-register periodically (refreshes tools + context)."""
            while True:
                await asyncio.sleep(ENTITY_REFRESH_INTERVAL)
                try:
                    await client.register()
                    logger.debug("Refreshed tool registry and context")
                except Exception as e:
                    logger.warning("Registration refresh failed: %s", e)

        refresh_task = asyncio.create_task(_refresh_loop())
    except Exception as e:
        logger.warning("Could not register with Alfred: %s", e)
    yield
    if refresh_task is not None:
        refresh_task.cancel()
    try:
        from alfred_ext.register import client

        await client.unregister()
        logger.info("Unregistered from Alfred registry")
    except Exception as e:
        logger.warning("Could not unregister from Alfred: %s", e)


app = FastAPI(title="home-service", lifespan=lifespan)


@app.post("/mcp")
async def mcp_endpoint(request: McpRequest) -> McpResponse:
    """Handle an MCP tool call."""
    from alfred_ext.register import client

    try:
        result = await client.dispatch(request.method, request.params)
        return McpResponse(
            id=request.id,
            result=result if isinstance(result, dict) else {"data": result},
        )
    except KeyError as e:
        return McpResponse(id=request.id, error=str(e))
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        logger.error("Tool execution failed: %s", error_msg)
        return McpResponse(id=request.id, error=error_msg)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "home-service"}
