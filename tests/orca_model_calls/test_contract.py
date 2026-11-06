import copy
from pathlib import Path

import pytest

from orca_model_calls.atif import envelope, loads, validate


@pytest.mark.parametrize(
    "mutation",
    [
        lambda a, e, c: a.update(unknown=True),
        lambda a, e, c: a["agent"].update(extra=True),
        lambda a, e, c: e["calls"].append(copy.deepcopy(c)),
        lambda a, e, c: c.update(ordinal=3),
        lambda a, e, c: c.update(ordinal=1.0),
        lambda a, e, c: c["response"]["usage"]["raw"].update(total_tokens=12.0),
        lambda a, e, c: c["response"].update(request_id="wrong"),
        lambda a, e, c: c["response"].update(step_number=2),
        lambda a, e, c: c["response"].update(timestamp="not-a-timestamp"),
        lambda a, e, c: c["messages"][0]["content"][0].update(type="image"),
        lambda a, e, c: e.update(dropped_calls="one"),
        lambda a, e, c: c["response"]["finish_reason"].update(unified="length"),
        lambda a, e, c: c["response"]["message"]["content"].reverse(),
        lambda a, e, c: c["response"]["usage"]["raw"].update(prompt_tokens_details=1),
        lambda a, e, c: a["final_metrics"].update(cost=float("nan")),
    ],
)
def test_contract_negative(record, mutation):
    artifact = envelope(record(), "synthetic-session")
    ext = artifact["extra"]["llmspec_model_calls"]
    mutation(artifact, ext, ext["calls"][0])
    with pytest.raises(Exception):
        validate(artifact)


def test_golden_fixtures():
    for path in Path("tests/fixtures/model_calls").glob("valid-*.json"):
        validate(loads(path.read_bytes()))
    for path in Path("tests/fixtures/model_calls").glob("invalid-*.json"):
        with pytest.raises(Exception):
            validate(loads(path.read_bytes()))


def test_exporter_schema_is_identical():
    assert (
        Path("schemas/llmspec-model-calls-v1.schema.json").read_bytes()
        == Path("tools/agento11y-export/schema.json").read_bytes()
    )
