#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from agents import (
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from openai import AsyncOpenAI
from temporalio.client import Client
from temporalio.contrib.openai_agents import ModelActivityParameters, OpenAIAgentsPlugin
from temporalio.worker import Worker

from nixpkgs_radar.activities import (
    clone_or_fetch_nixpkgs,
    fetch_upstream_version,
    find_latest_nixos_release,
    find_maintained_packages,
    get_version_at_ref,
)
from nixpkgs_radar.workflow import NixpkgsRadarWorkflow

set_default_openai_client(
    AsyncOpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY", "not-needed"),
    ),
    use_for_tracing=False,
)
set_default_openai_api("chat_completions")
set_tracing_disabled(True)


async def main() -> None:
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, shutdown.set)
    loop.add_signal_handler(signal.SIGINT, shutdown.set)

    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        plugins=[
            OpenAIAgentsPlugin(
                model_params=ModelActivityParameters(
                    start_to_close_timeout=timedelta(minutes=10),
                    heartbeat_timeout=timedelta(seconds=30),
                ),
            ),
        ],
    )

    async with Worker(
        client,
        task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "nixpkgs-radar-queue"),
        workflows=[NixpkgsRadarWorkflow],
        activities=[
            clone_or_fetch_nixpkgs,
            find_latest_nixos_release,
            find_maintained_packages,
            get_version_at_ref,
            fetch_upstream_version,
        ],
        activity_executor=ThreadPoolExecutor(max_workers=8),
    ):
        await shutdown.wait()


asyncio.run(main())
