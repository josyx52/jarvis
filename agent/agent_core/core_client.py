import gzip
import json
import requests

from agent_core.config import CORE_URL, TELEMETRY_ENDPOINT, HTTP_TIMEOUT_SECONDS, API_KEY


# ---------------------------------------------------
# DEBUG CONFIG
# ---------------------------------------------------

# True  -> apenas imprime os dados (não envia)
# False -> envia realmente para o servidor
DEBUG_PRINT_ONLY = False


class CoreClient:

    def __init__(self):

        self.url = f"{CORE_URL}{TELEMETRY_ENDPOINT}"

        print(f"[CoreClient] endpoint: {self.url}")

    # ---------------------------------------------------
    # SEND TELEMETRY
    # ---------------------------------------------------

    def send(self, batch: list[dict]) -> tuple[bool, str]:

        try:

            print("\n================ TELEMETRY BATCH ================\n")

            for frame in batch:

                try:
                    print(json.dumps(frame, indent=2)[:4000])
                except Exception:
                    print(frame)

                print("\n---------------------------------------------\n")

            print("============== END TELEMETRY BATCH ==============\n")

            # ---------------------------------------------------
            # SERIALIZE
            # ---------------------------------------------------

            raw = json.dumps(batch).encode("utf-8")

            compressed = gzip.compress(raw)

            print(f"[CoreClient] raw size: {len(raw)} bytes")
            print(f"[CoreClient] compressed size: {len(compressed)} bytes")

            # ---------------------------------------------------
            # DEBUG MODE
            # ---------------------------------------------------

            if DEBUG_PRINT_ONLY:

                print("\n[CoreClient] DEBUG MODE ENABLED")
                print("[CoreClient] Telemetry NOT sent to server\n")

                return True, ""

            # ---------------------------------------------------
            # REAL HTTP SEND
            # ---------------------------------------------------

            response = requests.post(

                self.url,

                data=compressed,

                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                    "X-API-Key": API_KEY,
                },

                timeout=HTTP_TIMEOUT_SECONDS
            )

            if response.status_code == 200:

                print("[CoreClient] batch successfully sent")

                return True, ""

            print(f"[CoreClient] server returned {response.status_code}")

            return False, f"HTTP {response.status_code}: {response.text[:300]}"

        except Exception as e:

            print("[CoreClient] send error:", str(e))

            return False, str(e)