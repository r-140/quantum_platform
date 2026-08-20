from app.tasks.calibration import _inject_demo_parity_errors


def test_demo_error_injection_preserves_shot_count() -> None:
    before = {"00": 512, "11": 512}
    after = _inject_demo_parity_errors(before, 0.25)
    assert sum(after.values()) == 1024
    assert after.get("01", 0) + after.get("10", 0) == 256
    assert before == {"00": 512, "11": 512}


def test_demo_error_injection_rejects_invalid_rate() -> None:
    try:
        _inject_demo_parity_errors({"00": 1}, 1.1)
    except ValueError as exc:
        assert "between 0 and 1" in str(exc)
    else:
        raise AssertionError("expected ValueError")
