from typing import Any
from tools.osquery_tool import OsqueryTool


class OsqueryCollector:

    def __init__(self):
        self.osquery = OsqueryTool()

    # --------------------------------
    # DISK
    # --------------------------------

    def collect_disk(self) -> list[dict[str, Any]]:

        result = self.osquery.safe_run_query("""
        SELECT
            device_id,
            file_system,
            size,
            free_space
        FROM logical_drives;
        """)

        return result or []

    # --------------------------------
    # SERVICES
    # --------------------------------

    def collect_services(self) -> list[dict[str, Any]]:

        result = self.osquery.safe_run_query("""
        SELECT
            name,
            status,
            pid
        FROM services;
        """)

        return result or []

    # --------------------------------
    # NETWORK
    # --------------------------------

    def collect_network(self) -> list[dict[str, Any]]:

        result = self.osquery.safe_run_query("""
        SELECT
            interface,
            address,
            mask,
            broadcast,
            point_to_point
        FROM interface_addresses;
        """)

        return result or []

    # --------------------------------
    # PROCESS SNAPSHOT
    # --------------------------------

    def collect_processes(self, limit: int | None = None) -> list[dict[str, Any]]:

        query = """
        SELECT
            pid,
            name,
            path,
            resident_size,
            percent_processor_time,
            user_time,
            system_time,
            disk_bytes_read,
            disk_bytes_written,
            threads
        FROM processes
        """

        if limit:
            query += f"\nLIMIT {int(limit)}"

        query += ";"

        result = self.osquery.safe_run_query(query)

        return result or []

    # --------------------------------
    # TOP PROCESSES
    # --------------------------------

    def collect_top_cpu_processes(self, limit: int = 20):

        result = self.osquery.safe_run_query(f"""
        SELECT
            pid,
            name,
            percent_processor_time,
            user_time,
            system_time
        FROM processes
        ORDER BY percent_processor_time DESC
        LIMIT {int(limit)};
        """)

        return result or []

    def collect_top_memory_processes(self, limit: int = 20):

        result = self.osquery.safe_run_query(f"""
        SELECT
            pid,
            name,
            resident_size,
            total_size
        FROM processes
        ORDER BY resident_size DESC
        LIMIT {int(limit)};
        """)

        return result or []

    # --------------------------------
    # INVENTORY SNAPSHOT
    # --------------------------------

    def collect_inventory_snapshot(self):

        return {

            "disk": self.collect_disk(),

            "services": self.collect_services(),

            "network": self.collect_network(),

            "processes": self.collect_processes()

        }

    # --------------------------------
    # PROCESS HOTSPOTS
    # --------------------------------

    def collect_process_hotspots(self):

        return {

            "top_cpu_processes": self.collect_top_cpu_processes(),

            "top_memory_processes": self.collect_top_memory_processes()

        }