# -*- coding: utf-8 -*-
"""外部笔记注入：把本地文本文件按轮拼进系统提示词。

用途
----
AstrBot 的人设是一段静态文本。可现实中总有一些「每轮都要让它看见、但又不该
写死人设」的内容：

* 它自己随手记下的便签 / 待办（谁是谁、说好要做什么）
* 它给自己写的补充页（人设之外的成长记录）
* 一份临时的行为守则、当天的任务清单、项目约定
* 多台机器 / 多个角色共享的一份事实清单

这些都适合放在普通文本文件里，让本插件在 ``on_llm_request`` 阶段读出来、按
顺序拼到 system prompt 末尾。

文件格式
--------
* 以 ``- `` / ``* `` / ``• `` 开头的行算一条；没有这种行的文件，把每个非空、
  且不以 ``#`` 开头的行都当一条。
* 默认取「最新的 N 条」（写在文件末尾的更新），也可以按来源切成从头部取。

性能
----
按 (路径, 修改时间, 大小) 缓存解析结果，文件没变就不重复读盘、不重复解析。
"""

from __future__ import annotations

import os
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

BULLETS = ("- ", "* ", "• ")

DEFAULTS = {
    "enabled": True,
    "position": "system_prompt",
    "max_total_chars": 1600,
    "sources": [],
    "show_when_empty": False,
}


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Source:
    """一个笔记来源：标签 + 文件 + 取多少条 + 最多多少字。"""

    __slots__ = ("label", "path", "max_entries", "max_chars", "take")

    def __init__(self, label: str, path: str, max_entries: int, max_chars: int, take: str):
        self.label = label
        self.path = path
        self.max_entries = max_entries
        self.max_chars = max_chars
        self.take = take  # tail：取末尾最新的；head：从头取

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Source({self.label!r}, {self.path!r}, {self.max_entries}, {self.max_chars}, {self.take!r})"


def _parse_source(raw: str) -> Source | None:
    """解析 ``标签|路径|条数|字数|head|tail``，后三项可省略。"""
    text = str(raw or "").strip()
    if not text:
        return None
    fields = [part.strip() for part in text.split("|")]
    if len(fields) < 2:
        # 只写了路径：用文件名当标签
        path = fields[0]
        return Source(Path(path).stem, path, 8, 700, "tail") if path else None
    label, path = fields[0], fields[1]
    max_entries = _as_int(fields[2], 8) if len(fields) > 2 and fields[2] else 8
    max_chars = _as_int(fields[3], 700) if len(fields) > 3 and fields[3] else 700
    take = (fields[4].lower() if len(fields) > 4 and fields[4] else "tail")
    if take not in ("head", "tail"):
        take = "tail"
    if not path:
        return None
    return Source(label or Path(path).stem, path, max(0, max_entries), max(0, max_chars), take)


def _entries(text: str) -> list[str]:
    """把文件内容切成条目列表。"""
    bullets: list[str] = []
    plain: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(BULLETS):
            bullets.append(stripped[2:].strip())
        elif not stripped.startswith("#"):
            plain.append(stripped)
    return bullets or plain


@register(
    "astrbot_plugin_note_inject",
    "wzq",
    "把本地文本文件（便签、备忘、补充人设、守则）按轮注入系统提示词",
    "1.0.0",
)
class NoteInjectPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.conf = config or {}
        self._cache: dict[str, tuple[tuple[float, int], list[str]]] = {}

    # ------------------------------------------------------------------ 配置
    def _opt(self, key: str):
        try:
            value = self.conf.get(key)
        except Exception:
            value = None
        return DEFAULTS[key] if value is None else value

    def _sources(self) -> list[Source]:
        raw = self._opt("sources") or []
        if isinstance(raw, str):
            raw = [raw]
        out: list[Source] = []
        for item in raw:
            src = _parse_source(item)
            if src:
                out.append(src)
            else:
                logger.warning(f"[note_inject] 配置项格式不对，已跳过: {item!r}")
        return out

    # ------------------------------------------------------------------ 读文件
    def _load_entries(self, path: str) -> list[str]:
        """带 mtime 缓存的读取。"""
        try:
            stat = os.stat(path)
        except OSError:
            self._cache.pop(path, None)
            return []
        key = (stat.st_mtime, stat.st_size)
        cached = self._cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        try:
            text = Path(path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning(f"[note_inject] 读 {path} 失败: {exc}")
            return []
        entries = _entries(text)
        self._cache[path] = (key, entries)
        return entries

    def _block_for(self, src: Source) -> tuple[str, str]:
        """返回 (正文, 原始全文)，任意一项为空表示没有内容。"""
        entries = self._load_entries(src.path)
        if not entries:
            return "", ""
        picked = entries[: src.max_entries] if src.take == "head" and src.max_entries else entries
        if src.take == "tail" and src.max_entries:
            picked = entries[-src.max_entries:]

        chosen: list[str] = []
        used = 0
        for item in picked:
            cost = len(item) + 3
            if src.max_chars and used + cost > src.max_chars:
                break
            chosen.append(item)
            used += cost
        if not chosen:
            return "", ""
        body = "\n".join(f"- {item}" for item in chosen)
        return f"【{src.label}】\n{body}", body

    def build_blocks(self) -> list[tuple[str, str]]:
        """按配置顺序生成 [(块文本, 块正文)]。"""
        blocks: list[tuple[str, str]] = []
        budget = max(200, _as_int(self._opt("max_total_chars"), 1600))
        spent = 0
        for src in self._sources():
            block, body = self._block_for(src)
            if not block:
                continue
            if spent + len(block) > budget:
                remain = budget - spent
                if remain < 80:
                    logger.info("[note_inject] 总字数预算已用完，后面的来源被跳过")
                    break
                block, body = block[:remain], body[:remain]
            blocks.append((block, body))
            spent += len(block) + 2
        return blocks

    # ------------------------------------------------------------------ 注入
    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not _as_bool(self._opt("enabled"), True):
            return
        try:
            blocks = self.build_blocks()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[note_inject] 生成内容失败: {exc}")
            return
        if not blocks:
            if _as_bool(self._opt("show_when_empty"), False):
                self._append(req, "【笔记】暂无记录。")
            return

        base = req.system_prompt or ""
        fresh = [block for block, body in blocks if body not in base]
        if not fresh:
            return
        self._append(req, "\n\n".join(fresh))

    def _append(self, req: ProviderRequest, text: str) -> None:
        position = str(self._opt("position") or "system_prompt").lower()
        if position in ("user_content", "user", "extra"):
            try:
                parts = getattr(req, "extra_user_content_parts", None)
                if parts is None:
                    raise AttributeError
                parts.append({"type": "text", "text": text})
                return
            except Exception:
                pass
        req.system_prompt = f"{req.system_prompt or ''}\n\n{text}"
        logger.info("[note_inject] 已追加 %d 个笔记块", text.count("【"))

    # ------------------------------------------------------------------ 指令
    @filter.command("笔记注入")
    async def preview(self, event: AstrMessageEvent):
        """预览当前会被注入的内容"""
        sources = self._sources()
        if not sources:
            yield event.plain_result("还没有配置任何笔记来源。")
            return
        blocks = self.build_blocks()
        if not blocks:
            yield event.plain_result(f"配了 {len(sources)} 个来源，但都没读到内容。")
            return
        total = sum(len(b) for b, _ in blocks)
        head = f"共 {len(blocks)}/{len(sources)} 个来源，{total} 字\n\n"
        yield event.plain_result(head + "\n\n".join(b for b, _ in blocks))
