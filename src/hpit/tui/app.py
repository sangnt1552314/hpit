from datetime import datetime
from typing import List, Optional

from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import ContentSwitcher, Footer, ListView, Static

from hpit import __version__
from hpit.core import api, config
from hpit.core.command import kill_running_commands
from hpit.core.models import Job
from hpit.tui.pages.base import Page
from hpit.tui.pages.cleanup import CleanupPage
from hpit.tui.pages.cluster import ClusterPage
from hpit.tui.pages.files import FilesPage
from hpit.tui.pages.jobs import JobsPage
from hpit.tui.pages.logs import LogsPage
from hpit.tui.pages.overview import OverviewPage
from hpit.tui.pages.projects import ProjectsPage
from hpit.tui.pages.storage import StoragePage
from hpit.tui.pages.tools import ToolsPage
from hpit.tui.screens.dialogs import ConfirmCancelScreen, HelpScreen
from hpit.tui.screens.job_details import JobDetailsScreen
from hpit.tui.screens.log_view import LogScreen
from hpit.tui.theme import HPIT_THEME
from hpit.tui.util import call_backend
from hpit.tui.widgets.sidebar import Sidebar


PAGES = [
    ("overview", "Overview", OverviewPage),
    ("jobs", "Jobs", JobsPage),
    ("projects", "Projects", ProjectsPage),
    ("cluster", "Cluster", ClusterPage),
    ("storage", "Storage", StoragePage),
    ("cleanup", "Cleanup", CleanupPage),
    ("logs", "Logs", LogsPage),
    ("files", "Files", FilesPage),
    ("tools", "Tools", ToolsPage),
]


class HPITApp(App):
    TITLE = "HPIT"
    CSS_PATH = "styles.tcss"
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("escape", "back", "Menu"),
    ] + [
        Binding(str(i), f"show_page('{page_id}')", show=False)
        for i, (page_id, _, _) in enumerate(PAGES, start=1)
    ]

    def __init__(self):
        super().__init__()
        self.include_finished = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static(f"HPIT [#7F848E]v{__version__}[/]", id="brand")
            yield Static("", id="crumb")
            yield Static("○ Loading…", id="connection")
            yield Static("", id="clock")

        with Horizontal(id="body"):
            yield Sidebar([(page_id, title) for page_id, title, _ in PAGES])
            with ContentSwitcher(initial="overview", id="pages"):
                for _, _, page_class in PAGES:
                    yield page_class()

        # Footer lists bindings of the focused widget and its parents,
        # so it only ever shows keys that work in the current context.
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(HPIT_THEME)
        self.theme = "hpit"

        self.main_screen.query_one(Sidebar).focus()
        self._update_crumb("overview")
        self.update_clock()
        self.set_interval(30, self.update_clock)

        self.load_jobs()
        if config.REFRESH_INTERVAL > 0:
            self.set_interval(config.REFRESH_INTERVAL, self.load_jobs)

    def on_unmount(self) -> None:
        # Don't leave a slow du/find running after quitting.
        kill_running_commands()

    async def on_event(self, event: events.Event) -> None:
        # Focus can be lost (e.g. terminal focus-out with no matching
        # focus-in); hand it back to the sidebar before the input is routed.
        if (
            isinstance(event, (events.Key, events.MouseDown))
            and len(self.screen_stack) == 1
            and self.screen.focused is None
        ):
            self.main_screen.query_one(Sidebar).focus()
        await super().on_event(event)

    @property
    def main_screen(self):
        # Pages live on the bottom screen; details/log screens push on top,
        # and background refreshes must still reach the pages underneath.
        return self.screen_stack[0]

    # Navigation

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None and event.item.id:
            self.show_page(event.item.id.replace("nav-", ""))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        widget = self.current_page().main_widget()
        if widget is not None:
            widget.focus()

    def show_page(self, page_id: str) -> None:
        self.main_screen.query_one("#pages", ContentSwitcher).current = page_id
        self.main_screen.query_one(Sidebar).mark_active(page_id)
        self._update_crumb(page_id)

    def action_show_page(self, page_id: str) -> None:
        # Moving the sidebar cursor triggers show_page via Highlighted.
        sidebar = self.main_screen.query_one(Sidebar)
        sidebar.index = [p[0] for p in PAGES].index(page_id)
        sidebar.focus()

    def current_page(self) -> Page:
        switcher = self.main_screen.query_one("#pages", ContentSwitcher)
        return switcher.get_child_by_id(switcher.current or "overview", Page)

    def check_action(self, action: str, parameters) -> Optional[bool]:
        if action == "back":
            # Hide "Back" when already on the sidebar.
            return not self.main_screen.query_one(Sidebar).has_focus
        return True

    def action_back(self) -> None:
        self.main_screen.query_one(Sidebar).focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_refresh(self) -> None:
        self.load_jobs()
        self.current_page().refresh_page()

    def _update_crumb(self, page_id: str) -> None:
        title = {p[0]: p[1] for p in PAGES}[page_id]
        mock = "  [#E5C07B]MOCK[/]" if config.MOCK else ""
        self.main_screen.query_one("#crumb", Static).update(
            f"[#5C6370]/[/] {config.USER}@{config.CLUSTER_NAME} [#5C6370]/[/] {title}{mock}"
        )

    def update_clock(self) -> None:
        self.main_screen.query_one("#clock", Static).update(datetime.now().strftime("%H:%M"))

    # Jobs (fetched once here and shared with every page)

    def set_include_finished(self, value: bool) -> None:
        self.include_finished = value
        self.notify("Showing finished jobs too" if value else "Showing active jobs")
        self.load_jobs()

    # qstat runs in a thread so the UI stays responsive. exclusive=True
    # cancels a still-running refresh instead of stacking them up.
    @work(thread=True, exclusive=True, group="jobs")
    def load_jobs(self) -> None:
        self.call_from_thread(self.set_connection, "○ Refreshing…", "")
        jobs, error = call_backend(api.get_jobs, self.include_finished)
        self.call_from_thread(self._distribute_jobs, jobs or [], error)

    def _distribute_jobs(self, jobs: List[Job], error: Optional[str]) -> None:
        for page in self.main_screen.query(Page):
            page.show_jobs(jobs, error)
        if error:
            self.set_connection("● PBS error", "error")
        else:
            self.set_connection("● PBS connected", "ok")

    def set_connection(self, label: str, status: str) -> None:
        connection = self.main_screen.query_one("#connection", Static)
        connection.update(label)
        connection.set_classes(status)

    # Actions shared by pages and screens

    def open_job_details(self, job_id: str) -> None:
        self.push_screen(JobDetailsScreen(job_id))

    def open_job_logs(self, job_id: str) -> None:
        self.push_screen(LogScreen(job_id))

    def request_cancel(self, job: Job) -> None:
        if job.state == "F":
            self.notify("That job has already finished.", severity="warning")
            return

        def confirmed(result: bool) -> None:
            if result:
                self.cancel_job(job)

        self.push_screen(ConfirmCancelScreen(job), confirmed)

    @work(thread=True, group="cancel")
    def cancel_job(self, job: Job) -> None:
        _, error = call_backend(api.cancel_job, job.job_id)
        if error:
            self.call_from_thread(self.notify, f"qdel failed: {error}", severity="error", timeout=10)
        else:
            self.call_from_thread(self.notify, f"Cancelled {job.short_id} ({job.name})")
            self.call_from_thread(self.load_jobs)
