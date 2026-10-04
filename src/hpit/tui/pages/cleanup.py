from typing import Dict, List, Optional, Set

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from hpit.core import api
from hpit.core.models import CleanupCandidate, CleanupScan
from hpit.core.units import human_bytes
from hpit.tui.pages.base import Page
from hpit.tui.screens.dialogs import CommandsScreen, DeleteConfirmScreen
from hpit.tui.theme import MUTED
from hpit.tui.util import call_backend, format_age, truncate
from hpit.tui.widgets.tables import Table as BaseTable


class Table(BaseTable):
    BINDINGS = [Binding("enter", "select_cursor", "Select", show=False)]

SAFETY_STYLE = {"safe": ("● safe", "#98C379"), "review": ("◆ review", "#E5C07B")}


class CleanupPage(Page):
    """Suggest what could be cleaned. Nothing is deleted automatically:
    pick items, then copy the `rm` commands to run yourself, or delete
    them for good after a typed confirmation."""

    TITLE = "Cleanup"

    BINDINGS = [
        Binding("space", "toggle", "Select"),
        Binding("a", "select_safe", "Select all safe"),
        Binding("n", "clear", "Clear"),
        Binding("c", "copy_commands", "Copy commands"),
        Binding("D", "delete", "Delete forever"),
        Binding("i", "ignore", "Ignore"),
        Binding("s", "scan", "Scan"),
    ]

    def __init__(self):
        super().__init__(id="cleanup")
        self.result: Optional[CleanupScan] = None
        self.by_path: Dict[str, CleanupCandidate] = {}
        self.selected: Set[str] = set()
        self.scanning = False
        self.deleting = False

    def compose(self) -> ComposeResult:
        yield Static("", id="cleanup-status", classes="message")
        yield Table(id="cleanup-table")
        yield Static("", id="cleanup-summary", classes="message")

    def on_mount(self) -> None:
        table = self.query_one("#cleanup-table", Table)
        self.select_column = table.add_column(" ", key="sel")
        table.add_columns("Category", "Path", "Size", "Changed", "Safety", "Why")

    def load(self) -> None:
        if self.result is None and not self.scanning:
            cached, _ = call_backend(api.load_cached_cleanup)
            if cached:
                self._show_result(cached)
            else:
                self._set_status(Text(
                    "Not scanned yet. Press s to look for things you could clean up "
                    "(home and scratch; takes a few minutes the first time).", style=MUTED,
                ))
        self._update_summary()

    def refresh_page(self) -> None:
        # `r` re-reads the saved scan; `s` runs a new one (it is slow).
        self.load()

    # Scanning

    def action_scan(self) -> None:
        if self.scanning:
            self.notify("A scan is already running.")
            return
        self.scanning = True
        self._set_status(Text("Starting scan…", style="#E5C07B"))
        self.run_scan()

    @work(thread=True, exclusive=True, group="cleanup-scan")
    def run_scan(self) -> None:
        def progress(message: str) -> None:
            self.app.call_from_thread(self._set_status, Text(message, style="#E5C07B"))

        result, error = call_backend(api.scan_cleanup, progress)
        self.app.call_from_thread(self._scan_finished, result, error)

    def _scan_finished(self, result: Optional[CleanupScan], error: Optional[str]) -> None:
        self.scanning = False
        if result is None:
            self._set_status(Text(error or "Scan failed.", style="#E06C75"))
        else:
            self.selected &= {c.path for c in result.candidates}
            self._show_result(result)

    def _show_result(self, result: CleanupScan) -> None:
        self.result = result
        self.by_path = {c.path: c for c in result.candidates}
        table = self.query_one("#cleanup-table", Table)
        cursor = table.selected_key()
        table.clear()
        for c in result.candidates:
            label, color = SAFETY_STYLE.get(c.safety, (c.safety, ""))
            why = c.reason
            if c.in_use_by:
                label, color = "▲ in use", "#E06C75"
                why = f"under a running job's folder · {why}"
            table.add_row(
                self._mark(c.path),
                Text(c.category, style=MUTED),
                Text(truncate(c.display, 64), style="#61AFEF"),
                Text(human_bytes(c.size_bytes) if c.size_bytes is not None else "?", justify="right"),
                Text(format_age(c.modified) if c.modified else "--", style=MUTED),
                Text(label, style=color),
                Text(truncate(why, 70), style=MUTED),
                key=c.path,
            )
        if cursor in self.by_path:
            table.move_cursor(row=table.get_row_index(cursor))

        roots = " and ".join(result.roots)
        self._set_status(Text(
            f"{len(result.candidates)} suggestions in {roots} · scanned {format_age(result.scanned_at)}. "
            "Select items, then c to copy the rm commands, or D to delete them forever.",
            style=MUTED,
        ))
        self._update_summary()

    # Selection

    def _mark(self, path: str) -> Text:
        return Text("■", style="bold #61AFEF") if path in self.selected else Text("□", style=MUTED)

    def _refresh_marks(self, paths) -> None:
        table = self.query_one("#cleanup-table", Table)
        for path in paths:
            if path in table.rows:
                table.update_cell(path, self.select_column, self._mark(path))
        self._update_summary()

    def on_data_table_row_selected(self, event: Table.RowSelected) -> None:
        event.stop()
        self.action_toggle()  # Enter selects, same as space.

    def action_toggle(self) -> None:
        path = self.main_widget().selected_key()
        if not path:
            return
        self.selected ^= {path}
        self._refresh_marks([path])
        self.main_widget().action_cursor_down()

    def action_select_safe(self) -> None:
        safe = {c.path for c in self.by_path.values() if c.safety == "safe" and not c.in_use_by}
        self.selected |= safe
        self._refresh_marks(safe)

    def action_clear(self) -> None:
        cleared, self.selected = self.selected, set()
        self._refresh_marks(cleared)

    def _update_summary(self) -> None:
        chosen = [self.by_path[p] for p in self.selected if p in self.by_path]
        total = sum(c.size_bytes or 0 for c in chosen)
        text = Text()
        if chosen:
            text.append(f"Selected {len(chosen)} item(s), about {human_bytes(total)}", style="bold #E6EDF3")
            in_use = sum(1 for c in chosen if c.in_use_by)
            if in_use:
                text.append(f"  ·  {in_use} under a running job's folder", style="#E06C75")
            text.append("  ·  c: copy rm commands   D: delete forever", style=MUTED)
        elif self.result:
            reclaim = sum(c.size_bytes or 0 for c in self.result.candidates)
            safe = sum(c.size_bytes or 0 for c in self.result.candidates if c.safety == "safe" and not c.in_use_by)
            text.append(f"Up to {human_bytes(reclaim)} could be freed; {human_bytes(safe)} of it is safe to clean.", style=MUTED)
        self.query_one("#cleanup-summary", Static).update(text)

    # Actions

    def _chosen(self) -> List[CleanupCandidate]:
        chosen = [c for c in self.result.candidates if c.path in self.selected] if self.result else []
        if not chosen:
            self.notify("Select items first (space), or press a to select all safe ones.", severity="warning")
        return chosen

    def action_copy_commands(self) -> None:
        chosen = self._chosen()
        if chosen:
            commands, error = call_backend(api.cleanup_commands, chosen)
            if error:
                self.notify(error, severity="error")
                return
            total = human_bytes(sum(c.size_bytes or 0 for c in chosen))
            self.app.push_screen(CommandsScreen(f"rm commands · {len(chosen)} item(s) · {total}", commands))

    def action_delete(self) -> None:
        chosen = self._chosen()
        if not chosen or self.deleting:
            return
        total = sum(c.size_bytes or 0 for c in chosen)
        summary = Text()
        summary.append(f"{len(chosen)} item(s), about {human_bytes(total)}:\n", style="bold")
        for c in chosen[:8]:
            summary.append(f"  {human_bytes(c.size_bytes):>9}  ", style=MUTED)
            summary.append(truncate(c.display, 52) + "\n", style="#E06C75" if c.in_use_by else "#61AFEF")
        if len(chosen) > 8:
            summary.append(f"  … and {len(chosen) - 8} more\n", style=MUTED)
        in_use = sum(1 for c in chosen if c.in_use_by)
        if in_use:
            summary.append(f"{in_use} item(s) are under a running job's folder and will be skipped.\n", style="#E06C75")

        def confirmed(ok: bool) -> None:
            if ok:
                self.deleting = True
                self._set_status(Text("Deleting…", style="#E06C75"))
                self.run_delete(chosen)

        self.app.push_screen(DeleteConfirmScreen(summary), confirmed)

    @work(thread=True, exclusive=True, group="cleanup-delete")
    def run_delete(self, chosen: List[CleanupCandidate]) -> None:
        def progress(message: str) -> None:
            self.app.call_from_thread(self._set_status, Text(message, style="#E06C75"))

        roots = self.result.roots if self.result else []
        results, error = call_backend(api.delete_cleanup, chosen, roots, progress)
        self.app.call_from_thread(self._delete_finished, results, error)

    def _delete_finished(self, results, error: Optional[str]) -> None:
        self.deleting = False
        if results is None:
            self._set_status(Text(error or "Delete failed.", style="#E06C75"))
            return

        deleted = [r for r in results if r.ok]
        skipped = [r for r in results if not r.ok]
        freed = sum(r.size_bytes for r in deleted)
        gone = {r.path for r in deleted}
        if self.result:
            self.result.candidates = [
                c for c in self.result.candidates
                if c.path not in gone and not (c.members and all(m in gone for m in c.members))
            ]
            self.selected = {p for p in self.selected if p in {c.path for c in self.result.candidates}}
            self._show_result(self.result)

        status = Text(f"Deleted {len(deleted)} path(s), about {human_bytes(freed)} freed.", style="bold #98C379")
        for r in skipped[:5]:
            status.append(f"\n{r.message}: {r.path}", style="#E5C07B")
        if len(skipped) > 5:
            status.append(f"\n… {len(skipped) - 5} more skipped", style="#E5C07B")
        status.append("\nHistory: ~/.cache/hpit/cleanup-history.log", style=MUTED)
        self._set_status(status)
        self.notify(f"Deleted {len(deleted)}, skipped {len(skipped)}", title="Cleanup")

    def action_ignore(self) -> None:
        path = self.main_widget().selected_key()
        if not path:
            return
        _, error = call_backend(api.ignore, path)
        if error:
            self.notify(error, severity="error")
            return
        self.selected.discard(path)
        if self.result:
            self.result.candidates = [c for c in self.result.candidates if c.path != path]
            self._show_result(self.result)
        self.notify(f"Won't suggest {path} again (~/.config/hpit/cleanup-ignore)")

    def check_action(self, action: str, parameters) -> Optional[bool]:
        if action in ("toggle", "select_safe", "clear", "copy_commands", "delete", "ignore"):
            return bool(self.result and self.result.candidates)
        return True

    def _set_status(self, text) -> None:
        self.query_one("#cleanup-status", Static).update(text)

    def main_widget(self) -> Table:
        return self.query_one("#cleanup-table", Table)
