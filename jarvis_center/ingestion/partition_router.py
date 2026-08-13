import hashlib
import json
from typing import Any

import redis


class PartitionRouter:
    def __init__(
        self,
        partitions: int = 8,
        stream_maxlen: int = 5000,
        redis_host: str = "localhost",
        redis_port: int = 6379,
    ):
        self.partitions = partitions
        self.stream_maxlen = stream_maxlen

        self.redis = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=True
        )

    # --------------------------------
    # BUILD HOST KEY
    # --------------------------------

    def _host_key(self, frame: dict) -> str:
        host = frame.get("host", {}) or {}

        hostname = host.get("hostname") or "unknown"
        boot_id = host.get("boot_id") or "default"

        return f"{hostname}::{boot_id}"

    # --------------------------------
    # HASH → PARTITION
    # --------------------------------

    def _partition(self, host_key: str) -> int:
        digest = hashlib.sha1(host_key.encode("utf-8")).hexdigest()
        return int(digest, 16) % self.partitions

    # --------------------------------
    # STREAM NAME
    # --------------------------------

    def _stream_name(self, partition: int) -> str:
        return f"jarvis:stream:{partition}"

    # --------------------------------
    # SERIALIZE
    # --------------------------------

    def _serialize_frame(self, frame: dict[str, Any]) -> str:
        return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))

    # --------------------------------
    # PUSH SINGLE FRAME
    # --------------------------------

    def push(self, frame: dict, pipe=None) -> int:
        if not isinstance(frame, dict):
            raise ValueError("frame must be a dict")

        host_key = self._host_key(frame)
        partition = self._partition(host_key)
        stream = self._stream_name(partition)

        client = pipe if pipe is not None else self.redis

        client.xadd(
            stream,
            {"frame": self._serialize_frame(frame)},
            maxlen=self.stream_maxlen,
            approximate=True
        )

        return partition

    # --------------------------------
    # PUSH MULTIPLE FRAMES
    # --------------------------------

    def push_many(self, frames: list[dict]) -> dict[int, int]:
        if not isinstance(frames, list):
            raise ValueError("frames must be a list")

        counters: dict[int, int] = {}

        pipe = self.redis.pipeline(transaction=False)

        for frame in frames:
            partition = self.push(frame, pipe=pipe)
            counters[partition] = counters.get(partition, 0) + 1

        pipe.execute()
        return counters