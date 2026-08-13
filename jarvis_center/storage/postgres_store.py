import psycopg2
import json
from psycopg2.extras import execute_values


class PostgresStore:

    def __init__(self):

        self.conn = psycopg2.connect(
            host="localhost",
            database="jarvis",
            user="postgres",
            password="Vermelho555@"
        )

        self.buffer_events = []
        self.buffer_correlations = []
        self.buffer_predictions = []
        self.buffer_alerts = []
        self.buffer_investigations = []

        self.max_buffer = 100

    # --------------------------------
    # SNAPSHOT (🔥 NOVO - CRÍTICO)
    # --------------------------------

    def insert_snapshot(self, host: str, snapshot: dict):

        cursor = self.conn.cursor()

        query = """
        INSERT INTO snapshots (
            host,
            snapshot_time,
            cpu_percent,
            memory_percent,
            disk_percent,
            memory_used,
            disk_used,
            disk_free,
            process_count,
            service_count,
            network_count,
            trace_count,
            raw_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        cursor.execute(query, (
            host,
            snapshot.get("timestamp"),
            snapshot.get("cpu", {}).get("percent", 0),
            snapshot.get("memory", {}).get("percent", 0),
            snapshot.get("disk", {}).get("percent", 0),
            snapshot.get("memory", {}).get("used", 0),
            snapshot.get("disk", {}).get("used", 0),
            snapshot.get("disk", {}).get("free", 0),
            len(snapshot.get("processes", [])),
            len(snapshot.get("services", [])),
            len(snapshot.get("network", [])),
            len(snapshot.get("connections", [])),
            json.dumps(snapshot)
        ))

        self.conn.commit()

    # --------------------------------
    # FLUSH
    # --------------------------------

    def flush(self):

        cursor = self.conn.cursor()

        if self.buffer_events:
            execute_values(
                cursor,
                """
                INSERT INTO events(host, event_type, severity, payload)
                VALUES %s
                """,
                self.buffer_events
            )
            self.buffer_events.clear()

        if self.buffer_correlations:
            execute_values(
                cursor,
                """
                INSERT INTO correlations(host, correlation_type, severity, payload)
                VALUES %s
                """,
                self.buffer_correlations
            )
            self.buffer_correlations.clear()

        if self.buffer_predictions:
            execute_values(
                cursor,
                """
                INSERT INTO predictions(host, prediction_type, severity, payload)
                VALUES %s
                """,
                self.buffer_predictions
            )
            self.buffer_predictions.clear()

        if self.buffer_alerts:
            execute_values(
                cursor,
                """
                INSERT INTO alerts(host, severity, title, payload)
                VALUES %s
                """,
                self.buffer_alerts
            )
            self.buffer_alerts.clear()

        if self.buffer_investigations:
            execute_values(
                cursor,
                """
                INSERT INTO investigations(host, severity, details)
                VALUES %s
                """,
                self.buffer_investigations
            )
            self.buffer_investigations.clear()

        self.conn.commit()

    # --------------------------------
    # INSERTS
    # --------------------------------

    def insert_event(self, host, event_type, severity, payload):

        self.buffer_events.append(
            (host, event_type, severity, json.dumps(payload))
        )

        if len(self.buffer_events) >= self.max_buffer:
            self.flush()

    def insert_correlation(self, host, correlation_type, severity, payload):

        self.buffer_correlations.append(
            (host, correlation_type, severity, json.dumps(payload))
        )

        if len(self.buffer_correlations) >= self.max_buffer:
            self.flush()

    def insert_prediction(self, host, prediction_type, severity, payload):

        self.buffer_predictions.append(
            (host, prediction_type, severity, json.dumps(payload))
        )

        if len(self.buffer_predictions) >= self.max_buffer:
            self.flush()

    def insert_alert(self, host, severity, title, payload):

        self.buffer_alerts.append(
            (host, severity, title, json.dumps(payload))
        )

        if len(self.buffer_alerts) >= self.max_buffer:
            self.flush()

    def insert_investigation(self, host, severity, details):

        self.buffer_investigations.append(
            (host, severity, details)
        )

        if len(self.buffer_investigations) >= self.max_buffer:
            self.flush()