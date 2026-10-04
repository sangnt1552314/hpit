from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from hpit.core.models import Job
from hpit.tui.theme import MUTED
from hpit.tui.util import key_values


class ConfirmCancelScreen(ModalScreen):
    """Ask before `qdel`. Returns True only if "Cancel job" is pressed."""

    BINDINGS = [Binding("escape", "dismiss(False)", "Keep job")]

    def __init__(self, job: Job):
        super().__init__()
        self.job = job

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="panel danger") as dialog:
            dialog.border_title = "Cancel job?"
            yield Static(key_values([
                ("Job ID", Text(self.job.job_id, style="bold")),
                ("Name", Text(self.job.name, style="bold")),
                ("Status", self.job.state_name),
            ]))
            yield Static(
                Text("This runs qdel and cannot be undone.", style=MUTED),
                classes="message",
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("Keep job", id="keep", variant="default")
                yield Button("Cancel job", id="confirm", variant="error")

    def on_mount(self) -> None:
        # Safe default: Enter right away keeps the job.
        self.query_one("#keep", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


HELP_TEXT = [
    ("Anywhere", [
        ("↑ ↓", "Move"),
        ("Enter", "Open"),
        ("Esc", "Back to the menu (sidebar)"),
        ("1 – 9", "Jump to a page"),
        ("r", "Refresh"),
        ("?", "This help"),
        ("q", "Quit"),
    ]),
    ("Jobs", [
        ("/", "Filter jobs"),
        ("h", "Show / hide finished jobs"),
        ("l", "Logs of selected job"),
        ("k", "Cancel selected job (asks first)"),
    ]),
    ("Projects", [
        ("↑ ↓", "Members of the selected project"),
        ("[ ]", "Previous / next month"),
        ("c", "Pick dates on a calendar"),
        ("L", "Log in to amgr"),
    ]),
    ("Cluster", [("f", "Only nodes with free GPUs")]),
    ("Cleanup", [
        ("s", "Scan for things to clean"),
        ("space", "Select / unselect"),
        ("a / n", "Select all safe / clear"),
        ("c", "Copy rm commands (paste in your terminal)"),
        ("D", "Delete forever (type 'delete' to confirm)"),
        ("i", "Never suggest this item again"),
    ]),
    ("Logs", [("o / e", "stdout / stderr"), ("f", "Follow (auto refresh)")]),
    ("Storage", [("→ / ⏎", "Open location / folder"), ("← / ⌫", "Back up"), ("s", "Scan folder sizes (du)")]),
    ("Files", [("→ / ⏎", "Open folder"), ("← / ⌫", "Back up"), ("b", "Find files over 1 GB"), ("s / ~", "Scratch / home")]),
]


class HelpScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        text = Text()
        for i, (section, keys) in enumerate(HELP_TEXT):
            if i:
                text.append("\n\n")
            text.append(section, style="bold #61AFEF")
            for key, action in keys:
                text.append(f"\n  {key:<11}", style="#E5C07B")
                text.append(action)
        with Vertical(id="help", classes="panel") as panel:
            panel.border_title = "Help"
            yield Static(text)


class PasswordScreen(ModalScreen):
    """Ask for the NUS password for `amgr login`. Returns it, or None.

    The password is passed straight to amgr on stdin and never stored.
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def compose(self) -> ComposeResult:
        from textual.widgets import Input

        with Vertical(id="dialog", classes="panel") as dialog:
            dialog.border_title = "amgr login"
            yield Static("Project credits need an accounting login (same as `amgr login`).")
            yield Input(placeholder="NUS password", password=True, id="password")
            yield Static(
                Text("Sent to amgr on stdin only; HPIT does not store it.", style=MUTED),
                classes="message",
            )

    def on_input_submitted(self, event) -> None:
        self.dismiss(event.value or None)


class CommandsScreen(ModalScreen):
    """Show shell commands to copy and paste into a terminal."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("y", "copy", "Copy to clipboard"),
    ]

    def __init__(self, title: str, commands: str):
        super().__init__()
        self.title_text = title
        self.commands = commands

    def compose(self) -> ComposeResult:
        from textual.widgets import Footer, TextArea

        with Vertical(id="commands", classes="panel") as panel:
            panel.border_title = self.title_text
            yield Static(Text(
                "Nothing has been deleted. Press y to copy, then paste into your terminal. "
                "(If copying doesn't work: hold ⌥ Option in iTerm and drag to select.)",
                style=MUTED,
            ), classes="message")
            yield TextArea(self.commands, read_only=True, id="commands-text", show_line_numbers=False)
        yield Footer()

    def action_copy(self) -> None:
        # OSC 52: works in iTerm2 (and over SSH) when clipboard access is allowed.
        self.app.copy_to_clipboard(self.commands)
        self.notify("Copied to the clipboard. Paste it in your terminal, review, then press Enter.")


class DeleteConfirmScreen(ModalScreen):
    """Permanent deletion: lists what will go and needs the word `delete`.

    Returns True only when confirmed.
    """

    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, summary: Text):
        super().__init__()
        self.summary = summary

    def compose(self) -> ComposeResult:
        from textual.widgets import Input

        with Vertical(id="dialog", classes="panel danger") as dialog:
            dialog.border_title = "Delete forever?"
            yield Static(self.summary, id="delete-summary")
            yield Static(Text(
                "This cannot be undone. Each path is checked again first; anything "
                "not owned by you, outside home/scratch, or used by a running job is skipped.",
                style="#E5C07B",
            ), classes="message")
            yield Input(placeholder="Type delete to confirm", id="delete-word")
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("Delete forever", id="confirm", variant="error", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#delete-word").focus()

    def on_input_changed(self, event) -> None:
        self.query_one("#confirm", Button).disabled = event.value.strip() != "delete"

    def on_input_submitted(self, event) -> None:
        if event.value.strip() == "delete":
            self.dismiss(True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm" and not event.button.disabled)
