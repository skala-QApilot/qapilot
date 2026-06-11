import asyncio
import uuid
from qapilot.shared.schemas import ToolInput
from qapilot.tools.codebase_scanner_tool import CodebaseScannerTool

async def main():
    tool = CodebaseScannerTool()
    result = await tool.run(ToolInput(trace_id=str(uuid.uuid4()), params={"trigger": "init"}))
    print(result)

asyncio.run(main())
