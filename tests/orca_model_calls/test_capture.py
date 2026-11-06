from orca_model_calls.capture import Recorder, encode


def test_identity_pairing_and_immutability(record, response):
    recorder = record()
    response["id"] = "second-response"
    record(recorder)
    ext = recorder.extension()
    a, b = ext["calls"]
    assert [a["ordinal"], b["ordinal"]] == [1, 2]
    assert a["request_id"] != b["request_id"]
    assert a["response"]["id"] == "synthetic-response"
    assert b["response"]["request_id"] == b["request_id"]
    assert a["execution_generation_id"] == b["execution_generation_id"]
    assert Recorder().execution_generation_id != recorder.execution_generation_id
    ext["calls"].clear()
    assert len(recorder.extension()["calls"]) == 2


def test_budgets(record):
    for recorder in (Recorder(call_byte_limit=1), Recorder(byte_limit=600)):
        record(recorder)
        ext = recorder.extension()
        assert ext["calls"] == []
        assert ext["truncated"] and ext["dropped_calls"] == 1
        assert ext["rejected_calls"] == 0
    recorder = Recorder(byte_limit=1)
    assert recorder.extension()["truncated"]


def test_rejected_calls(record):
    recorder = record(provider="")
    assert recorder.extension()["rejected_calls"] == 1
    record(recorder, messages=[{"role": "unknown", "content": "x"}])
    assert recorder.extension()["rejected_calls"] == 2
    assert not recorder.extension()["truncated"]


def test_final_extension_bound(record):
    recorder = record()
    size = len(encode(recorder.extension()))
    bounded = Recorder(byte_limit=size)
    record(bounded)
    assert len(bounded.extension()["calls"]) == 1
    record(bounded)
    assert bounded.extension()["dropped_calls"] == 1
    assert len(encode(bounded.extension())) <= size
