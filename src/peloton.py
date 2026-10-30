"""Peloton on-demand class search for the morning briefing.

Peloton has no public API. This uses the endpoints its own web app calls, as
established by the issue #1 spike: the OAuth password grant to log in once,
the refresh grant to stay logged in, and the on-demand catalog search with
server-side filters. The password is never stored; only tokens are, in the
profile's ``peloton_tokens.json`` (mode 0600).

Catalog searches always exclude classes the account has already taken
(``has_workout=false``), so the briefing never re-suggests a class.

Public API:
    PelotonError           — Peloton refused or could not be reached
    PelotonAuthError       — stored login is missing, expired or revoked
    login                  — exchange a username and password for tokens
    revoke                 — revoke the stored refresh token and delete it
    has_tokens             — whether a profile is logged in
    find_classes_tool      — tool definition schema for litellm
    PelotonClassSearch     — executes ``find_peloton_classes`` tool calls

Example:
    search = PelotonClassSearch(profile.peloton_tokens)
    result = search.execute({"discipline": "running", "duration_min": 30})
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import (
    PELOTON_CLASS_RESULTS,
    PELOTON_MAX_INSTRUCTORS,
    PELOTON_REQUEST_TIMEOUT_S,
    PELOTON_TOKEN_REFRESH_MARGIN_S,
)

logger = logging.getLogger(__name__)

AUTH_BASE = "https://auth.onepeloton.com"
API_BASE = "https://api.onepeloton.com"
CLASS_LINK = (
    "https://members.onepeloton.com/classes/{discipline}"
    "?modal=classDetailsModal&classId={class_id}"
)
"""Opens the class in the Peloton app on a phone, or on the web otherwise."""

# The Peloton web app's public OAuth client id; the API accepts no other.
CLIENT_ID = "mgsmWCD0A8Qn6uz6mmqI6qeBNHH9IPwS"
_SCOPE = "openid offline_access"
_HEADERS = {
    "content-type": "application/json; charset=utf-8",
    "accept": "application/json",
    "Peloton-Platform": "web",
}

DISCIPLINES = ("running", "walking", "cycling", "strength", "yoga", "stretching")
"""Catalog browse categories the tool offers."""

LANGUAGES = {"english": "en-US", "german": "de-DE"}
"""Language names the tool takes, mapped to the ``class_languages`` filter.

The catalog's ``language`` parameter is broken: ``german`` returns nothing and
every class reports ``english``. ``class_languages`` filters by the language
the class is taught in, which each class carries as ``origin_locale``.
"""

DIFFICULTIES = ("beginner", "intermediate", "advanced")


class PelotonError(Exception):
    """Peloton refused a request or could not be reached."""


class PelotonAuthError(PelotonError):
    """The stored Peloton login is missing, expired or revoked."""


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """Make one JSON request to Peloton and return the parsed body.

    Raises:
        PelotonAuthError: Peloton answered 401 or 403.
        PelotonError: Any other refusal, a network failure or a non-JSON body.
    """
    headers = dict(_HEADERS)
    if token:
        headers["authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=PELOTON_REQUEST_TIMEOUT_S
        ) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        if exc.code in (401, 403):
            raise PelotonAuthError(f"Peloton refused the login ({exc.code})") from exc
        raise PelotonError(f"Peloton answered {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PelotonError(f"Peloton is unreachable: {exc}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise PelotonError("Peloton returned a non-JSON response") from exc


def _save_tokens(path: Path, payload: dict[str, Any], refresh_token: str) -> None:
    """Write the access and refresh tokens, readable by the owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or refresh_token,
        "expires_at": time.time() + float(payload.get("expires_in") or 0),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(keep, fh)


