# src/agent.py
# Nikon Expert — ReAct 排障 Agent（P1 骨架）
# 让 LLM 自主决定调用哪些检索工具、多步取证后再下结论。
# 对外暴露同步生成器 run_agent_sync()，产出结构化事件供 engine/UI 消费。

import os
import queue
import asyncio
import threading
import traceback
import contextvars

from llama_index.core.agent.workflow import (
    ReActAgent, AgentStream, AgentInput, ToolCall, ToolCallResult,
)

from src.tools import build_tools, SourceCollector


MAX_ITER = int(os.getenv("AGENT_MAX_ITERATIONS", "6"))

# Agent 系统提示：排障专家 + 工具使用纪律 + 引用约束
SYSTEM_PROMPT = """你是「Nikon Expert」—— 晶圆厂 Nikon 光刻机设备的故障排查专家 Agent。

你的工作方式（务必遵守）：
1. 面对故障描述，先判断「要下结论还缺什么信息」，再决定调用哪个工具去取证，不要凭空回答。
2. 善用工具组合，多步取证：
   - 遇到报警 / 故障 → **先用 troubleshooting_lookup 查现场验证过的经验**（根因、真正有效的措施、发生次数），回答里注明「现场处理过 N 次」；
   - 出现明确报警码 → 再用 error_code_lookup 查含义（现场写法如 STG-29-80 也直接传给它，会自动换算成手册里的主码/细分码）；
   - 想找历史处置先例 / 定位根因 → 用 fault_history_lookup；
   - 问「怎么做 / 步骤 / 流程 / SOP」（拆装、调整、清洁、软件操作、确认作业）→ **先用 procedure_lookup** 取完整的原文步骤，按原文顺序整理成中文步骤，不增删步骤，按钮名/参数名/数值保留原文；
   - 查具体部件、参数 → 用 keyword_search；
   - 概念/原理/「为什么」类 → 用 semantic_search；
   - 涉及接线/电路/信号/机械装配、需要看图纸时 → 用 inspect_diagram（定位相关页 + 文字层内容 + 原图链接）；
   - 问题里有接插件号（J7554A、P1394A）或线缆号（4S095-884），要查"接到哪、经过哪根线、哪几针" → 先用 netlist_trace（S207D 线表，可往外追几跳）；
   - 要确认"某元件/传感器连到哪、用哪根线/pin、到哪个面板板卡"，netlist_trace 查不到或没有编号时 → 用 trace_connection（按线号交叉引用还原连接）。
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

【思考（Thought）的写法】
每一步的 Thought 会实时显示给现场工程师看，**一律用中文**，一两句话说清楚“刚才查到了什么、接下来查什么、为什么”，不要写英文。
**Thought 里绝不写结论正文。** 信息够了就严格按格式输出：先写一句 `Thought: 信息已足够，给出结论。`，换行写 `Answer:`，把上面的结论模板全部写在 `Answer:` 后面。
"""


def history_text(content) -> str:
    """取聊天记录里一条消息的纯文字。
    gradio 6 回传的 content 是 [{'type':'text','text':...}, ...] 列表，不是字符串；文件/图片块忽略。"""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if isinstance(content, (list, tuple)):
        # 多轮后会出现嵌套：{'type':'text','text':[{'type':'text',...}]}，递归取文字
        return "\n".join(history_text(b.get("text", "")) if isinstance(b, dict) else history_text(b)
                         for b in content).strip()
    return ""


_DSML_RE = None


def _strip_dsml(text: str) -> str:
    """DeepSeek 偶尔不走工具调用接口，把调用当文字吐出（<｜DSML｜invoke ...>），显示前去掉。"""
    global _DSML_RE
    if "DSML" not in text:
        return text
    import re
    if _DSML_RE is None:
        _DSML_RE = re.compile(r"<[｜|]+\s*DSML\s*[｜|]+.*", re.S)   # 从第一个 DSML 标记起全部丢弃
    return _DSML_RE.sub("", text).rstrip(" \n-")


