"""tests/unit/connector_key/test_connector_key_slack.py

Primary-key contract for the vendored Slack connector: synthetic Slack Web API replies (stubbed at the HTTP
transport) loaded through real dlt, for awkward channel names, replies and the single-table layout.
"""

from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import pytest

from dango.ingestion.dlt_sources.slack import slack_source
from tests.factories.connector_contract import (
    FakeHttp,
    KeyCase,
    assert_key_contract,
    loaded_tables,
    make_pipeline,
)

CHANNELS = ["general", "dev-ops", "2024-plans", "über-chat"]


def _qs(request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(request.url).query).items()}


def _message(ts: str, text: str, **extra):
    return {"type": "message", "user": "U1", "text": text, "ts": ts, **extra}


def _slack_http(channels: list[str], threads: bool = False, null_extra: bool = False) -> FakeHttp:
    ids = {name: f"C{i:03d}" for i, name in enumerate(channels)}

    def conversations_list(_req):
        return {
            "ok": True,
            "channels": [{"id": cid, "name": n, "created": 1600000000} for n, cid in ids.items()],
            "response_metadata": {"next_cursor": ""},
        }

    def history(req):
        cid = _qs(req)["channel"]
        # same ts in every channel: only (channel, ts) is unique
        msgs = [
            _message("1700000001.000100", f"hello {cid}"),
            _message("1700000002.000200", "second"),
        ]
        if threads:
            msgs.append(
                _message(
                    "1700000003.000300", "parent", thread_ts="1700000003.000300", reply_count=1
                )
            )
        if null_extra:
            msgs.append(
                _message(
                    "1700000004.000400", "bot", subtype="bot_message", blocks=[{"type": "section"}]
                )
            )
        return {"ok": True, "messages": msgs, "response_metadata": {"next_cursor": ""}}

    def replies(req):
        return {
            "ok": True,
            "messages": [
                _message("1700000003.000300", "parent", thread_ts="1700000003.000300"),
                _message("1700000005.000500", "reply", thread_ts="1700000003.000300"),
            ],
            "response_metadata": {"next_cursor": ""},
        }

    return (
        FakeHttp()
        .route(r"slack\.com/api/conversations\.list", conversations_list)
        .route(r"slack\.com/api/conversations\.history", history)
        .route(r"slack\.com/api/conversations\.replies", replies)
        .route(
            r"slack\.com/api/users\.list", {"ok": True, "members": [{"id": "U1", "name": "ann"}]}
        )
    )


def _open(channels, *, per_channel: bool, replies: bool = False, null_extra: bool = False):
    @contextmanager
    def open_(_rev: int):
        http = _slack_http(channels, threads=replies, null_extra=null_extra)
        with http.active():
            source = slack_source(
                access_token="xoxb-fake",
                selected_channels=None,
                table_per_channel=per_channel,
                replies=replies,
            ).with_resources(
                *(
                    list(channels) + ([f"{c}_replies" for c in channels] if replies else [])
                    if per_channel
                    else (["messages"] + (["replies"] if replies else []))
                ),
                "users",
            )
            yield source

    return open_


@pytest.mark.unit
class TestSlackPrimaryKeyContract:
    def test_per_channel_tables_with_awkward_channel_names(self, tmp_path):
        case = KeyCase(
            id="slack[per-channel]",
            open=_open(CHANNELS, per_channel=True),
            pk={
                "general_message": ("channel", "ts"),
                "dev_ops_message": ("channel", "ts"),
                "_2024_plans_message": ("channel", "ts"),
                "_ber_chat_message": ("channel", "ts"),
                "users": ("id",),
            },
            probe=("users", "id", "name", "ann"),
            min_rows={"general_message": 2},
        )
        assert_key_contract(case, tmp_path)

    def test_single_messages_table_keeps_channel_in_the_key(self, tmp_path):
        case = KeyCase(
            id="slack[single-table]",
            open=_open(["general", "dev-ops"], per_channel=False),
            pk={"messages": ("channel", "ts"), "users": ("id",)},
            min_rows={"messages": 4},
        )
        state = assert_key_contract(case, tmp_path)
        # two channels share the same ts values (2 messages each); (channel, ts) must keep all four
        assert state["messages"].rows == 4

    def test_thread_replies_are_keyed_on_thread_ts_and_ts(self, tmp_path):
        case = KeyCase(
            id="slack[replies]",
            open=_open(["general"], per_channel=True, replies=True),
            pk={
                "general_message": ("channel", "ts"),
                "general_replies_message": ("thread_ts", "ts"),
            },
        )
        assert_key_contract(case, tmp_path)

    def test_bot_messages_with_blocks_and_subtypes(self, tmp_path):
        case = KeyCase(
            id="slack[subtype]",
            open=_open(["general"], per_channel=True, null_extra=True),
            pk={"general_message": ("channel", "ts"), "general_bot_message": ("channel", "ts")},
        )
        assert_key_contract(case, tmp_path)

    def test_channel_with_no_messages_does_not_break_the_run(self, tmp_path):
        @contextmanager
        def open_(_rev):
            http = (
                FakeHttp()
                .route(
                    r"conversations\.list",
                    {
                        "ok": True,
                        "channels": [{"id": "C1", "name": "quiet"}],
                        "response_metadata": {"next_cursor": ""},
                    },
                )
                .route(
                    r"conversations\.history",
                    {"ok": True, "messages": [], "response_metadata": {"next_cursor": ""}},
                )
                .route(r"users\.list", {"ok": True, "members": [{"id": "U1", "name": "ann"}]})
            )
            with http.active():
                yield slack_source(access_token="x", selected_channels=None).with_resources(
                    "quiet", "users"
                )

        pipeline = make_pipeline(tmp_path)
        with open_(0) as source:
            pipeline.run(source)
        assert loaded_tables(pipeline)["users"].pk == ("id",)
