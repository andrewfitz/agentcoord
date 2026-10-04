"""Read-only curses operator view of one workspace's authoritative service."""
from __future__ import annotations

import curses
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

PAGE_SIZE = 20
REFRESH_SECONDS = 5
TABS = ("Actors", "Actions", "Work", "History")


class MonitorError(RuntimeError):
    """The selected operator query did not return a valid response."""


def _safe(value) -> str:
    return "".join(character if character.isprintable() else " " for character in str(value))


def _data(envelope: dict) -> dict:
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        error = envelope.get("error", {}) if isinstance(envelope, dict) else {}
        raise MonitorError(f"{error.get('code', 'SERVICE_UNAVAILABLE')}: {error.get('message', 'Operator service is unavailable')}")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise MonitorError("Operator service returned an invalid projection")
    return data


def snapshot(client, *, cursor=None, view="current", filters=None, section="all") -> dict:
    arguments = {"limit": PAGE_SIZE, "view": view, "section": section}
    if cursor is not None:
        arguments["cursor"] = cursor
    if filters:
        arguments["filters"] = filters
    return _data(client.call("operator.snapshot", arguments))


def history(client, *, cursor=None, query="", filters=None) -> dict:
    arguments = {"limit": PAGE_SIZE}
    if cursor is not None:
        arguments["cursor"] = cursor
    if query:
        arguments["query"] = query
    if filters:
        arguments["filters"] = filters
    return _data(client.call("operator.history", arguments))


def detail(client, row: dict, *, offset: int = 0) -> dict:
    if row.get("kind") == "message":
        return _data(client.call("message.get", {"id": row["id"], "offset": offset, "limit": 65536}))
    sequence = row.get("item", {}).get("sequence")
    if sequence is None:
        return row.get("item", {})
    return _data(client.call("operator.history", {"sequence": sequence}))


def detail_text(data: dict) -> str:
    if "body" in data:
        metadata = {key: value for key, value in data.items() if key != "body"}
        return json.dumps(metadata, ensure_ascii=False, indent=2) + "\n\n" + data["body"] + "\n\nEsc back  n/b body chunks"
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n\nEsc back"


def _items(value) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return _items(value.get("items", []))
    return []


def rows(data: dict, tab: int, *, now_us: int | None = None) -> list[dict]:
    now_us = int(time.time() * 1_000_000) if now_us is None else now_us
    result = []
    if tab == 0:
        for actor in _items(data.get("actors", [])):
            observed_us = actor.get("observed_us")
            age = (now_us - observed_us) // 1_000_000 if type(observed_us) is int and 0 <= observed_us <= now_us else None
            presence = actor.get("observed_state", "unknown") if age is not None else "unknown"
            result.append({"kind": "actor", "id": actor["id"], "item": actor,
                           "text": f"{actor.get('label', actor['id'])}  {actor.get('task', '')}  reported={actor.get('reported_state', 'unknown')} observed={presence} age={str(age) + 's' if age is not None else 'unknown'}"})
    elif tab == 1:
        for item in _items(data.get("actions", [])):
            result.append({"kind": item.get("kind", "action"), "id": item["id"], "item": item,
                           "text": f"{item.get('kind', 'action')} {item.get('summary', item.get('subject', ''))}  {item.get('next_action', '')}"})
    elif tab == 2:
        for category in ("activities", "readiness", "jobs", "failures"):
            for item in _items(data.get(category, [])):
                result.append({"kind": category.rstrip("s"), "id": item["id"], "item": item,
                               "text": f"{category}: {item.get('state', item.get('status', ''))} {item.get('task', item.get('artifact', ''))} {item.get('note', item.get('error', ''))}"})
    else:
        for item in _items(data.get("history", data.get("items", []))):
            record_kind = "message" if item.get("domain") == "messages" else item.get("kind", "history")
            metadata = item.get("metadata", {})
            summary = item.get("summary") or (metadata.get("subject") or metadata.get("note") or metadata.get("task") or item.get("record_id", "") if isinstance(metadata, dict) else "")
            result.append({"kind": record_kind, "id": item.get("record_id", item.get("id", item.get("sequence"))), "item": item,
                           "text": f"{item.get('sequence', '')} {item.get('domain', '')}/{item.get('kind', '')} {summary}"})
    return result


