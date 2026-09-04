"""
"Tasks" the bot can perform: booking an appointment, checking an order,
starting a return.

These are the actions Claude can trigger via tool use (function calling).
Every function here is a mock — in-memory fake data, no real database or
calendar — so the POC runs with zero external dependencies beyond the two
AI APIs. Swap a function's body for a real API/database call when you're
ready to go past the POC stage; TOOL_SCHEMAS (what Claude sees) doesn't
need to change at all.

SECURITY NOTE before you connect real systems: check_order_status() and
process_return_request() below act on any order_id with no check that the
customer asking is actually who the order belongs to — anyone chatting
with the bot can look up or start a return for ANY order if they can
guess or already know its ID (and IDs like "A1001" are easy to guess in
sequence). That's harmless against this fake data, but it's a real
data-exposure bug the moment order_id maps to a real order. Before wiring
these to a real system, verify identity first — e.g. also require an
email or phone number on the order and check it matches what the
customer gives you, the same way a human agent would ask "can you
confirm the email on the order?" before discussing it.
"""

import random
import string
from datetime import datetime, timezone

# --- Fake data, just for the POC -------------------------------------------

_FAKE_ORDERS = {
    "A1001": {
        "status": "Shipped",
        "eta": "Aug 24, 2026",
        "items": ["Ceramic Table Vase — Sand"],
    },
    "A1002": {
        "status": "Processing",
        "eta": "Aug 27, 2026",
        "items": ["Waffle-Weave Throw — Charcoal"],
    },
    "A1003": {
        "status": "Delivered",
        "eta": "Aug 15, 2026",
        "items": ["Rift Oak Coffee Table — Natural"],
    },
}

_booked_appointments = []  # populated at runtime, resets on restart


def _new_reference(prefix: str) -> str:
    return prefix + "".join(random.choices(string.digits, k=6))


# --- Tool implementations ---------------------------------------------------
# check_order_status() and process_return_request() have no identity check
# by design — this is fake data with nothing to protect. See the SECURITY
# NOTE at the top of this file before connecting either one to a real
# system: add identity verification here first.

def check_order_status(order_id: str) -> dict:
    order = _FAKE_ORDERS.get(order_id.strip().upper())
    if not order:
        return {"found": False, "message": f"No order found with ID {order_id}."}
    return {"found": True, "order_id": order_id.strip().upper(), **order}


def book_appointment(customer_name: str, date: str, time: str, service_type: str) -> dict:
    confirmation_id = _new_reference("APT-")
    _booked_appointments.append(
        {
            "id": confirmation_id,
            "customer_name": customer_name,
            "date": date,
            "time": time,
            "service_type": service_type,
            "booked_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return {
        "confirmed": True,
        "confirmation_id": confirmation_id,
        "message": (
            f"Booked {service_type} for {customer_name} on {date} at {time}. "
            f"Confirmation: {confirmation_id}."
        ),
    }


def process_return_request(order_id: str, reason: str) -> dict:
    order = _FAKE_ORDERS.get(order_id.strip().upper())
    if not order:
        return {"accepted": False, "message": f"No order found with ID {order_id}."}
    rma = _new_reference("RMA-")
    return {
        "accepted": True,
        "rma_number": rma,
        "message": (
            f"Return started for order {order_id.strip().upper()}. RMA {rma} — "
            "a prepaid shipping label will be emailed within 24 hours."
        ),
    }


# --- Tool schemas passed to Claude ------------------------------------------
# See https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview

TOOL_SCHEMAS = [
    {
        "name": "check_order_status",
        "description": (
            "Look up the shipping or processing status of a customer's order "
            "by order ID. Use this whenever a customer asks where an order "
            "is or when it will arrive."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order ID, e.g. 'A1001'.",
                }
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "book_appointment",
        "description": (
            "Book a customer service appointment, such as an in-home design "
            "consultation or an in-store pickup slot. Confirm the details "
            "with the customer before calling this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format.",
                },
                "time": {
                    "type": "string",
                    "description": "Time of day, e.g. '3:00 PM'.",
                },
                "service_type": {
                    "type": "string",
                    "description": "What the appointment is for, e.g. 'design consultation'.",
                },
            },
            "required": ["customer_name", "date", "time", "service_type"],
        },
    },
    {
        "name": "process_return_request",
        "description": (
            "Start a return for an order and issue a return authorization "
            "(RMA) number. Confirm the order ID and reason with the "
            "customer before calling this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["order_id", "reason"],
        },
    },
]

TOOL_FUNCTIONS = {
    "check_order_status": check_order_status,
    "book_appointment": book_appointment,
    "process_return_request": process_return_request,
}
