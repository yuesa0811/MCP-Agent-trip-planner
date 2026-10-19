"""0.1.11 服务端的 stdio 兼容入口，由 uvx 的独立环境运行。"""


def main():
    from amap_mcp_server import server

    # 该版本公交工具有 print(data)，会污染 JSON-RPC stdout，且可能采用 Windows GBK。
    # 仅屏蔽此模块的调试 print，不改安装包、SDK 的协议写入或正常 stderr 日志。
    server.print = lambda *args, **kwargs: None
    server.mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
