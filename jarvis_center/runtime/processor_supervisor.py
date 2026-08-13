import subprocess
import sys
import time
from pathlib import Path


class ProcessorSupervisor:
    def __init__(
        self,
        partitions: int = 8,
        min_processors: int = 8,
        max_processors: int = 16,
        scale_up_threshold: int = 5000,
        scale_down_threshold: int = 500,
    ):
        self.partitions = partitions
        self.min_processors = min_processors
        self.max_processors = max_processors
        self.scale_up_threshold = scale_up_threshold
        self.scale_down_threshold = scale_down_threshold

        self.root = Path(__file__).resolve().parent.parent
        self.run_processor_path = self.root / "run_processor.py"

        self.processes: dict[int, subprocess.Popen] = {}

    # --------------------------------
    # START ONE PROCESS PER PARTITION
    # --------------------------------

    def _start_partition_processor(self, partition: int):
        if partition in self.processes:
            proc = self.processes[partition]
            if proc.poll() is None:
                return

        cmd = [sys.executable, str(self.run_processor_path), str(partition)]

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.root)
        )

        self.processes[partition] = proc
        print(f"[supervisor] started processor partition={partition} pid={proc.pid}")

    # --------------------------------
    # KEEP ALL PARTITIONS ALIVE
    # --------------------------------

    def _ensure_processors(self):
        for partition in range(self.partitions):
            self._start_partition_processor(partition)

    # --------------------------------
    # RESTART DEAD PROCESSES
    # --------------------------------

    def _restart_dead(self):
        for partition, proc in list(self.processes.items()):
            if proc.poll() is not None:
                print(
                    f"[supervisor] processor partition={partition} exited rc={proc.returncode}, restarting"
                )
                self._start_partition_processor(partition)

    # --------------------------------
    # STOP ALL
    # --------------------------------

    def _stop_all(self):
        for partition, proc in self.processes.items():
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

        time.sleep(2)

        for partition, proc in self.processes.items():
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

    # --------------------------------
    # MAIN LOOP
    # --------------------------------

    def run(self):
        print("[supervisor] starting")

        self._ensure_processors()

        try:
            while True:
                self._restart_dead()
                print(f"[supervisor] backlog=0 processors={len(self.processes)}")
                time.sleep(5)

        except KeyboardInterrupt:
            print("[supervisor] stopping")
            self._stop_all()