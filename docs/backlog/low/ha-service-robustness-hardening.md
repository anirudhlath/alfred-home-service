# HA Service Robustness Hardening (Plan 2 Deferred Minors)

**Priority:** low
**Source:** Whole-branch review of `feat/ha-discovery-rewrite` (HA Plan 2 rewrite),
2026-07-19. Two items from that review were fixed before merge (idempotent
`apply_credentials` reconnect-loop fix + a risk-map config-drift invariant test);
everything below was explicitly deferred as non-blocking minors.

## Summary
A cluster of small robustness gaps in the Plan 2 HA connection/forwarding/dispatch
stack. None are blocking for merge — the shipped config and current call patterns
don't trigger them — but each is a plausible failure mode under malformed input,
concurrent requests, or infrastructure hiccups. Track and fix opportunistically.

## Context / Motivation

- **`HAConnection._handle_message` indexes `msg["id"]` directly**
  (`app/ha_connection.py:257`, `_handle_message`, case `"result"`:
  `fut = self._pending.get(int(msg["id"]))`) and **`_handle_state_changed` indexes
  `new["state"]`/`old["state"]` directly** (`app/ha_connection.py:284-285`:
  `new_state = str(new["state"]) if new else None`). A malformed frame from HA (or a
  future HA API change dropping one of these keys) raises `KeyError`/`TypeError`
  inside the reader loop's `async for raw in ws: await self._handle_message(...)`
  (`_run`, `app/ha_connection.py:155-156`), which propagates out of the `async with
  connect(...)` block and tears down the whole connection — forcing a full
  reconnect + registry refetch instead of just dropping the one bad frame. Should
  use `.get()` with a skip-and-log fallback for missing/malformed fields.

- **`apply_credentials` has no `asyncio.Lock`** (`app/ha_connection.py:113`). The
  idempotency guard added in the Plan 2 review fix (no-op when unchanged AND
  connected) closes the main production reconnect loop, but there remains a narrow
  startup race: the env-var-driven initial connect task and a concurrent
  `POST /credentials` call (e.g. a user re-saving the same creds via the Settings UI
  right as the service boots) can both observe `conn_state != "connected"` and both
  proceed to `stop()` + reconnect concurrently, racing `self._url`/`self._token`/
  `self._task` writes. Serialize the whole method body under an `asyncio.Lock`
  instance attribute.

- **`call_service` doesn't wrap a mid-send `ConnectionClosed`**
  (`app/ha_connection.py:329-347`, via `_cmd` at `app/ha_connection.py:235-244`).
  If the WebSocket drops between `ws.send(...)` and the result arriving, `_cmd`
  currently lets `websockets.exceptions.ConnectionClosed` propagate raw instead of
  wrapping it in `HACommandError` (compare `_fail_pending`, `app/ha_connection.py:
  246-250`, which only fires for futures already pending when the reader loop's
  `finally` runs — a send-time close races ahead of that). Callers of
  `call_service` (`app/server.py` `/mcp` dispatch) currently only catch generic
  `Exception`, so this doesn't crash the endpoint today, but the error message loses
  the structured `HACommandError(code, message)` shape the rest of the codebase
  relies on. Wrap the `ws.send` call in `call_service` (or in `_cmd` generally) with
  a `try/except (ConnectionClosed) as exc: raise HACommandError("connection_lost",
  str(exc))`.

