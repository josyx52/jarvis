import json
import time
import traceback

from agent_core.config import BATCH_SIZE, SEND_INTERVAL_SECONDS
from agent_core.core_client import CoreClient
from storage.queue_store import QueueStore


class SenderWorker:

    def __init__(self, store):

        self.store = store
        self.queue = QueueStore(store)
        self.client = CoreClient()

        print("[Sender] initialized")

    def run(self):

        rows = self.queue.get_pending(limit=BATCH_SIZE)

        if not rows:
            return

        print(f"[Sender] sending batch size={len(rows)}")

        batch = []
        ids = []
        attempts_map = {}

        for r in rows:

            item_id = int(r["id"])

            ids.append(item_id)
            attempts_map[item_id] = int(r["attempts"])

            try:
                batch.append(json.loads(r["payload"]))
            except Exception:
                print(f"[Sender] invalid payload id={item_id}")

        if not batch:
            return

        try:

            success, error = self.client.send(batch)

        except Exception as e:

            print("[Sender] send error")
            traceback.print_exc()

            self.queue.mark_retry(ids, attempts_map, str(e))

            return

        if success:

            print(f"[Sender] sent {len(ids)} events")

            self.queue.mark_sent(ids)

        else:

            print("[Sender] send failed:", error)

            self.queue.mark_retry(ids, attempts_map, error)

        time.sleep(SEND_INTERVAL_SECONDS)
