#!/bin/sh
set -eu
mkdir -p /logs/verifier
if [ "$(cat /work/capture-smoke.txt 2>/dev/null)" = OK ]; then
  printf '{"reward":1.0}\n' > /logs/verifier/reward.json
else
  printf '{"reward":0.0}\n' > /logs/verifier/reward.json
fi
printf '{"mode":"synthetic_capture_smoke"}\n' > /logs/verifier/reward-details.json
