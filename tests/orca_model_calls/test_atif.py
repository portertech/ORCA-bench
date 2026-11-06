import pytest

from orca_model_calls.atif import atomic_write, envelope, loads, validate


def test_envelope_and_atomic_replace(record, tmp_path):
    recorder = record()
    artifact = envelope(
        recorder,
        "synthetic-session",
        {
            "steps": [
                {"step_id": 9, "source": "system", "message": "synthetic"},
                {"step_id": 9, "source": "agent", "message": "answer"},
            ],
            "final_metrics": {"total_prompt_tokens": 10},
        },
    )
    validate(artifact)
    assert [s["step_id"] for s in artifact["steps"]] == [1, 2]
    assert artifact["steps"][0]["source"] == "environment"
    assert artifact["steps"][0]["extra"]["original_source"] == "system"
    assert set(artifact) == {
        "schema_version",
        "session_id",
        "trajectory_id",
        "agent",
        "steps",
        "final_metrics",
        "extra",
    }
    assert set(artifact["agent"]) == {"name", "version"}
    path = tmp_path / "artifact.json"
    atomic_write(path, artifact)
    artifact["final_metrics"] = {}
    atomic_write(path, artifact)
    assert loads(path.read_bytes()) == artifact
    assert list(tmp_path.iterdir()) == [path]
    artifact["agent"]["extra"] = True
    with pytest.raises(Exception):
        atomic_write(path, artifact)
    assert "extra" not in loads(path.read_bytes())["agent"]


@pytest.mark.parametrize("data", ['{"x": NaN}', '{"x": 1, "x": 2}', '{"x": Infinity}'])
def test_strict_json(data):
    with pytest.raises(ValueError):
        loads(data)
