/*
 * JarvisHook.dll — Universal Native Binary Instrumentation
 *
 * Cobre qualquer executável Windows normal via hooks nas APIs de rede:
 *
 *   Tier A — Winsock TCP  : connect, send, recv, WSASend, WSARecv, closesocket
 *   Tier B — Winsock UDP  : WSASendTo, WSARecvFrom
 *   Tier C — WinHTTP      : WinHttpConnect, WinHttpOpenRequest, WinHttpSendRequest,
 *                           WinHttpReceiveResponse, WinHttpReadData, WinHttpCloseHandle
 *   Tier D — WinInet      : InternetConnectA, HttpOpenRequestA,
 *                           HttpSendRequestA, InternetReadFile, InternetCloseHandle
 *
 * Fixes de produção aplicados:
 *   [F1] send_otlp usa Real_* directamente — sem re-entrância nos hooks
 *   [F2] __try/__except em todos os hooks — crash do hook não derruba o processo
 *   [F3] Todos os maps têm limite máximo — sem memory leaks em processos longos
 *
 * Compatível com x64 e x86 (compilar ambos — ver build.py).
 *
 * Build:
 *   vcpkg install detours:x64-windows detours:x86-windows
 *   python build.py
 */

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <winhttp.h>
#include <wininet.h>
#include <psapi.h>
#include <bcrypt.h>
#include <detours.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "wininet.lib")
#pragma comment(lib, "psapi.lib")
#pragma comment(lib, "bcrypt.lib")

// ═════════════════════════════════════════════════════════════════════════════
// CONFIG + LIMITES
// ═════════════════════════════════════════════════════════════════════════════

static char g_endpoint[256]     = "http://localhost:4318";
static char g_service_name[256] = "native-service";

// [F3] Limites máximos de todos os maps — evita leaks em processos longos
static constexpr size_t SPAN_BUFFER_MAX   = 4000;
static constexpr size_t SOCKET_MAP_MAX    = 8000;
static constexpr size_t HTTP_MAP_MAX      = 2000;

// ═════════════════════════════════════════════════════════════════════════════
// SPAN
// ═════════════════════════════════════════════════════════════════════════════

struct Span {
    char     trace_id[33];
    char     span_id[17];
    char     name[512];
    uint64_t start_ns;
    uint64_t end_ns;
    int      kind;
    bool     error;
    char     peer_host[256];
    int      peer_port;
    uint64_t bytes_sent;
    uint64_t bytes_recv;
    int      http_status;
    char     http_method[16];
    char     http_path[512];
    char     transport[8];
};

static std::vector<Span> g_spans;
static std::mutex        g_spans_mutex;
static std::atomic<bool> g_running{true};

// ═════════════════════════════════════════════════════════════════════════════
// SOCKET STATE (TCP)
// ═════════════════════════════════════════════════════════════════════════════

struct SocketInfo {
    char     remote_ip[64];
    int      remote_port;
    uint64_t bytes_sent;
    uint64_t bytes_recv;
    uint64_t connect_ns;
    char     trace_id[33];
    char     span_id[17];
    char     http_method[16];
    char     http_path[512];
    int      http_status;
    bool     pending_http;
};

static std::unordered_map<SOCKET, SocketInfo> g_sockets;
static std::mutex                             g_sockets_mutex;

// ═════════════════════════════════════════════════════════════════════════════
// WINHTTP / WININET STATE
// ═════════════════════════════════════════════════════════════════════════════

struct HttpConnInfo {
    char host[256];
    int  port;
};

struct HttpReqInfo {
    uintptr_t conn_handle;
    char      method[16];
    char      path[512];
    uint64_t  start_ns;
    char      trace_id[33];
    char      span_id[17];
    int       http_status;
    uint64_t  bytes_sent;
    uint64_t  bytes_recv;
};

static std::unordered_map<uintptr_t, HttpConnInfo> g_wh_conns;
static std::unordered_map<uintptr_t, HttpReqInfo>  g_wh_reqs;
static std::mutex                                   g_wh_mutex;

static std::unordered_map<uintptr_t, HttpConnInfo> g_wi_conns;
static std::unordered_map<uintptr_t, HttpReqInfo>  g_wi_reqs;
static std::mutex                                   g_wi_mutex;

// ═════════════════════════════════════════════════════════════════════════════
// FUNCTION POINTER DECLARATIONS (definidos no DllMain)
// ═════════════════════════════════════════════════════════════════════════════

// Tier A — Winsock TCP
static int  (WSAAPI* Real_connect)(SOCKET, const sockaddr*, int)                   = connect;
static int  (WSAAPI* Real_send)   (SOCKET, const char*, int, int)                  = send;
static int  (WSAAPI* Real_recv)   (SOCKET, char*, int, int)                        = recv;
static int  (WSAAPI* Real_WSASend)(SOCKET, LPWSABUF, DWORD, LPDWORD, DWORD,
    LPWSAOVERLAPPED, LPWSAOVERLAPPED_COMPLETION_ROUTINE)                            = WSASend;
static int  (WSAAPI* Real_WSARecv)(SOCKET, LPWSABUF, DWORD, LPDWORD, LPDWORD,
    LPWSAOVERLAPPED, LPWSAOVERLAPPED_COMPLETION_ROUTINE)                            = WSARecv;
static int  (WSAAPI* Real_closesocket)(SOCKET)                                     = closesocket;

// Tier B — Winsock UDP
static int  (WSAAPI* Real_WSASendTo)(SOCKET, LPWSABUF, DWORD, LPDWORD, DWORD,
    const sockaddr*, int, LPWSAOVERLAPPED, LPWSAOVERLAPPED_COMPLETION_ROUTINE)      = WSASendTo;
static int  (WSAAPI* Real_WSARecvFrom)(SOCKET, LPWSABUF, DWORD, LPDWORD, LPDWORD,
    sockaddr*, LPINT, LPWSAOVERLAPPED, LPWSAOVERLAPPED_COMPLETION_ROUTINE)          = WSARecvFrom;

// Tier C — WinHTTP (carregados dinamicamente)
typedef HINTERNET (WINAPI* PFN_WinHttpConnect)        (HINTERNET, LPCWSTR, INTERNET_PORT, DWORD);
typedef HINTERNET (WINAPI* PFN_WinHttpOpenRequest)    (HINTERNET, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR*, DWORD);
typedef BOOL      (WINAPI* PFN_WinHttpSendRequest)    (HINTERNET, LPCWSTR, DWORD, LPVOID, DWORD, DWORD, DWORD_PTR);
typedef BOOL      (WINAPI* PFN_WinHttpReceiveResponse)(HINTERNET, LPVOID);
typedef BOOL      (WINAPI* PFN_WinHttpReadData)       (HINTERNET, LPVOID, DWORD, LPDWORD);
typedef BOOL      (WINAPI* PFN_WinHttpQueryHeaders)   (HINTERNET, DWORD, LPCWSTR, LPVOID, LPDWORD, LPDWORD);
typedef BOOL      (WINAPI* PFN_WinHttpCloseHandle)    (HINTERNET);

