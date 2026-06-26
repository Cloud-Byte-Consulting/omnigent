#!/usr/bin/env python3
"""openengine_session_shim.py — create an Omnigent session for an Open Engine issue.

Resolves plan risk 7: Omnigent does not currently inject session_id into the
in-session agent subprocess. This shim creates the session, captures the id,
and surfaces it so the runner prompt can stamp AGENT CLAIMED with a real id.

Usage:
    OMNIGENT_URL=http://localhost:8000 \\
    OMNIGENT_AGENT_ID=ag_abc123 \\
    python scripts/openengine_session_shim.py ENG-123

Output (stdout, shell-sourceable):
    OMNIGENT_SESSION_ID=conv_xyz789

Optional env vars:
    OMNIGENT_API_KEY    Bearer token for authenticated deployments.
    OMNIGENT_SESSION_TITLE  Human-readable title (default: "Open Engine ENG-123").
    OMNIGENT_SESSION_FILE   If set, also writes the bare session_id to this path
                            so an in-session runner can read it without parsing.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error


def create_session(
    base_url: str,
    agent_id: str,
    issue_id: str,
    title: str | None = None,
    api_key: str | None = None,
) -> str:
    """POST /v1/sessions with openengine.issue label; return session_id.

    :param base_url: Omnigent server base URL, e.g. ``"http://localhost:8000"``.
    :param agent_id: Durable agent id, e.g. ``"ag_abc123"``.
    :param issue_id: Linear issue id, e.g. ``"ENG-123"``.
    :param title: Optional session title.
    :param api_key: Optional Bearer token.
    :returns: The created session id, e.g. ``"conv_xyz789"``.
    :raises SystemExit: On HTTP or JSON errors with a human-readable message.
    """
    payload = {
        "agent_id": agent_id,
        "labels": {"openengine.issue": issue_id},
        "title": title or f"Open Engine {issue_id}",
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/sessions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        sys.exit(f"POST /v1/sessions failed {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"Cannot reach Omnigent at {base_url}: {exc.reason}")

    session_id: str = body.get("id", "")
    if not session_id:
        sys.exit(f"Response missing 'id': {body}")
    return session_id


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(
            "Usage: openengine_session_shim.py <LINEAR_ISSUE_ID>\n"
            "  e.g. openengine_session_shim.py ENG-123"
        )
    issue_id = sys.argv[1]

    base_url = os.environ.get("OMNIGENT_URL", "http://localhost:8000")
    agent_id = os.environ.get("OMNIGENT_AGENT_ID", "")
    api_key = os.environ.get("OMNIGENT_API_KEY")
    title = os.environ.get("OMNIGENT_SESSION_TITLE")

    if not agent_id:
        sys.exit("OMNIGENT_AGENT_ID env var required (durable agent id, e.g. ag_abc123)")

    session_id = create_session(base_url, agent_id, issue_id, title, api_key)

    # Shell-sourceable so the runner can: eval $(python scripts/openengine_session_shim.py ENG-123)
    print(f"OMNIGENT_SESSION_ID={session_id}")

    # Also write bare id to a file for runners that prefer file injection.
    session_file = os.environ.get("OMNIGENT_SESSION_FILE")
    if session_file:
        with open(session_file, "w") as f:
            f.write(session_id)
        print(f"# session_id written to {session_file}", file=sys.stderr)


if __name__ == "__main__":
    main()
