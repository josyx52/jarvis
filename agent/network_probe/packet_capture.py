"""
PacketCapture — captura pacotes TCP ao nível NDIS via WinDivert.

Modo primário: WinDivert SNIFF (pydivert)
  - Captura ao nível NDIS — abaixo do TCP/IP stack
  - Vê TODO o tráfego incluindo loopback (127.0.0.1 ↔ 127.0.0.1)
  - Vê tráfego entre VMs no mesmo host (Hyper-V internal)
  - Modo SNIFF = apenas cópia, zero risco de perda de pacotes
  - Zero instalação adicional — WinDivert.dll bundled no agente

Modo fallback: Raw Sockets SIO_RCVALL / AF_PACKET
  - Activado automaticamente se WinDivert não disponível
  - Não vê loopback no Windows (limitação do raw socket)

Instalar WinDivert:
  pip install pydivert
  + copiar WinDivert.dll e WinDivert64.sys para a pasta do agente
"""

import os
import sys
import socket
import struct
import threading
import time


_CAPTURE_PORTS = {
    5432, 3306, 1433, 1521,           # bases de dados relacionais
    6379, 6380,                         # Redis
    27017, 27018,                       # MongoDB
    9092, 9093,                         # Kafka
    5672, 15672, 61616,                # mensageria
    80, 8080, 8000, 8443, 443, 3000,  # HTTP/S
    9200, 9300,                         # Elasticsearch
    4317, 4318,                         # OTel (excluir do decode mas capturar topologia)
}

_STREAM_BUF_MAX = 16 * 1024   # 16 KB por stream
_STREAM_TTL     = 60.0


# ─────────────────────────────────────────
# TCP STREAM REASSEMBLY
# ─────────────────────────────────────────

class _StreamBuffer:

    def __init__(self):
        self._streams: dict = {}
        self._lock = threading.Lock()

    def append(self, key: tuple, data: bytes) -> bytes:
        with self._lock:
            s = self._streams.setdefault(key, {"buf": b"", "ts": 0.0})
            s["buf"] += data
            s["ts"]   = time.time()
            if len(s["buf"]) > _STREAM_BUF_MAX:
                del self._streams[key]
                return b""
            return s["buf"]

    def clear(self, key: tuple):
        with self._lock:
            self._streams.pop(key, None)

    def evict_stale(self):
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._streams.items() if now - v["ts"] > _STREAM_TTL]:
                del self._streams[k]


# ─────────────────────────────────────────
# PACKET CAPTURE
# ─────────────────────────────────────────

