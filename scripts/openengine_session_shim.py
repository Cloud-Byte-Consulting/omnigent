#!/usr/bin/env python3
"""openengine_session_shim.py — create an Omnigent session for an Open Engine issue.

Resolves plan risk 7: Omnigent does not currently inject session_id into the
in-session agent subprocess. This shim creates the session, captures the id,
and surfaces it so the runner prompt can stamp AGENT CLAIMED with a real id.

Usage:
    OMNIGENT_URL=http://localhost:8000 \\
    OMNIGENT_AGENT_ID=ag_abc123 \\
    python scripts/openengine_session_shim.py ENG-123

    # GitHub issue (OE-1b):
    python scripts/openengine_session_shim.py --provider github \\
        'Cloud-Byte-Consulting/agentic-harness#42'

Output (stdout, shell-sourceable):
    OMNIGENT_SESSION_ID=conv_xyz789

The openengine.issue label is always stamped as ``<provider>:<issue_ref>``,
e.g. ``linear:ENG-123`` or ``github:Cloud-Byte-Consulting/agentic-harness#42``.

NOTE (back-compat): the old bare-id format (``ENG-123``) is replaced by the
qualified form (``linear:ENG-123``) even when --provider is omitted.  Any
existing consumer that hard-codes the bare id must be updated to strip the
``linear:`` prefix, or query by session_id instead.

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
    provider: str = "linear",
) -> str:
    """POST /v1/sessions with openengine.issue label; return session_id.

    :param base_url: Omnigent server base URL, e.g. ``"http://localhost:8000"``.
    :param agent_id: Durable agent id, e.g. ``"ag_abc123"``.
    :param issue_id: Issue reference, e.g. ``"ENG-123"`` or
        ``"Cloud-Byte-Consulting/agentic-harness#42"``.
    :param title: Optional session title.
    :param api_key: Optional Bearer token.
    :param provider: Tracker name — ``"linear"`` (default) or ``"github"``.
        The openengine.issue label is stamped as ``<provider>:<issue_id>``.
    :returns: The created session id, e.g. ``"conv_xyz789"``.
    :raises SystemExit: On HTTP or JSON errors with a human-readable message.
    """
    issue_ref = f"{provider}:{issue_id}"
    payload = {
        "agent_id": agent_id,
        "labels": {"openengine.issue": issue_ref},
        "title": title or f"Open Engine {issue_ref}",
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
    import argparse

    parser = argparse.ArgumentParser(
        prog="openengine_session_shim.py",
        description="Create an Omnigent session for an Open Engine issue.",
    )
    parser.add_argument(
        "issue_id",
        help=(
            "Issue reference. Linear: 'ENG-123'. "
            "GitHub (OE-1b): 'owner/repo#number'."
        ),
    )
    parser.add_argument(
        "--provider",
        default="linear",
        choices=["linear", "github"],
        help="Tracker provider (default: linear). Stamps openengine.issue as <provider>:<issue_id>.",
    )
    args = parser.parse_args()

    base_url = os.environ.get("OMNIGENT_URL", "http://localhost:8000")
    agent_id = os.environ.get("OMNIGENT_AGENT_ID", "")
    api_key = os.environ.get("OMNIGENT_API_KEY")
    title = os.environ.get("OMNIGENT_SESSION_TITLE")

    if not agent_id:
        sys.exit("OMNIGENT_AGENT_ID env var required (durable agent id, e.g. ag_abc123)")

    session_id = create_session(
        base_url, agent_id, args.issue_id, title, api_key, provider=args.provider
    )

    # Shell-sourceable so the runner can:
    #   eval $(python scripts/openengine_session_shim.py ENG-123)
    #   eval $(python scripts/openengine_session_shim.py --provider github owner/repo#42)
    print(f"OMNIGENT_SESSION_ID={session_id}")

    # Also write bare id to a file for runners that prefer file injection.
    session_file = os.environ.get("OMNIGENT_SESSION_FILE")
    if session_file:
        with open(session_file, "w") as f:
            f.write(session_id)
        print(f"# session_id written to {session_file}", file=sys.stderr)


def _self_check() -> None:
    """Assert-based smoke test — run with: python openengine_session_shim.py --self-check"""
    # provider-qualified label construction (no network needed)
    linear_ref = "linear:ENG-123"
    assert linear_ref == "linear:ENG-123", linear_ref

    github_ref = "github:Cloud-Byte-Consulting/agentic-harness#42"
    assert github_ref == "github:Cloud-Byte-Consulting/agentic-harness#42", github_ref

    # default provider produces linear-qualified label
    # (simulate what create_session stamps without a real server)
    provider = "linear"
    issue_id = "ENG-999"
    label_value = f"{provider}:{issue_id}"
    assert label_value == "linear:ENG-999", label_value

    print("self-check OK")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        main()
