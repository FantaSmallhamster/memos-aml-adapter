import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = server.DB_PATH
        self.old_add_interval = server.ADD_MIN_INTERVAL
        self.old_search_interval = server.SEARCH_MIN_INTERVAL
        self.old_add_tokens = server.MAX_ADD_ESTIMATED_TOKENS
        self.old_legacy_add_tokens = server.LEGACY_ADD_ESTIMATED_TOKENS
        self.old_search_tokens = server.MAX_SEARCH_ESTIMATED_TOKENS
        server.DB_PATH = Path(self.temp.name) / "ledger.sqlite3"
        server.ADD_MIN_INTERVAL = 0
        server.SEARCH_MIN_INTERVAL = 0
        server.MAX_ADD_ESTIMATED_TOKENS = 6000
        server.LEGACY_ADD_ESTIMATED_TOKENS = 3000
        server.MAX_SEARCH_ESTIMATED_TOKENS = 6000
        server._inflight.clear()
        server._add_next_start = 0
        server._add_cooldown_until = 0
        server._search_next_start = 0
        server._search_cooldown_until = 0

    def tearDown(self):
        server.DB_PATH = self.old_db_path
        server.ADD_MIN_INTERVAL = self.old_add_interval
        server.SEARCH_MIN_INTERVAL = self.old_search_interval
        server.MAX_ADD_ESTIMATED_TOKENS = self.old_add_tokens
        server.LEGACY_ADD_ESTIMATED_TOKENS = self.old_legacy_add_tokens
        server.MAX_SEARCH_ESTIMATED_TOKENS = self.old_search_tokens
        self.temp.cleanup()

    def test_add_splits_large_payload_and_is_idempotent(self):
        calls = []

        def fake_upstream(path, body, timeout=None):
            self.assertEqual(path, "add/message")
            calls.append(body)
            return {"code": 0, "data": {"success": True, "status": "completed"}}

        payload = {
            "request_id": "run:add:1",
            "user_id": "user-1",
            "session_id": "session-1",
            "messages": [{"role": "user", "content": "中" * 13000}],
        }
        with patch.object(server, "upstream", side_effect=fake_upstream):
            first = server.add(payload)
            second = server.add(payload)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 3)
        self.assertEqual("".join(call["messages"][0]["content"] for call in calls), "中" * 13000)

    def test_add_recursively_splits_on_40305_and_skips_completed_parts(self):
        calls = []

        def fake_upstream(path, body, timeout=None):
            calls.append([item["content"] for item in body["messages"]])
            if len(body["messages"]) > 1:
                raise server.AdapterError(413, "too large", upstream_code="40305")
            return {"code": 0, "data": {"success": True, "status": "completed"}}

        payload = {
            "request_id": "run:add:2",
            "user_id": "user-2",
            "session_id": "session-2",
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ],
        }
        with patch.object(server, "upstream", side_effect=fake_upstream):
            server.add(payload)
            server.add(payload)
        self.assertEqual(calls, [["first", "second"], ["first"], ["second"]])

    def test_existing_partial_add_keeps_legacy_split_plan(self):
        request_id = "cambench:partial"
        payload_hash = "payload-hash"
        with server.db_connect() as connection:
            connection.execute(
                "INSERT INTO add_parts(request_id, payload_hash, part_id, part_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (request_id, payload_hash, "0", "part-hash", 1),
            )
        self.assertEqual(server.get_add_plan(request_id, payload_hash), 3000)
        self.assertEqual(server.get_add_plan("cambench:new", "new-hash"), 6000)

    def test_search_runs_four_disjoint_groups_and_merges_top_100(self):
        calls = []
        field_for_view = {
            "detail_factual": ("memory_detail_list", "memory_value"),
            "preference": ("preference_detail_list", "preference"),
            "event": ("event_detail_list", "event_value"),
            "tool_memory": ("tool_memory_detail_list", "tool_value"),
        }

        def fake_upstream(path, body, timeout=None):
            self.assertEqual(path, "search/memory")
            calls.append(body)
            view = body["include_memory_view"][0]
            list_name, value_name = field_for_view[view]
            data = {
                list_name: [
                    {"id": f"{view}-{index}", value_name: f"memory {view} {index}", "relativity": index / 100}
                    for index in range(25)
                ]
            }
            return {"code": 0, "data": data}

        with patch.object(server, "upstream", side_effect=fake_upstream):
            result = server.search({"user_id": "user-3", "query": "question", "top_k": 100})
        self.assertEqual(len(calls), 4)
        self.assertEqual([call["include_memory_view"] for call in calls], [list(x) for x in server.SEARCH_VIEW_GROUPS])
        self.assertTrue(all(call["memory_limit_number"] == 25 for call in calls))
        self.assertEqual(len(result["data"]), 100)
        self.assertGreaterEqual(result["data"][0]["score"], result["data"][-1]["score"])

    def test_search_splits_query_after_40305(self):
        calls = []

        def fake_upstream(path, body, timeout=None):
            calls.append(body["query"])
            if len(body["query"]) > 5:
                raise server.AdapterError(413, "too large", upstream_code="40305")
            return {
                "code": 0,
                "data": {"memory_detail_list": [{"id": body["query"], "memory_value": body["query"], "relativity": 1}]},
            }

        with patch.object(server, "upstream", side_effect=fake_upstream):
            rows = server.search_group("abcdefghij", "user-4", ("detail_factual",), time.monotonic())
        self.assertEqual("".join(row["content"] for row in rows), "abcdefghij")
        self.assertTrue(all(len(row["content"]) <= 5 for row in rows))
        self.assertEqual(calls[0], "abcdefghij")


if __name__ == "__main__":
    unittest.main()
