#!/usr/bin/env python3
"""Small synthetic MemOS Cloud image Add/Search capability probe.

Reads API credentials on the server via manage.environment(). Never prints or
stores credentials or the image payload. Creates one synthetic test memory.
"""
import base64
import json
import os
import struct
import time
import urllib.error
import urllib.request
import uuid
import zlib

import manage


def png_solid_red(width=128, height=128):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    rows = b"".join(b"\x00" + bytes((255, 0, 0)) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def post(base, path, key, payload, timeout=300):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Token " + key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        # Avoid printing provider error bodies, which can include request details.
        return exc.code, {"code": None}


def main():
    env = manage.environment()
    key = env.get("MEMOS_API_KEY", "")
    if not key:
        raise SystemExit("MemOS API key is not configured")
    base = env.get("MEMOS_BASE_URL", "https://memos.memtensor.cn/api/openmem/v1").rstrip("/")
    run = uuid.uuid4().hex
    user_id = "synthetic-multimodal-capability:" + run
    conversation_id = "synthetic-image-test:" + run
    image = base64.b64encode(png_solid_red()).decode("ascii")
    add_payload = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Please remember the attached synthetic test image for a later question."},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}},
            ],
        }],
    }
    status, result = post(base, "/add/message", key, add_payload)
    data = result.get("data") if isinstance(result, dict) else None
    data = data if isinstance(data, dict) else {}
    print(json.dumps({
        "operation": "add_inline_base64_png",
        "http_status": status,
        "api_code": result.get("code") if isinstance(result, dict) else None,
        "success": data.get("success"),
        "status": data.get("status"),
        "task_id_returned": isinstance(data.get("task_id"), str) and bool(data["task_id"]),
    }))
    if status != 200 or not isinstance(result, dict) or result.get("code") != 0 or data.get("success") is not True:
        return 2

    task_id = data.get("task_id")
    task_state = data.get("status")
    polls = 0
    if task_id and task_state not in ("completed", "failed"):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            time.sleep(5)
            polls += 1
            poll_status, poll_result = post(base, "/get/status", key, {"task_id": task_id}, timeout=30)
            poll_data = poll_result.get("data") if isinstance(poll_result, dict) else None
            task_state = poll_data.get("status") if poll_status == 200 and isinstance(poll_data, dict) else None
            if task_state in ("completed", "failed"):
                break
    print(json.dumps({"operation": "wait_for_add", "status": task_state, "polls": polls}))
    if task_state == "failed":
        return 3

    search_payload = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "query": "What color is the main shape in the synthetic test image?",
        "memory_limit_number": 10,
        "include_preference": False,
    }
    search_status, search_result = post(base, "/search/memory", key, search_payload, timeout=120)
    search_data = search_result.get("data") if isinstance(search_result, dict) else None
    records = search_data.get("memory_detail_list", []) if isinstance(search_data, dict) else []
    texts = [
        " ".join(str(record.get(k, "")) for k in ("memory_key", "memory_value"))
        for record in records if isinstance(record, dict)
    ]
    print(json.dumps({
        "operation": "text_search_for_image_memory",
        "http_status": search_status,
        "api_code": search_result.get("code") if isinstance(search_result, dict) else None,
        "memory_records": len(records),
        "retrieved_red_or_square": any("red" in text.lower() or "square" in text.lower() for text in texts),
        "records_are_text": all(isinstance(record.get("memory_value"), str) for record in records if isinstance(record, dict)),
    }))

    image_query_payload = {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "query": [
            {"type": "text", "text": "What is shown?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}},
        ],
        "memory_limit_number": 1,
    }
    query_status, query_result = post(base, "/search/memory", key, image_query_payload, timeout=30)
    print(json.dumps({
        "operation": "image_content_part_search_query",
        "http_status": query_status,
        "api_code": query_result.get("code") if isinstance(query_result, dict) else None,
        "accepted": query_status == 200 and isinstance(query_result, dict) and query_result.get("code") == 0,
    }))
    print("synthetic_user_id=" + user_id)
    return 0 if search_status == 200 and isinstance(search_result, dict) and search_result.get("code") == 0 else 4


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
