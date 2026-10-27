"""Run one turn of a graph for a job and report whether it now waits for the user.

ask_user interrupts end the job as waiting_input (the reply requeues the same row);
await_feedback interrupts end it as done (the next message creates a new job that resumes there).
"""

from typing import Any, Literal

from langgraph.types import Command

TurnEnd = Literal["waiting_input", "done"]


def run_turn(graph: Any, thread_id: str, message_id: str, context: Any, fresh_input: dict[str, Any]) -> TurnEnd:
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    interrupts = [i for t in snapshot.tasks for i in t.interrupts]
    values = snapshot.values or {}
    if interrupts:
        graph.invoke(Command(resume={"message_id": message_id}), config, context=context)
    elif snapshot.next and values.get("message_id") == message_id:
        graph.invoke(None, config, context=context)  # a lost-lease rerun continues from the last step
    else:
        graph.invoke(fresh_input, config, context=context)
    after = graph.get_state(config)
    kinds = [i.value.get("kind") for t in after.tasks for i in t.interrupts if isinstance(i.value, dict)]
    return "waiting_input" if "ask_user" in kinds else "done"
