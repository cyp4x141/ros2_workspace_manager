"""Typed manifest relations and non-recursive selection operations."""


def build_graph(packages):
    forward = {name: set() for name in packages}
    reverse = {name: set() for name in packages}
    members = {}
    for name, package in packages.items():
        for group in package.groups:
            members.setdefault(group, set()).add(name)
    for name, package in packages.items():
        deps = {dep.name for dep in package.dependencies if dep.kind != 'doc'}
        for group in package.group_dependencies:
            deps.update(members.get(group, set()))
        forward[name] = deps.intersection(packages)
        for dependency in forward[name]:
            reverse[dependency].add(name)
    return forward, reverse


def closure(targets, graph):
    seen = set()
    pending = list(targets)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(graph.get(name, ()))
    return seen
