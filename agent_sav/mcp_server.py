"""The support tools as an MCP server, for other agents.

The guarantees are those of the chat: the bearer token says who the customer is, the rules decide, and the
customer agrees before anything is written. The agreement is asked through elicitation, so it comes from the
person and not from the calling model; a client without elicitation can read, but not act.
"""

from typing import Annotated, Literal
from uuid import uuid4

from mcp.server import MCPServer
from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from pydantic import BaseModel, Field, ValidationError

from . import tools as toolbox
from .service import Support
from .tools import Reason


class Agreement(BaseModel):
    confirm: bool = Field(description="Confirmer cette demande")


def bearer(header: str | None) -> str:
    scheme, _, token = (header or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


class BearerAuth:
    """Rejects requests whose token is not a customer's, before they reach the MCP server."""

    def __init__(self, app, support: Support):
        self.app, self.support = app, support

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            token = bearer(headers.get(b"authorization", b"").decode("latin-1"))
            if self.support.shop.token_customer(token) is None:
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401, "headers": [
                    (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="agent-sav"'),
                ]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_server(support: Support) -> MCPServer:
    server = MCPServer("agent-sav", instructions=(
        "Service client de Maison Orme, pour le client identifié par le jeton. Commence par list_orders. "
        "Les outils propose_* et cancel_case appliquent les règles de la boutique, puis demandent l’accord du client."
    ))

    def customer(ctx: Context) -> str:
        found = support.shop.token_customer(bearer((ctx.headers or {}).get("authorization")))
        if found is None:
            raise ToolError("Authentification requise.")
        return found

    def read(ctx: Context, name: str, **args) -> str:
        return toolbox.read(name, args, customer(ctx), support.shop, support.cases)

    def checked(name: str, args: dict) -> dict:
        try:
            return toolbox.ARGS[name].model_validate(args).model_dump()
        except ValidationError as error:
            raise ToolError("Arguments invalides : " + "; ".join(each["msg"] for each in error.errors())) from None

    def ask(ctx: Context, name: str, args: dict) -> Elicit[Agreement]:
        """Applies the rules, then asks the customer."""
        verdict, summary = support.cases.propose(customer(ctx), name, checked(name, args))
        if verdict.outcome == "refused":
            raise ToolError(f"Refusé par les règles ({verdict.rule}). {verdict.message}")
        return Elicit(f"{summary}\n\nConfirmez-vous cette demande ?", Agreement)

    def act(ctx: Context, name: str, args: dict, agreement: ElicitationResult[Agreement] | None) -> str:
        if agreement and not (isinstance(agreement, AcceptedElicitation) and agreement.data.confirm):
            return "Le client n’a pas confirmé : rien n’a été enregistré."
        customer_id, args, key = customer(ctx), checked(name, args), f"mcp:{uuid4()}"
        if name == "cancel_case":
            support.cases.add_action(args["case_id"], "cancel", key)
            case_id = args["case_id"]
        else:
            case, verdict = support.cases.open(key, customer_id, name, args)
            if case is None:  # things have changed since the summary
                raise ToolError(f"Refusé par les règles ({verdict.rule}). {verdict.message}")
            case_id = case["id"]
        support.run_cases()
        return toolbox.dump({"case_id": case_id, "status": support.cases.get(case_id)["status"]})

    @server.tool(description=toolbox.READ_TOOLS["list_orders"][1])
    def list_orders(ctx: Context) -> str:
        return read(ctx, "list_orders")

    @server.tool(description=toolbox.READ_TOOLS["check_stock"][1])
    def check_stock(item_id: str, ctx: Context) -> str:
        return read(ctx, "check_stock", item_id=item_id)

    @server.tool(description=toolbox.READ_TOOLS["list_cases"][1])
    def list_cases(ctx: Context) -> str:
        return read(ctx, "list_cases")

    @server.tool(description=toolbox.READ_TOOLS["read_policy"][1])
    def read_policy(ctx: Context) -> str:
        return read(ctx, "read_policy")

    def ask_return(ctx: Context, item_id: str, reason: Reason, details: str) -> Elicit[Agreement]:
        return ask(ctx, "propose_return", {"item_id": item_id, "reason": reason, "details": details})

    @server.tool(description=toolbox.ACTION_TOOLS["propose_return"][1])
    def propose_return(
        item_id: str, reason: Reason, ctx: Context,
        agreement: Annotated[ElicitationResult[Agreement], Resolve(ask_return)], details: str = "",
    ) -> str:
        return act(ctx, "propose_return", {"item_id": item_id, "reason": reason, "details": details}, agreement)

    def ask_exchange(
        ctx: Context, item_id: str, reason: Reason, size: Literal["S", "M", "L", "XL"] | None, color: str | None, details: str,
    ) -> Elicit[Agreement]:
        return ask(ctx, "propose_exchange",
                   {"item_id": item_id, "reason": reason, "size": size, "color": color, "details": details})

    @server.tool(description=toolbox.ACTION_TOOLS["propose_exchange"][1])
    def propose_exchange(
        item_id: str, reason: Reason, ctx: Context,
        agreement: Annotated[ElicitationResult[Agreement], Resolve(ask_exchange)],
        size: Literal["S", "M", "L", "XL"] | None = None, color: str | None = None, details: str = "",
    ) -> str:
        return act(ctx, "propose_exchange",
                   {"item_id": item_id, "reason": reason, "size": size, "color": color, "details": details}, agreement)

    def ask_refund(
        ctx: Context, order_id: str, reason: Literal["lost_parcel", "goodwill"], amount_euros: float | None, details: str,
    ) -> Elicit[Agreement]:
        return ask(ctx, "propose_refund",
                   {"order_id": order_id, "reason": reason, "amount_euros": amount_euros, "details": details})

    @server.tool(description=toolbox.ACTION_TOOLS["propose_refund"][1])
    def propose_refund(
        order_id: str, reason: Literal["lost_parcel", "goodwill"], ctx: Context,
        agreement: Annotated[ElicitationResult[Agreement], Resolve(ask_refund)],
        amount_euros: float | None = None, details: str = "",
    ) -> str:
        return act(ctx, "propose_refund",
                   {"order_id": order_id, "reason": reason, "amount_euros": amount_euros, "details": details}, agreement)

    def ask_cancel(ctx: Context, case_id: str) -> Elicit[Agreement]:
        return ask(ctx, "cancel_case", {"case_id": case_id})

    @server.tool(description=toolbox.ACTION_TOOLS["cancel_case"][1])
    def cancel_case(
        case_id: str, ctx: Context, agreement: Annotated[ElicitationResult[Agreement], Resolve(ask_cancel)],
    ) -> str:
        return act(ctx, "cancel_case", {"case_id": case_id}, agreement)

    @server.tool(description=toolbox.ACTION_TOOLS["escalate"][1])
    def escalate(question: str, ctx: Context) -> str:
        return act(ctx, "escalate", {"question": question}, None)

    return server


def build_app(support: Support):
    """Only reachable from this machine: put a reverse proxy in front to publish it."""
    return BearerAuth(build_server(support).streamable_http_app(stateless_http=False), support)
