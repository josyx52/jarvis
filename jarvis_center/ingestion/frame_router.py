from frame_queue.redis_queue import RedisQueue


class FrameRouter:

    def __init__(self, partitions: int = 8):
        self.queue = RedisQueue(partitions=partitions)

    # --------------------------------
    # ROUTE SINGLE FRAME
    # --------------------------------

    def route(self, frame: dict) -> int:
        if not isinstance(frame, dict):
            raise ValueError("frame must be dict")

        partition = self.queue.push(frame)

        print(f"[FRAME_ROUTER] routed -> partition={partition}")

        return partition

    # --------------------------------
    # ROUTE MULTIPLE FRAMES
    # --------------------------------

    def route_many(self, frames: list[dict]) -> dict:
        result = self.queue.push_many(frames)

        print(f"[FRAME_ROUTER] routed batch -> {result}")

        return result