static PFN_WinHttpConnect         Real_WinHttpConnect         = nullptr;
static PFN_WinHttpOpenRequest     Real_WinHttpOpenRequest     = nullptr;
static PFN_WinHttpSendRequest     Real_WinHttpSendRequest     = nullptr;
static PFN_WinHttpReceiveResponse Real_WinHttpReceiveResponse = nullptr;
static PFN_WinHttpReadData        Real_WinHttpReadData        = nullptr;
static PFN_WinHttpQueryHeaders    Real_WinHttpQueryHeaders    = nullptr;
static PFN_WinHttpCloseHandle     Real_WinHttpCloseHandle     = nullptr;

// Tier D — WinInet A + W (carregados dinamicamente)
typedef HINTERNET (WINAPI* PFN_InternetConnectA)   (HINTERNET, LPCSTR,  INTERNET_PORT, LPCSTR,  LPCSTR,  DWORD, DWORD, DWORD_PTR);
typedef HINTERNET (WINAPI* PFN_InternetConnectW)   (HINTERNET, LPCWSTR, INTERNET_PORT, LPCWSTR, LPCWSTR, DWORD, DWORD, DWORD_PTR);
typedef HINTERNET (WINAPI* PFN_HttpOpenRequestA)   (HINTERNET, LPCSTR,  LPCSTR,  LPCSTR,  LPCSTR,  LPCSTR*,  DWORD, DWORD_PTR);
typedef HINTERNET (WINAPI* PFN_HttpOpenRequestW)   (HINTERNET, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR*, DWORD, DWORD_PTR);
typedef BOOL      (WINAPI* PFN_HttpSendRequestA)   (HINTERNET, LPCSTR,  DWORD, LPVOID, DWORD);
typedef BOOL      (WINAPI* PFN_HttpSendRequestW)   (HINTERNET, LPCWSTR, DWORD, LPVOID, DWORD);
typedef BOOL      (WINAPI* PFN_InternetReadFile)   (HINTERNET, LPVOID, DWORD, LPDWORD);
typedef BOOL      (WINAPI* PFN_HttpQueryInfoA)     (HINTERNET, DWORD, LPVOID, LPDWORD, LPDWORD);
typedef BOOL      (WINAPI* PFN_InternetCloseHandle)(HINTERNET);

static PFN_InternetConnectA    Real_InternetConnectA    = nullptr;
static PFN_InternetConnectW    Real_InternetConnectW    = nullptr;
static PFN_HttpOpenRequestA    Real_HttpOpenRequestA    = nullptr;
static PFN_HttpOpenRequestW    Real_HttpOpenRequestW    = nullptr;
static PFN_HttpSendRequestA    Real_HttpSendRequestA    = nullptr;
static PFN_HttpSendRequestW    Real_HttpSendRequestW    = nullptr;
static PFN_InternetReadFile    Real_InternetReadFile    = nullptr;
static PFN_HttpQueryInfoA      Real_HttpQueryInfoA      = nullptr;
static PFN_InternetCloseHandle Real_InternetCloseHandle = nullptr;

// ═════════════════════════════════════════════════════════════════════════════
// UTILITIES
// ═════════════════════════════════════════════════════════════════════════════

static uint64_t now_ns() {
    FILETIME ft;
    GetSystemTimeAsFileTime(&ft);
    uint64_t t = ((uint64_t)ft.dwHighDateTime << 32) | ft.dwLowDateTime;
    t -= 116444736000000000ULL;
    return t * 100;
}

static void rand_hex(char* buf, int bytes) {
    static const char hex[] = "0123456789abcdef";
    BYTE rnd[32] = {};
    BCryptGenRandom(nullptr, rnd, (ULONG)bytes, BCRYPT_USE_SYSTEM_PREFERRED_RNG);
    for (int i = 0; i < bytes; i++) {
        buf[i * 2]     = hex[rnd[i] >> 4];
        buf[i * 2 + 1] = hex[rnd[i] & 0xF];
    }
    buf[bytes * 2] = '\0';
}

static void escape_json(const char* src, char* dst, size_t dsz) {
    size_t j = 0;
    for (size_t i = 0; src[i] && j + 3 < dsz; i++) {
        unsigned char c = (unsigned char)src[i];
        if      (c == '"')  { dst[j++] = '\\'; dst[j++] = '"';  }
        else if (c == '\\') { dst[j++] = '\\'; dst[j++] = '\\'; }
        else if (c < 0x20)  { dst[j++] = ' '; }
        else                 { dst[j++] = c; }
    }
    dst[j] = '\0';
}

static std::string wstr_to_utf8(LPCWSTR ws) {
    if (!ws) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, ws, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 0) return {};
    std::string s(n - 1, '\0');
    WideCharToMultiByte(CP_UTF8, 0, ws, -1, &s[0], n, nullptr, nullptr);
    return s;
}

static bool is_loopback(const char* ip) {
    return strncmp(ip, "127.", 4) == 0
        || strcmp(ip, "::1") == 0
        || strcmp(ip, "localhost") == 0;
}

// ═════════════════════════════════════════════════════════════════════════════
// HTTP PARSER (payload Winsock)
// ═════════════════════════════════════════════════════════════════════════════

static void parse_http_request(SocketInfo& info, const char* data, int len) {
    if (len < 8 || info.pending_http) return;
    static const char* METHODS[] = {
        "GET ", "POST ", "PUT ", "DELETE ", "PATCH ",
        "HEAD ", "OPTIONS ", "CONNECT ", nullptr
    };
    for (int i = 0; METHODS[i]; ++i) {
        int mlen = (int)strlen(METHODS[i]);
        if (len >= mlen && memcmp(data, METHODS[i], mlen) == 0) {
            strncpy_s(info.http_method, METHODS[i], mlen - 1);
            info.http_method[mlen - 1] = '\0';
            const char* ps  = data + mlen;
            int          rem = len - mlen;
            const char* pe  = (const char*)memchr(ps, ' ', std::min(rem, 512));
            if (pe) {
                int pl = std::min((int)(pe - ps), (int)sizeof(info.http_path) - 1);
                memcpy(info.http_path, ps, pl);
                info.http_path[pl] = '\0';
                info.pending_http  = true;
            }
            return;
        }
    }
}

static void parse_http_response(SocketInfo& info, const char* data, int len) {
    if (len < 12 || info.http_status != 0) return;
    if (memcmp(data, "HTTP/", 5) != 0) return;
    const char* sp = (const char*)memchr(data, ' ', std::min(len, 20));
    if (sp && (sp + 4) <= (data + len))
        info.http_status = atoi(sp + 1);
}

// ═════════════════════════════════════════════════════════════════════════════
// SPAN BUFFER
// ═════════════════════════════════════════════════════════════════════════════

static void push_span(Span& sp) {
    std::lock_guard<std::mutex> lk(g_spans_mutex);
    if (g_spans.size() < SPAN_BUFFER_MAX)  // [F3]
        g_spans.push_back(sp);
}

