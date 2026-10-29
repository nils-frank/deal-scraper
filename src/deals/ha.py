"""Home Assistant REST API client: watchlist read + notify push.

One HA long-lived access token, used for both calls. No MQTT, no intermediate
automation: this calls HA's REST API directly.
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15


def _url() -> str:
    return os.environ.get("HA_URL", "http://localhost:8123")


def _headers() -> dict:
    # Read per call, so commands that never talk to Home Assistant (`deals find`)
    # need no HA_TOKEN.
    return {
        "Authorization": f"Bearer {os.environ['HA_TOKEN']}",
        "Content-Type": "application/json",
    }


def get_todo_items(entity_id: str) -> list[dict]:
    """Returns {"uid", "summary", "description"} for every needs_action item on the
    given todo list, via todo.get_items with return_response=true. uid is needed to
    target todo.update_item later without relying on summary text matching."""
    log.info("POST %s/api/services/todo/get_items entity_id=%s", _url(), entity_id)
    resp = requests.post(
        f"{_url()}/api/services/todo/get_items",
        params={"return_response": "true"},
        headers=_headers(),
        json={"entity_id": entity_id},
        timeout=REQUEST_TIMEOUT,
    )
    log.info("  -> %d: %s", resp.status_code, resp.text[:500])
    resp.raise_for_status()
    body = resp.json()
    service_response = body.get("service_response", {})
    entity_result = service_response.get(entity_id, {})
    items = entity_result.get("items", [])
    result = [
        {
            "uid": item.get("uid"),
            "summary": item["summary"].strip(),
            "description": item.get("description"),
        }
        for item in items
        if item.get("summary") and item.get("status", "needs_action") == "needs_action"
    ]
    log.info("get_todo_items(%s): %d raw item(s), %d needs_action -> %s", entity_id, len(items), len(result), [i["summary"] for i in result])
    return result


def update_todo_item_description(entity_id: str, uid: str, description: str | None) -> None:
    """POST todo/update_item - sets the item's description field (visible when the
    todo list item is expanded in the app), without touching its summary or status.
    A description of None clears the field."""
    log.info("POST %s/api/services/todo/update_item uid=%s description=%r", _url(), uid, description)
    resp = requests.post(
        f"{_url()}/api/services/todo/update_item",
        headers=_headers(),
        json={"entity_id": entity_id, "item": uid, "description": description},
        timeout=REQUEST_TIMEOUT,
    )
    log.info("  -> %d: %s", resp.status_code, resp.text[:300])
    resp.raise_for_status()


def add_todo_item(entity_id: str, summary: str, description: str | None = None) -> None:
    """POST todo/add_item - adds a new item to a to-do list. Used for the
    Angebote list, which is backed by a real local_todo entity and so - unlike
    the old states-API sensor this replaced - survives a Home Assistant restart."""
    payload = {"entity_id": entity_id, "item": summary}
    if description:
        payload["description"] = description
    log.info("POST %s/api/services/todo/add_item payload=%s", _url(), payload)
    resp = requests.post(
        f"{_url()}/api/services/todo/add_item",
        headers=_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    log.info("  -> %d: %s", resp.status_code, resp.text[:300])
    resp.raise_for_status()


def remove_todo_item(entity_id: str, summary: str) -> None:
    """POST todo/remove_item - removes an item from a to-do list by its summary
    text (the local_todo integration resolves summary to uid internally)."""
    log.info("POST %s/api/services/todo/remove_item entity_id=%s item=%r", _url(), entity_id, summary)
    resp = requests.post(
        f"{_url()}/api/services/todo/remove_item",
        headers=_headers(),
        json={"entity_id": entity_id, "item": summary},
        timeout=REQUEST_TIMEOUT,
    )
    log.info("  -> %d: %s", resp.status_code, resp.text[:300])
    resp.raise_for_status()


def notify(service: str, message: str, title: str | None = None, data: dict | None = None) -> None:
    """POST /api/services/notify/<service>."""
    payload = {"message": message}
    if title:
        payload["title"] = title
    if data:
        payload["data"] = data
    log.info("POST %s/api/services/notify/%s payload=%s", _url(), service, payload)
    resp = requests.post(
        f"{_url()}/api/services/notify/{service}",
        headers=_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    log.info("  -> %d: %s", resp.status_code, resp.text[:300])
    resp.raise_for_status()