- **`StateForwarder._publish_loop` only catches `aiomqtt.MqttError`**
  (`app/state_forwarder.py:85-100`). Any other exception raised inside the `async
  with aiomqtt.Client(...)` block (e.g. a `json`/pydantic surprise, an unexpected
  `OSError` subtype aiomqtt doesn't wrap, a bug in future code added to the loop)
  propagates out of `_publish_loop`, silently killing the background task
  (`start()`'s `asyncio.create_task(self._publish_loop(), ...)` — nothing awaits or
  observes the task's exception) — state forwarding to MQTT stops permanently with
  no retry and no visible error until someone notices HA state isn't reaching
  Alfred's bus. Broaden to `except Exception as exc` with the same backoff/retry
  behavior as the `MqttError` branch (log at `ERROR` for the non-MqttError case
  since it's unexpected).

- **`StateForwarder`: no test for a mid-publish `MqttError` while a message is
  `inflight`** (`tests/test_state_forwarder.py`). `_publish_loop` deliberately
  retains `inflight` across reconnects ("not lost on MqttError" — the code comment
  at `app/state_forwarder.py:87`) so a message doesn't get silently dropped if the
  broker connection drops between `dequeue` and `publish` succeeding. That retention
  behavior has no regression test — add one that fails a publish mid-loop (mock
  `client.publish` to raise `aiomqtt.MqttError` once) and asserts the same message
  is retried and eventually delivered after reconnect, rather than being requeued
  or lost.

- **`server.py` `/mcp` endpoint's `except KeyError` is too broad**
  (`app/server.py:226-227`, `mcp_endpoint`): `except KeyError as exc: return
  McpResponse(id=request.id, error=str(exc))`. This is meant to catch "unknown tool
  name" from `client.dispatch(...)`'s internal registry lookup, but it also catches
  a `KeyError` raised *inside* a tool handler for an unrelated reason (e.g. a
  handler doing `service_data["some_field"]` on missing input) and reports it with
  the same generic `str(exc)` (which for a bare `KeyError` is often just the
  quoted key, e.g. `'some_field'` — useless to the caller). Narrow this to only
  catch the registry's own "unknown tool" signal (e.g. check
  `request.method not in <registry>` up front, or have `dispatch` raise a
  dedicated exception type for "unknown tool" distinct from a handler-raised
  `KeyError`) so a handler's own `KeyError` gets a descriptive message via the
  general `except Exception` branch instead.

- **Test coverage gaps** (all `tests/`):
  - `EntityIndex`: no fixture exercising a precedence collision (e.g. an entity
    whose `name` AND `original_name` AND registry-vs-device naming sources
    disagree, or duplicate friendly names across areas) to lock in the documented
    precedence order.
  - `CapabilityGenerator`: no test asserting the `MAX_LISTED_ENTITIES` cap (30,
    `app/capability_generator.py:27`) actually truncates `_target_description`'s
    entity list when a domain has more than 30 entities.
  - `CapabilityGenerator`: no negative audience assertions for `lock`/`cover` —
    existing tests assert what tools/risk *are* generated for these domains but
    nothing asserts they are NOT tagged `audience="reflex"` (they're conscious-tier
    by construction since they're outside `REFLEX_DOMAINS`, but there's no test
    pinning that down explicitly the way the disjointness test added in this
    review pins down the risk side).
  - `tests/fake_ha.py`: `_TARGET` (line 220, `_TARGET = {"entity": [{}]}`) has no
    type annotation — should be `_TARGET: dict[str, list[dict[str, Any]]] = {...}`
    for consistency with the rest of the module's typed module-level constants.

## Acceptance Criteria
- [ ] `HAConnection._handle_message`/`_handle_state_changed` use `.get()` (not
      direct indexing) for `msg["id"]`, `new["state"]`, `old["state"]`, with a
      log-and-skip fallback for malformed frames instead of tearing down the
      connection.
- [ ] `HAConnection.apply_credentials` is serialized with an `asyncio.Lock` instance
      attribute covering the full method body.
- [ ] `HAConnection.call_service` (or `_cmd`) wraps a mid-send `ConnectionClosed`
      in `HACommandError("connection_lost", ...)`.
- [ ] `StateForwarder._publish_loop` catches `Exception` broadly (not just
      `aiomqtt.MqttError`) with the same backoff/retry loop, logging unexpected
      exception types at `ERROR`.
- [ ] `tests/test_state_forwarder.py` gains a test for a mid-publish `MqttError`
      while a message is `inflight`, asserting retry-then-deliver (no drop, no
      duplicate).
- [ ] `server.py`'s `/mcp` endpoint's unknown-tool branch is narrowed so a
      handler-raised `KeyError` surfaces a descriptive message instead of the bare
      `str(exc)` unknown-tool path.
- [ ] `tests/test_entity_index.py` (or equivalent) gains a precedence-collision
      fixture.
- [ ] `tests/test_capability_generator.py` gains a `MAX_LISTED_ENTITIES` cap test
      and negative `audience` assertions for `lock`/`cover`.
- [ ] `tests/fake_ha.py`'s `_TARGET` constant gets an explicit type annotation.