class PacketCapture:

    def __init__(self, ports: set | None = None):
        self._ports      = ports or _CAPTURE_PORTS
        self._payloads: list = []
        self._lock       = threading.Lock()
        self._running    = False
        self._stream_buf = _StreamBuffer()
        self._evict_tick = 0
        self._mode       = self._detect_mode()

    _WINDIVERT_DIR = os.path.join(
        os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "JarvisAgent"
    )

    def _detect_mode(self) -> str:
        # Verificar config antes de tentar carregar o driver
        # WINDIVERT_ENABLED=False por defeito — evita alertas de AV em instalacoes novas
        try:
            from agent_core.config import WINDIVERT_ENABLED
            if not WINDIVERT_ENABLED:
                return "rawsocket"
        except ImportError:
            pass
        try:
            if os.path.isdir(self._WINDIVERT_DIR):
                os.add_dll_directory(self._WINDIVERT_DIR)
            import pydivert  # noqa: F401
            return "windivert"
        except (ImportError, OSError):
            pass
        return "rawsocket"

    # ─── LIFECYCLE ───

    def start(self):
        self._running = True
        if self._mode == "windivert":
            self._start_windivert()
        elif sys.platform == "win32":
            self._start_rawsocket_windows()
        else:
            self._start_rawsocket_linux()

    def stop(self):
        self._running = False

    def drain(self) -> list:
        with self._lock:
            out           = self._payloads[:]
            self._payloads = []
        return out

    # ─── WINDIVERT (primário) ───

    def _start_windivert(self):
        threading.Thread(
            target=self._capture_windivert,
            daemon=True,
            name="pkt-windivert"
        ).start()
        print("[PacketCapture] WinDivert SNIFF iniciado — captura loopback + interfaces externas")

    def _capture_windivert(self):
        try:
            if os.path.isdir(self._WINDIVERT_DIR):
                os.add_dll_directory(self._WINDIVERT_DIR)
            import pydivert

            # Capturar todo o tráfego TCP (todos os portos)
            flt = "tcp"

            # Flag.SNIFF = cópia apenas, pacote segue normalmente
            with pydivert.WinDivert(flt, flags=pydivert.Flag.SNIFF) as w:
                while self._running:
                    try:
                        pkt = w.recv(bufsize=65535)
                        if pkt:
                            self._handle_windivert(pkt)
                    except Exception:
                        continue

        except Exception as e:
            print(f"[PacketCapture] WinDivert erro: {e} — a tentar fallback raw socket")
            self._mode = "rawsocket"
            if sys.platform == "win32":
                self._start_rawsocket_windows()

    def _handle_windivert(self, pkt):
        try:
            payload = bytes(pkt.tcp.payload) if pkt.tcp and pkt.tcp.payload else b""
            if not payload:
                return

            src_ip   = str(pkt.ip.src_addr)
            dst_ip   = str(pkt.ip.dst_addr)
            src_port = pkt.tcp.src_port
            dst_port = pkt.tcp.dst_port
            is_rst   = pkt.tcp.rst
            is_fin   = pkt.tcp.fin

            stream_key = (src_ip, src_port, dst_ip, dst_port)

            if is_rst or is_fin:
                self._stream_buf.clear(stream_key)
                return

            accumulated = self._stream_buf.append(stream_key, payload)
            if not accumulated:
                return

            self._emit_packet(src_ip, src_port, dst_ip, dst_port, accumulated)

        except Exception:
            pass

    # ─── RAW SOCKET WINDOWS (fallback) ───

    def _start_rawsocket_windows(self):
        ips = self._local_ips()
        for ip in ips:
            threading.Thread(
                target=self._capture_rawsocket_win,
                args=(ip,),
                daemon=True,
                name=f"pkt-raw-{ip}"
            ).start()
        print(f"[PacketCapture] Raw socket Windows fallback — {len(ips)} interface(s) (sem loopback)")

    def _local_ips(self) -> list:
        ips = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith("127."):
                    ips.append(ip)
        except Exception:
            pass
        if not ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ips.append(s.getsockname()[0])
                s.close()
            except Exception:
                pass
        return list(set(ips))

    def _capture_rawsocket_win(self, local_ip: str):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
            sock.bind((local_ip, 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
            sock.ioctl(socket.SIO_RCVALL, socket.RCVALL_ON)
            sock.settimeout(1.0)
            while self._running:
                try:
                    raw, _ = sock.recvfrom(65535)
                    self._parse_ip_packet(raw)
                except socket.timeout:
                    continue
                except Exception:
                    break
            sock.ioctl(socket.SIO_RCVALL, socket.RCVALL_OFF)
            sock.close()
        except PermissionError:
            print("[PacketCapture] sem permissão para raw socket — necessário admin")
        except Exception as e:
            print(f"[PacketCapture] raw socket erro ({local_ip}): {e}")

    # ─── RAW SOCKET LINUX (fallback) ───

    def _start_rawsocket_linux(self):
        threading.Thread(
            target=self._capture_rawsocket_linux,
            daemon=True,
            name="pkt-linux"
        ).start()
        print("[PacketCapture] AF_PACKET Linux iniciado")

    def _capture_rawsocket_linux(self):
        try:
            ETH_P_ALL = 0x0003
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
            sock.settimeout(1.0)
            while self._running:
                try:
                    raw, _ = sock.recvfrom(65535)
                    self._parse_ip_packet(raw[14:])  # skip 14-byte Ethernet header
                except socket.timeout:
                    continue
                except Exception:
                    break
            sock.close()
        except PermissionError:
            print("[PacketCapture] sem permissão AF_PACKET — necessário root/CAP_NET_RAW")
        except Exception as e:
            print(f"[PacketCapture] AF_PACKET erro: {e}")

    # ─── PARSE IP/TCP (raw socket) ───

    def _parse_ip_packet(self, raw: bytes):
        try:
            if len(raw) < 20:
                return
            ihl      = (raw[0] & 0x0F) * 4
            protocol = raw[9]
            if protocol != 6:  # TCP
                return

            src_ip = socket.inet_ntoa(raw[12:16])
            dst_ip = socket.inet_ntoa(raw[16:20])

            tcp = ihl
            if len(raw) < tcp + 20:
                return

            src_port = struct.unpack("!H", raw[tcp:tcp + 2])[0]
            dst_port = struct.unpack("!H", raw[tcp + 2:tcp + 4])[0]

            if src_port not in self._ports and dst_port not in self._ports:
                return

            data_off  = (raw[tcp + 12] >> 4) * 4
            tcp_flags = raw[tcp + 13]
            payload   = raw[tcp + data_off:]

            if not payload:
                return

            stream_key = (src_ip, src_port, dst_ip, dst_port)

            if tcp_flags & 0x04 or tcp_flags & 0x01:  # RST | FIN
                self._stream_buf.clear(stream_key)
                return

            accumulated = self._stream_buf.append(stream_key, payload)
            if accumulated:
                self._emit_packet(src_ip, src_port, dst_ip, dst_port, accumulated)

        except Exception:
            pass

    # ─── EMIT ───

    def _emit_packet(self, src_ip, src_port, dst_ip, dst_port, payload):
        with self._lock:
            self._payloads.append({
                "src_ip":    src_ip,
                "src_port":  src_port,
                "dst_ip":    dst_ip,
                "dst_port":  dst_port,
                "payload":   payload,
                "timestamp": time.time(),
            })
        self._evict_tick += 1
        if self._evict_tick % 1000 == 0:
            self._stream_buf.evict_stale()

    @property
    def mode(self) -> str:
        return self._mode
