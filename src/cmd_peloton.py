"""The 'peloton' subcommand: log a profile in to Peloton for class search.

The morning briefing searches Peloton's on-demand catalog only for a profile
that is logged in. Logging in asks for the password once, sends it to Peloton
and keeps only the tokens Peloton returns.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
import time
from pathlib import Path

from peloton import PelotonAuthError, PelotonError, access_token, login, revoke

logger = logging.getLogger(__name__)

_TOKEN_IGNORE_ENTRY = "/peloton_tokens.json"


def _ensure_tokens_gitignored(profile_root: Path) -> None:
    """Keep the token file out of a state directory kept under git."""
    path = profile_root / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if _TOKEN_IGNORE_ENTRY in {line.strip() for line in existing.splitlines()}:
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{separator}# Peloton login tokens\n{_TOKEN_IGNORE_ENTRY}\n")


def _tokens_path(args: argparse.Namespace) -> Path:
    """Return the resolved profile's token path, or exit with guidance."""
    profile = getattr(args, "profile_config", None)
    if profile is None:
        logger.error("Peloton login needs a profile. Pass --profile NAME.")
        sys.exit(1)
    return profile.peloton_tokens


def cmd_peloton(args: argparse.Namespace) -> None:
    """Handle 'peloton login', 'peloton status' and 'peloton logout'.

    Args:
        args: Parsed CLI arguments with ``peloton_cmd`` and the resolved
            ``profile_config``.
    """
    path = _tokens_path(args)
    if args.peloton_cmd == "login":
        username = input("Peloton username or email: ").strip()
        password = getpass.getpass("Peloton password (sent to Peloton, not stored): ")
        try:
            login(path, username, password)
        except PelotonError as exc:
            logger.error("%s. Check the credentials and try again.", exc)
            sys.exit(1)
        finally:
            del password
        _ensure_tokens_gitignored(path.parent)
        print(f"Logged in. Tokens saved to {path}; the password was not stored.")
    elif args.peloton_cmd == "status":
        if not path.is_file():
            print("Not logged in. Run 'peloton login' to enable class search.")
            return
        try:
            access_token(path)
        except PelotonAuthError as exc:
            print(f"Login no longer valid ({exc}). Run 'peloton login' again.")
            sys.exit(1)
        except PelotonError as exc:
            print(f"Logged in, but Peloton is unreachable: {exc}")
            sys.exit(1)
        expires_at = json.loads(path.read_text(encoding="utf-8"))["expires_at"]
        hours = max(0.0, (float(expires_at) - time.time()) / 3600)
        print(f"Logged in; the access token renews automatically ({hours:.0f}h left).")
    elif args.peloton_cmd == "logout":
        if revoke(path):
            print(f"Logged out; deleted {path}.")
        else:
            print("Not logged in.")
