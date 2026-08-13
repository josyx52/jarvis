"""
ProtocolDecoder — decodifica payloads TCP em pedidos de aplicação.

Protocolos suportados:
  - HTTP/1.1  (requests e responses)
  - PostgreSQL wire protocol (Simple Query, Prepared Statement)
  - Redis RESP (commands)
  - MySQL wire protocol (COM_QUERY)
  - MongoDB wire protocol (OP_QUERY, OP_MSG)
  - Kafka wire protocol (Produce, Fetch — detecção básica)

Sem dependências externas — apenas stdlib Python.
"""

import struct


# Portos → protocolo esperado (para direcionar o decoder)
_PORT_PROTOCOL: dict[int, str] = {
    5432:  "postgresql",
    3306:  "mysql",
    6379:  "redis",
    6380:  "redis",
    27017: "mongodb",
    27018: "mongodb",
    9092:  "kafka",
    9093:  "kafka",
    80:    "http",
    8080:  "http",
    8000:  "http",
    443:   "http",
    8443:  "http",
}

_HTTP_METHODS = frozenset([
    "GET", "POST", "PUT", "DELETE", "PATCH",
    "HEAD", "OPTIONS", "TRACE", "CONNECT",
])

_SQL_KEYWORDS = (
    "SELECT", "INSERT", "UPDATE", "DELETE",
    "CREATE", "DROP", "ALTER", "TRUNCATE",
    "EXPLAIN", "BEGIN", "COMMIT", "ROLLBACK",
    "MERGE", "UPSERT", "CALL", "EXEC",
)


