"""In-memory conversation state.

Each user can have at most one pending input at a time. State is keyed by
Telegram user id and survives callback navigation within a wizard.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LoginState:
    phone: str = ""
    code_hash: str = ""
    client: object = None
    step: str = ""  # phone | code | password


@dataclass
class TransferWizard:
    step: str = "start"
    source_type: str = "any"           # pg | pubg | pc | pubc | any
    source: dict | None = None         # {id, name, title}
    dest: dict | None = None           # {id, name}
    dialogs: list = field(default_factory=list)
    dest_page: int = 0
    count_mode: str = "latest"         # latest | custom
    count: int = 10
    custom_start: int | None = None
    custom_end: int | None = None
    mode: str = "forward"
    options: set = field(default_factory=set)
    filter_type: str = "all"
    dedup: bool = True
    schedule_kind: str = "now"         # now | later | daily | weekly
    schedule_time: str | None = None
    schedule_weekday: int | None = None
    job_name: str | None = None
    edit_mode: bool = False
    edit_step: str = ""
    progress_msg_id: int | None = None


class Store:
    def __init__(self) -> None:
        self.login: dict[int, LoginState] = {}
        self.transfer: dict[int, TransferWizard] = {}
        self.pending_input: dict[int, str] = {}   # uid -> expected input kind
        self.running: dict[int, object] = {}      # uid -> engine handle (for stop)
        self.progress: dict[int, dict] = {}       # uid -> live progress snapshot (for refresh)

    def get_transfer(self, user_id: int) -> TransferWizard:
        if user_id not in self.transfer:
            self.transfer[user_id] = TransferWizard()
        return self.transfer[user_id]

    def reset_transfer(self, user_id: int) -> None:
        self.transfer.pop(user_id, None)

    def set_pending(self, user_id: int, kind: str | None) -> None:
        if kind is None:
            self.pending_input.pop(user_id, None)
        else:
            self.pending_input[user_id] = kind

    def pending(self, user_id: int) -> str | None:
        return self.pending_input.get(user_id)


store = Store()
