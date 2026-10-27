"""Keep task queue positions consistent with task dependencies."""

import heapq
import sqlite3


class TaskAboveDependencyError(ValueError):
	"""Raised when a chosen position would put a task above one of its dependencies."""

	def __init__(self, dependency_id: str) -> None:
		"""Store the blocking dependency ID.

		Args:
			dependency_id: The task that must come first.
		"""
		self.dependency_id = dependency_id
		super().__init__(dependency_id)


def dependency_cycle(
	connection: sqlite3.Connection, task_id: str, dependency_id: str
) -> tuple[str, ...] | None:
	"""Return the chain of task IDs that the new dependency would turn into a loop.

	The chain starts and ends with ``task_id``.

	Args:
		connection: The open database connection to read dependencies from.
		task_id: The task receiving the new dependency.
		dependency_id: The task it would depend on.

	Returns:
		The loop chain, or None when the link is safe to add.
	"""
	# The rows contain every existing dependency link.
	rows = connection.execute(
		"SELECT task_id, depends_on_task_id FROM task_dependencies"
	).fetchall()
	# Each task maps to the tasks it depends on.
	links: dict[str, list[str]] = {}
	for row in rows:
		links.setdefault(row["task_id"], []).append(row["depends_on_task_id"])

	# Tasks still to visit, starting from the new dependency.
	pending = [dependency_id]
	# Visited tasks are checked only once, even when many paths reach them.
	visited = {dependency_id}
	# Each visited task maps to the task it was reached from, so the chain can be rebuilt.
	parents: dict[str, str | None] = {dependency_id: None}
	while pending:
		current_id = pending.pop()
		if current_id == task_id:
			# Walk back to the new dependency to build the loop chain.
			chain = [current_id]
			while parents[current_id] is not None:
				current_id = parents[current_id]
				chain.append(current_id)
			return (task_id, *reversed(chain))
		for next_id in links.get(current_id, []):
			if next_id not in visited:
				visited.add(next_id)
				parents[next_id] = current_id
				pending.append(next_id)
	return None


def order_task_queue(
	connection: sqlite3.Connection,
	ordered_ids: list[str],
	task_id: str,
	*,
	refuse_above_dependency: bool = False,
	carry_dependents: bool = False,
) -> list[str]:
	"""Reorder one queue so a task and its dependents follow their dependencies.

	Only dependencies between tasks in ``ordered_ids`` count, so links to other
	releases never move anything. The list is changed in place and returned.

	Args:
		connection: The open database connection to read dependencies from.
		ordered_ids: The task IDs of one queue, in their intended order.
		task_id: The task that was just added, linked or moved.
		refuse_above_dependency: Refuse when the user chose a position above
			one of the task's dependencies.
		carry_dependents: Bring the task's dependents along even when the task
			itself did not need to move, as when it was moved down the queue.

	Returns:
		``ordered_ids``, reordered.

	Raises:
		TaskAboveDependencyError: The task sits above a dependency and
			``refuse_above_dependency`` is set.
	"""
	# The starting positions decide the order of carried tasks that could go either way.
	original_indexes = {item: index for index, item in enumerate(ordered_ids)}
	# Only links within this queue affect its order.
	queue_ids = set(ordered_ids)
	# The rows contain every existing dependency link.
	rows = connection.execute(
		"SELECT task_id, depends_on_task_id FROM task_dependencies"
	).fetchall()
	# Each task maps to the tasks in this queue that it depends on.
	dependencies: dict[str, set[str]] = {}
	# Each task maps to the tasks in this queue that depend on it.
	dependents: dict[str, set[str]] = {}
	for row in rows:
		dependent_id = row["task_id"]
		dependency_id = row["depends_on_task_id"]
		if dependent_id in queue_ids and dependency_id in queue_ids:
			dependencies.setdefault(dependent_id, set()).add(dependency_id)
			dependents.setdefault(dependency_id, set()).add(dependent_id)

	# The task's own dependencies in this queue.
	root_dependencies = dependencies.get(task_id, set())
	# Dependencies that currently sit below the task. A chosen position above one
	# is refused; otherwise the task moves to just below the lowest of them.
	late_dependencies = [
		item
		for item in root_dependencies
		if ordered_ids.index(item) > ordered_ids.index(task_id)
	]
	if refuse_above_dependency and late_dependencies:
		blocking_id = min(late_dependencies, key=ordered_ids.index)
		raise TaskAboveDependencyError(blocking_id)
	# Whether the task itself had to move down past its dependencies.
	root_moved = bool(late_dependencies)
	if root_moved:
		ordered_ids.remove(task_id)
		latest_index = max(ordered_ids.index(item) for item in root_dependencies)
		ordered_ids.insert(latest_index + 1, task_id)
	if not (carry_dependents or root_moved):
		return ordered_ids

	# Carried tasks are every task in this queue that depends on the moved task,
	# directly or through other tasks.
	carried: set[str] = set()
	# Tasks whose dependents have not been collected yet.
	pending = [task_id]
	while pending:
		for child_id in dependents.get(pending.pop(), set()):
			if child_id not in carried:
				carried.add(child_id)
				pending.append(child_id)

	# Take the carried tasks out, then put each one back after all its dependencies.
	ordered_ids[:] = [item for item in ordered_ids if item not in carried]
	# For each carried task, the number of its carried dependencies not yet placed.
	remaining = {item: len(dependencies[item] & carried) for item in carried}
	# Carried tasks whose carried dependencies are all placed, earliest first.
	ready = [(original_indexes[item], item) for item in carried if remaining[item] == 0]
	heapq.heapify(ready)
	# Each carried task goes after the one placed before it, so carried tasks keep
	# their starting order wherever their dependencies allow.
	previous_id = task_id
	while ready:
		_, current_id = heapq.heappop(ready)
		latest_index = max(ordered_ids.index(item) for item in dependencies[current_id])
		insert_index = max(latest_index, ordered_ids.index(previous_id)) + 1
		ordered_ids.insert(insert_index, current_id)
		previous_id = current_id
		for child_id in dependents.get(current_id, set()) & carried:
			remaining[child_id] -= 1
			if remaining[child_id] == 0:
				heapq.heappush(ready, (original_indexes[child_id], child_id))
	return ordered_ids
