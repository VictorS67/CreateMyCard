# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import json
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLOUD_ROOT = PROJECT_ROOT / "cloud"
if str(CLOUD_ROOT) not in sys.path:
    sys.path.insert(0, str(CLOUD_ROOT))

from config.config import Settings  # noqa: E402
from services.widget_batch_store import (  # noqa: E402
    WidgetBatchCaseRecord,
    WidgetBatchRecordingDisabledError,
    WidgetBatchStore,
)


def _settings(tmp_path: Path, enabled: bool = True) -> Settings:
    return Settings(
        enable_widget_batch_recording=enabled,
        widget_batch_results_path=str(tmp_path),
    )


def _record(store: WidgetBatchStore, case_id: str = "2x2-q1") -> dict:
    context = store.context_from_query(
        {"batchId": "nested2-2x2-1", "caseId": case_id, "size": "2x2"},
        "generateWidgetCardTerseDslNested2",
    )
    assert context is not None
    return store.record_case(
        WidgetBatchCaseRecord(
            context=context,
            request_id=f"request-{case_id}",
            raw_payload={"content": {"title": "天气"}},
            business_response={"status": "success", "artifactUrl": "https://example.test/a"},
            final_frame={"reply": {"streamInfo": {"streamType": "final"}}},
            render_messages=[{"createSurface": {"surfaceId": "card"}}],
            duration_ms=123.456,
            started_at="2026-08-11T08:00:00+00:00",
            completed_at="2026-08-11T08:00:01+00:00",
            status="success",
            error_code="",
            artifact_url="https://example.test/a",
            artifact_digest="sha256:test",
        )
    )


def test_store_records_manifest_case_files_and_download(tmp_path):
    store = WidgetBatchStore(_settings(tmp_path))
    manifest = _record(store)

    assert manifest["summary"] == {"total": 1, "passed": 1, "failed": 0}
    case_dir = tmp_path / "nested2-2x2-1" / "cases" / "2x2-q1"
    assert json.loads((case_dir / "input.json").read_text(encoding="utf-8"))["content"] == {
        "title": "天气"
    }
    output = (case_dir / "output.a2ui.jsonl").read_text(encoding="utf-8")
    assert [json.loads(line) for line in output.splitlines()] == [
        {"createSurface": {"surfaceId": "card"}}
    ]
    metrics = json.loads((case_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["durationMs"] == 123.46

    filename, archive_bytes = store.build_download("nested2-2x2-1")
    assert filename == "widget-batch-nested2-2x2-1.zip"
    with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "cases/2x2-q1/input.json",
            "cases/2x2-q1/metrics.json",
            "cases/2x2-q1/output.a2ui.jsonl",
            "cases/2x2-q1/response.json",
        }


def test_store_validates_query_and_keeps_case_ids_stable(tmp_path):
    store = WidgetBatchStore(_settings(tmp_path))
    assert store.context_from_query({}, "generateWidgetCardTerseDslNested2") is None
    with pytest.raises(ValueError, match="supplied together"):
        store.context_from_query({"batchId": "batch-1"}, "operation")
    with pytest.raises(ValueError, match="unsupported characters"):
        store.context_from_query(
            {"batchId": "../escape", "caseId": "q1", "size": "2x2"},
            "operation",
        )

    _record(store, "2x2-q2")
    _record(store, "2x2-q1")
    manifest = store.get_batch("nested2-2x2-1")
    assert [item["caseId"] for item in manifest["cases"]] == ["2x2-q1", "2x2-q2"]


def test_store_rejects_explicit_batch_metadata_when_disabled(tmp_path):
    store = WidgetBatchStore(_settings(tmp_path, enabled=False))
    with pytest.raises(WidgetBatchRecordingDisabledError):
        store.context_from_query(
            {"batchId": "batch-1", "caseId": "q1", "size": "2x2"},
            "operation",
        )
