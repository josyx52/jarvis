import json
import time
import uuid

import redis

from runtime.central_runtime import CentralRuntime


class FrameProcessor:
    def __init__(self, partition: int = 0):
        self.partition = partition

        self.stream = f"jarvis:stream:{partition}"
        self.group = "jarvis-processors"
        self.consumer = f"processor-{partition}-{uuid.uuid4().hex}"
        self.dlq_stream = f"jarvis:dlq:{partition}"

        self.redis = redis.Redis(
            host="localhost",
            port=6379,
            decode_responses=True
        )

        self.runtime = CentralRuntime()

        self.read_count = 500
        self.block_ms = 3000
        self.idle_sleep = 0.2
        self.claim_min_idle_ms = 60000
        self._trim_counter = 0
        self._trim_every = 100   # trim stream a cada 100 iteracoes
        self._stream_maxlen = 5000

        self._ensure_group()

    # --------------------------------
    # GROUP
    # --------------------------------

    def _ensure_group(self):
        try:
            self.redis.xgroup_create(
                name=self.stream,
                groupname=self.group,
                id="0",
                mkstream=True
            )
            print(f"[processor p{self.partition}] group created")
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    # --------------------------------
    # SAFE JSON LOAD
    # --------------------------------

    def _decode_frame(self, raw: str) -> dict | None:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
            return None
        except Exception as e:
            print(f"[processor p{self.partition}] invalid frame json: {e}")
            return None

    # --------------------------------
    # RUNTIME DISPATCH
    # --------------------------------

    def _process_frame(self, frame: dict):
        if hasattr(self.runtime, "process_frame"):
            return self.runtime.process_frame(frame)

        if hasattr(self.runtime, "process"):
            return self.runtime.process(frame)

        raise AttributeError(
            "CentralRuntime has neither process_frame(frame) nor process(frame)"
        )

    # --------------------------------
    # DLQ
    # --------------------------------

    def _send_to_dlq(self, msg_id: str, frame: dict | None, error: str):
        try:
            payload = {
                "original_stream": self.stream,
                "original_message_id": msg_id,
                "consumer": self.consumer,
                "error": error,
                "failed_at": str(time.time()),
                "frame": json.dumps(frame) if frame is not None else ""
            }
            self.redis.xadd(self.dlq_stream, payload, maxlen=10000, approximate=True)
        except Exception as dlq_err:
            print(f"[processor p{self.partition}] DLQ FAILED id={msg_id} error={dlq_err}")

    # --------------------------------
    # CLAIM STALE PENDING
    # --------------------------------

    def _claim_stale_messages(self):
        try:
            pending_summary = self.redis.xpending(self.stream, self.group)
        except Exception as e:
            print(f"[processor p{self.partition}] XPENDING FAILED error={e}")
            return []

        if not pending_summary:
            return []

        total_pending = pending_summary.get("pending", 0)
        if total_pending == 0:
            return []

        try:
            consumers = self.redis.xpending_range(
                self.stream,
                self.group,
                min="-",
                max="+",
                count=min(100, total_pending)
            )
        except Exception as e:
            print(f"[processor p{self.partition}] XPENDING RANGE FAILED error={e}")
            return []

        stale_ids = [
            item["message_id"]
            for item in consumers
            if item.get("idle", 0) >= self.claim_min_idle_ms
        ]

        if not stale_ids:
            return []

        try:
            claimed = self.redis.xclaim(
                self.stream,
                self.group,
                self.consumer,
                min_idle_time=self.claim_min_idle_ms,
                message_ids=stale_ids
            )
            if claimed:
                print(
                    f"[processor p{self.partition}] claimed={len(claimed)} stale messages"
                )
            return claimed
        except Exception as e:
            print(f"[processor p{self.partition}] XCLAIM FAILED error={e}")
            return []

    # --------------------------------
    # HANDLE RECORDS
    # --------------------------------

    def _handle_records(self, records):
        for msg_id, fields in records:
            raw_frame = fields.get("frame")

            if not raw_frame:
                print(
                    f"[processor p{self.partition}] message without 'frame' field: {msg_id}"
                )
                try:
                    self.redis.xack(self.stream, self.group, msg_id)
                except Exception:
                    pass
                continue

            frame = self._decode_frame(raw_frame)

            if frame is None:
                try:
                    self._send_to_dlq(msg_id, None, "invalid frame json")
                    self.redis.xack(self.stream, self.group, msg_id)
                except Exception:
                    pass
                continue

            frame_type = frame.get("frame_type", "unknown")
            hostname = (frame.get("host") or {}).get("hostname", "unknown")

            try:
                self._process_frame(frame)
                self.redis.xack(self.stream, self.group, msg_id)
                print(
                    f"[processor p{self.partition}] ack frame_type={frame_type} host={hostname} id={msg_id}"
                )
            except Exception as e:
                err = str(e)
                print(
                    f"[processor p{self.partition}] PROCESS FAILED frame_type={frame_type} host={hostname} id={msg_id} error={err}"
                )

                try:
                    self._send_to_dlq(msg_id, frame, err)
                    self.redis.xack(self.stream, self.group, msg_id)
                    print(
                        f"[processor p{self.partition}] moved to DLQ and acked frame_type={frame_type} host={hostname} id={msg_id}"
                    )
                except Exception as ack_err:
                    print(
                        f"[processor p{self.partition}] FAIL-ACK FAILED frame_type={frame_type} host={hostname} id={msg_id} error={ack_err}"
                    )

    # --------------------------------
    # MAIN LOOP
    # --------------------------------

    def run(self):
        print(
            f"[processor p{self.partition}] started consumer={self.consumer} stream={self.stream}"
        )

        while True:
            try:
                # Auto-trim periodico para evitar acumulacao
                self._trim_counter += 1
                if self._trim_counter % self._trim_every == 0:
                    try:
                        self.redis.xtrim(self.stream, maxlen=self._stream_maxlen, approximate=True)
                    except Exception:
                        pass

                stale = self._claim_stale_messages()
                if stale:
                    self._handle_records(stale)

                response = self.redis.xreadgroup(
                    groupname=self.group,
                    consumername=self.consumer,
                    streams={self.stream: ">"},
                    count=self.read_count,
                    block=self.block_ms
                )

                if not response:
                    time.sleep(self.idle_sleep)
                    continue

                for stream_name, records in response:
                    self._handle_records(records)

            except KeyboardInterrupt:
                print(f"[processor p{self.partition}] stopping")
                break

            except Exception as e:
                print(f"[processor p{self.partition}] LOOP FAILED error={e}")
                time.sleep(1)