def _load_tokens(path: Path) -> dict[str, Any]:
    """Return the stored tokens, or raise when the profile is not logged in."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PelotonAuthError("Not logged in to Peloton") from exc
    except ValueError as exc:
        raise PelotonAuthError(f"Unreadable Peloton token file {path}") from exc
    if not isinstance(stored, dict) or not stored.get("access_token"):
        raise PelotonAuthError(f"Peloton token file {path} holds no token")
    return stored


def has_tokens(path: Path | None) -> bool:
    """Return whether a Peloton token file exists at ``path``."""
    return path is not None and path.is_file()


def login(path: Path, username: str, password: str) -> None:
    """Exchange a username and password for tokens and store the tokens.

    Args:
        path: Where to store the tokens.
        username: Peloton username or email.
        password: Peloton password. Sent once to Peloton, never stored.

    Raises:
        PelotonAuthError: Peloton rejected the credentials.
        PelotonError: Peloton could not be reached.
    """
    try:
        payload = _request(
            "POST",
            f"{AUTH_BASE}/oauth/token",
            body={
                "grant_type": "password",
                "client_id": CLIENT_ID,
                "scope": _SCOPE,
                "username": username,
                "password": password,
            },
        )
    except PelotonError as exc:
        # The token endpoint answers bad credentials with 400, not 401.
        if "invalid_grant" in str(exc):
            raise PelotonAuthError("Peloton rejected the username or password") from exc
        raise
    if not isinstance(payload, dict) or "access_token" not in payload:
        raise PelotonAuthError("Peloton issued no access token")
    _save_tokens(path, payload, refresh_token="")


def revoke(path: Path) -> bool:
    """Revoke the stored refresh token with Peloton and delete the token file.

    The file is deleted even when Peloton cannot be reached, so logging out
    always works locally.

    Args:
        path: The profile's token file.

    Returns:
        Whether there was a token file to remove.
    """
    try:
        stored = _load_tokens(path)
    except PelotonAuthError:
        if not path.exists():
            return False
        stored = {}
    if stored.get("refresh_token"):
        try:
            _request(
                "POST",
                f"{AUTH_BASE}/oauth/revoke",
                body={"client_id": CLIENT_ID, "token": stored["refresh_token"]},
            )
        except PelotonError as exc:
            logger.warning("Could not revoke the Peloton token: %s", exc)
    path.unlink(missing_ok=True)
    return True


def access_token(path: Path) -> str:
    """Return a valid access token, refreshing and storing it when needed.

    Raises:
        PelotonAuthError: Not logged in, or the refresh token was refused.
        PelotonError: Peloton could not be reached.
    """
    stored = _load_tokens(path)
    if float(stored.get("expires_at") or 0) > time.time() + (
        PELOTON_TOKEN_REFRESH_MARGIN_S
    ):
        return str(stored["access_token"])
    refresh_token = stored.get("refresh_token")
    if not refresh_token:
        raise PelotonAuthError("Peloton login expired and holds no refresh token")
    try:
        payload = _request(
            "POST",
            f"{AUTH_BASE}/oauth/token",
            body={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )
    except PelotonError as exc:
        if isinstance(exc, PelotonAuthError) or "invalid_grant" in str(exc):
            raise PelotonAuthError("Peloton refused the stored login") from exc
        raise
    if not isinstance(payload, dict) or "access_token" not in payload:
        raise PelotonAuthError("Peloton issued no access token on refresh")
    _save_tokens(path, payload, refresh_token=str(refresh_token))
    logger.info("Refreshed the Peloton access token")
    return str(payload["access_token"])


def find_classes_tool() -> list[dict]:
    """Tool definition for searching Peloton's on-demand classes.

    Returns:
        A list with a single tool definition dict for litellm.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "find_peloton_classes",
                "description": (
                    "Search Peloton's on-demand classes for today's session. "
                    "Returns up to "
                    f"{PELOTON_CLASS_RESULTS} classes the user has not taken, "
                    "newest first, each with title, instructor, length, "
                    "language, difficulty, air date and a link that opens it "
                    "in the Peloton app. An unknown class type or instructor "
                    "returns an error listing the valid names."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "discipline": {
                            "type": "string",
                            "enum": list(DISCIPLINES),
                        },
                        "class_type": {
                            "type": "string",
                            "description": (
                                "Peloton class type within the discipline, "
                                "e.g. 'Intervals', 'Endurance', 'Full Body', "
                                "'Upper Body', 'Focus Flow'."
                            ),
                        },
                        "duration_min": {
                            "type": "integer",
                            "description": "Class length in minutes, e.g. 20 or 30.",
                        },
                        "language": {
                            "type": "string",
                            "enum": list(LANGUAGES),
                            "description": "Language the class is taught in.",
                        },
                        "instructors": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": PELOTON_MAX_INSTRUCTORS,
                            "description": "Full instructor names.",
                        },
                        "difficulty": {"type": "string", "enum": list(DIFFICULTIES)},
                    },
                    "required": ["discipline"],
                },
            },
        }
    ]


def _match_option(options: list[dict[str, Any]], wanted: str) -> dict | None:
    """Return the filter option whose display name matches ``wanted``.

    An exact case-insensitive match wins; otherwise a unique substring match
    (so "Marcel Maurer" and "Maurer" both resolve, "Marcel" does not when two
    instructors share the name).
    """
    needle = wanted.strip().casefold()
    exact = [o for o in options if o.get("display_name", "").casefold() == needle]
    if exact:
        return exact[0]
    partial = [o for o in options if needle in o.get("display_name", "").casefold()]
    return partial[0] if len(partial) == 1 else None


def _class_summary(
    ride: dict[str, Any], instructors: dict[str, str], discipline: str
) -> dict[str, Any]:
    """Reduce a catalog record to what the briefing writer needs."""
    aired = ride.get("original_air_time")
    locale = ride.get("origin_locale") or ""
    language = next((name for name, code in LANGUAGES.items() if code == locale), "")
    return {
        "title": ride.get("title"),
        "instructor": instructors.get(ride.get("instructor_id") or "", ""),
        "duration_min": round((ride.get("duration") or 0) / 60),
        "language": language or locale,
        "difficulty": ride.get("difficulty_level"),
        "aired": (
            datetime.fromtimestamp(aired, tz=timezone.utc).date().isoformat()
            if isinstance(aired, int | float)
            else None
        ),
        "url": CLASS_LINK.format(discipline=discipline, class_id=ride.get("id")),
    }


