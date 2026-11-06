"""Dedicated, opt-in capturing Terminus-2 agent."""

from harbor.agents.terminus_2.terminus_2 import Terminus2
from harbor.llms.litellm_config import configure_litellm_debug

from . import ARTIFACT_NAME, PRODUCER_VERSION
from .atif import atomic_write, envelope, loads
from .capture import Recorder
from .lite_llm import CapturingLiteLLM


class AgentO11yTerminus2(Terminus2):
    def __init__(
        self,
        *args,
        capture_byte_limit=67108864,
        capture_call_byte_limit=4194304,
        capture_callback_timeout=10.0,
        **kwargs,
    ):
        self._capture_limits = (capture_byte_limit, capture_call_byte_limit)
        self._capture_callback_timeout = capture_callback_timeout
        self.recorder = Recorder(*self._capture_limits)
        self._capture_has_run = False
        if (kwargs.get("trajectory_config") or {}).get("linear_history"):
            raise ValueError(
                "linear-history trajectory splitting is not supported by capture"
            )
        super().__init__(*args, **kwargs)

    def _init_llm(self, **options):
        backend = options.pop("llm_backend")
        if getattr(backend, "value", backend) != "litellm":
            raise ValueError("capture requires the LiteLLM backend")
        constructor = dict(options.pop("llm_kwargs") or {})
        configure_litellm_debug(debug=constructor.pop("litellm_debug", False))
        return CapturingLiteLLM(
            **{**options, **constructor},
            recorder=self.recorder,
            callback_timeout=self._capture_callback_timeout,
        )

    def version(self):
        return PRODUCER_VERSION

    def _reset_per_run_state(self):
        super()._reset_per_run_state()
        self.recorder = Recorder(*self._capture_limits)
        self._llm.recorder = self.recorder

    async def run(self, instruction, environment, context):
        path = self.logs_dir / ARTIFACT_NAME
        if self._capture_has_run or path.exists():
            raise ValueError(
                "multi-step/resume capture is unsupported; use a fresh trial directory"
            )
        self._capture_has_run = True
        try:
            await super().run(instruction, environment, context)
        finally:
            trajectory_path = self.logs_dir / "trajectory.json"
            trajectory = (
                loads(trajectory_path.read_bytes())
                if trajectory_path.exists()
                else None
            )
            atomic_write(path, envelope(self.recorder, self._session_id, trajectory))

    async def resume(self, *args, **kwargs):
        raise ValueError("resume capture is unsupported; use a fresh trial")
