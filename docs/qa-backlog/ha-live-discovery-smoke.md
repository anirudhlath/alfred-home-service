# Live HA Discovery + Reversible Toggle Smoke Test

**Feature:** home-service HA WebSocket discovery, generated capabilities, state ingest
**Priority:** critical
**Type:** e2e

## Prerequisites
- Real apartment Home Assistant reachable (e.g. http://192.168.50.159:8123)
- Long-lived access token minted from the HA profile page
- home-service running locally (Redis/MQTT optional for the first steps)

## Test Steps
1. `curl -s http://localhost:8000/health` — expect `ha.state = "disconnected"`.
2. `curl -s -X POST http://localhost:8000/credentials -H 'Content-Type: application/json' -d '{"url": "http://192.168.50.159:8123", "token": "<TOKEN>"}'`
3. `curl -s http://localhost:8000/health` — expect `connected` with real entity/area counts.
4. Deliberately push a WRONG token — expect `ha.state = "auth_failed"` in the response health.
5. Push the correct token again, then flip any light in the HA app; within seconds `curl -s http://localhost:8000/health` — `last_event_age_s` should reset to a small number.
6. With Mosquitto running, `mosquitto_sub -t home/state_changed -C 1` while toggling a light — expect one StateChangedEvent JSON.
7. Pick a real light and toggle it REVERSIBLY via the generated tool:
   `curl -s -X POST http://localhost:8000/mcp -d '{"method": "home.light_turn_on", "params": {"target": "<real area name>"}, "id": "qa-1"}' -H 'Content-Type: application/json'` — then turn it back off with `home.light_turn_off`.
8. Restart home-service; confirm it reports `disconnected` until credentials are re-pushed (or re-pushed automatically by Alfred core once Plans 1+3 are live).

## Expected Result
- Health transitions disconnected → connected → auth_failed → connected as driven.
- Entity/area counts match the real apartment.
- MQTT carries every state change; the light responds to /mcp calls addressed by area/friendly name.

## Notes
- Steps 1–5 and 7 need no Alfred core at all. Step 8's automatic re-push needs Plan 1 + Plan 3 core work.
- Delete this file once verified on the real apartment HA.