class PelotonClassSearch:
    """Execute ``find_peloton_classes`` calls for one profile's login.

    Remembers the first login failure so the caller can tell the user to log
    in again, and caches each discipline's filter options for the run.
    """

    def __init__(self, tokens_path: Path) -> None:
        """Bind the search to a profile's token file."""
        self._tokens_path = tokens_path
        self._filters: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.auth_failed = False

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        """GET a catalog path with the current access token."""
        token = access_token(self._tokens_path)
        query = urllib.parse.urlencode(params)
        return _request("GET", f"{API_BASE}{path}?{query}", token=token)

    def _filter_options(self, discipline: str) -> dict[str, list[dict[str, Any]]]:
        """Return ``{filter name: options}`` for a discipline, cached."""
        if discipline not in self._filters:
            body = self._get(
                "/api/ride/filters",
                {"library_type": "on_demand", "browse_category": discipline},
            )
            self._filters[discipline] = {
                f["name"]: f.get("values") or []
                for f in (body.get("filters") or [])
                if isinstance(f, dict) and f.get("name")
            }
        return self._filters[discipline]

    def execute(self, arguments: dict[str, Any]) -> str:
        """Run one search and return its JSON result for the tool message.

        Errors come back as ``{"error": ...}`` JSON rather than exceptions:
        the class is an optional part of the briefing, and the writer falls
        back to describing the class when the search cannot run.
        """
        try:
            return json.dumps(self._search(arguments))
        except PelotonAuthError as exc:
            self.auth_failed = True
            logger.warning("Peloton class search: %s", exc)
            return json.dumps(
                {"error": "Peloton login unavailable; describe the class instead."}
            )
        except PelotonError as exc:
            logger.warning("Peloton class search failed: %s", exc)
            return json.dumps(
                {"error": "Peloton is unavailable; describe the class instead."}
            )

    def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Resolve the arguments to catalog filters and run the search."""
        discipline = str(arguments.get("discipline") or "").strip().lower()
        if discipline not in DISCIPLINES:
            return {"error": f"discipline must be one of {list(DISCIPLINES)}"}
        options = self._filter_options(discipline)
        params: dict[str, Any] = {
            "browse_category": discipline,
            "content_format": "audio,video",
            "has_workout": "false",
            "limit": PELOTON_CLASS_RESULTS,
            "page": 0,
            "sort_by": "original_air_time",
            "desc": "true",
        }

        class_type = str(arguments.get("class_type") or "").strip()
        if class_type:
            type_options = options.get("class_type_id", [])
            match = _match_option(type_options, class_type)
            if match is None:
                return {
                    "error": f"unknown class_type {class_type!r}",
                    "valid": [o.get("display_name") for o in type_options],
                }
            params["class_type_id"] = match["value"]

        duration = arguments.get("duration_min")
        if duration:
            try:
                params["duration"] = int(duration) * 60
            except (TypeError, ValueError):
                return {"error": "duration_min must be a whole number of minutes"}

        language = str(arguments.get("language") or "").strip().lower()
        if language:
            if language not in LANGUAGES:
                return {"error": f"language must be one of {list(LANGUAGES)}"}
            params["class_languages"] = LANGUAGES[language]

        difficulty = str(arguments.get("difficulty") or "").strip().lower()
        if difficulty:
            if difficulty not in DIFFICULTIES:
                return {"error": f"difficulty must be one of {list(DIFFICULTIES)}"}
            params["difficulty_level"] = difficulty

        names = [str(n) for n in arguments.get("instructors") or [] if str(n).strip()]
        instructor_ids: list[str | None] = [None]
        if names:
            instructor_options = options.get("instructor_id", [])
            instructor_ids = []
            for name in names[:PELOTON_MAX_INSTRUCTORS]:
                match = _match_option(instructor_options, name)
                if match is None:
                    return {
                        "error": (
                            f"no single {discipline} instructor matches {name!r}"
                        ),
                        "valid": [o.get("display_name") for o in instructor_options],
                    }
                instructor_ids.append(match["value"])

        classes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for instructor_id in instructor_ids:
            query = dict(params)
            if instructor_id:
                query["instructor_id"] = instructor_id
            body = self._get("/api/v2/ride/archived", query)
            names_by_id = {
                i.get("id"): i.get("name", "")
                for i in body.get("instructors") or []
                if isinstance(i, dict)
            }
            for ride in body.get("data") or []:
                if not isinstance(ride, dict) or ride.get("id") in seen:
                    continue
                seen.add(ride.get("id"))
                classes.append(_class_summary(ride, names_by_id, discipline))
        classes.sort(key=lambda c: c.get("aired") or "", reverse=True)
        return {"classes": classes[:PELOTON_CLASS_RESULTS]}
