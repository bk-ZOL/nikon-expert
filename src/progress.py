# src/progress.py
# 一次提问的实时进度：检索/精排/智能体各阶段调用 report()，网页每秒读一次显示给使用者。
# 网页建一个 Progress 交给检索线程 bind()，各阶段 report() 写、网页读，共享同一个对象。
# （不能在 chat() 里设 contextvar：Gradio 每推进一步生成器都复制一次上下文，设了下一步就丢。）
# 没有 bind() 的调用方（MCP、脚本）report() 什么都不做。

import time
from contextvars import ContextVar
from typing import Optional


class Progress:
    def __init__(self):
        self.t0 = time.time()
        self.stage = "正在检索知识库"

    def elapsed(self) -> int:
        return int(time.time() - self.t0)


_current: ContextVar = ContextVar("nikon_progress", default=None)


def bind(p: Progress):
    """在检索线程里绑定进度对象（网页把同一个对象传进来，读写共享）。"""
    _current.set(p)


def report(stage: str) -> None:
    p: Optional[Progress] = _current.get()
    if p is not None:
        p.stage = stage
