from ingestion.partition_router import PartitionRouter


class RedisQueue:

    def __init__(self, partitions: int = 8):
        self.router = PartitionRouter(partitions=partitions)

    # --------------------------------
    # PUSH SINGLE FRAME
    # --------------------------------

    def push(self, frame: dict) -> int:
        if not isinstance(frame, dict):
            raise ValueError("frame must be dict")

        partition = self.router.push(frame)

        print(f"[REDIS_QUEUE] pushed -> partition={partition}")

        return partition

    # --------------------------------
    # PUSH MULTIPLE FRAMES
    # --------------------------------

    def push_many(self, frames: list[dict]) -> dict:
        if not isinstance(frames, list):
            raise ValueError("frames must be list")

        result = self.router.push_many(frames)

        print(f"[REDIS_QUEUE] batch pushed -> {result}")

        return result