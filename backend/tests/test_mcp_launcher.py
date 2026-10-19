"""兼容入口只屏蔽第三方模块的 print，保留协议输出和正常 stderr。"""

import sys
from types import ModuleType, SimpleNamespace

from scripts.run_amap_mcp import main


def test_server_debug_print_does_not_pollute_stdout(monkeypatch, capsys):
    def run(transport):
        assert transport == "stdio"
        server.print("调试内容不应进入协议流")
        print('{"jsonrpc":"2.0"}')
        print("正常 stderr 日志", file=sys.stderr)
    server = SimpleNamespace(mcp=SimpleNamespace(run=run))
    package = ModuleType("amap_mcp_server")
    package.server = server
    monkeypatch.setitem(sys.modules, "amap_mcp_server", package)
    main()
    captured = capsys.readouterr()
    assert captured.out == '{"jsonrpc":"2.0"}\n'
    assert captured.err == "正常 stderr 日志\n"
