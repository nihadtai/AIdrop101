# Notifications and Digests

`tools/notification_tools.py` sends Telegram and Slack messages, builds radar email HTML, manages subscribers, and dispatches daily digests. It also has a generic human-approval notification helper, while graph interruption remains the actual approval control mechanism.

Email recipient/subscriber data is operational data. Validate addresses, avoid logging addresses unnecessarily, and use provider-safe test paths before sending broad notifications.

Related: `integrations/discovery.md`, `web/operations-api.md`, `governance/security.md`.