static void emit_socket_span(const SocketInfo& info) {
    if (info.bytes_sent == 0 && info.bytes_recv == 0) return;
    Span sp = {};
    memcpy(sp.trace_id, info.trace_id, sizeof(sp.trace_id));
    memcpy(sp.span_id,  info.span_id,  sizeof(sp.span_id));
    if (info.pending_http && info.http_method[0]) {
        snprintf(sp.name, sizeof(sp.name), "%s %s", info.http_method, info.http_path);
        sp.http_status = info.http_status;
        sp.error       = (info.http_status >= 500);
        memcpy(sp.http_method, info.http_method, sizeof(sp.http_method));
        memcpy(sp.http_path,   info.http_path,   sizeof(sp.http_path));
        strncpy_s(sp.transport, "http", sizeof(sp.transport));
    } else {
        snprintf(sp.name, sizeof(sp.name), "tcp %s:%d", info.remote_ip, info.remote_port);
        strncpy_s(sp.transport, "tcp", sizeof(sp.transport));
    }
    sp.kind       = 3;
    sp.start_ns   = info.connect_ns;
    sp.end_ns     = now_ns();
    sp.peer_port  = info.remote_port;
    sp.bytes_sent = info.bytes_sent;
    sp.bytes_recv = info.bytes_recv;
    strncpy_s(sp.peer_host, info.remote_ip, sizeof(sp.peer_host) - 1);
    push_span(sp);
}

// ═════════════════════════════════════════════════════════════════════════════
// OTLP JSON + SEND
// [F1] send_otlp usa Real_* directamente — não passa pelos hooks
//      Elimina o loop infinito de spans sobre localhost:4318
// ═════════════════════════════════════════════════════════════════════════════

static std::string build_otlp_json(const std::vector<Span>& spans) {
    char svc[512];
    escape_json(g_service_name, svc, sizeof(svc));

    std::ostringstream j;
    j << "{\"resourceSpans\":[{\"resource\":{\"attributes\":["
      << "{\"key\":\"service.name\",\"value\":{\"stringValue\":\"" << svc << "\"}},"
      << "{\"key\":\"telemetry.sdk.name\",\"value\":{\"stringValue\":\"jarvis-hook\"}}"
      << "]},\"scopeSpans\":[{\"spans\":[";

    for (size_t i = 0; i < spans.size(); ++i) {
        const auto& s = spans[i];
        if (i) j << ",";
        char name[600]; escape_json(s.name,       name, sizeof(name));
        char host[300]; escape_json(s.peer_host,  host, sizeof(host));
        char meth[32];  escape_json(s.http_method, meth, sizeof(meth));
        char path[600]; escape_json(s.http_path,   path, sizeof(path));
        char trns[16];  escape_json(s.transport,   trns, sizeof(trns));
        j << "{"
          << "\"traceId\":\"" << s.trace_id << "\","
          << "\"spanId\":\""  << s.span_id  << "\","
          << "\"name\":\""    << name       << "\","
          << "\"kind\":"      << s.kind     << ","
          << "\"startTimeUnixNano\":\"" << s.start_ns << "\","
          << "\"endTimeUnixNano\":\""   << s.end_ns   << "\","
          << "\"status\":{\"code\":" << (s.error ? 2 : 0) << "},"
          << "\"attributes\":["
          << "{\"key\":\"net.peer.name\",\"value\":{\"stringValue\":\"" << host << "\"}},"
          << "{\"key\":\"net.peer.port\",\"value\":{\"intValue\":"      << s.peer_port  << "}},"
          << "{\"key\":\"net.transport\",\"value\":{\"stringValue\":\"" << trns         << "\"}},"
          << "{\"key\":\"net.bytes_sent\",\"value\":{\"intValue\":"     << s.bytes_sent << "}},"
          << "{\"key\":\"net.bytes_recv\",\"value\":{\"intValue\":"     << s.bytes_recv << "}}";
        if (s.http_status > 0) {
            j << ",{\"key\":\"http.status_code\",\"value\":{\"intValue\":"  << s.http_status << "}}"
              << ",{\"key\":\"http.method\",\"value\":{\"stringValue\":\""  << meth          << "\"}}"
              << ",{\"key\":\"http.target\",\"value\":{\"stringValue\":\""  << path          << "\"}}";
        }
        j << "]}";
    }
    j << "]}]}]}";
    return j.str();
}

static void send_otlp(const std::string& body) {
    // [F1] Usa Real_* directamente — bypassa os hooks completamente
    //      Sem isto: WinHttpSendRequest → Hook_WinHttpSendRequest → g_wh_reqs →
    //               Hook_WinHttpCloseHandle → push_span → próxima flush → loop infinito
    if (!Real_WinHttpConnect || !Real_WinHttpOpenRequest ||
        !Real_WinHttpSendRequest || !Real_WinHttpReceiveResponse ||
        !Real_WinHttpCloseHandle) return;

    HINTERNET session = WinHttpOpen(
        L"JarvisHook/1.0", WINHTTP_ACCESS_TYPE_NO_PROXY, nullptr, nullptr, 0);
    if (!session) return;

    HINTERNET conn = Real_WinHttpConnect(session, L"localhost", 4318, 0);
    if (!conn) { Real_WinHttpCloseHandle(session); return; }

    HINTERNET req = Real_WinHttpOpenRequest(
        conn, L"POST", L"/v1/traces",
        nullptr, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, 0);
    if (!req) { Real_WinHttpCloseHandle(conn); Real_WinHttpCloseHandle(session); return; }

    WinHttpAddRequestHeaders(req,
        L"Content-Type: application/json\r\n",
        (DWORD)-1, WINHTTP_ADDREQ_FLAG_ADD);

    Real_WinHttpSendRequest(req, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
        (LPVOID)body.c_str(), (DWORD)body.size(), (DWORD)body.size(), 0);
    Real_WinHttpReceiveResponse(req, nullptr);

    Real_WinHttpCloseHandle(req);
    Real_WinHttpCloseHandle(conn);
    Real_WinHttpCloseHandle(session);
}

static void flush_thread_fn() {
    while (g_running.load()) {
        std::this_thread::sleep_for(std::chrono::seconds(5));
        std::vector<Span> batch;
        {
            std::lock_guard<std::mutex> lk(g_spans_mutex);
            if (g_spans.empty()) continue;
            batch.swap(g_spans);
        }
        __try { send_otlp(build_otlp_json(batch)); } __except(EXCEPTION_EXECUTE_HANDLER) {}
    }
}

// ═════════════════════════════════════════════════════════════════════════════
// TIER A — WINSOCK TCP
// [F2] __try/__except em todos os hooks — crash não derruba o processo alvo
// [F3] Verificação de tamanho antes de inserir nos maps
// ═════════════════════════════════════════════════════════════════════════════

