"""In-memory task dependency graph; no scheduling, execution or persistence.

Edges point from a task to its prerequisites. All mutations must be serialized
by the caller; TaskInbox holds its lock for every graph operation.
"""

from __future__ import annotations

import heapq


class DependencyCycle(ValueError):
    """An edge would create a direct or transitive dependency cycle."""


class TaskDAG:
    """DAG with stable admission-order tie breaking, independent of priority."""

    def __init__(self) -> None:
        self._requires: dict[str, frozenset[str]] = {}
        self._sequence: dict[str, int] = {}

    def add_task(self, task_id: str, sequence: int) -> None:
        if task_id in self._requires:
            raise ValueError("task already exists")
        if type(sequence) is not int or sequence <= 0:
            raise ValueError("invalid admission sequence")
        self._requires[task_id] = frozenset()
        self._sequence[task_id] = sequence

    def dependencies(self, task_id: str) -> tuple[str, ...]:
        return tuple(sorted(self._requires[task_id], key=self._sequence.__getitem__))

    def set_dependencies(self, task_id: str, prerequisites: tuple[str, ...]) -> None:
        """Replace edges atomically; reject any cycle before mutating state."""
        self.validate_dependencies(task_id, prerequisites)
        self._requires[task_id] = frozenset(prerequisites)

    def validate_dependencies(self, task_id: str, prerequisites: tuple[str, ...]) -> None:
        """Preflight without mutation, so durable backends can commit first."""
        if task_id not in self._requires:
            raise KeyError("unknown task")
        if len(prerequisites) != len(set(prerequisites)):
            raise ValueError("duplicate dependencies")
        for prerequisite in prerequisites:
            if prerequisite not in self._requires:
                raise KeyError("unknown dependency")
        if task_id in prerequisites:
            raise DependencyCycle("task cannot depend on itself")

        # The graph is already acyclic. Only a new edge task -> prerequisite
        # can introduce a cycle, if prerequisite ->* task already exists.
        seen: set[str] = set()
        remaining = list(prerequisites)
        while remaining:
            current = remaining.pop()
            if current == task_id:
                raise DependencyCycle("transitive dependency cycle")
            if current in seen:
                continue
            seen.add(current)
            remaining.extend(self._requires[current])

    def topological_order(self, task_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Return each prerequisite before dependents; ties by admission order.

        The caller supplies a closed owner/project subset. No edges may point
        outside it; this makes accidental cross-scope ordering fail closed.
        """
        selected = set(task_ids)
        if len(selected) != len(task_ids) or any(t not in self._requires for t in selected):
            raise ValueError("invalid task set")
        indegree = {task_id: 0 for task_id in selected}
        dependents: dict[str, list[str]] = {task_id: [] for task_id in selected}
        for task_id in selected:
            for dependency_id in self._requires[task_id]:
                if dependency_id not in selected:
                    raise ValueError("dependency outside requested task set")
                indegree[task_id] += 1
                dependents[dependency_id].append(task_id)

        ready = [(self._sequence[task_id], task_id) for task_id in selected
                 if indegree[task_id] == 0]
        heapq.heapify(ready)
        result: list[str] = []
        while ready:
            _, task_id = heapq.heappop(ready)
            result.append(task_id)
            for dependent in dependents[task_id]:
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heapq.heappush(ready, (self._sequence[dependent], dependent))
        if len(result) != len(selected):
            raise DependencyCycle("cyclic task set")
        return tuple(result)
