from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ValidatorJob:
    name: str
    command: list[str]
    report: Path
    timeout: int = 600


def run_validators_parallel(
    jobs: list[ValidatorJob],
    execute: Callable[[list[str], Path, int], dict],
    *,
    max_workers: int = 4,
) -> list[dict]:
    """并行执行只读静态校验，并按传入顺序稳定返回。"""
    if not jobs:
        return []
    workers = max(1, min(max_workers, len(jobs)))
    indexed: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="v3-validator") as pool:
        futures = {
            pool.submit(execute, job.command, job.report, job.timeout): (index, job)
            for index, job in enumerate(jobs)
        }
        for future in as_completed(futures):
            index, job = futures[future]
            try:
                payload = future.result()
            except Exception as exc:  # 防御性 fail-closed；正常 execute 已封装异常。
                payload = {
                    "status": "BLOCKED",
                    "stage": job.name,
                    "findings": [{"code": "VALIDATOR_WORKER_FAILED", "message": str(exc)}],
                }
            payload["stage"] = job.name
            indexed[index] = payload
    return [indexed[index] for index in range(len(jobs))]
