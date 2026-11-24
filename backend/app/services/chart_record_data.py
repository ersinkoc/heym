"""Per-record checklist text on a dashboard widget's Chart Output node.

A text widget's checkboxes on a detail page (``?record=``) save for that record only. The
markdown is kept inside the widget's own workflow, on the Chart Output node, under
``recordData[<record>]["text"]``, and a run on that page shows it in place of the computed
markdown. The markdown task endpoints in ``app/api/dashboards.py`` are the only writers:
every other save keeps the stored entries (``keep_record_data``), and a graph handed to a
model leaves them out (``without_record_data``).
"""

from typing import Any

RECORD_DATA_KEY = "recordData"
CHART_OUTPUT_TYPE = "chartOutput"


def _is_chart_output(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == CHART_OUTPUT_TYPE


def _node_data(node: dict[str, Any]) -> dict[str, Any]:
    data = node.get("data")
    return data if isinstance(data, dict) else {}


def record_text(node_data: Any, record: Any) -> str | None:
    """The markdown saved for ``record`` on a Chart Output node's data, if any."""
    if not isinstance(record, str) or not isinstance(node_data, dict):
        return None
    entries = node_data.get(RECORD_DATA_KEY)
    entry = entries.get(record) if isinstance(entries, dict) else None
    text = entry.get("text") if isinstance(entry, dict) else None
    return text if isinstance(text, str) else None


def with_record_text(node_data: dict[str, Any], record: str, text: str) -> dict[str, Any]:
    """``node_data`` with ``text`` saved for ``record``; other records and fields stay."""
    entries = node_data.get(RECORD_DATA_KEY)
    entries = dict(entries) if isinstance(entries, dict) else {}
    entry = entries.get(record)
    entries[record] = {**(entry if isinstance(entry, dict) else {}), "text": text}
    return {**node_data, RECORD_DATA_KEY: entries}


def _with_entries(node: dict[str, Any], entries: Any) -> dict[str, Any]:
    data = dict(_node_data(node))
    if entries is None:
        data.pop(RECORD_DATA_KEY, None)
    else:
        data[RECORD_DATA_KEY] = entries
    return {**node, "data": data}


def keep_record_data(stored_nodes: Any, new_nodes: Any) -> Any:
    """``new_nodes`` with each Chart Output node's ``recordData`` as stored.

    A node takes the entries stored on the node with its id; when neither graph has more
    than one Chart Output node, the one node takes the stored node's entries whatever its
    id. Entries that are not stored are dropped, since only the markdown task endpoints
    write them.
    """
    if not isinstance(new_nodes, list):
        return new_nodes
    stored_chart = [node for node in stored_nodes or [] if _is_chart_output(node)]
    new_chart = [node for node in new_nodes if _is_chart_output(node)]
    if not stored_chart and not any(RECORD_DATA_KEY in _node_data(n) for n in new_chart):
        return new_nodes
    stored_by_id = {node.get("id"): _node_data(node).get(RECORD_DATA_KEY) for node in stored_chart}
    only_one = len(stored_chart) == 1 and len(new_chart) == 1
    kept: list[Any] = []
    for node in new_nodes:
        if not _is_chart_output(node):
            kept.append(node)
            continue
        if node.get("id") in stored_by_id:
            entries = stored_by_id[node.get("id")]
        elif only_one:
            entries = _node_data(stored_chart[0]).get(RECORD_DATA_KEY)
        else:
            entries = None
        data = _node_data(node)
        same = data.get(RECORD_DATA_KEY) == entries
        if same and (RECORD_DATA_KEY in data) == (entries is not None):
            kept.append(node)
        else:
            kept.append(_with_entries(node, entries))
    return kept


def without_record_data(nodes: Any) -> Any:
    """``nodes`` without the per-record entries, as a graph shown to a model."""
    if not isinstance(nodes, list):
        return nodes
    return [
        _with_entries(node, None)
        if _is_chart_output(node) and RECORD_DATA_KEY in _node_data(node)
        else node
        for node in nodes
    ]
