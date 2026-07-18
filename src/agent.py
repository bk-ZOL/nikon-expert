# src/agent.py
# Nikon Expert — ReAct 排障 Agent（P1 骨架）
# 让 LLM 自主决定调用哪些检索工具、多步取证后再下结论。
# 对外暴露同步生成器 run_agent_sync()，产出结构化事件供 engine/UI 消费。

import os
import queue
import asyncio
import threading

from llama_index.core.agent.workflow import (
    ReActAgent, AgentStream, ToolCall, ToolCallResult,
)

from src.tools import build_tools, SourceCollector


MAX_ITER = int(os.getenv("AGENT_MAX_ITERATIONS", "6"))

# Agent 系统提示：排障专家 + 工具使用纪律 + 引用约束
SYSTEM_PROMPT = """你是「Nikon Expert」—— 晶圆厂 Nikon 光刻机设备的故障排查专家 Agent。

你的工作方式（务必遵守）：
1. 面对故障描述，先判断「要下结论还缺什么信息」，再决定调用哪个工具去取证，不要凭空回答。
2. 善用工具组合，多步取证：
   - 出现明确报警码 → 先用 error_code_lookup 查含义；
   - 想找历史处置先例 / 定位根因 → 用 fault_history_lookup；
   - 查具体部件、参数、操作步骤 → 用 keyword_search；
   - 概念/原理/「为什么」类 → 用 semantic_search；
   - 涉及接线/电路/信号/机械装配、需要看图纸时 → 用 inspect_diagram（定位相关页 + 文字层内容 + 原图链接）；
   - 要确认"某元件/传感器连到哪、用哪根线/pin、到哪个面板板卡" → 用 trace_connection（按线号交叉引用还原连接）。
3. 允许多轮调用：拿到一个工具结果后，若信息仍不足以支撑排查结论，继续调用其他工具。
   但务必高效：不要用相同参数重复调用同一个工具；一旦已取到足够信息，立即停止取证、直接给出最终结论，不要为凑步数而反复搜索。
4. 所有技术结论必须来自工具返回的资料，禁止捏造 Error Code、参数数值、操作步骤或章节号。
5. 若多轮取证后知识库仍无充分记录，如实说明「知识库中未找到充分记录，建议联系 Nikon FAE」，不要用通用常识硬填。

最终回答用中文，按以下结构输出（每个技术结论末尾标注来源，如 [来源：故障履历 2025-03] 或 [来源：手册第X页]）：

**【故障初步判断】**
（对现象的客观定性，一到两句）

**【排查步骤】**
Step 1：具体可执行操作 [来源：...]
Step 2：... [来源：...]

**【注意事项】**
（安全 / 工具 / 前置条件，如有）

**【相关历史案例】**（如取到）
（故障履历中的相似案例简述 + 引用）
"""


def _to_chat_history(history):
    """把 UI 的 [{'role','content'},...] 转成 ChatMessage 列表（取最近几轮）。"""
    if not history:
        return None
    from llama_index.core.llms import ChatMessage
    msgs = []
    for m in history[-6:]:
        role = m.get("role", "user")
        content = (m.get("content", "") or "")[:800]
        if not content:
            continue
        msgs.append(ChatMessage(role="user" if role == "user" else "assistant", content=content))
    return msgs or None


async def _agent_events(question: str, history=None):
    """异步事件生成器：yield 结构化事件 dict。"""
    from src.engine import _init_engine
    from llama_index.core import Settings

    eng = _init_engine()
    collector = SourceCollector()
    tools = build_tools(collector, retriever=eng.get("retriever"))

    agent = ReActAgent(
        tools=tools,
        llm=Settings.llm,
        system_prompt=SYSTEM_PROMPT,
        max_iterations=MAX_ITER,
    )

    handler = agent.run(question, chat_history=_to_chat_history(history))

    async for ev in handler.stream_events():
        # 注意：ToolCallResult 是 ToolCall 的子类，必须先判子类
        if isinstance(ev, ToolCallResult):
            yield {"kind": "observation",
                   "tool": ev.tool_name,
                   "output": str(ev.tool_output)}
        elif isinstance(ev, ToolCall):
            yield {"kind": "tool_call",
                   "tool": ev.tool_name,
                   "args": dict(ev.tool_kwargs)}
        # AgentStream（原始 ReAct 推理 token）P1 暂不透传，避免 Thought/Action 脚手架噪声

    resp = await handler
    yield {"kind": "answer",
           "text": str(resp),
           "citations": collector.citations(),
           "citations_data": collector.citations_data(),
           "has_result": collector.has_any()}


def run_agent_sync(question: str, history=None):
    """同步生成器：在后台线程跑异步 agent，通过队列把事件同步吐出。

    yield 的事件 dict 形如：
      {"kind": "tool_call", "tool": str, "args": dict}
      {"kind": "observation", "tool": str, "output": str}
      {"kind": "answer", "text": str, "citations": [...], "citations_data": [...], "has_result": bool}
      {"kind": "error", "error": str}
    """
    q: queue.Queue = queue.Queue()
    SENTINEL = object()

    def worker():
        async def go():
            async for ev in _agent_events(question, history):
                q.put(ev)
        try:
            asyncio.run(go())
        except Exception as e:
            q.put({"kind": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            q.put(SENTINEL)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    while True:
        ev = q.get()
        if ev is SENTINEL:
            break
        yield ev
    t.join(timeout=1)
