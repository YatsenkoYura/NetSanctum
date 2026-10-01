"""Morning briefing: what changed while the owner was away, in three lines.

Sources, in order: open background tasks, recent episodic memory, recent
cascade outcomes. Extractive and bounded — the chat model turns this into
prose at request time, this just gathers the facts.
"""

from sqlalchemy import func, select

from app.modules.miku.models import MikuCascadeLog, MikuEpisodeMemory, MikuTask


async def build_briefing(db, user_id: int, *, limit: int = 5) -> dict:
    open_tasks = (
        await db.scalars(
            select(MikuTask).where(MikuTask.user_id == user_id, MikuTask.state == "open").limit(10)
        )
    ).all()
    episodes = (
        await db.scalars(
            select(MikuEpisodeMemory)
            .where(MikuEpisodeMemory.user_id == user_id)
            .order_by(MikuEpisodeMemory.occurred_at.desc())
            .limit(limit)
        )
    ).all()
    recent = (
        await db.scalars(
            select(MikuCascadeLog)
            .where(MikuCascadeLog.user_id == user_id)
            .order_by(MikuCascadeLog.created_at.desc())
            .limit(limit)
        )
    ).all()
    total_tasks = await db.scalar(
        select(func.count(MikuTask.id)).where(MikuTask.user_id == user_id, MikuTask.state == "open")
    )
    lines: list[str] = []
    if open_tasks:
        lines.append(f"Открытых задач: {total_tasks or len(open_tasks)}")
        for task in open_tasks[:3]:
            lines.append(f"• {task.goal[:120]}")
    for episode in episodes[:3]:
        lines.append(f"• {episode.summary[:160]}")
    for cascade in recent[:3]:
        lines.append(f"• {cascade.goal[:120]} — {cascade.status}")
    return {
        "lines": lines[:9],
        "open_tasks": total_tasks or 0,
        "episodes": len(episodes),
        "recent_turns": len(recent),
    }


__all__ = ["build_briefing"]
