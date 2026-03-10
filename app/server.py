"""MCP JSON-RPC server for home-service.

Receives tool calls from Alfred's Home Agent and dispatches
to registered tool handlers via AlfredClient.dispatch().
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger(__name__)


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
    """Register tools with Alfred on startup."""
    try:
        from alfred_ext.register import client

        await client.register()
        logger.info("Registered tools with Alfred registry")
    except Exception as e:
        # Registration failure is non-fatal — Alfred may not be running yet
        logger.warning("Could not register with Alfred: %s", e)
    yield


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
        logger.error("Tool execution failed: %s", e)
        return McpResponse(id=request.id, error=str(e))


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "service": "home-service"}
