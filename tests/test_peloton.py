"""Tests for the Peloton login and class search used by the morning briefing."""

from __future__ import annotations

import json
import stat
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import peloton
from cmd_peloton import cmd_peloton
from config import PELOTON_CLASS_RESULTS
from peloton import (
    PelotonAuthError,
    PelotonClassSearch,
    PelotonError,
    _match_option,
    access_token,
    find_classes_tool,
    has_tokens,
    login,
    revoke,
)

FILTERS = {
    "filters": [
        {
            "name": "class_type_id",
            "values": [
                {"display_name": "Intervals", "value": "type-intervals"},
                {"display_name": "Endurance", "value": "type-endurance"},
            ],
        },
        {
            "name": "instructor_id",
            "values": [
                {"display_name": "Adrian Williams", "value": "ins-adrian"},
                {"display_name": "Marcel Dinkins", "value": "ins-dinkins"},
                {"display_name": "Marcel Maurer", "value": "ins-maurer"},
            ],
        },
    ]
}


def _ride(ride_id: str, instructor_id: str, aired: int, locale: str = "en-US"):
    return {
        "id": ride_id,
        "title": f"30 min Intervals Run {ride_id}",
        "instructor_id": instructor_id,
        "duration": 1800,
        "difficulty_level": "intermediate",
        "original_air_time": aired,
        "origin_locale": locale,
        "language": "english",
    }


INSTRUCTORS = [
    {"id": "ins-adrian", "name": "Adrian Williams"},
    {"id": "ins-maurer", "name": "Marcel Maurer"},
]


