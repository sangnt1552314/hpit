from collections import Counter
from datetime import date
from typing import Dict, List, Optional

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Static

from hpit.core import api, config
from hpit.core.models import Job, MemberUsage, Project
from hpit.tui.pages.base import Page
from hpit.core.accounting import month_bounds, shift_month
from hpit.tui.screens.calendar import CalendarScreen
from hpit.tui.screens.dialogs import PasswordScreen
from hpit.tui.theme import MUTED
from hpit.tui.util import call_backend, usage_bar
from hpit.tui.widgets.tables import Table


class ProjectsPage(Page):
    """Project GPU-hours from amgr (what `hpc project` shows)."""

    TITLE = "Projects"

    BINDINGS = [
        Binding("left_square_bracket", "shift_period(-1)", "Prev month", key_display="["),
        Binding("right_square_bracket", "shift_period(1)", "Next month", key_display="]"),
        Binding("c", "calendar", "Pick dates"),
        Binding("l", "login", "Log in"),
    ]

    def __init__(self):
        super().__init__(id="projects")
        self.projects: List[Project] = []
        self.my_jobs: Dict[str, int] = {}
        self.needs_login = False
        self.selected: Optional[Project] = None
        # Usage period; defaults to the current month so far.
        self.period_start, self.period_end = month_bounds(date.today())

    def compose(self) -> ComposeResult:
        yield Static("Loading projects…", id="projects-message", classes="message")
        yield Table(id="projects-table")
        yield Static("", id="usage-title", classes="section-title")
        yield DataTable(id="usage-table", show_cursor=False)

    def on_mount(self) -> None:
        self.query_one("#projects-table", Table).add_columns(
            "Project", "GPU-h left", "Total", "Used", "Reserved", "Ends", "Status", "My jobs",
        )
        usage = self.query_one("#usage-table", DataTable)
        # "here" = charged to this project; "all" = any project.
        usage.add_columns(
            "Member", "Running here", "GPUs", "Reserved GPU-h",
            "Running all", "Queued all", "GPU-h month", "Jobs month",
        )
        usage.can_focus = False

    def load(self) -> None:
        self.query_one("#projects-message", Static).update("Loading projects…")
        self.load_projects()

    # amgr logs in automatically from .env if needed (once per session).
    @work(thread=True, exclusive=True, group="projects")
    def load_projects(self) -> None:
        projects, error = call_backend(api.get_projects)
        needs_login = projects is None and not call_backend(api.is_logged_in)[0]
        self.app.call_from_thread(self._show_projects, projects, error, needs_login)

    def _show_projects(self, projects: Optional[List[Project]], error: Optional[str], needs_login: bool) -> None:
        self.needs_login = needs_login
        self.refresh_bindings()
        message = self.query_one("#projects-message", Static)

        if projects is None:
            hint = "\n\nPress l to log in now." if needs_login else ""
            message.update(Text(f"{error}{hint}", style="#E5C07B" if needs_login else "#E06C75"))
            self.projects = []
        else:
            message.update(Text(
                "1 GPU-hour = 100 SU. Use a project with  #PBS -P <project>\n"
                "Member jobs come from the site's qstat snapshot (every few minutes); "
                "GPUs are estimated as 12 CPUs per GPU.\n\"here\" = charged to this project; \"all\" = any project "
                "(queued jobs aren't tied to a project until they start).",
                style=MUTED,
            ))
            # Current projects first; ended ones (still listed by amgr) last.
            self.projects = sorted(projects, key=lambda p: (p.ended or not p.active, p.name))
        self._render_projects()

    def _render_projects(self) -> None:
        table = self.query_one("#projects-table", Table)
        selected = table.selected_key()
        table.clear()
        for p in self.projects:
            if p.ended:
                status = Text("● ended", style="#E06C75")
            elif p.active:
                status = Text("● active", style="#98C379")
            else:
                status = Text("○ inactive", style=MUTED)
            used = usage_bar(p.used_fraction, 12)
            used.append(f" {p.used_fraction:.0%}", style=MUTED)
            table.add_row(
                Text(p.name, style="bold"),
                Text(f"{p.gpu_hours_left:,.1f}", justify="right"),
                Text(f"{p.gpu_hours_total:,.0f}", justify="right", style=MUTED),
                used,
                Text(f"{p.gpu_hours_reserved:,.1f}", justify="right"),
                p.end_date,
                status,
                Text(str(self.my_jobs.get(p.name, 0) or ""), justify="right"),
                key=p.name,
            )
        if selected in {p.name for p in self.projects}:
            table.move_cursor(row=table.get_row_index(selected))

    def show_jobs(self, jobs: List[Job], error: Optional[str]) -> None:
        # Count your active jobs charging each project.
        self.my_jobs = dict(Counter(j.project for j in jobs if j.project and j.state != "F"))
        if self.projects:
            self._render_projects()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "projects-table" or event.row_key is None:
            return
        project = next((p for p in self.projects if p.name == event.row_key.value), None)
        if project:
            self.selected = project
            self._reload_usage()

    def _reload_usage(self) -> None:
        if self.selected is None:
            return
        start, end = self.period_start.isoformat(), self.period_end.isoformat()
        whole_month = (self.period_start, self.period_end) == month_bounds(self.period_start)
        label = self.period_start.strftime("%B %Y") if whole_month else f"{start} → {end}"
        self.query_one("#usage-title", Static).update(
            f"Members · {self.selected.name} · usage {label}"
        )
        self.load_usage(self.selected, start, end)

    def action_shift_period(self, months: int) -> None:
        target = shift_month(self.period_start, months)
        if target > date.today():
            self.app.bell()  # No usage in the future.
            return
        self.period_start, self.period_end = month_bounds(target)
        self._reload_usage()

    def action_calendar(self) -> None:
        def picked(period) -> None:
            if period:
                self.period_start, self.period_end = period
                self._reload_usage()

        self.app.push_screen(CalendarScreen(self.period_start, self.period_end), picked)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()  # Usage already follows the highlighted row.

    @work(thread=True, exclusive=True, group="project-usage")
    def load_usage(self, project: Project, start: str, end: str) -> None:
        usage, error = call_backend(api.get_project_usage, project.name, start, end, project.users)
        self.app.call_from_thread(self._show_usage, usage, error)

    def _show_usage(self, usage: Optional[List[MemberUsage]], error: Optional[str]) -> None:
        table = self.query_one("#usage-table", DataTable)
        table.clear()
        if usage is None:
            self.query_one("#usage-title", Static).update(Text(error or "", style="#E06C75"))
            return
        def count(n: int, style: str = "") -> Text:
            return Text(str(n) if n else "·", justify="right", style=style if n else MUTED)

        for m in usage:
            style = "bold #61AFEF" if m.user == config.USER else ""
            hours_style = "#98C379" if m.gpu_hours < 0 else ""
            table.add_row(
                Text(m.user, style=style),
                count(m.running_here, "bold #98C379"),
                count(m.gpus_here, "bold"),
                Text(f"{m.reserved_gpu_hours:,.0f}" if m.reserved_gpu_hours else "·",
                     justify="right", style="" if m.reserved_gpu_hours else MUTED),
                count(m.running),
                count(m.queued, "#E5C07B"),
                Text(f"{m.gpu_hours:,.1f}", justify="right", style=hours_style),
                Text(str(m.jobs), justify="right"),
            )

    def check_action(self, action: str, parameters) -> Optional[bool]:
        if action == "login":
            return self.needs_login
        return True

    def action_login(self) -> None:
        def submitted(password: Optional[str]) -> None:
            if password:
                self.query_one("#projects-message", Static).update("Logging in…")
                self.run_login(password)

        self.app.push_screen(PasswordScreen(), submitted)

    @work(thread=True, exclusive=True, group="projects")
    def run_login(self, password: str) -> None:
        _, error = call_backend(api.login, password)
        if error:
            self.app.call_from_thread(self._show_projects, None, error, True)
        else:
            self.app.call_from_thread(self.app.notify, "Logged in to amgr")
            self.app.call_from_thread(self.load)

    def main_widget(self) -> Table:
        return self.query_one("#projects-table", Table)