class ReActStreamParser:
    """把 ReAct 的原始流（"Thought: …\nAction: …" / "Thought: …\nAnswer: …"）拆成可显示的片段。

    feed(delta) 返回 [(kind, text), …]，kind 为 "thought"（思考）或 "answer"（结论）。
    - Action / Action Input 不显示（界面另有 🔧 行）；一步里出现第一个 Action 后，本步剩余内容都丢弃
      （模型常常一口气写好几组 Thought/Action，只有第一组会真正执行）。
    - 末尾扣住 HOLD 个字符再放，免得把 "Action"/"Answer" 关键字或 DSML 标记的前半截先显示出去。
    - 结论里一旦出现 DSML 工具调用标记，就截断、不再输出。
    """
    HOLD = 12
    THOUGHT_MAX = 150     # 每步思考最多显示的字数
    _STOP_THOUGHT = ("\nAction:", "Action:", "Answer:")

    def __init__(self):
        self.answer_started = False   # 整次运行是否已开始输出结论
        self.answer_text = ""
        self.reset()

    def reset(self):
        self.buf = ""         # 本步累计原文
        self.pos = 0          # 已经处理到 buf 的哪里
        self.mode = "head"    # head（等 Thought:/Answer:）/ thought / muted（思考已收起）/ answer / done
        self.thought_len = 0

    def _emit_upto(self, end, kind, out):
        if end > self.pos:
            out.append((kind, self.buf[self.pos:end]))
            self.pos = end

    def feed(self, delta: str, final: bool = False) -> list:
        out = []
        self.buf += delta
        while True:
            if self.mode == "head":
                i_t, i_a = self.buf.find("Thought:", self.pos), self.buf.find("Answer:", self.pos)
                if i_a != -1 and (i_t == -1 or i_a < i_t):
                    self.pos, self.mode = i_a + len("Answer:"), "answer"
                    continue
                if i_t != -1:
                    self.pos, self.mode = i_t + len("Thought:"), "thought"
                    continue
                if "Action:" in self.buf[self.pos:]:
                    self.mode = "done"
                return out
            if self.mode == "thought":
                # 模型有时把整篇结论塞进 Thought（没写 Answer:），框架会让它重写，界面上就会重复好几遍。
                # 思考只显示开头一两句：到 THOUGHT_MAX 字或出现结论标题「**【」就收起（mute_at），后面静默跳过。
                cut = self.buf.find("**【", self.pos)
                mute_at = self.pos + max(self.THOUGHT_MAX - self.thought_len, 0)
                if cut != -1:
                    mute_at = min(mute_at, cut)
                hits = [(self.buf.find(k, self.pos), k) for k in self._STOP_THOUGHT]
                hits = [(i, k) for i, k in hits if i != -1]
                stop_i, stop_k = min(hits) if hits else (None, None)
                if stop_i is not None and stop_i <= mute_at:
                    self._emit_thought(stop_i, out)
                    if stop_k == "Answer:":
                        self.pos, self.mode = stop_i + len(stop_k), "answer"
                        continue
                    self.mode = "done"
                    return out
                end = len(self.buf) if final else max(self.pos, len(self.buf) - self.HOLD)
                if mute_at < end or stop_i is not None:
                    self._emit_thought(min(mute_at, end), out)
                    out.append(("thought", "…"))
                    self.mode = "muted"
                    continue
                self._emit_thought(end, out)
                return out
            if self.mode == "muted":
                # 思考已收起：只等 Action:（本步结束）或 Answer:（开始结论）
                i_ans = self.buf.find("Answer:", self.pos)
                i_act = self.buf.find("Action:", self.pos)
                if i_ans != -1 and (i_act == -1 or i_ans < i_act):
                    self.pos, self.mode = i_ans + len("Answer:"), "answer"
                    continue
                if i_act != -1:
                    self.mode = "done"
                return out
            if self.mode == "answer":
                if not self.answer_started:
                    # 去掉 "Answer:" 后紧跟的空白
                    while self.pos < len(self.buf) and self.buf[self.pos] in " \n":
                        self.pos += 1
                cut = min([i for i in (self.buf.find("DSML", self.pos), self.buf.find("<｜", self.pos),
                                       self.buf.find("<|", self.pos)) if i != -1], default=-1)
                if cut != -1:
                    self._emit_answer(cut, out)
                    self.mode = "done"
                    return out
                end = len(self.buf) if final else max(self.pos, len(self.buf) - self.HOLD)
                self._emit_answer(end, out)
                return out
            return out   # done

    def _emit_thought(self, end, out):
        if end > self.pos:
            self.thought_len += end - self.pos
            self._emit_upto(end, "thought", out)

    def _emit_answer(self, end, out):
        if end > self.pos:
            t = self.buf[self.pos:end]
            self.answer_started = True
            self.answer_text += t
            out.append(("answer", t))
            self.pos = end

    def flush(self) -> list:
        """本步结束：把扣住的尾巴放出来。"""
        return self.feed("", final=True)


def _to_chat_history(history):
    """把 UI 的 [{'role','content'},...] 转成 ChatMessage 列表（取最近几轮）。"""
    if not history:
        return None
    from llama_index.core.llms import ChatMessage
    msgs = []
    for m in history[-6:]:
        role = m.get("role", "user")
        content = history_text(m.get("content", ""))[:800]
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
    tools = build_tools(collector, retriever=eng.get("retriever"), question=question, history=history)

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
        elif isinstance(ev, AgentStream):
            # 原始 ReAct 推理 token：交给 ReActStreamParser 拆出 Thought（思考）/ Answer（结论）实时显示
            yield {"kind": "stream", "delta": ev.delta or ""}
        elif isinstance(ev, AgentInput):
            yield {"kind": "step"}    # 新一步开始，解析状态归零

    resp = await handler
    yield {"kind": "answer",
           "text": _strip_dsml(str(resp)),
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
        except BaseException as e:  # CancelledError 等不是 Exception 子类，漏掉会让 agent 无声结束
            traceback.print_exc()
            q.put({"kind": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            q.put(SENTINEL)

    # 带上调用方的 contextvars（ACL 登录身份），否则工具里的检索拿不到当前用户
    ctx = contextvars.copy_context()
    t = threading.Thread(target=lambda: ctx.run(worker), daemon=True)
    t.start()
    while True:
        ev = q.get()
        if ev is SENTINEL:
            break
        yield ev
    t.join(timeout=1)
