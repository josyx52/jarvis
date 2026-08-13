import psutil
import time


class MetricsCollector:

    def collect(self):

        cpu = psutil.cpu_percent(interval=1)

        memory = psutil.virtual_memory()

        disk = psutil.disk_usage("/")

        net = psutil.net_io_counters()

        return {
            "timestamp": time.time(),
            "cpu_percent": cpu,
            "memory_percent": memory.percent,
            "memory_used": memory.used,
            "disk_percent": disk.percent,
            "disk_used": disk.used,
            "disk_free": disk.free,
            "net_bytes_sent": net.bytes_sent,
            "net_bytes_recv": net.bytes_recv
        }