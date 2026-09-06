from typing import Any, cast

from app.worker import tools


def test_stdio_mcp_registers_only_open_pr(monkeypatch) -> None:
    listed = tools._mcp_result({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listed is not None
    listed = cast(Any, listed)
    assert [item["name"] for item in listed["result"]["tools"]] == ["open_pr"]

    monkeypatch.setattr(
        tools,
        "_bridge_call",
        lambda name, arguments: {"pr_number": 17, "head_sha": "abc"},
    )
    called = tools._mcp_result(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "open_pr",
                "arguments": {"title": "Implement", "body": "Done"},
            },
        }
    )

    assert called is not None
    called = cast(Any, called)
    assert called["result"]["structuredContent"]["pr_number"] == 17
