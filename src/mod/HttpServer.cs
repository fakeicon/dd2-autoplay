using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Newtonsoft.Json;

namespace DD2TextMod
{
    internal sealed class HttpServer
    {
        private readonly int _port;
        private TcpListener _listener;
        private Thread _thread;
        private volatile bool _running;

        public HttpServer(int port) { _port = port; }

        public void Start()
        {
            _listener = new TcpListener(IPAddress.Loopback, _port);
            _listener.Server.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
            _listener.Start();
            _running = true;
            _thread = new Thread(AcceptLoop) { IsBackground = true, Name = "DD2TextModHttp" };
            _thread.Start();
            Plugin.Log?.LogInfo("HTTP bound: " + _listener.LocalEndpoint);
        }

        public void Stop()
        {
            _running = false;
            try { _listener?.Stop(); } catch { }
            Plugin.Log?.LogInfo("HTTP stopped");
        }

        private void AcceptLoop()
        {
            Plugin.Log?.LogInfo("HTTP accept loop started");
            while (_running)
            {
                TcpClient client = null;
                try
                {
                    client = _listener.AcceptTcpClient();
                    var c = client;
                    ThreadPool.QueueUserWorkItem(_ => Handle(c));
                }
                catch (Exception e)
                {
                    if (!_running) break;
                    Plugin.Log?.LogError("HTTP accept error: " + e.Message);
                    Thread.Sleep(200);
                    try { client?.Close(); } catch { }
                }
            }
        }

        private static int FindHeaderEnd(byte[] data)
        {
            for (int i = 0; i + 3 < data.Length; i++)
            {
                if (data[i] == 13 && data[i + 1] == 10 && data[i + 2] == 13 && data[i + 3] == 10) return i;
            }
            return -1;
        }

        private void Handle(object state)
        {
            var client = (TcpClient)state;
            try
            {
                using (client)
                using (var stream = client.GetStream())
                {
                    // 全按字节读，避免 UTF-8 中文被当成"字符数"读超时。
                    var head = new MemoryStream();
                    var buf = new byte[8192];
                    int headerEnd = -1;
                    while (headerEnd < 0)
                    {
                        int n = stream.Read(buf, 0, buf.Length);
                        if (n <= 0) return;
                        head.Write(buf, 0, n);
                        headerEnd = FindHeaderEnd(head.ToArray());
                        if (head.Length > 256 * 1024) return;
                    }

                    var all = head.ToArray();
                    string headerText = Encoding.UTF8.GetString(all, 0, headerEnd);
                    var lines = headerText.Split(new[] { "\r\n" }, StringSplitOptions.None);
                    if (lines.Length == 0) return;
                    var parts = lines[0].Split(' ');
                    if (parts.Length < 2) return;
                    string method = parts[0];
                    string rawPath = parts[1];
                    string query = "";
                    int qi = rawPath.IndexOf('?');
                    string path = rawPath;
                    if (qi >= 0)
                    {
                        query = rawPath.Substring(qi + 1);
                        path = rawPath.Substring(0, qi);
                    }

                    int contentLength = 0;
                    for (int i = 1; i < lines.Length; i++)
                    {
                        if (lines[i].StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase))
                            int.TryParse(lines[i].Substring(15).Trim(), out contentLength);
                    }

                    int bodyStart = headerEnd + 4;
                    int have = all.Length - bodyStart;
                    var bodyBytes = new byte[Math.Max(contentLength, 0)];
                    int copied = Math.Min(have, bodyBytes.Length);
                    if (copied > 0) Array.Copy(all, bodyStart, bodyBytes, 0, copied);
                    while (copied < bodyBytes.Length)
                    {
                        int n = stream.Read(bodyBytes, copied, bodyBytes.Length - copied);
                        if (n <= 0) break;
                        copied += n;
                    }
                    string body = Encoding.UTF8.GetString(bodyBytes, 0, copied);

                    object result;
                    int status = 200;
                    try
                    {
                        if (method == "GET" && path == "/health")
                        {
                            // 版本串要能区分"跑的是哪一份 DLL"：只写 0.4.0 时，改完 mod 重启也
                            // 没法从 /health 判断新代码到底生效没有（这正是"以为成功其实没成功"）。
                            // 改 mod 后顺手改这个后缀，就能一眼认出线上版本。
                            result = new { ok = true, plugin = "dd2-textmod", version = "0.4.1-onmain-gate" };
                        }
                        else if (method == "GET" && path == "/state")
                        {
                            result = Plugin.OnMain(() => StateBuilder.Build());
                        }
                        else if (method == "GET" && path == "/ui")
                        {
                            result = Plugin.OnMain(() => UiDriver.Build());
                        }
                        else if (method == "GET" && path == "/brief")
                        {
                            // 极简状态（<800 字节）：mode/scene/row/速度/金币/队伍血量/按钮文字
                            result = Plugin.OnMain(() => Brief.Build(), 8000);
                        }
                        else if (method == "GET" && path == "/delta")
                        {
                            // 版本号 + 差量：?since=<上次的 n>；没变化回 {"n":x,"same":true}
                            string since = GetParam(query, "since");
                            result = Plugin.OnMain(() => Brief.Delta(since), 8000);
                        }
                        else if (method == "GET" && path == "/catalog")
                        {
                            string ids = GetParam(query, "ids");
                            result = Plugin.OnMain(() => Catalog.Build(ids), 25000);
                        }
                        else if (method == "POST" && path == "/action")
                        {
                            result = Plugin.OnMain(() => StateBuilder.Act(body), 8000);
                        }
                        else
                        {
                            status = 404;
                            result = new { ok = false, error = "not found: " + path };
                        }
                    }
                    catch (Exception e)
                    {
                        status = 500;
                        result = new { ok = false, error = e.Message };
                    }

                    string json;
                    try
                    {
                        json = JsonConvert.SerializeObject(result, Formatting.Indented);
                    }
                    catch (Exception se)
                    {
                        json = "{\"ok\":false,\"error\":\"serialize: " + se.Message.Replace("\"", "'").Replace("\r", " ").Replace("\n", " ") + "\"}";
                    }
                    byte[] payload = Encoding.UTF8.GetBytes(json);
                    string header = "HTTP/1.1 " + status + " " + (status == 200 ? "OK" : "ERR") + "\r\n" +
                                    "Content-Type: application/json; charset=utf-8\r\n" +
                                    "Content-Length: " + payload.Length + "\r\n" +
                                    "Connection: close\r\n\r\n";
                    byte[] headBytes = Encoding.UTF8.GetBytes(header);
                    stream.Write(headBytes, 0, headBytes.Length);
                    stream.Write(payload, 0, payload.Length);
                }
            }
            catch (Exception e)
            {
                Plugin.Log?.LogDebug("HTTP handle: " + e.Message);
            }
        }

        private static string GetParam(string query, string name)
        {
            if (string.IsNullOrEmpty(query)) return "";
            foreach (var kv in query.Split('&'))
            {
                int eq = kv.IndexOf('=');
                if (eq <= 0) continue;
                if (kv.Substring(0, eq) == name) return Uri.UnescapeDataString(kv.Substring(eq + 1));
            }
            return "";
        }
    }
}
