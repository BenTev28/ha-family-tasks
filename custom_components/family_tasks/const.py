"""Constants for family_tasks."""

from __future__ import annotations

DOMAIN = "family_tasks"
VERSION = "0.1.0"
PRODID = f"-//Home Assistant//family_tasks {VERSION}//EN"

CONF_LISTS = "lists"
CONF_ASSIGNEES = "assignees"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_COMPLETED_DAYS = "completed_days"

ATTR_PERSON = "person"
ATTR_TAG = "tag"
ATTR_COLOR = "color"

DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 30
DEFAULT_COMPLETED_DAYS = 7

EVENT_TASK_ADDED = f"{DOMAIN}_task_added"
EVENT_TASK_COMPLETED = f"{DOMAIN}_task_completed"
EVENT_TASK_ASSIGNED = f"{DOMAIN}_task_assigned"

SIGNAL_UPDATED = f"{DOMAIN}_updated"

FRONTEND_URL = f"/{DOMAIN}"
CARD_FILENAME = "family-tasks-card.js"

DEFAULT_COLORS = [
    "#1e88e5",
    "#d81b60",
    "#43a047",
    "#fb8c00",
    "#8e24aa",
    "#00897b",
    "#f4511e",
    "#3949ab",
]
