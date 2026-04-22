import os
import logging
from datetime import date, datetime

from dotenv import load_dotenv
from todoist_api_python.api import TodoistAPI

load_dotenv()

logger = logging.getLogger(__name__)

# Translate human priority (1=urgent) to Todoist API priority (4=urgent)
PRIORITY_MAP = {1: 4, 2: 3, 3: 2, 4: 1}

def _get_client() -> TodoistAPI:
    return TodoistAPI(os.environ["TODOIST_API_KEY"])

def read_tasks(filter_type: str = "today") -> list[dict]:
    client = _get_client()
    today = date.today()
    all_tasks = []

    # v4 returns an iterator of lists — flatten it
    for task_batch in client.get_tasks():
        all_tasks.extend(task_batch)

    def get_due_date(task) -> date | None:
        if not task.due:
            return None
        try:
            due = task.due.date
            if isinstance(due, datetime):
                return due.date()
            return due #already a date object
        except AttributeError:
            return None

    if filter_type == "today":
        tasks = [t for t in all_tasks if get_due_date(t) == today]
    elif filter_type == "upcoming":
        from datetime import timedelta
        in_7_days = today + timedelta(days=7)
        tasks =  [t for t in all_tasks if get_due_date(t) and today <= get_due_date(t) <= in_7_days]
    elif filter_type == "overdue":
        tasks = [t for t in all_tasks if get_due_date(t) and get_due_date(t) < today]
    else:
        tasks = all_tasks   
   
    return [
        {
            "id":       t.id,
            "content":  t.content,
            "due":      t.due.string if t.due else "No due date",
            "priority": t.priority,
        }
        for t in tasks[:10]
    ]

def create_task(content: str, due_date: str = None, priority: int = 3) -> None:
    client = _get_client()
    api_priority = PRIORITY_MAP.get(priority, 2)

    kwargs = {
        "content":  content,
        "priority": api_priority,
    }
    if due_date:
        kwargs["due_date"] = due_date

    client.add_task(**kwargs)

def find_tasks_by_name(name: str) -> list[dict]:
    """Search active tasks by name — used for complete and delete flows."""
    client = _get_client()
    all_tasks = []

    for task_batch in client.get_tasks():
        all_tasks.extend(task_batch)

    matches = [
        {
            "id":       t.id,
            "content":  t.content,
            "due":      t.due.string if t.due else "No due date",
            "priority": t.priority,
        }
        for t in all_tasks
        if name.lower() in t.content.lower()
    ]
    return matches

def complete_task(task_id: str) -> None:
    client = _get_client()
    client.complete_task(task_id=task_id)

def delete_task(task_id: str) -> None:
    client = _get_client()
    client.delete_task(task_id=task_id)
