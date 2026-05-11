"""Tool 모듈.

TOOL_REGISTRY: Agent에서 use_tool()로 호출할 때 사용하는 Tool 등록부.
"""

from qapilot.tools.base_tool import BaseTool
from qapilot.tools.codebase_scanner_tool import CodebaseScannerTool
from qapilot.tools.domain_knowledge import DomainKnowledgeTool
from qapilot.tools.ui_test_tool import UITestTool
from qapilot.tools.api_trace_tool import APITraceTool
from qapilot.tools.db_test_tool import DBTestTool
from qapilot.tools.report_tool import ReportTool

TOOL_REGISTRY: dict[str, type[BaseTool]] = {
    "codebase_scanner": CodebaseScannerTool,
    "domain_knowledge": DomainKnowledgeTool,
    "ui_test": UITestTool,
    "api_trace": APITraceTool,
    "db_test": DBTestTool,
    "report": ReportTool,
}