static int WSAAPI Hook_connect(SOCKET s, const sockaddr* name, int namelen) {
    int result = Real_connect(s, name, namelen);
    __try {
        if (result == 0 || WSAGetLastError() == WSAEWOULDBLOCK) {
            char ip[INET6_ADDRSTRLEN] = {};
            int  port = 0;
            if (name->sa_family == AF_INET) {
                auto* sin = reinterpret_cast<const sockaddr_in*>(name);
                inet_ntop(AF_INET, &sin->sin_addr, ip, sizeof(ip));
                port = ntohs(sin->sin_port);
            } else if (name->sa_family == AF_INET6) {
                auto* sin6 = reinterpret_cast<const sockaddr_in6*>(name);
                inet_ntop(AF_INET6, &sin6->sin6_addr, ip, sizeof(ip));
                port = ntohs(sin6->sin6_port);
            }
            if (ip[0] && !is_loopback(ip)) {
                std::lock_guard<std::mutex> lk(g_sockets_mutex);
                if (g_sockets.size() < SOCKET_MAP_MAX) {  // [F3]
                    SocketInfo info = {};
                    strncpy_s(info.remote_ip, ip, sizeof(info.remote_ip) - 1);
                    info.remote_port = port;
                    info.connect_ns  = now_ns();
                    rand_hex(info.trace_id, 16);
                    rand_hex(info.span_id,  8);
                    g_sockets[s] = info;
                }
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return result;
}

static int WSAAPI Hook_send(SOCKET s, const char* buf, int len, int flags) {
    int result = Real_send(s, buf, len, flags);
    __try {
        if (result > 0) {
            std::lock_guard<std::mutex> lk(g_sockets_mutex);
            auto it = g_sockets.find(s);
            if (it != g_sockets.end()) {
                it->second.bytes_sent += result;
                if (buf && len > 0) parse_http_request(it->second, buf, len);
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return result;
}

static int WSAAPI Hook_recv(SOCKET s, char* buf, int len, int flags) {
    int result = Real_recv(s, buf, len, flags);
    __try {
        if (result > 0) {
            std::lock_guard<std::mutex> lk(g_sockets_mutex);
            auto it = g_sockets.find(s);
            if (it != g_sockets.end()) {
                it->second.bytes_recv += result;
                if (buf) parse_http_response(it->second, buf, result);
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return result;
}

static int WSAAPI Hook_WSASend(
    SOCKET s, LPWSABUF lpBuffers, DWORD dwBufferCount,
    LPDWORD lpNumberOfBytesSent, DWORD dwFlags,
    LPWSAOVERLAPPED lpOverlapped,
    LPWSAOVERLAPPED_COMPLETION_ROUTINE lpCompletionRoutine)
{
    int result = Real_WSASend(s, lpBuffers, dwBufferCount, lpNumberOfBytesSent,
        dwFlags, lpOverlapped, lpCompletionRoutine);
    __try {
        if (result == 0 && lpBuffers && dwBufferCount > 0) {
            std::lock_guard<std::mutex> lk(g_sockets_mutex);
            auto it = g_sockets.find(s);
            if (it != g_sockets.end()) {
                DWORD total = 0;
                for (DWORD i = 0; i < dwBufferCount; i++) total += lpBuffers[i].len;
                it->second.bytes_sent += total;
                if (lpBuffers[0].buf && lpBuffers[0].len > 0)
                    parse_http_request(it->second, lpBuffers[0].buf, (int)lpBuffers[0].len);
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return result;
}

static int WSAAPI Hook_WSARecv(
    SOCKET s, LPWSABUF lpBuffers, DWORD dwBufferCount,
    LPDWORD lpNumberOfBytesRecvd, LPDWORD lpFlags,
    LPWSAOVERLAPPED lpOverlapped,
    LPWSAOVERLAPPED_COMPLETION_ROUTINE lpCompletionRoutine)
{
    int result = Real_WSARecv(s, lpBuffers, dwBufferCount, lpNumberOfBytesRecvd,
        lpFlags, lpOverlapped, lpCompletionRoutine);
    __try {
        if (result == 0 && lpNumberOfBytesRecvd && *lpNumberOfBytesRecvd > 0) {
            std::lock_guard<std::mutex> lk(g_sockets_mutex);
            auto it = g_sockets.find(s);
            if (it != g_sockets.end()) {
                it->second.bytes_recv += *lpNumberOfBytesRecvd;
                if (lpBuffers && lpBuffers[0].buf)
                    parse_http_response(it->second, lpBuffers[0].buf, (int)*lpNumberOfBytesRecvd);
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return result;
}

static int WSAAPI Hook_closesocket(SOCKET s) {
    SocketInfo info = {};
    bool found = false;
    __try {
        std::lock_guard<std::mutex> lk(g_sockets_mutex);
        auto it = g_sockets.find(s);
        if (it != g_sockets.end()) { info = it->second; found = true; g_sockets.erase(it); }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    if (found) emit_socket_span(info);
    return Real_closesocket(s);
}

// ═════════════════════════════════════════════════════════════════════════════
// TIER B — WINSOCK UDP
// ═════════════════════════════════════════════════════════════════════════════

static int WSAAPI Hook_WSASendTo(
    SOCKET s, LPWSABUF lpBuffers, DWORD dwBufferCount,
    LPDWORD lpNumberOfBytesSent, DWORD dwFlags,
    const sockaddr* lpTo, int iTolen,
    LPWSAOVERLAPPED lpOverlapped,
    LPWSAOVERLAPPED_COMPLETION_ROUTINE lpCompletionRoutine)
{
    int result = Real_WSASendTo(s, lpBuffers, dwBufferCount, lpNumberOfBytesSent,
        dwFlags, lpTo, iTolen, lpOverlapped, lpCompletionRoutine);
    __try {
        if (result == 0 && lpTo) {
            char ip[INET6_ADDRSTRLEN] = {};
            int  port = 0;
            if (lpTo->sa_family == AF_INET) {
                auto* sin = reinterpret_cast<const sockaddr_in*>(lpTo);
                inet_ntop(AF_INET, &sin->sin_addr, ip, sizeof(ip));
                port = ntohs(sin->sin_port);
            } else if (lpTo->sa_family == AF_INET6) {
                auto* sin6 = reinterpret_cast<const sockaddr_in6*>(lpTo);
                inet_ntop(AF_INET6, &sin6->sin6_addr, ip, sizeof(ip));
                port = ntohs(sin6->sin6_port);
            }
            if (ip[0] && !is_loopback(ip) && lpBuffers && dwBufferCount > 0) {
                DWORD total = 0;
                for (DWORD i = 0; i < dwBufferCount; i++) total += lpBuffers[i].len;
                Span sp = {};
                rand_hex(sp.trace_id, 16);
                rand_hex(sp.span_id,  8);
                snprintf(sp.name, sizeof(sp.name), "udp %s:%d", ip, port);
                sp.kind = 3; sp.start_ns = now_ns(); sp.end_ns = sp.start_ns;
                sp.peer_port = port; sp.bytes_sent = total;
                strncpy_s(sp.peer_host, ip, sizeof(sp.peer_host) - 1);
                strncpy_s(sp.transport, "udp", sizeof(sp.transport));
                push_span(sp);
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return result;
}

static int WSAAPI Hook_WSARecvFrom(
    SOCKET s, LPWSABUF lpBuffers, DWORD dwBufferCount,
    LPDWORD lpNumberOfBytesRecvd, LPDWORD lpFlags,
    sockaddr* lpFrom, LPINT lpFromlen,
    LPWSAOVERLAPPED lpOverlapped,
    LPWSAOVERLAPPED_COMPLETION_ROUTINE lpCompletionRoutine)
{
    int result = Real_WSARecvFrom(s, lpBuffers, dwBufferCount, lpNumberOfBytesRecvd,
        lpFlags, lpFrom, lpFromlen, lpOverlapped, lpCompletionRoutine);
    __try {
        if (result == 0 && lpFrom && lpNumberOfBytesRecvd && *lpNumberOfBytesRecvd > 0) {
            char ip[INET6_ADDRSTRLEN] = {};
            int  port = 0;
            if (lpFrom->sa_family == AF_INET) {
                auto* sin = reinterpret_cast<const sockaddr_in*>(lpFrom);
                inet_ntop(AF_INET, &sin->sin_addr, ip, sizeof(ip));
                port = ntohs(sin->sin_port);
            }
            if (ip[0] && !is_loopback(ip)) {
                Span sp = {};
                rand_hex(sp.trace_id, 16); rand_hex(sp.span_id, 8);
                snprintf(sp.name, sizeof(sp.name), "udp recv %s:%d", ip, port);
                sp.kind = 2; sp.start_ns = now_ns(); sp.end_ns = sp.start_ns;
                sp.peer_port = port; sp.bytes_recv = *lpNumberOfBytesRecvd;
                strncpy_s(sp.peer_host, ip, sizeof(sp.peer_host) - 1);
                strncpy_s(sp.transport, "udp", sizeof(sp.transport));
                push_span(sp);
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return result;
}

// ═════════════════════════════════════════════════════════════════════════════
// TIER C — WINHTTP
// ═════════════════════════════════════════════════════════════════════════════

static HINTERNET WINAPI Hook_WinHttpConnect(
    HINTERNET hSession, LPCWSTR pswzServerName, INTERNET_PORT nServerPort, DWORD dwReserved)
{
    HINTERNET h = Real_WinHttpConnect(hSession, pswzServerName, nServerPort, dwReserved);
    __try {
        if (h && pswzServerName) {
            std::string host = wstr_to_utf8(pswzServerName);
            if (!is_loopback(host.c_str())) {
                std::lock_guard<std::mutex> lk(g_wh_mutex);
                if (g_wh_conns.size() < HTTP_MAP_MAX) {  // [F3]
                    HttpConnInfo info = {};
                    strncpy_s(info.host, host.c_str(), sizeof(info.host) - 1);
                    info.port = (int)nServerPort;
                    g_wh_conns[(uintptr_t)h] = info;
                }
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return h;
}

static HINTERNET WINAPI Hook_WinHttpOpenRequest(
    HINTERNET hConnect, LPCWSTR pwszVerb, LPCWSTR pwszObjectName,
    LPCWSTR pwszVersion, LPCWSTR pwszReferrer,
    LPCWSTR* ppwszAcceptTypes, DWORD dwFlags)
{
    HINTERNET h = Real_WinHttpOpenRequest(hConnect, pwszVerb, pwszObjectName,
        pwszVersion, pwszReferrer, ppwszAcceptTypes, dwFlags);
    __try {
        if (h) {
            std::lock_guard<std::mutex> lk(g_wh_mutex);
            // Só rastrear se a conexão pai existir (não é loopback)
            if (g_wh_conns.count((uintptr_t)hConnect) && g_wh_reqs.size() < HTTP_MAP_MAX) {  // [F3]
                HttpReqInfo info = {};
                info.conn_handle = (uintptr_t)hConnect;
                std::string verb = pwszVerb        ? wstr_to_utf8(pwszVerb)        : "GET";
                std::string path = pwszObjectName  ? wstr_to_utf8(pwszObjectName)  : "/";
                strncpy_s(info.method, verb.c_str(), sizeof(info.method) - 1);
                strncpy_s(info.path,   path.c_str(), sizeof(info.path)   - 1);
                rand_hex(info.trace_id, 16);
                rand_hex(info.span_id,  8);
                g_wh_reqs[(uintptr_t)h] = info;
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return h;
}

static BOOL WINAPI Hook_WinHttpSendRequest(
    HINTERNET hRequest, LPCWSTR pwszHeaders, DWORD dwHeadersLength,
    LPVOID lpOptional, DWORD dwOptionalLength,
    DWORD dwTotalLength, DWORD_PTR dwContext)
{
    __try {
        std::lock_guard<std::mutex> lk(g_wh_mutex);
        auto it = g_wh_reqs.find((uintptr_t)hRequest);
        if (it != g_wh_reqs.end()) it->second.start_ns = now_ns();
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return Real_WinHttpSendRequest(hRequest, pwszHeaders, dwHeadersLength,
        lpOptional, dwOptionalLength, dwTotalLength, dwContext);
}

static BOOL WINAPI Hook_WinHttpReceiveResponse(HINTERNET hRequest, LPVOID lpReserved) {
    BOOL result = Real_WinHttpReceiveResponse(hRequest, lpReserved);
    __try {
        if (result && Real_WinHttpQueryHeaders) {
            DWORD status = 0, size = sizeof(DWORD);
            if (Real_WinHttpQueryHeaders(hRequest,
                WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                WINHTTP_HEADER_NAME_BY_INDEX, &status, &size, WINHTTP_NO_HEADER_INDEX)) {
                std::lock_guard<std::mutex> lk(g_wh_mutex);
                auto it = g_wh_reqs.find((uintptr_t)hRequest);
                if (it != g_wh_reqs.end()) it->second.http_status = (int)status;
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return result;
}

static BOOL WINAPI Hook_WinHttpReadData(
    HINTERNET hRequest, LPVOID lpBuffer, DWORD dwNumberOfBytesToRead, LPDWORD lpdwNumberOfBytesRead)
{
    BOOL result = Real_WinHttpReadData(hRequest, lpBuffer, dwNumberOfBytesToRead, lpdwNumberOfBytesRead);
    __try {
        if (result && lpdwNumberOfBytesRead && *lpdwNumberOfBytesRead > 0) {
            std::lock_guard<std::mutex> lk(g_wh_mutex);
            auto it = g_wh_reqs.find((uintptr_t)hRequest);
            if (it != g_wh_reqs.end()) it->second.bytes_recv += *lpdwNumberOfBytesRead;
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return result;
}

static BOOL WINAPI Hook_WinHttpCloseHandle(HINTERNET hInternet) {
    HttpReqInfo  req  = {};
    HttpConnInfo conn = {};
    bool is_req = false;
    __try {
        std::lock_guard<std::mutex> lk(g_wh_mutex);
        auto rit = g_wh_reqs.find((uintptr_t)hInternet);
        if (rit != g_wh_reqs.end()) {
            req = rit->second; is_req = true;
            auto cit = g_wh_conns.find(req.conn_handle);
            if (cit != g_wh_conns.end()) conn = cit->second;
            g_wh_reqs.erase(rit);
        }
        g_wh_conns.erase((uintptr_t)hInternet);
    } __except(EXCEPTION_EXECUTE_HANDLER) {}

    if (is_req && req.start_ns > 0) {
        __try {
            Span sp = {};
            memcpy(sp.trace_id, req.trace_id, sizeof(sp.trace_id));
            memcpy(sp.span_id,  req.span_id,  sizeof(sp.span_id));
            snprintf(sp.name, sizeof(sp.name), "%s %s", req.method, req.path);
            sp.kind = 3; sp.start_ns = req.start_ns; sp.end_ns = now_ns();
            sp.http_status = req.http_status; sp.error = (req.http_status >= 500);
            sp.bytes_recv = req.bytes_recv; sp.peer_port = conn.port;
            strncpy_s(sp.peer_host,   conn.host,  sizeof(sp.peer_host)   - 1);
            strncpy_s(sp.http_method, req.method, sizeof(sp.http_method) - 1);
            strncpy_s(sp.http_path,   req.path,   sizeof(sp.http_path)   - 1);
            strncpy_s(sp.transport, conn.port == 443 ? "https" : "http", sizeof(sp.transport));
            push_span(sp);
        } __except(EXCEPTION_EXECUTE_HANDLER) {}
    }
    return Real_WinHttpCloseHandle(hInternet);
}

// ═════════════════════════════════════════════════════════════════════════════
// TIER D — WININET
// ═════════════════════════════════════════════════════════════════════════════

static HINTERNET WINAPI Hook_InternetConnectA(
    HINTERNET hInternet, LPCSTR lpszServerName, INTERNET_PORT nServerPort,
    LPCSTR lpszUserName, LPCSTR lpszPassword,
    DWORD dwService, DWORD dwFlags, DWORD_PTR dwContext)
{
    HINTERNET h = Real_InternetConnectA(hInternet, lpszServerName, nServerPort,
        lpszUserName, lpszPassword, dwService, dwFlags, dwContext);
    __try {
        if (h && lpszServerName && !is_loopback(lpszServerName)) {
            std::lock_guard<std::mutex> lk(g_wi_mutex);
            if (g_wi_conns.size() < HTTP_MAP_MAX) {  // [F3]
                HttpConnInfo info = {};
                strncpy_s(info.host, lpszServerName, sizeof(info.host) - 1);
                info.port = (int)nServerPort;
                g_wi_conns[(uintptr_t)h] = info;
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return h;
}

static HINTERNET WINAPI Hook_HttpOpenRequestA(
    HINTERNET hConnect, LPCSTR lpszVerb, LPCSTR lpszObjectName,
    LPCSTR lpszVersion, LPCSTR lpszReferrer,
    LPCSTR* lplpszAcceptTypes, DWORD dwFlags, DWORD_PTR dwContext)
{
    HINTERNET h = Real_HttpOpenRequestA(hConnect, lpszVerb, lpszObjectName,
        lpszVersion, lpszReferrer, lplpszAcceptTypes, dwFlags, dwContext);
    __try {
        if (h) {
            std::lock_guard<std::mutex> lk(g_wi_mutex);
            if (g_wi_conns.count((uintptr_t)hConnect) && g_wi_reqs.size() < HTTP_MAP_MAX) {  // [F3]
                HttpReqInfo info = {};
                info.conn_handle = (uintptr_t)hConnect;
                strncpy_s(info.method, lpszVerb       ? lpszVerb       : "GET", sizeof(info.method) - 1);
                strncpy_s(info.path,   lpszObjectName ? lpszObjectName : "/",   sizeof(info.path)   - 1);
                rand_hex(info.trace_id, 16); rand_hex(info.span_id, 8);
                g_wi_reqs[(uintptr_t)h] = info;
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return h;
}

static BOOL WINAPI Hook_HttpSendRequestA(
    HINTERNET hRequest, LPCSTR lpszHeaders, DWORD dwHeadersLength,
    LPVOID lpOptional, DWORD dwOptionalLength)
{
    uint64_t t0 = now_ns();
    BOOL result = Real_HttpSendRequestA(hRequest, lpszHeaders, dwHeadersLength,
        lpOptional, dwOptionalLength);
    __try {
        std::lock_guard<std::mutex> lk(g_wi_mutex);
        auto it = g_wi_reqs.find((uintptr_t)hRequest);
        if (it != g_wi_reqs.end()) {
            it->second.start_ns     = t0;
            it->second.bytes_sent  += dwOptionalLength;
            if (Real_HttpQueryInfoA) {
                DWORD status = 0, size = sizeof(DWORD);
                if (Real_HttpQueryInfoA(hRequest,
                    HTTP_QUERY_STATUS_CODE | HTTP_QUERY_FLAG_NUMBER,
                    &status, &size, nullptr))
                    it->second.http_status = (int)status;
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return result;
}

static BOOL WINAPI Hook_InternetReadFile(
    HINTERNET hFile, LPVOID lpBuffer, DWORD dwNumberOfBytesToRead, LPDWORD lpdwNumberOfBytesRead)
{
    BOOL result = Real_InternetReadFile(hFile, lpBuffer, dwNumberOfBytesToRead, lpdwNumberOfBytesRead);
    __try {
        if (result && lpdwNumberOfBytesRead && *lpdwNumberOfBytesRead > 0) {
            std::lock_guard<std::mutex> lk(g_wi_mutex);
            auto it = g_wi_reqs.find((uintptr_t)hFile);
            if (it != g_wi_reqs.end()) it->second.bytes_recv += *lpdwNumberOfBytesRead;
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return result;
}

// ── WinInet W versions ────────────────────────────────────────────────────────

static HINTERNET WINAPI Hook_InternetConnectW(
    HINTERNET hInternet, LPCWSTR lpszServerName, INTERNET_PORT nServerPort,
    LPCWSTR lpszUserName, LPCWSTR lpszPassword,
    DWORD dwService, DWORD dwFlags, DWORD_PTR dwContext)
{
    HINTERNET h = Real_InternetConnectW(hInternet, lpszServerName, nServerPort,
        lpszUserName, lpszPassword, dwService, dwFlags, dwContext);
    __try {
        if (h && lpszServerName) {
            std::string host = wstr_to_utf8(lpszServerName);
            if (!is_loopback(host.c_str())) {
                std::lock_guard<std::mutex> lk(g_wi_mutex);
                if (g_wi_conns.size() < HTTP_MAP_MAX) {
                    HttpConnInfo info = {};
                    strncpy_s(info.host, host.c_str(), sizeof(info.host) - 1);
                    info.port = (int)nServerPort;
                    g_wi_conns[(uintptr_t)h] = info;
                }
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return h;
}

static HINTERNET WINAPI Hook_HttpOpenRequestW(
    HINTERNET hConnect, LPCWSTR lpszVerb, LPCWSTR lpszObjectName,
    LPCWSTR lpszVersion, LPCWSTR lpszReferrer,
    LPCWSTR* lplpszAcceptTypes, DWORD dwFlags, DWORD_PTR dwContext)
{
    HINTERNET h = Real_HttpOpenRequestW(hConnect, lpszVerb, lpszObjectName,
        lpszVersion, lpszReferrer, lplpszAcceptTypes, dwFlags, dwContext);
    __try {
        if (h) {
            std::lock_guard<std::mutex> lk(g_wi_mutex);
            if (g_wi_conns.count((uintptr_t)hConnect) && g_wi_reqs.size() < HTTP_MAP_MAX) {
                HttpReqInfo info = {};
                info.conn_handle = (uintptr_t)hConnect;
                std::string verb = lpszVerb       ? wstr_to_utf8(lpszVerb)       : "GET";
                std::string path = lpszObjectName ? wstr_to_utf8(lpszObjectName) : "/";
                strncpy_s(info.method, verb.c_str(), sizeof(info.method) - 1);
                strncpy_s(info.path,   path.c_str(), sizeof(info.path)   - 1);
                rand_hex(info.trace_id, 16);
                rand_hex(info.span_id,  8);
                g_wi_reqs[(uintptr_t)h] = info;
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return h;
}

static BOOL WINAPI Hook_HttpSendRequestW(
    HINTERNET hRequest, LPCWSTR lpszHeaders, DWORD dwHeadersLength,
    LPVOID lpOptional, DWORD dwOptionalLength)
{
    uint64_t t0 = now_ns();
    BOOL result = Real_HttpSendRequestW(hRequest, lpszHeaders, dwHeadersLength,
        lpOptional, dwOptionalLength);
    __try {
        std::lock_guard<std::mutex> lk(g_wi_mutex);
        auto it = g_wi_reqs.find((uintptr_t)hRequest);
        if (it != g_wi_reqs.end()) {
            it->second.start_ns    = t0;
            it->second.bytes_sent += dwOptionalLength;
            if (Real_HttpQueryInfoA) {
                DWORD status = 0, size = sizeof(DWORD);
                if (Real_HttpQueryInfoA(hRequest,
                    HTTP_QUERY_STATUS_CODE | HTTP_QUERY_FLAG_NUMBER,
                    &status, &size, nullptr))
                    it->second.http_status = (int)status;
            }
        }
    } __except(EXCEPTION_EXECUTE_HANDLER) {}
    return result;
}

static BOOL WINAPI Hook_InternetCloseHandle(HINTERNET hInternet) {
    HttpReqInfo  req  = {};
    HttpConnInfo conn = {};
    bool is_req = false;
    __try {
        std::lock_guard<std::mutex> lk(g_wi_mutex);
        auto rit = g_wi_reqs.find((uintptr_t)hInternet);
        if (rit != g_wi_reqs.end()) {
            req = rit->second; is_req = true;
            auto cit = g_wi_conns.find(req.conn_handle);
            if (cit != g_wi_conns.end()) conn = cit->second;
            g_wi_reqs.erase(rit);
        }
        g_wi_conns.erase((uintptr_t)hInternet);
    } __except(EXCEPTION_EXECUTE_HANDLER) {}

    if (is_req && req.start_ns > 0) {
        __try {
            Span sp = {};
            memcpy(sp.trace_id, req.trace_id, sizeof(sp.trace_id));
            memcpy(sp.span_id,  req.span_id,  sizeof(sp.span_id));
            snprintf(sp.name, sizeof(sp.name), "%s %s", req.method, req.path);
            sp.kind = 3; sp.start_ns = req.start_ns; sp.end_ns = now_ns();
            sp.http_status = req.http_status; sp.error = (req.http_status >= 500);
            sp.bytes_recv = req.bytes_recv; sp.peer_port = conn.port;
            strncpy_s(sp.peer_host,   conn.host,  sizeof(sp.peer_host)   - 1);
            strncpy_s(sp.http_method, req.method, sizeof(sp.http_method) - 1);
            strncpy_s(sp.http_path,   req.path,   sizeof(sp.http_path)   - 1);
            strncpy_s(sp.transport, conn.port == 443 ? "https" : "http", sizeof(sp.transport));
            push_span(sp);
        } __except(EXCEPTION_EXECUTE_HANDLER) {}
    }
    return Real_InternetCloseHandle(hInternet);
}

// ═════════════════════════════════════════════════════════════════════════════
// DLL ENTRY — instalação dinâmica de hooks
// ═════════════════════════════════════════════════════════════════════════════

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hModule);

        if (!GetEnvironmentVariableA("JARVIS_OTEL_ENDPOINT", g_endpoint, sizeof(g_endpoint)))
            strncpy_s(g_endpoint, "http://localhost:4318", sizeof(g_endpoint));

        if (!GetEnvironmentVariableA("OTEL_SERVICE_NAME", g_service_name, sizeof(g_service_name))) {
            char exe[MAX_PATH] = {};
            GetModuleFileNameA(nullptr, exe, MAX_PATH);
            char* fn = strrchr(exe, '\\');
            if (fn) {
                strncpy_s(g_service_name, fn + 1, sizeof(g_service_name));
                char* dot = strrchr(g_service_name, '.');
                if (dot) *dot = '\0';
            }
        }

        // Carregar WinHTTP e WinInet antes do DetourTransactionBegin
        // para obter os Real_* antes de instalar os hooks
        HMODULE hWinHTTP = GetModuleHandleA("winhttp.dll");
        if (!hWinHTTP) hWinHTTP = LoadLibraryA("winhttp.dll");
        if (hWinHTTP) {
            Real_WinHttpConnect         = (PFN_WinHttpConnect)        GetProcAddress(hWinHTTP, "WinHttpConnect");
            Real_WinHttpOpenRequest     = (PFN_WinHttpOpenRequest)    GetProcAddress(hWinHTTP, "WinHttpOpenRequest");
            Real_WinHttpSendRequest     = (PFN_WinHttpSendRequest)    GetProcAddress(hWinHTTP, "WinHttpSendRequest");
            Real_WinHttpReceiveResponse = (PFN_WinHttpReceiveResponse)GetProcAddress(hWinHTTP, "WinHttpReceiveResponse");
            Real_WinHttpReadData        = (PFN_WinHttpReadData)       GetProcAddress(hWinHTTP, "WinHttpReadData");
            Real_WinHttpQueryHeaders    = (PFN_WinHttpQueryHeaders)   GetProcAddress(hWinHTTP, "WinHttpQueryHeaders");
            Real_WinHttpCloseHandle     = (PFN_WinHttpCloseHandle)    GetProcAddress(hWinHTTP, "WinHttpCloseHandle");
        }

        HMODULE hWinInet = GetModuleHandleA("wininet.dll");
        if (!hWinInet) hWinInet = LoadLibraryA("wininet.dll");
        if (hWinInet) {
            Real_InternetConnectA    = (PFN_InternetConnectA)   GetProcAddress(hWinInet, "InternetConnectA");
            Real_InternetConnectW    = (PFN_InternetConnectW)   GetProcAddress(hWinInet, "InternetConnectW");
            Real_HttpOpenRequestA    = (PFN_HttpOpenRequestA)   GetProcAddress(hWinInet, "HttpOpenRequestA");
            Real_HttpOpenRequestW    = (PFN_HttpOpenRequestW)   GetProcAddress(hWinInet, "HttpOpenRequestW");
            Real_HttpSendRequestA    = (PFN_HttpSendRequestA)   GetProcAddress(hWinInet, "HttpSendRequestA");
            Real_HttpSendRequestW    = (PFN_HttpSendRequestW)   GetProcAddress(hWinInet, "HttpSendRequestW");
            Real_InternetReadFile    = (PFN_InternetReadFile)   GetProcAddress(hWinInet, "InternetReadFile");
            Real_HttpQueryInfoA      = (PFN_HttpQueryInfoA)     GetProcAddress(hWinInet, "HttpQueryInfoA");
            Real_InternetCloseHandle = (PFN_InternetCloseHandle)GetProcAddress(hWinInet, "InternetCloseHandle");
        }

        DetourTransactionBegin();
        DetourUpdateThread(GetCurrentThread());

        // Tier A — Winsock TCP
        DetourAttach(&(PVOID&)Real_connect,     Hook_connect);
        DetourAttach(&(PVOID&)Real_send,        Hook_send);
        DetourAttach(&(PVOID&)Real_recv,        Hook_recv);
        DetourAttach(&(PVOID&)Real_WSASend,     Hook_WSASend);
        DetourAttach(&(PVOID&)Real_WSARecv,     Hook_WSARecv);
        DetourAttach(&(PVOID&)Real_closesocket, Hook_closesocket);

        // Tier B — Winsock UDP
        DetourAttach(&(PVOID&)Real_WSASendTo,   Hook_WSASendTo);
        DetourAttach(&(PVOID&)Real_WSARecvFrom, Hook_WSARecvFrom);

        // Tier C — WinHTTP (só se disponível no processo)
        if (Real_WinHttpConnect)         DetourAttach(&(PVOID&)Real_WinHttpConnect,         Hook_WinHttpConnect);
        if (Real_WinHttpOpenRequest)     DetourAttach(&(PVOID&)Real_WinHttpOpenRequest,     Hook_WinHttpOpenRequest);
        if (Real_WinHttpSendRequest)     DetourAttach(&(PVOID&)Real_WinHttpSendRequest,     Hook_WinHttpSendRequest);
        if (Real_WinHttpReceiveResponse) DetourAttach(&(PVOID&)Real_WinHttpReceiveResponse, Hook_WinHttpReceiveResponse);
        if (Real_WinHttpReadData)        DetourAttach(&(PVOID&)Real_WinHttpReadData,        Hook_WinHttpReadData);
        if (Real_WinHttpCloseHandle)     DetourAttach(&(PVOID&)Real_WinHttpCloseHandle,     Hook_WinHttpCloseHandle);

        // Tier D — WinInet A + W (só se disponível no processo)
        if (Real_InternetConnectA)    DetourAttach(&(PVOID&)Real_InternetConnectA,    Hook_InternetConnectA);
        if (Real_InternetConnectW)    DetourAttach(&(PVOID&)Real_InternetConnectW,    Hook_InternetConnectW);
        if (Real_HttpOpenRequestA)    DetourAttach(&(PVOID&)Real_HttpOpenRequestA,    Hook_HttpOpenRequestA);
        if (Real_HttpOpenRequestW)    DetourAttach(&(PVOID&)Real_HttpOpenRequestW,    Hook_HttpOpenRequestW);
        if (Real_HttpSendRequestA)    DetourAttach(&(PVOID&)Real_HttpSendRequestA,    Hook_HttpSendRequestA);
        if (Real_HttpSendRequestW)    DetourAttach(&(PVOID&)Real_HttpSendRequestW,    Hook_HttpSendRequestW);
        if (Real_InternetReadFile)    DetourAttach(&(PVOID&)Real_InternetReadFile,    Hook_InternetReadFile);
        if (Real_InternetCloseHandle) DetourAttach(&(PVOID&)Real_InternetCloseHandle, Hook_InternetCloseHandle);

        DetourTransactionCommit();

        std::thread(flush_thread_fn).detach();

    } else if (reason == DLL_PROCESS_DETACH) {
        g_running.store(false);

        DetourTransactionBegin();
        DetourUpdateThread(GetCurrentThread());

        DetourDetach(&(PVOID&)Real_connect,     Hook_connect);
        DetourDetach(&(PVOID&)Real_send,        Hook_send);
        DetourDetach(&(PVOID&)Real_recv,        Hook_recv);
        DetourDetach(&(PVOID&)Real_WSASend,     Hook_WSASend);
        DetourDetach(&(PVOID&)Real_WSARecv,     Hook_WSARecv);
        DetourDetach(&(PVOID&)Real_closesocket, Hook_closesocket);
        DetourDetach(&(PVOID&)Real_WSASendTo,   Hook_WSASendTo);
        DetourDetach(&(PVOID&)Real_WSARecvFrom, Hook_WSARecvFrom);

        if (Real_WinHttpConnect)         DetourDetach(&(PVOID&)Real_WinHttpConnect,         Hook_WinHttpConnect);
        if (Real_WinHttpOpenRequest)     DetourDetach(&(PVOID&)Real_WinHttpOpenRequest,     Hook_WinHttpOpenRequest);
        if (Real_WinHttpSendRequest)     DetourDetach(&(PVOID&)Real_WinHttpSendRequest,     Hook_WinHttpSendRequest);
        if (Real_WinHttpReceiveResponse) DetourDetach(&(PVOID&)Real_WinHttpReceiveResponse, Hook_WinHttpReceiveResponse);
        if (Real_WinHttpReadData)        DetourDetach(&(PVOID&)Real_WinHttpReadData,        Hook_WinHttpReadData);
        if (Real_WinHttpCloseHandle)     DetourDetach(&(PVOID&)Real_WinHttpCloseHandle,     Hook_WinHttpCloseHandle);

        if (Real_InternetConnectA)    DetourDetach(&(PVOID&)Real_InternetConnectA,    Hook_InternetConnectA);
        if (Real_InternetConnectW)    DetourDetach(&(PVOID&)Real_InternetConnectW,    Hook_InternetConnectW);
        if (Real_HttpOpenRequestA)    DetourDetach(&(PVOID&)Real_HttpOpenRequestA,    Hook_HttpOpenRequestA);
        if (Real_HttpOpenRequestW)    DetourDetach(&(PVOID&)Real_HttpOpenRequestW,    Hook_HttpOpenRequestW);
        if (Real_HttpSendRequestA)    DetourDetach(&(PVOID&)Real_HttpSendRequestA,    Hook_HttpSendRequestA);
        if (Real_HttpSendRequestW)    DetourDetach(&(PVOID&)Real_HttpSendRequestW,    Hook_HttpSendRequestW);
        if (Real_InternetReadFile)    DetourDetach(&(PVOID&)Real_InternetReadFile,    Hook_InternetReadFile);
        if (Real_InternetCloseHandle) DetourDetach(&(PVOID&)Real_InternetCloseHandle, Hook_InternetCloseHandle);

        DetourTransactionCommit();
    }
    return TRUE;
}
