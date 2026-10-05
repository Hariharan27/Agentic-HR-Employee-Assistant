"""Parking Agent: the shared tool loop configured with the employee parking tools."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.agent.parking_tools import ParkingToolExecutor
from app.agent.runtime import AgentRunState, ToolAgent
from app.core.config import Settings
from app.llm.ports import LLMGateway

PARKING_AGENT_BASE_PROMPT = """You are the tool-calling Parking Agent for an employee workplace assistant.
Tool results are the only source of truth for dates, slots, availability, vehicles and bookings.
The authenticated employee is injected by the application; never put identity in arguments.

- If no parking date was given in this conversation, reply "Please provide the parking date."
- Turn date words into dates with resolve_dates (copy the user's words). Never compute dates.
- When the user asks what is available, or asks to book without naming a slot, call
  list_parking_slots and list EVERY slot for the date(s) with its status (free or taken), noting
  accessible slots, then ask which slot they want. Never choose a slot for the user.
- When the user names a slot (e.g. "B-22"), call build_parking_plan with that slot (and the dates
  if they changed). If it is ready, call prepare_parking with its plan_id in the same turn.
  If the chosen slot is taken on some dates, tell them which dates and the free slots there, and
  ask whether to use another slot or the waitlist on those dates; rebuild with alternatives only
  after they choose.
- Cancelling: prepare_parking_cancellation for the date they name.
Prepare tools only create a pending confirmation; never claim a booking was made. Keep replies
short; you may use **bold** and "- " bullets; no headings or tables. Do not mention tools, plan
ids or JSON.
"""


class ParkingAgent(ToolAgent):
    active_plan_label = "Current parking dates or plan"
    agent_name = "parking_agent"

    def __init__(self, *, settings: Settings, llm: LLMGateway, tools: ParkingToolExecutor) -> None:
        super().__init__(settings=settings, llm=llm, tools=tools, base_prompt=PARKING_AGENT_BASE_PROMPT)

    def today(self) -> date:
        return self.tools.today()

    def describe_active_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {key: plan.get(key) for key in ("plan_id", "dates", "slot_code", "eligible", "summary") if plan.get(key)}

    @staticmethod
    def board_text(data: dict[str, Any]) -> str:
        blocks = []
        for day in data.get("days", []):
            heading = f"{day['date']} ({day['weekday']})"
            if day.get("unavailable_reason"):
                blocks.append(f"{heading}: {day['unavailable_reason']}")
                continue
            lines = [
                f"- {slot['slot']}{' (accessible)' if slot['type'] == 'ACCESSIBLE' else ''}: {slot['status']}"
                for slot in day.get("slots", [])
            ]
            mine = f"\nYou already have slot {day['your_reservation']}." if day.get("your_reservation") else ""
            blocks.append(f"Parking slots on {heading}:\n" + "\n".join(lines) + mine)
        return "\n\n".join(blocks) + "\n\nWhich slot would you like?"

    def render_from_results(self, state: AgentRunState) -> str | None:
        for item in reversed(state.get("tool_results", [])):
            tool, data, ok = item.get("tool"), item.get("result", {}), item.get("status") == "success"
            if not ok:
                return self.friendly_failure(str(tool), data)
            if tool == "list_parking_slots":
                return self.board_text(data)
            if tool == "build_parking_plan" and data.get("summary"):
                return str(data["summary"])
            if tool == "resolve_dates" and data.get("question"):
                return str(data["question"])
        return None

    def finalize(self, state: AgentRunState, message: str) -> AgentRunState | None:
        results = state.get("tool_results", [])
        if not results or any(item.get("tool") == "prepare_parking" for item in results):
            return None
        last = results[-1]
        if last.get("status") != "success":
            return None
        if last.get("tool") == "build_parking_plan" and last["result"].get("eligible") and last["result"].get("plan_id"):
            prepared = self.execute_inline(state, "prepare_parking", {"plan_id": last["result"]["plan_id"]})
            if prepared.get("response"):
                return prepared
            return {**prepared, "response": self.render_from_results(prepared)}
        if last.get("tool") == "list_parking_slots":
            # Every slot is always listed exactly, so the employee can choose.
            return {"response": self.board_text(last["result"])}
        return None

    @staticmethod
    def friendly_failure(tool: str, data: dict[str, Any]) -> str:
        details = data.get("reason") or data.get("error")
        return f"I could not complete that parking step: {details}." if details else "I could not complete that parking step."
