from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class StageTelemetryRecorder:
    run_mode: str = "cold"
    worker_count: int = 1
    stages: list[dict[str, Any]] = field(default_factory=list)
    human_wait_ms: int = 0
    prompt_task_count: int = 0
    prompt_inline_characters: int = 0
    unresolved_item_count: int = 0
    started_at: str = field(default_factory=_utc_now)
    _machine_started: float = field(default_factory=time.perf_counter, repr=False)

    def begin_stage(
        self,
        name: str,
        *,
        input_hashes: dict[str, str] | None = None,
        worker_count: int | None = None,
    ) -> dict[str, Any]:
        """Start a real timer for pipeline code that cannot use an indented context block."""
        return {
            "name": name,
            "started_at": _utc_now(),
            "started": time.perf_counter(),
            "input_hashes": input_hashes or {},
            "worker_count": worker_count or self.worker_count,
            "finished": False,
        }

    def finish_stage(
        self,
        token: dict[str, Any],
        *,
        stage_result: dict[str, Any] | None = None,
        output_hashes: dict[str, str] | None = None,
        cache_status: str = "MISS",
        **facts: Any,
    ) -> None:
        if token.get("finished"):
            raise ValueError(f"telemetry stage already finished: {token.get('name')}")
        token["finished"] = True
        result = stage_result if isinstance(stage_result, dict) else {}
        findings = result.get("findings") if isinstance(result.get("findings"), list) else []
        duration_ms = max(0, round((time.perf_counter() - float(token["started"])) * 1000, 3))
        self.stages.append({
            "stage": str(token["name"]),
            "started_at": str(token["started_at"]),
            "finished_at": _utc_now(),
            "duration_ms": duration_ms,
            "input_hashes": dict(token["input_hashes"]),
            "output_hashes": output_hashes or {},
            "cache_status": cache_status,
            "worker_count": int(token["worker_count"]),
            "finding_count": len(findings),
            **({"status": result.get("status")} if result.get("status") else {}),
            **facts,
        })

    @contextmanager
    def stage(
        self,
        name: str,
        *,
        input_hashes: dict[str, str] | None = None,
        worker_count: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        token = self.begin_stage(name, input_hashes=input_hashes, worker_count=worker_count)
        facts: dict[str, Any] = {}
        try:
            yield facts
        finally:
            cache_status = str(facts.pop("cache_status", "MISS"))
            output_hashes = facts.pop("output_hashes", {})
            finding_count = int(facts.pop("finding_count", 0))
            self.finish_stage(
                token,
                output_hashes=output_hashes,
                cache_status=cache_status,
                finding_count=finding_count,
                **facts,
            )

    def record_prompt(self, *, task_count: int, inline_characters: int, unresolved_items: int) -> None:
        self.prompt_task_count += max(0, task_count)
        self.prompt_inline_characters += max(0, inline_characters)
        self.unresolved_item_count += max(0, unresolved_items)

    def report(self) -> dict[str, Any]:
        total_machine_ms = max(0, round((time.perf_counter() - self._machine_started) * 1000, 3))
        cache_hits = sum(1 for stage in self.stages if stage.get("cache_status") == "HIT")
        cache_lookups = sum(1 for stage in self.stages if stage.get("cache_status") in {"HIT", "MISS", "CORRUPT"})
        return {
            "schema_version": "stage-telemetry-v3",
            "started_at": self.started_at,
            "finished_at": _utc_now(),
            "run_mode": self.run_mode,
            "machine_duration_ms": total_machine_ms,
            "human_wait_ms": self.human_wait_ms,
            "prompt_task_count": self.prompt_task_count,
            "prompt_inline_characters": self.prompt_inline_characters,
            "unresolved_item_count": self.unresolved_item_count,
            "cache": {
                "hits": cache_hits,
                "lookups": cache_lookups,
                "hit_rate": round(cache_hits / cache_lookups, 6) if cache_lookups else 0.0,
            },
            "worker_count": self.worker_count,
            "stages": self.stages,
        }
