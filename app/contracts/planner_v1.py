from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["completed", "invalid"]
TaskView = Literal["today", "overdue", "upcoming", "all"]


class PlannerSpaceRef(BaseModel):
    kind: str = Field(description="collection, folder, or none")
    id: int | None = None
    name: str | None = None


class PlannerTaskItem(BaseModel):
    id: int
    title: str
    status: str
    priority: int
    due_at: str | None = Field(default=None, description="ISO datetime in UTC")
    space_name: str | None = None
    recurrence: str


class PlannerEventItem(BaseModel):
    id: int
    title: str
    status: str
    starts_at: str = Field(description="ISO datetime in UTC")
    location: str | None = None
    space_name: str | None = None
    recurrence: str


class PlannerCreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200, description="What needs doing")
    due_at: str | None = Field(default=None, description="ISO datetime, e.g. 2026-10-05T09:00:00+03:00")
    remind_at: str | None = Field(default=None, description="ISO datetime for the reminder push")
    remind: bool = Field(default=False, description="Remind exactly at the due moment")
    recurrence: str = Field(default="none", description="none, daily, weekdays, weekly, monthly")
    priority: int = Field(default=0, ge=0, le=3, description="0 normal, 3 most urgent")
    space: str | None = Field(default=None, max_length=120, description="Vault space name; omit for inbox")


class PlannerCreateTaskResult(BaseModel):
    status: Status = "completed"
    task_id: int | None = None
    message: str = Field(default="", max_length=500)


class PlannerListTasksRequest(BaseModel):
    view: TaskView = Field(default="today", description="today, overdue, upcoming, or all")
    space: str | None = Field(default=None, max_length=120, description="Filter by Vault space name")
    limit: int = Field(default=20, ge=1, le=50)


class PlannerListTasksResult(BaseModel):
    status: Status = "completed"
    tasks: list[PlannerTaskItem] = Field(default_factory=list)
    message: str = Field(default="", max_length=500)


class PlannerCompleteTaskRequest(BaseModel):
    task_id: int = Field(description="Task id from a previous list or create call")


class PlannerCompleteTaskResult(BaseModel):
    status: Status = "completed"
    next_id: int | None = Field(default=None, description="Next instance id for repeating tasks")
    message: str = Field(default="", max_length=500)


class PlannerSnoozeTaskRequest(BaseModel):
    task_id: int = Field(description="Task id from a previous list or create call")
    remind_at: str = Field(description="ISO datetime for the new reminder push")


class PlannerSnoozeTaskResult(BaseModel):
    status: Status = "completed"
    message: str = Field(default="", max_length=500)


class PlannerCreateEventRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200, description="Event title")
    starts_at: str = Field(description="ISO datetime, e.g. 2026-10-05T09:00:00+03:00")
    ends_at: str | None = Field(default=None, description="ISO datetime, optional")
    location: str | None = Field(default=None, max_length=200)
    remind_at: str | None = Field(default=None, description="ISO datetime for the reminder push")
    remind: bool = Field(default=False, description="Remind at the event start")
    recurrence: str = Field(default="none", description="none, daily, weekdays, weekly, monthly")
    space: str | None = Field(default=None, max_length=120, description="Vault space name; omit for inbox")


class PlannerCreateEventResult(BaseModel):
    status: Status = "completed"
    event_id: int | None = None
    message: str = Field(default="", max_length=500)


class PlannerListEventsRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=90, description="How many days ahead to list")
    space: str | None = Field(default=None, max_length=120, description="Filter by Vault space name")


class PlannerListEventsResult(BaseModel):
    status: Status = "completed"
    events: list[PlannerEventItem] = Field(default_factory=list)
    message: str = Field(default="", max_length=500)


class PlannerTodayRequest(BaseModel):
    pass


class PlannerTodayResult(BaseModel):
    status: Status = "completed"
    lines: list[str] = Field(default_factory=list, description="Agenda lines, overdue first")
    overdue_count: int = 0
    today_count: int = 0


class PlannerResolveSpaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120, description="Space name as the user said it")


class PlannerResolveSpaceResult(BaseModel):
    status: Status = "completed"
    space: PlannerSpaceRef | None = None
    candidates: list[str] = Field(default_factory=list, description="Close names when unsure")
    message: str = Field(default="", max_length=500)
