from typing import Any


class Predictor:
    def predict(self, snapshots: list[dict]) -> list[dict]:
        if len(snapshots) < 3:
            return []

        current = snapshots[0]
        previous = snapshots[1]
        older = snapshots[2]

        predictions = []

        predictions.extend(self._disk_predictions(current, previous, older))
        predictions.extend(self._memory_predictions(current, previous, older))
        predictions.extend(self._cpu_predictions(current, previous, older))
        predictions.extend(self._network_connection_predictions(current, previous, older))
        predictions.extend(self._network_throughput_predictions(current, previous, older))

        return predictions

    # --------------------------------
    # DISK
    # --------------------------------

    def _disk_predictions(self, current: dict, previous: dict, older: dict) -> list[dict]:
        predictions = []

        current_disks = {d.get("device_id"): d for d in (current.get("disk", []) or [])}
        previous_disks = {d.get("device_id"): d for d in (previous.get("disk", []) or [])}
        older_disks = {d.get("device_id"): d for d in (older.get("disk", []) or [])}

        for device_id, curr in current_disks.items():
            prev = previous_disks.get(device_id)
            old = older_disks.get(device_id)

            if not prev or not old:
                continue

            curr_free = self._to_int(curr.get("free_space"))
            prev_free = self._to_int(prev.get("free_space"))
            old_free = self._to_int(old.get("free_space"))

            drop1 = old_free - prev_free
            drop2 = prev_free - curr_free

            if drop1 > 0 and drop2 > 0:
                avg_drop = (drop1 + drop2) / 2

                if avg_drop > 500_000_000:
                    intervals_left = curr_free / avg_drop if avg_drop else 0
                    severity = "high" if intervals_left < 10 else "medium"

                    predictions.append({
                        "prediction_type": "disk_exhaustion_trend",
                        "severity": severity,
                        "summary": (
                            f"Disco {device_id} em tendência de esgotamento. "
                            f"Estimativa: {intervals_left:.1f} intervalos até encher."
                        ),
                        "prediction": {
                            "device_id": device_id,
                            "current_free_space": curr_free,
                            "average_drop_per_interval": avg_drop,
                            "estimated_intervals_left": round(intervals_left, 2)
                        }
                    })

        return predictions

    # --------------------------------
    # MEMORY
    # --------------------------------

    def _memory_predictions(self, current: dict, previous: dict, older: dict) -> list[dict]:
        predictions = []

        curr_metrics = current.get("metrics") or {}
        prev_metrics = previous.get("metrics") or {}
        old_metrics = older.get("metrics") or {}

        curr_mem = self._to_float(curr_metrics.get("memory_percent"))
        prev_mem = self._to_float(prev_metrics.get("memory_percent"))
        old_mem = self._to_float(old_metrics.get("memory_percent"))

        if curr_mem > prev_mem > old_mem and curr_mem >= 80:
            avg_growth = ((prev_mem - old_mem) + (curr_mem - prev_mem)) / 2
            severity = "high" if curr_mem >= 90 else "medium"

            predictions.append({
                "prediction_type": "memory_pressure_trend",
                "severity": severity,
                "summary": (
                    f"Memória em tendência de pressão. Uso atual: {curr_mem:.1f}%."
                ),
                "prediction": {
                    "current_memory_percent": round(curr_mem, 2),
                    "average_growth_per_interval": round(avg_growth, 2)
                }
            })

        return predictions

    # --------------------------------
    # CPU
    # --------------------------------

    def _cpu_predictions(self, current: dict, previous: dict, older: dict) -> list[dict]:
        predictions = []

        curr_metrics = current.get("metrics") or {}
        prev_metrics = previous.get("metrics") or {}
        old_metrics = older.get("metrics") or {}

        curr_cpu = self._to_float(curr_metrics.get("cpu_percent"))
        prev_cpu = self._to_float(prev_metrics.get("cpu_percent"))
        old_cpu = self._to_float(old_metrics.get("cpu_percent"))

        if curr_cpu > prev_cpu > old_cpu and curr_cpu >= 75:
            avg_growth = ((prev_cpu - old_cpu) + (curr_cpu - prev_cpu)) / 2
            severity = "high" if curr_cpu >= 90 else "medium"

            predictions.append({
                "prediction_type": "cpu_saturation_trend",
                "severity": severity,
                "summary": (
                    f"CPU em tendência de saturação. Uso atual: {curr_cpu:.1f}%."
                ),
                "prediction": {
                    "current_cpu_percent": round(curr_cpu, 2),
                    "average_growth_per_interval": round(avg_growth, 2)
                }
            })

        return predictions

    # --------------------------------
    # NETWORK CONNECTIONS
    # --------------------------------

    def _network_connection_predictions(self, current: dict, previous: dict, older: dict) -> list[dict]:
        predictions = []

        curr_conn = len(current.get("connections", []) or [])
        prev_conn = len(previous.get("connections", []) or [])
        old_conn = len(older.get("connections", []) or [])

        if curr_conn > prev_conn > old_conn and curr_conn >= 100:
            avg_growth = ((prev_conn - old_conn) + (curr_conn - prev_conn)) / 2
            severity = "high" if curr_conn >= 300 else "medium"

            predictions.append({
                "prediction_type": "network_connection_growth_trend",
                "severity": severity,
                "summary": (
                    f"Número de conexões em crescimento contínuo. Total atual: {curr_conn}."
                ),
                "prediction": {
                    "current_connections": curr_conn,
                    "average_growth_per_interval": round(avg_growth, 2)
                }
            })

        return predictions

    # --------------------------------
    # NETWORK THROUGHPUT
    # --------------------------------

    def _network_throughput_predictions(self, current: dict, previous: dict, older: dict) -> list[dict]:
        predictions = []

        curr_metrics = current.get("metrics") or {}
        prev_metrics = previous.get("metrics") or {}
        old_metrics = older.get("metrics") or {}

        curr_total = self._to_int(curr_metrics.get("net_bytes_sent")) + self._to_int(curr_metrics.get("net_bytes_recv"))
        prev_total = self._to_int(prev_metrics.get("net_bytes_sent")) + self._to_int(prev_metrics.get("net_bytes_recv"))
        old_total = self._to_int(old_metrics.get("net_bytes_sent")) + self._to_int(old_metrics.get("net_bytes_recv"))

        growth1 = prev_total - old_total
        growth2 = curr_total - prev_total

        if growth1 > 0 and growth2 > 0:
            avg_growth = (growth1 + growth2) / 2

            if avg_growth > 100_000_000:
                predictions.append({
                    "prediction_type": "network_throughput_saturation_trend",
                    "severity": "medium",
                    "summary": (
                        "Throughput de rede em crescimento contínuo detectado."
                    ),
                    "prediction": {
                        "current_total_bytes": curr_total,
                        "average_growth_per_interval": round(avg_growth, 2)
                    }
                })

        return predictions

    def _to_int(self, value: Any) -> int:
        try:
            return int(value)
        except Exception:
            return 0

    def _to_float(self, value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0