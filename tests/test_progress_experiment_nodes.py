from qapilot.shared import progress


def test_prd_only_experiment_nodes_have_progress_bands_and_labels():
    nodes = [
        "doc_import_exp",
        "requirement_extract_exp",
        "codebase_scan_exp",
        "ts_generate_prd_only",
        "tc_generate_doc_search",
        "tv_generate_codebase_aware",
        "save_experiment_scenarios",
    ]

    for node in nodes:
        assert node in progress._BANDS
        assert node in progress._LABELS


def test_prd_only_experiment_progress_is_emitted(monkeypatch):
    published: list[tuple[str, str, dict]] = []

    def _fake_publish(trace_id: str, event_type: str, payload: dict) -> None:
        published.append((trace_id, event_type, payload))

    monkeypatch.setattr(progress, "publish_run_event", _fake_publish)
    progress.reset("trace-exp")

    progress.node("trace-exp", "doc_import_exp", "init")
    progress.node("trace-exp", "tv_generate_codebase_aware", "init")

    assert published == [
        (
            "trace-exp",
            "progress",
            {
                "percent": progress._BANDS["doc_import_exp"][0],
                "node": "doc_import_exp",
                "message": progress._LABELS["doc_import_exp"],
            },
        ),
        (
            "trace-exp",
            "progress",
            {
                "percent": progress._BANDS["tv_generate_codebase_aware"][0],
                "node": "tv_generate_codebase_aware",
                "message": progress._LABELS["tv_generate_codebase_aware"],
            },
        ),
    ]
