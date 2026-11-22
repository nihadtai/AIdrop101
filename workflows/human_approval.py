"""
Human-in-the-Loop Approval Manager
====================================

Provides helper utilities for sending approval requests via
Telegram / Slack and recording decisions in the graph state.

Used by the pipeline's interrupt-based approval nodes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from state.graph_state import ECommerceState
from state.models import HumanApproval

logger = logging.getLogger(__name__)


class HumanApprovalManager:
    """
    Manages human-in-the-loop approval workflows.

    This class is responsible for:
      1. Formatting approval-request messages.
      2. Sending them via Telegram / Slack.
      3. Recording the decision back into graph state.
    """

    def __init__(
        self,
        telegram_bot_token: str | None = None,
        telegram_chat_id: str | None = None,
        slack_webhook_url: str | None = None,
    ):
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.slack_webhook_url = slack_webhook_url

    # ── Public API ────────────────────────────────────────────────────────

    async def request_approval(
        self,
        step_name: str,
        description: str,
        data: Dict[str, Any],
        state: ECommerceState,
    ) -> dict:
        """
        Create a pending-approval entry and notify the human operator.

        Returns a partial state update with ``pending_approval`` set.
        """
        approval = HumanApproval(
            step_name=step_name,
            description=description,
            data=data,
            requested_at=datetime.now(timezone.utc),
        )

        # Fire-and-forget notification (best-effort)
        await self._notify(step_name, description, data)

        return {
            "pending_approval": approval.model_dump(),
            "logs": state.get("logs", []) + [
                f"[HumanApproval] Approval requested for '{step_name}': {description}"
            ],
        }

    async def record_decision(
        self,
        approved: bool,
        feedback: str,
        state: ECommerceState,
    ) -> dict:
        """
        Record the human's decision and clear the pending-approval slot.
        """
        pending = state.get("pending_approval")
        if pending is None:
            logger.warning("record_decision called but no pending approval found.")
            return {}

        pending["approved"] = approved
        pending["feedback"] = feedback
        pending["responded_at"] = datetime.now(timezone.utc).isoformat()

        approvals = state.get("human_approvals", [])
        approvals.append(pending)

        decision_label = "✅ Approved" if approved else "❌ Rejected"
        return {
            "pending_approval": None,
            "human_approvals": approvals,
            "logs": state.get("logs", []) + [
                f"[HumanApproval] {decision_label} — {feedback}"
            ],
        }

    # ── Private helpers ───────────────────────────────────────────────────

    async def _notify(
        self,
        step_name: str,
        description: str,
        data: Dict[str, Any],
    ) -> None:
        """Send notification via available channels."""
        message = (
            f"🔔 *Approval Required*\n"
            f"*Step:* {step_name}\n"
            f"*Description:* {description}\n"
        )

        if self.telegram_bot_token and self.telegram_chat_id:
            await self._send_telegram(message)

        if self.slack_webhook_url:
            await self._send_slack(message)

    async def _send_telegram(self, message: str) -> None:
        """Send a message via Telegram Bot API."""
        try:
            import httpx

            url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            async with httpx.AsyncClient() as client:
                await client.post(url, json={
                    "chat_id": self.telegram_chat_id,
                    "text": message,
                    "parse_mode": "Markdown",
                })
            logger.info("Telegram notification sent.")
        except Exception as e:
            logger.warning(f"Failed to send Telegram notification: {e}")

    async def _send_slack(self, message: str) -> None:
        """Send a message via Slack Incoming Webhook."""
        try:
            import httpx

            async with httpx.AsyncClient() as client:
                await client.post(self.slack_webhook_url, json={"text": message})
            logger.info("Slack notification sent.")
        except Exception as e:
            logger.warning(f"Failed to send Slack notification: {e}")