@dataclass
class View:
    tab: int = 0
    selected: int = 0
    cursor: object = None
    previous: list = field(default_factory=list)
    next_cursor: object = None
    actor_view: str = "current"
    query: str = ""
    filters: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)
    error: str = ""
    detail_text: str | None = None
    detail_scroll: int = 0
    detail_row: dict | None = None
    detail_offset: int = 0
    detail_next: int | None = None
    detail_previous: list[int] = field(default_factory=list)

    def reset_page(self) -> None:
        self.cursor, self.next_cursor = None, None
        self.previous.clear()
        self.selected = 0
        self.data = {}


def _edit(screen, prompt: str) -> str | None:
    text = ""
    screen.timeout(-1)
    try:
        while True:
            height, width = screen.getmaxyx()
            screen.move(max(0, height - 1), 0)
            screen.clrtoeol()
            screen.addnstr(max(0, height - 1), 0, _safe(prompt + text), max(0, width - 1))
            screen.refresh()
            key = screen.get_wch()
            if key in ("\n", "\r", curses.KEY_ENTER):
                return text
            if key == "\x1b":
                return None
            if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                text = text[:-1]
            elif isinstance(key, str) and key.isprintable() and len(text) < 256:
                text += key
    finally:
        screen.timeout(100)


def _screen(screen, client) -> None:
    view = View()
    screen.keypad(True)
    screen.timeout(100)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    # One bounded background query keeps navigation/quit responsive on socket timeouts.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agentcoord-monitor")
    pending = None
    pending_view = None
    detail_pending = None
    detail_request = None
    queried = 0.0
    refresh = True
    try:
        while True:
            if pending is not None and pending.done():
                try:
                    result = pending.result()
                    if pending_view == (view.tab, view.cursor, view.actor_view, view.query, dict(view.filters)):
                        view.data = result
                        view.next_cursor = view.data.get("next_cursor", view.data.get("cursor"))
                        view.error = ""
                except Exception as error:  # noqa: BLE001 -- keep navigation usable after any query worker failure
                    view.error = _safe(str(error))
                pending = None
            if detail_pending is not None and detail_pending.done():
                try:
                    value = detail_pending.result()
                    if view.detail_row is not None and detail_request == (view.detail_row["id"], view.detail_offset):
                        view.detail_text = detail_text(value)
                        view.detail_next = value.get("next_offset")
                except Exception as error:  # noqa: BLE001 -- detail worker failures remain visible without closing the UI
                    if view.detail_text is not None:
                        view.detail_text = f"Detail unavailable: {_safe(error)}"
                detail_pending = None
            if pending is None and view.detail_text is None and (refresh or time.monotonic() - queried >= REFRESH_SECONDS):
                operation = history if view.tab == 3 else snapshot
                options = {"cursor": view.cursor, "filters": dict(view.filters)}
                options.update(query=view.query) if view.tab == 3 else options.update(view=view.actor_view, section=("actors", "actions", "work")[view.tab])
                pending = executor.submit(operation, client, **options)
                pending_view = (view.tab, view.cursor, view.actor_view, view.query, dict(view.filters))
                queried, refresh = time.monotonic(), False
            current = rows(view.data, view.tab)
            view.selected = min(view.selected, max(0, len(current) - 1))
            screen.erase()
            height, width = screen.getmaxyx()
            lines = []
            if view.detail_text is not None:
                lines = view.detail_text.splitlines()[view.detail_scroll:]
            else:
                lines = [f"agentcoord  {' | '.join(str(i + 1) + ' ' + title for i, title in enumerate(TABS))}  view={view.actor_view}",
                         "q quit  r refresh  n/b pages  v actor history  / search  a/t/p filters  x clear  Enter detail"]
                if view.error:
                    lines.append(f"Unavailable: {view.error}")
                if pending is not None:
                    lines.append("Loading bounded service query…")
                if not current and pending is None:
                    lines.append("No records match this view.")
                visible = max(1, height - len(lines) - 1)
                start = max(0, view.selected - visible + 1)
                lines.extend(("> " if i == view.selected else "  ") + row["text"] for i, row in enumerate(current[start:], start=start))
            for index, line in enumerate(lines[:max(0, height - 1)]):
                try:
                    screen.addnstr(index, 0, _safe(line), max(0, width - 1))
                except curses.error:
                    pass
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key == "q":
                return
            if view.detail_text is not None:
                if key in ("\x1b", curses.KEY_BACKSPACE, "\x7f"):
                    view.detail_text, view.detail_scroll = None, 0
                    view.detail_row = None
                    view.detail_previous.clear()
                elif key == curses.KEY_DOWN:
                    view.detail_scroll += 1
                elif key == curses.KEY_UP:
                    view.detail_scroll = max(0, view.detail_scroll - 1)
                elif key in ("n", "b") and detail_pending is None and view.detail_row is not None:
                    if key == "n" and view.detail_next is not None:
                        view.detail_previous.append(view.detail_offset)
                        view.detail_offset = view.detail_next
                    elif key == "b" and view.detail_previous:
                        view.detail_offset = view.detail_previous.pop()
                    else:
                        continue
                    view.detail_scroll = 0
                    detail_request = (view.detail_row["id"], view.detail_offset)
                    detail_pending = executor.submit(detail, client, view.detail_row, offset=view.detail_offset)
                continue
            if key in ("1", "2", "3", "4"):
                view.tab = int(key) - 1
                view.reset_page()
                refresh = True
            elif key == "r":
                refresh = True
            elif key == "n" and view.next_cursor is not None:
                view.previous.append(view.cursor)
                view.cursor, view.selected = view.next_cursor, 0
                refresh = True
            elif key == "b" and view.previous:
                view.cursor, view.selected = view.previous.pop(), 0
                refresh = True
            elif key == "v":
                view.actor_view = "all" if view.actor_view == "current" else "current"
                view.reset_page()
                refresh = True
            elif key in ("/", "a", "t", "p"):
                value = _edit(screen, {"/": "History search: ", "a": "Actor ID: ", "t": "Task: ", "p": "Path: "}[key])
                if value is not None:
                    if key == "/":
                        view.query, view.tab = value, 3
                    else:
                        name = {"a": "actor_id", "t": "task", "p": "path"}[key]
                        view.filters[name] = value
                    view.reset_page()
                    refresh = True
            elif key == "x":
                view.query, view.filters = "", {}
                view.reset_page()
                refresh = True
            elif key == curses.KEY_DOWN:
                view.selected = min(view.selected + 1, max(0, len(current) - 1))
            elif key == curses.KEY_UP:
                view.selected = max(0, view.selected - 1)
            elif key in ("\n", "\r", curses.KEY_ENTER) and current and detail_pending is None:
                selected = current[view.selected]
                view.detail_row, view.detail_offset, view.detail_next = selected, 0, None
                view.detail_text = json.dumps(selected["item"], ensure_ascii=False, indent=2) + "\n\nLoading full record…"
                detail_request = (selected["id"], 0)
                detail_pending = executor.submit(detail, client, selected)
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()
        executor.shutdown(wait=False, cancel_futures=True)


def run(client, *, screen_runner=curses.wrapper) -> int:
    try:
        screen_runner(_screen, client)
        return 0
    except (curses.error, OSError, ValueError) as error:
        print(f"Cannot open coordination monitor: {_safe(error)}", file=sys.stderr)
        return 2
