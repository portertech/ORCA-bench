"""Public artifact validator. Does not print captured prompt contents."""

import argparse
import hashlib
from pathlib import Path

from .atif import loads, validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    raw = args.artifact.read_bytes()
    try:
        artifact = validate(loads(raw))
    except Exception as exc:
        # JSON schema exception messages can include instance prompt contents.
        parser.exit(1, f"invalid artifact ({type(exc).__name__})\n")
    ext = artifact["extra"]["llmspec_model_calls"]
    status = ""
    if ext["schema_version"] == "llmspec.model-calls.v2":
        status = f" finalized={ext['finalized']} pending={ext['pending_requests']} uncertain={ext['uncertain_requests']}"
    print(
        f"valid bytes={len(raw)} sha256={hashlib.sha256(raw).hexdigest()} calls={len(ext['calls'])} truncated={ext['truncated']} dropped={ext['dropped_calls']} rejected={ext['rejected_calls']}{status}"
    )


if __name__ == "__main__":
    main()