class FakePeloton:
    """Stands in for ``peloton._request``, recording every call."""

    def __init__(self, archived: dict[str, list[dict]] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.archived = archived or {}
        self.fail_with: Exception | None = None

    def __call__(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((method, url, body))
        if self.fail_with is not None:
            raise self.fail_with
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path == "/api/ride/filters":
            return FILTERS
        if parsed.path == "/api/v2/ride/archived":
            rides = self.archived.get(params.get("instructor_id", ""), [])
            return {"data": rides, "instructors": INSTRUCTORS}
        raise AssertionError(f"unexpected request {method} {url}")

    def queries(self, path: str) -> list[dict[str, str]]:
        """Return the query params of every GET to ``path``."""
        found = []
        for method, url, _ in self.calls:
            parsed = urllib.parse.urlparse(url)
            if method == "GET" and parsed.path == path:
                found.append(dict(urllib.parse.parse_qsl(parsed.query)))
        return found


def _store(path: Path, *, expires_in: float, refresh: str | None = "r1") -> None:
    path.write_text(
        json.dumps(
            {
                "access_token": "a1",
                "refresh_token": refresh,
                "expires_at": time.time() + expires_in,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def tokens(tmp_path: Path) -> Path:
    path = tmp_path / "peloton_tokens.json"
    _store(path, expires_in=3600)
    return path


class TestLoginAndTokens:
    def test_login_stores_tokens_only_owner_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[dict] = []

        def fake(method, url, *, token=None, body=None):
            sent.append(body)
            return {"access_token": "a1", "refresh_token": "r1", "expires_in": 172800}

        monkeypatch.setattr(peloton, "_request", fake)
        path = tmp_path / "peloton_tokens.json"

        login(path, "me@example.com", "secret")

        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["access_token"] == "a1"
        assert stored["refresh_token"] == "r1"
        assert "secret" not in path.read_text(encoding="utf-8")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert sent[0]["grant_type"] == "password"

    def test_rejected_password_is_an_auth_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake(method, url, *, token=None, body=None):
            raise PelotonError('Peloton answered 400: {"error": "invalid_grant"}')

        monkeypatch.setattr(peloton, "_request", fake)
        path = tmp_path / "peloton_tokens.json"

        with pytest.raises(PelotonAuthError):
            login(path, "me@example.com", "wrong")
        assert not path.exists()

    def test_fresh_token_is_reused_without_a_request(
        self, tokens: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakePeloton()
        monkeypatch.setattr(peloton, "_request", fake)

        assert access_token(tokens) == "a1"
        assert fake.calls == []

    def test_expiring_token_is_refreshed_keeping_the_refresh_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "peloton_tokens.json"
        _store(path, expires_in=60)
        sent: list[dict] = []

        def fake(method, url, *, token=None, body=None):
            sent.append(body)
            return {"access_token": "a2", "expires_in": 172800}

        monkeypatch.setattr(peloton, "_request", fake)

        assert access_token(path) == "a2"
        assert sent[0] == {
            "grant_type": "refresh_token",
            "client_id": peloton.CLIENT_ID,
            "refresh_token": "r1",
        }
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored["access_token"] == "a2"
        assert stored["refresh_token"] == "r1"

    def test_refused_refresh_is_an_auth_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "peloton_tokens.json"
        _store(path, expires_in=0)

        def fake(method, url, *, token=None, body=None):
            raise PelotonError('Peloton answered 400: {"error": "invalid_grant"}')

        monkeypatch.setattr(peloton, "_request", fake)

        with pytest.raises(PelotonAuthError):
            access_token(path)

    def test_missing_file_is_an_auth_error(self, tmp_path: Path) -> None:
        with pytest.raises(PelotonAuthError):
            access_token(tmp_path / "absent.json")

    def test_revoke_deletes_even_when_peloton_is_unreachable(
        self, tokens: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakePeloton()
        fake.fail_with = PelotonError("Peloton is unreachable")
        monkeypatch.setattr(peloton, "_request", fake)

        assert revoke(tokens) is True
        assert not tokens.exists()
        assert fake.calls[0][2] == {"client_id": peloton.CLIENT_ID, "token": "r1"}

    def test_revoke_without_a_login(self, tmp_path: Path) -> None:
        assert revoke(tmp_path / "absent.json") is False

    def test_has_tokens(self, tokens: Path, tmp_path: Path) -> None:
        assert has_tokens(tokens)
        assert not has_tokens(tmp_path / "absent.json")
        assert not has_tokens(None)


class TestMatchOption:
    OPTIONS = FILTERS["filters"][1]["values"]

    def test_exact_name_case_insensitive(self) -> None:
        assert _match_option(self.OPTIONS, "marcel maurer")["value"] == "ins-maurer"

    def test_unique_partial_name(self) -> None:
        assert _match_option(self.OPTIONS, "Maurer")["value"] == "ins-maurer"

    def test_ambiguous_partial_name_does_not_guess(self) -> None:
        assert _match_option(self.OPTIONS, "Marcel") is None


class TestClassSearch:
    def test_filters_map_to_catalog_parameters(
        self, tokens: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakePeloton({"": [_ride("r1", "ins-adrian", 1_793_000_000)]})
        monkeypatch.setattr(peloton, "_request", fake)

        result = json.loads(
            PelotonClassSearch(tokens).execute(
                {
                    "discipline": "running",
                    "class_type": "intervals",
                    "duration_min": 30,
                    "language": "german",
                    "difficulty": "intermediate",
                }
            )
        )

        (query,) = fake.queries("/api/v2/ride/archived")
        assert query["browse_category"] == "running"
        assert query["has_workout"] == "false"
        assert query["class_type_id"] == "type-intervals"
        assert query["duration"] == "1800"
        assert query["class_languages"] == "de-DE"
        assert query["difficulty_level"] == "intermediate"
        assert "language" not in query
        (found,) = result["classes"]
        assert found["instructor"] == "Adrian Williams"
        assert found["duration_min"] == 30
        assert found["language"] == "english"
        assert found["aired"] == "2026-10-26"
        assert found["url"] == (
            "https://members.onepeloton.com/classes/running"
            "?modal=classDetailsModal&classId=r1"
        )

    def test_unknown_class_type_lists_the_valid_ones(
        self, tokens: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(peloton, "_request", FakePeloton())

        result = json.loads(
            PelotonClassSearch(tokens).execute(
                {"discipline": "running", "class_type": "Tempo"}
            )
        )

        assert "Tempo" in result["error"]
        assert result["valid"] == ["Intervals", "Endurance"]

    def test_ambiguous_instructor_is_refused(
        self, tokens: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(peloton, "_request", FakePeloton())

        result = json.loads(
            PelotonClassSearch(tokens).execute(
                {"discipline": "running", "instructors": ["Marcel"]}
            )
        )

        assert "Marcel" in result["error"]
        assert "Marcel Maurer" in result["valid"]

    def test_one_request_per_instructor_merged_newest_first(
        self, tokens: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakePeloton(
            {
                "ins-adrian": [_ride("old", "ins-adrian", 1_700_000_000)],
                "ins-maurer": [
                    _ride("new", "ins-maurer", 1_790_000_000, "de-DE"),
                    _ride("old", "ins-adrian", 1_700_000_000),
                ],
            }
        )
        monkeypatch.setattr(peloton, "_request", fake)
        search = PelotonClassSearch(tokens)

        result = json.loads(
            search.execute(
                {
                    "discipline": "running",
                    "instructors": ["Adrian Williams", "Marcel Maurer"],
                }
            )
        )

        queried = [q["instructor_id"] for q in fake.queries("/api/v2/ride/archived")]
        assert queried == ["ins-adrian", "ins-maurer"]
        assert [c["url"].rsplit("=", 1)[1] for c in result["classes"]] == [
            "new",
            "old",
        ]
        assert result["classes"][0]["language"] == "german"

        search.execute({"discipline": "running"})
        assert len(fake.queries("/api/ride/filters")) == 1

    def test_results_are_capped(
        self, tokens: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rides = [
            _ride(f"r{i}", "ins-adrian", 1_700_000_000 + i)
            for i in range(PELOTON_CLASS_RESULTS + 3)
        ]
        monkeypatch.setattr(peloton, "_request", FakePeloton({"": rides}))

        result = json.loads(
            PelotonClassSearch(tokens).execute({"discipline": "running"})
        )

        assert len(result["classes"]) == PELOTON_CLASS_RESULTS

    def test_auth_failure_is_remembered_and_reported_as_data(
        self, tokens: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakePeloton()
        fake.fail_with = PelotonAuthError("Peloton refused the login (401)")
        monkeypatch.setattr(peloton, "_request", fake)
        search = PelotonClassSearch(tokens)

        result = json.loads(search.execute({"discipline": "running"}))

        assert "describe the class instead" in result["error"]
        assert search.auth_failed

    def test_outage_is_not_an_auth_failure(
        self, tokens: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakePeloton()
        fake.fail_with = PelotonError("Peloton is unreachable")
        monkeypatch.setattr(peloton, "_request", fake)
        search = PelotonClassSearch(tokens)

        result = json.loads(search.execute({"discipline": "running"}))

        assert "error" in result
        assert not search.auth_failed

    def test_unknown_discipline(self, tokens: Path) -> None:
        result = json.loads(PelotonClassSearch(tokens).execute({"discipline": "golf"}))

        assert "discipline" in result["error"]

    def test_tool_schema_names_the_tool(self) -> None:
        (tool,) = find_classes_tool()

        assert tool["function"]["name"] == "find_peloton_classes"
        assert tool["function"]["parameters"]["required"] == ["discipline"]


class TestCmdPeloton:
    @staticmethod
    def _args(tmp_path: Path, cmd: str) -> SimpleNamespace:
        profile = SimpleNamespace(peloton_tokens=tmp_path / "peloton_tokens.json")
        return SimpleNamespace(peloton_cmd=cmd, profile_config=profile)

    def test_login_gitignores_the_token_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr("builtins.input", lambda prompt: "me@example.com")
        monkeypatch.setattr("cmd_peloton.getpass.getpass", lambda prompt: "secret")
        monkeypatch.setattr(
            "cmd_peloton.login",
            lambda path, user, pw: _store(path, expires_in=3600),
        )
        (tmp_path / ".gitignore").write_text("/health.db", encoding="utf-8")

        cmd_peloton(self._args(tmp_path, "login"))
        cmd_peloton(self._args(tmp_path, "login"))

        ignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert ignore.splitlines().count("/peloton_tokens.json") == 1
        assert ignore.startswith("/health.db\n")
        assert "Logged in" in capsys.readouterr().out

    def test_status_without_login(self, tmp_path: Path, capsys) -> None:
        cmd_peloton(self._args(tmp_path, "status"))

        assert "peloton login" in capsys.readouterr().out

    def test_status_with_refused_login_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        _store(tmp_path / "peloton_tokens.json", expires_in=0)

        def refused(path: Path) -> str:
            raise PelotonAuthError("Peloton refused the stored login")

        monkeypatch.setattr("cmd_peloton.access_token", refused)

        with pytest.raises(SystemExit):
            cmd_peloton(self._args(tmp_path, "status"))
        assert "peloton login" in capsys.readouterr().out

    def test_without_a_profile_exits(self) -> None:
        with pytest.raises(SystemExit):
            cmd_peloton(SimpleNamespace(peloton_cmd="status", profile_config=None))
