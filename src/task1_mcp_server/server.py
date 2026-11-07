"""Task 1 MCP server: two tools, strict validation, stdio transport.

Built on the MCP Python SDK v2 low-level ``Server``. In v2 the low-level server
advertises a tool's input schema but does not enforce it, so this module validates
every call itself with Pydantic and raises ``MCPError(INVALID_PARAMS, ...)``. The
SDK passes ``MCPError`` through as a JSON-RPC error response (code -32602) instead
of converting it to an ``isError`` tool result.

Error mapping
-------------
* unknown tool name                        -> JSON-RPC error -32602
* malformed / invalid / injected arguments -> JSON-RPC error -32602 (details in error.data)
* well-formed customer_id with no record   -> tool result with isError=true (business outcome)
* unexpected exception in a handler        -> SDK-sanitized JSON-RPC error -32603

STDIO isolation
---------------
* No code in this package writes to stdout. Logging is configured onto stderr.
* SDK v2's ``stdio_server`` additionally points fd 1 at stderr while serving and
  keeps the protocol on a private descriptor, so stray ``print`` calls, raw fd 1
  writes, and child-process output cannot reach the JSON-RPC stream.
* ``sys.stdout`` is deliberately NOT reassigned: the transport claims the real
  descriptor itself, and swapping the Python object could misdirect it. Instead
  ``__main__`` makes it line-buffered and flushes it before the transport restores
  fd 1, so buffered stray output also ends up on stderr.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from mcp import MCPError
from mcp.server import Server, ServerRequestContext
from mcp.types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from . import config as cfg
from .data import Store, default_store
from .models import (
    CUSTOMER_ID_PATTERN,
    REASON_MIN_LENGTH,
    GetCustomerRecordArgs,
    TriggerRefundArgs,
    amount_to_decimal,
    validation_error_data,
)
from .sanitizer import Sanitizer

SERVER_NAME = "quilr-task1-customer-service"
SERVER_VERSION = "1.0.0"

log = logging.getLogger("task1.server")

_CUSTOMER_ID_SCHEMA = {
    "type": "string",
    "pattern": CUSTOMER_ID_PATTERN,
    "description": "Customer identifier: CUST- followed by 5 uppercase letters or digits, e.g. CUST-A1B2C.",
}

TOOLS: list[Tool] = [
    Tool(
        name="get_customer_record",
        description="Fetch a customer record by customer_id.",
        input_schema={
            "type": "object",
            "properties": {"customer_id": _CUSTOMER_ID_SCHEMA},
            "required": ["customer_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="trigger_refund",
        description=(
            "Queue a refund for a customer. reason is measured after sanitization "
            f"(whitespace normalization) and must be at least {REASON_MIN_LENGTH} characters."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "customer_id": _CUSTOMER_ID_SCHEMA,
                "amount": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "multipleOf": 0.01,
                    "description": "Refund amount, greater than 0, at most 2 decimal places.",
                },
                "reason": {"type": "string", "minLength": REASON_MIN_LENGTH},
            },
            "required": ["customer_id", "amount", "reason"],
            "additionalProperties": False,
        },
    ),
]


def _json_result(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, sort_keys=True))],
        structured_content=payload,
    )


def _not_found(customer_id: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=f"No customer record exists for {customer_id}.")],
        is_error=True,
    )


def _fault_inject() -> None:
    """Test hook: try every common way of polluting stdout. Never enable in production."""
    print("FAULT-INJECTION: print() to stdout")
    os.write(1, b"FAULT-INJECTION: raw write to fd 1\n")
    subprocess.run(
        [sys.executable, "-c", "print('FAULT-INJECTION: child process stdout')"],
        check=False,
    )


class CustomerService:
    def __init__(self, store: Store | None = None, sanitizer: Sanitizer | None = None,
                 fault_injection: bool = False) -> None:
        self.store = store or default_store()
        self.sanitizer = sanitizer or Sanitizer()
        self.fault_injection = fault_injection
        self._handlers: dict[str, tuple[type[BaseModel], Callable[[Any], CallToolResult]]] = {
            "get_customer_record": (GetCustomerRecordArgs, self._get_customer_record),
            "trigger_refund": (TriggerRefundArgs, self._trigger_refund),
        }

    # -- protocol handlers -------------------------------------------------------------

    async def list_tools(self, ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=TOOLS)

    async def call_tool(self, ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        entry = self._handlers.get(params.name)
        if entry is None:
            log.warning("rejected call: unknown tool %r", params.name[:64])
            raise MCPError(INVALID_PARAMS, f"Unknown tool: {params.name[:64]!r}")
        model_cls, handler = entry
        arguments = params.arguments if params.arguments is not None else {}
        try:
            args = model_cls.model_validate(arguments, context={"sanitizer": self.sanitizer})
        except ValidationError as exc:
            details = validation_error_data(exc)
            log.warning("rejected call: tool=%s invalid arguments %s",
                        params.name, [(d["field"], d["type"]) for d in details])
            raise MCPError(INVALID_PARAMS, f"Invalid arguments for tool '{params.name}'", {"errors": details}) from None
        if self.fault_injection:
            _fault_inject()
        return handler(args)

    # -- tool implementations -------------------------------------------------------------

    def _get_customer_record(self, args: GetCustomerRecordArgs) -> CallToolResult:
        customer = self.store.get(args.customer_id)
        if customer is None:
            log.info("get_customer_record: %s not found", args.customer_id)
            return _not_found(args.customer_id)
        log.info("get_customer_record: %s found", args.customer_id)
        return _json_result({
            "customer_id": customer.customer_id,
            "name": customer.name,
            "email": customer.email,
            "tier": customer.tier,
            "currency": customer.currency,
        })

    def _trigger_refund(self, args: TriggerRefundArgs) -> CallToolResult:
        customer = self.store.get(args.customer_id)
        if customer is None:
            log.info("trigger_refund: %s not found", args.customer_id)
            return _not_found(args.customer_id)
        refund = self.store.add_refund(customer, amount_to_decimal(args.amount), args.reason)
        log.info("trigger_refund: %s queued for %s", refund.refund_id, refund.customer_id)
        return _json_result({
            "refund_id": refund.refund_id,
            "customer_id": refund.customer_id,
            "amount": str(refund.amount),
            "currency": refund.currency,
            "reason": refund.reason,
            "status": refund.status,
        })


def build_server(service: CustomerService | None = None) -> Server:
    svc = service or CustomerService()
    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=svc.list_tools,
        on_call_tool=svc.call_tool,
    )


def build_from_environment() -> Server:
    service = CustomerService(
        sanitizer=Sanitizer(cfg.load_sanitizer_config()),
        fault_injection=cfg.fault_injection_enabled(),
    )
    if service.fault_injection:
        log.warning("FAULT INJECTION ENABLED (%s=1): test use only", cfg.ENV_FAULT_INJECT)
    return build_server(service)