class ProtocolDecoder:

    # --------------------------------
    # ENTRADA PRINCIPAL
    # --------------------------------

    def decode(self, packet: dict) -> dict | None:
        """
        Recebe um packet_data do PacketCapture e tenta decodificar.
        Retorna dict com protocolo e dados ou None se não reconhecido.
        """
        payload  = packet.get("payload", b"")
        dst_port = packet.get("dst_port", 0)
        src_port = packet.get("src_port", 0)

        if not payload:
            return None

        # Protocolo pelo porto de destino (cliente → servidor)
        proto = _PORT_PROTOCOL.get(dst_port)
        if proto:
            result = self._decode_by_proto(proto, payload, packet)
            if result:
                return result

        # Protocolo pelo porto de origem (resposta servidor → cliente)
        proto = _PORT_PROTOCOL.get(src_port)
        if proto:
            result = self._decode_by_proto(proto, payload, packet)
            if result:
                return result

        # Fallback: tentar todos os decoders por conteúdo
        for fn in (self._http, self._redis, self._postgresql, self._mysql):
            result = fn(payload)
            if result:
                return self._enrich(result, packet)

        return None

    def _decode_by_proto(self, proto: str, payload: bytes, packet: dict) -> dict | None:
        fn = {
            "http":       self._http,
            "postgresql": self._postgresql,
            "redis":      self._redis,
            "mysql":      self._mysql,
            "mongodb":    self._mongodb,
            "kafka":      self._kafka,
        }.get(proto)

        if fn:
            result = fn(payload)
            if result:
                return self._enrich(result, packet)
        return None

    def _enrich(self, result: dict, packet: dict) -> dict:
        result["src_ip"]   = packet.get("src_ip", "")
        result["src_port"] = packet.get("src_port", 0)
        result["dst_ip"]   = packet.get("dst_ip", "")
        result["dst_port"] = packet.get("dst_port", 0)
        result["timestamp"] = packet.get("timestamp", 0)
        return result

    # --------------------------------
    # HTTP/1.1
    # --------------------------------

    def _http(self, payload: bytes) -> dict | None:
        try:
            text  = payload[:8192].decode("utf-8", errors="ignore")
            lines = text.split("\r\n")
            if not lines:
                return None

            first = lines[0]

            # Request
            parts = first.split(" ")
            if len(parts) >= 2 and parts[0] in _HTTP_METHODS:
                method = parts[0]
                path   = parts[1]
                host   = ""
                content_type = ""
                for line in lines[1:30]:
                    low = line.lower()
                    if low.startswith("host:"):
                        host = line[5:].strip()
                    elif low.startswith("content-type:"):
                        content_type = line[13:].strip()
                return {
                    "protocol":     "http",
                    "direction":    "request",
                    "method":       method,
                    "path":         path,
                    "host":         host,
                    "content_type": content_type,
                    "span_name":    f"HTTP {method} {path}",
                }

            # Response
            if first.startswith("HTTP/1"):
                status_code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                return {
                    "protocol":     "http",
                    "direction":    "response",
                    "status_code":  status_code,
                    "span_name":    f"HTTP {status_code}",
                }
        except Exception:
            pass
        return None

    # --------------------------------
    # POSTGRESQL WIRE PROTOCOL
    # --------------------------------

    def _postgresql(self, payload: bytes) -> dict | None:
        try:
            if len(payload) < 5:
                return None

            msg_type = payload[0]

            # Simple Query: 'Q' (0x51)
            if msg_type == ord('Q'):
                length = struct.unpack(">I", payload[1:5])[0]
                query  = payload[5:5 + length - 4].decode("utf-8", errors="ignore").rstrip("\x00")
                if query:
                    return {
                        "protocol":    "postgresql",
                        "type":        "simple_query",
                        "query":       query[:2000],
                        "query_type":  self._sql_type(query),
                        "span_name":   f"postgresql {self._sql_type(query)}",
                    }

            # Prepared Statement Parse: 'P' (0x50)
            if msg_type == ord('P'):
                length = struct.unpack(">I", payload[1:5])[0]
                body   = payload[5:5 + length - 4].decode("utf-8", errors="ignore")
                parts  = body.split("\x00")
                if len(parts) >= 2 and parts[1]:
                    q = parts[1]
                    return {
                        "protocol":   "postgresql",
                        "type":       "prepared_statement",
                        "query":      q[:2000],
                        "query_type": self._sql_type(q),
                        "span_name":  f"postgresql {self._sql_type(q)} (prepared)",
                    }

            # Execute: 'E' (0x45)
            if msg_type == ord('E'):
                return {
                    "protocol":  "postgresql",
                    "type":      "execute",
                    "span_name": "postgresql execute",
                }

        except Exception:
            pass
        return None

    # --------------------------------
    # REDIS RESP
    # --------------------------------

    def _redis(self, payload: bytes) -> dict | None:
        try:
            text = payload[:4096].decode("utf-8", errors="ignore")

            # RESP Array: *N\r\n$len\r\ntoken\r\n...
            if text.startswith("*"):
                lines = text.split("\r\n")
                tokens = []
                i = 1
                while i < len(lines) - 1 and len(tokens) < 8:
                    if lines[i].startswith("$"):
                        i += 1
                        if i < len(lines):
                            tokens.append(lines[i])
                    i += 1

                if tokens:
                    command = tokens[0].upper()
                    key     = tokens[1] if len(tokens) > 1 else ""
                    return {
                        "protocol":  "redis",
                        "type":      "command",
                        "command":   command,
                        "key":       key,
                        "span_name": f"redis {command}",
                    }

            # Inline: PING, INFO, QUIT
            stripped = text.strip().upper().split("\r\n")[0].split(" ")[0]
            if stripped in ("PING", "INFO", "QUIT", "AUTH", "SELECT"):
                return {
                    "protocol":  "redis",
                    "type":      "inline",
                    "command":   stripped,
                    "key":       "",
                    "span_name": f"redis {stripped}",
                }

        except Exception:
            pass
        return None

    # --------------------------------
    # MYSQL WIRE PROTOCOL
    # --------------------------------

    def _mysql(self, payload: bytes) -> dict | None:
        try:
            if len(payload) < 5:
                return None

            # Packet: 3 bytes length (LE) + 1 byte seq + 1 byte command
            pkt_len = struct.unpack("<I", payload[:3] + b"\x00")[0]
            cmd     = payload[4]

            # COM_QUERY = 0x03
            if cmd == 0x03 and pkt_len > 1:
                query = payload[5:5 + pkt_len - 1].decode("utf-8", errors="ignore")
                if query and len(query) >= 3:
                    return {
                        "protocol":   "mysql",
                        "type":       "query",
                        "query":      query[:2000],
                        "query_type": self._sql_type(query),
                        "span_name":  f"mysql {self._sql_type(query)}",
                    }

            # COM_STMT_EXECUTE = 0x17
            if cmd == 0x17:
                return {
                    "protocol":  "mysql",
                    "type":      "execute_prepared",
                    "span_name": "mysql execute_prepared",
                }

        except Exception:
            pass
        return None

    # --------------------------------
    # MONGODB WIRE PROTOCOL
    # --------------------------------

    def _mongodb(self, payload: bytes) -> dict | None:
        try:
            if len(payload) < 16:
                return None

            # Standard wire protocol header: length(4) + requestId(4) + responseTo(4) + opCode(4)
            msg_length = struct.unpack("<I", payload[0:4])[0]
            op_code    = struct.unpack("<I", payload[12:16])[0]

            # OP_MSG = 2013 (MongoDB 3.6+)
            if op_code == 2013 and len(payload) >= 21:
                # Tentar extrair o nome da operação do documento BSON
                body = payload[20:]
                op   = self._extract_mongo_op(body)
                return {
                    "protocol":  "mongodb",
                    "type":      "op_msg",
                    "operation": op,
                    "span_name": f"mongodb {op}",
                }

            # OP_QUERY = 2004 (legacy)
            if op_code == 2004 and len(payload) >= 20:
                coll = self._extract_cstring(payload, 20)
                return {
                    "protocol":   "mongodb",
                    "type":       "op_query",
                    "collection": coll,
                    "span_name":  f"mongodb query {coll}",
                }

        except Exception:
            pass
        return None

    def _extract_mongo_op(self, bson_data: bytes) -> str:
        """Tenta extrair o nome do comando do primeiro documento BSON."""
        try:
            # BSON document: length(4) + elements
            if len(bson_data) < 5:
                return "unknown"
            pos = 4
            while pos < len(bson_data) - 1:
                elem_type = bson_data[pos]
                if elem_type == 0:
                    break
                pos += 1
                # Ler key (cstring)
                end = bson_data.index(b"\x00", pos)
                key = bson_data[pos:end].decode("utf-8", errors="ignore")
                return key.lower()  # primeiro campo = nome do comando
        except Exception:
            pass
        return "unknown"

    def _extract_cstring(self, data: bytes, offset: int) -> str:
        try:
            end = data.index(b"\x00", offset)
            return data[offset:end].decode("utf-8", errors="ignore")
        except Exception:
            return ""

    # --------------------------------
    # KAFKA WIRE PROTOCOL
    # --------------------------------

    def _kafka(self, payload: bytes) -> dict | None:
        try:
            if len(payload) < 8:
                return None

            # Kafka request: length(4) + api_key(2) + api_version(2) + correlation_id(4)
            api_key = struct.unpack(">H", payload[4:6])[0]

            _API_KEYS = {
                0:  "Produce",
                1:  "Fetch",
                2:  "ListOffsets",
                3:  "Metadata",
                8:  "OffsetCommit",
                9:  "OffsetFetch",
                10: "FindCoordinator",
                11: "JoinGroup",
                12: "Heartbeat",
                19: "CreateTopics",
                20: "DeleteTopics",
            }

            op = _API_KEYS.get(api_key)
            if op:
                return {
                    "protocol":  "kafka",
                    "type":      "request",
                    "api_key":   api_key,
                    "operation": op,
                    "span_name": f"kafka {op}",
                }
        except Exception:
            pass
        return None

    # --------------------------------
    # HELPERS SQL
    # --------------------------------

    def _sql_type(self, query: str) -> str:
        q = query.strip().upper().lstrip("(")
        for kw in _SQL_KEYWORDS:
            if q.startswith(kw):
                return kw
        return "OTHER"
